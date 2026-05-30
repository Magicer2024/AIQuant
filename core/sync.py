"""
sync.py  ——  数据同步模块（akshare 版）
========================================
数据源：akshare（支持多线程并发）

调度计划：
  - 每天 16:30  同步当日全市场收盘数据
  - 首次运行    自动拉取历史数据（自动断点续传）
"""

import pandas as pd
import time
import os
import signal
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeoutError
from core.data_fetcher import get_market_index
from core.db import (
    init_db, upsert_stock_list, update_strategy_scores_batch,
    get_all_stocks, get_latest_date, get_latest_date_all, get_stock_count_in_db,
    log_sync, db_stats, get_conn, get_daily_price, upsert_index_daily, get_index_daily
)
from qlib_engine.data_bridge import append_daily_data, batch_append_daily, append_calendar_dates
from qlib_engine import init_qlib as _init_qlib


def _write_daily_price(code: str, df: pd.DataFrame) -> int:
    """写入 daily_price 表（与 Qlib 二进制并行写入，供仪表板/打分读取）"""
    if df.empty:
        return 0

    # Guard: detect obviously wrong data (e.g. index prices stored as stock)
    first_close = float(df.iloc[0].get("close", 0) or 0)
    if first_close > 3000:
        first_dt = df.index[0]
        dt_str = str(first_dt.date()) if hasattr(first_dt, "date") else str(first_dt)[:10]
        with get_conn() as conn:
            prev = conn.execute(
                "SELECT close FROM daily_price WHERE code=? AND trade_date < ? ORDER BY trade_date DESC LIMIT 1",
                (code, dt_str)
            ).fetchone()
        if prev and prev["close"] and prev["close"] > 0:
            ratio = first_close / prev["close"]
            if ratio > 5:
                print(f"  [WARN] {code} close={first_close:.1f} is {ratio:.0f}x prev close={prev['close']:.2f}, likely bad data — skipped")
                return 0

    records = []
    for dt, row in df.iterrows():
        records.append({
            "code":       code,
            "trade_date": str(dt.date()) if hasattr(dt, "date") else str(dt)[:10],
            "open":       float(row.get("open", 0) or 0),
            "high":       float(row.get("high", 0) or 0),
            "low":        float(row.get("low", 0) or 0),
            "close":      float(row.get("close", 0) or 0),
            "volume":     float(row.get("volume", 0) or 0),
            "amount":     float(row.get("amount", 0) or 0),
            "pct_change": float(row.get("pct_change", 0) or 0),
            "turnover":   float(row.get("turnover", 0) or 0),
        })
    with get_conn() as conn:
        conn.executemany("""
            INSERT INTO daily_price
              (code,trade_date,open,high,low,close,volume,amount,pct_change,turnover)
            VALUES
              (:code,:trade_date,:open,:high,:low,:close,:volume,:amount,:pct_change,:turnover)
            ON CONFLICT(code, trade_date) DO UPDATE SET
              open=excluded.open, high=excluded.high, low=excluded.low,
              close=excluded.close, volume=excluded.volume, amount=excluded.amount,
              pct_change=excluded.pct_change, turnover=excluded.turnover
        """, records)
    return len(records)
from strategy.strategies import (
    strategy_volume_breakout, strategy_ma_convergence,
    strategy_price_volume_divergence, strategy_bottom_fishing,
    strategy_whale_accumulation, fuse_signals, DEFAULT_WEIGHTS
)

# ─────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────
HISTORY_START    = "20240101"   # 历史数据起始日期
BASE_INTERVAL    = 0.05         # baostock 无频率限制，0.05秒即可
BATCH_SIZE       = 100          # 每批处理股票数
INTER_BATCH_DELAY = 0.5         # 每批次间隔（秒）

# ─────────────────────────────────────────────
# 交易日判断工具
# ─────────────────────────────────────────────
def is_trading_day(today: str = None) -> bool:
    """判断指定日期是否为A股交易日

    :param today: 日期字符串，默认为今天
    """
    if today is None:
        today = date.today().strftime("%Y-%m-%d")
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        trade_dates = set(pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d").tolist())
        return today in trade_dates
    except Exception:
        # 接口失败时按工作日 fallback（使用查询日期而非今天）
        from datetime import datetime
        queried_dt = datetime.strptime(today, "%Y-%m-%d").date()
        return queried_dt.weekday() < 5


def is_after_market_close() -> bool:
    """当前是否已收盘（16:00 之后）"""
    now = datetime.now()
    return (now.hour, now.minute) >= (16, 0)


# 过滤规则
_sync_stats = {
    "total": 0,
    "success": 0,
    "failed": 0,
    "skipped_bse": 0,
    "skipped_st": 0,
}


def _should_skip(code: str, name: str) -> bool:
    """跳过 ST、退市、北交所（baostock 不支持）。同时递增 _sync_stats。"""
    if not code or not name:
        return True
    if "ST" in name.upper() or "退" in name or "*" in name:
        _sync_stats["skipped_st"] += 1
        return True
    if code.startswith(("430", "830", "870", "920")):
        _sync_stats["skipped_bse"] += 1
        return True
    return False


# ─────────────────────────────────────────────
# 主要大盘指数（供大盘择时用）
# ─────────────────────────────────────────────
MAJOR_INDICES = [
    ("000001", "上证指数"),   # 上证指数
    ("399001", "深证成指"),   # 深证成指
    ("000300", "沪深300"),   # 沪深300
    ("000905", "中证500"),   # 中证500
    ("000852", "中证1000"),  # 中证1000
]

# baostock 指数代码前缀映射
_BS_INDEX_PREFIX = {"000001": "sh", "399001": "sz", "000300": "sh",
                     "000905": "sh", "000852": "sh"}


def sync_index_data(code: str, name: str, start_date: str = None,
                     end_date: str = None, verbose: bool = True) -> int:
    """
    同步单个指数的历史数据，写入 index_daily 表。
    start_date / end_date: "YYYYMMDD"
    返回写入行数。
    """
    bs_code = f"{_BS_INDEX_PREFIX.get(code, 'sh')}.{code}"
    if verbose:
        print(f"  同步指数 {code} {name}...")
    try:
        df = get_market_index(bs_code, start_date=start_date, end_date=end_date)
        if df is None or df.empty:
            if verbose:
                print(f"  指数 {code} 无数据")
            return 0
        n = upsert_index_daily(code, df)
        if verbose:
            print(f"  指数 {code} 写入 {n} 行")
        return n
    except Exception as e:
        if verbose:
            print(f"  指数 {code} 失败: {e}")
        return 0


def sync_all_indices(start_date: str = None, end_date: str = None,
                     verbose: bool = True) -> dict:
    """
    同步所有主要指数的历史/增量数据。
    """
    if end_date is None:
        end_date = date.today().strftime("%Y%m%d")
    if start_date is None:
        start_date = HISTORY_START
    results = {}
    for code, name in MAJOR_INDICES:
        n = sync_index_data(code, name, start_date, end_date, verbose=verbose)
        results[code] = n
    return results


# ─────────────────────────────────────────────
# 股票列表
# ─────────────────────────────────────────────
def update_stock_list() -> bool:
    """
    从 akshare 拉取最新A股股票列表并写入数据库
    """
    init_db()
    print("\n>>> 拉取股票列表（akshare）...")
    try:
        import akshare as ak
        # 获取上海和深圳的股票列表
        df_sh = ak.stock_info_sh_name_code()
        df_sz = ak.stock_info_sz_name_code()
        
        records = []
        skipped = 0
        
        # 处理上海股票
        for _, row in df_sh.iterrows():
            code = str(row['证券代码']).strip()
            name = str(row['证券简称']).strip()
            if _should_skip(code, name):
                skipped += 1
                continue
            records.append({"code": code, "name": name, "market": "SH"})
        
        # 处理深圳股票
        for _, row in df_sz.iterrows():
            code = str(row['A股代码']).strip()
            name = str(row['A股简称']).strip()
            if _should_skip(code, name):
                skipped += 1
                continue
            records.append({"code": code, "name": name, "market": "SZ"})

        upsert_stock_list(records)
        print(f"    写入 {len(records)} 只，跳过 {skipped} 只（ST/退市）")
        return True

    except Exception as e:
        print(f"    [ERROR] {type(e).__name__}: {e}")
        return False


# ─────────────────────────────────────────────
# 单只股票同步
# ─────────────────────────────────────────────
def _format_code_for_baostock(code: str) -> str:
    """
    将股票代码转换为 baostock 格式（小写前缀）
    
    规则：
    - 600/601/603/688 开头 → sh.600000（上交所）
    - 000/001/002/003/300 开头 → sz.000001（深交所）
    - 430/830/870/920 开头 → bj.920000（北交所）
    """
    # 如果已经是 baostock 格式（小写前缀），直接返回
    if code.startswith(('sh.', 'sz.', 'bj.')):
        return code
    
    # 如果已经有大写后缀（如 600000.SH），转换为小写前缀格式
    if '.' in code:
        parts = code.split('.')
        if len(parts) == 2:
            num, suffix = parts
            suffix_lower = suffix.lower()
            if suffix_lower in ('sh', 'sz', 'bj'):
                return f"{suffix_lower}.{num}"
        # 其他情况，取数字部分
        code = parts[0]
    
    # 根据代码前缀判断交易所（baostock 使用小写前缀）
    if code.startswith(('600', '601', '603', '605', '688', '689')):
        return f"sh.{code}"
    elif code.startswith(('000', '001', '002', '003', '300', '301')):
        return f"sz.{code}"
    elif code.startswith(('430', '830', '870', '920')):
        return f"bj.{code}"
    else:
        # 默认尝试上交所
        return f"sh.{code}"


def sync_one_stock_with_timeout(code: str, start_date: str = HISTORY_START,
                                end_date: str = None, verbose: bool = False,
                                auto_login: bool = True, timeout: float = 30.0,
                                max_retries: int = 2) -> bool:
    """带超时和重试的单股票同步（指数退避 1s, 2s）"""
    for attempt in range(max_retries + 1):
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(sync_one_stock, code, start_date, end_date, verbose, auto_login)
            try:
                ok = fut.result(timeout=timeout)
                if ok:
                    return True
                if attempt < max_retries and verbose:
                    print(f"  [{code}] 第{attempt+1}次失败，{2**attempt}秒后重试...")
            except FutureTimeoutError:
                if attempt < max_retries and verbose:
                    print(f"  [{code}] 超时({timeout}s)，{2**attempt}秒后重试...")
            except Exception:
                if attempt < max_retries and verbose:
                    print(f"  [{code}] 异常，{2**attempt}秒后重试...")
        if attempt < max_retries:
            time.sleep(2 ** attempt)
    return False


def sync_one_stock(code: str, start_date: str = HISTORY_START,
                   end_date: str = None, verbose: bool = False,
                   auto_login: bool = True) -> bool:
    """
    拉取单只股票行情并写入数据库（主数据源：baostock，备用：akshare）

    注意：baostock 的 query_history_k_data_plus 不是线程安全的，
    如果在多线程环境中使用，需要确保每个线程有自己的登录会话

    :param auto_login: 为 True 时自动 login/logout；为 False 时依赖外部已登录的会话，
                       适合批量调用场景（外部统一 login，批量查询后再 logout）
    """
    # 标准化日期格式：去除 - 分隔符，统一为 YYYYMMDD
    start_date = start_date.replace("-", "")
    if end_date is None:
        end_date = date.today().strftime("%Y%m%d")
    else:
        end_date = end_date.replace("-", "")

    # 转换代码格式（添加交易所后缀）
    bs_code = _format_code_for_baostock(code)
    
    # 北交所股票 baostock 不支持，跳过
    if bs_code.startswith('bj.'):
        if verbose:
            print(f"  [{code}] 跳过：baostock 不支持北交所股票")
        return False

    # 主数据源：baostock（带断线重连）
    import baostock as bs
    baostock_failed = False
    for bs_attempt in range(2):
        try:
            # 会话管理：
            # - auto_login=True：每次调用自己 login/logout
            # - auto_login=False + 首次尝试：用外部会话
            # - auto_login=False + 重试（socket 断开后）：重新 login 并保留给外部
            own_login = auto_login or bs_attempt > 0
            if own_login:
                lg = bs.login()
                if lg.error_code != '0':
                    if verbose:
                        print(f"  [{code}] baostock 登录失败: {lg.error_msg}")
                    bs.logout()
                    raise Exception("baostock login failed")

            bs_start = start_date[:4] + "-" + start_date[4:6] + "-" + start_date[6:]
            bs_end = end_date[:4] + "-" + end_date[4:6] + "-" + end_date[6:]

            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,code,open,high,low,close,preclose,volume,amount,turn,pctChg",
                start_date=bs_start, end_date=bs_end,
                frequency="d", adjustflag="2"
            )

            data_list = []
            while (rs.error_code == '0') & rs.next():
                data_list.append(rs.get_row_data())

            # auto_login=True 时登出；auto_login=False 时保留会话给外部
            if auto_login:
                bs.logout()

            if data_list:
                df = pd.DataFrame(data_list, columns=rs.fields)
                numeric_cols = ['open', 'high', 'low', 'close', 'preclose', 'volume', 'amount', 'turn', 'pctChg']
                for col in numeric_cols:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors='coerce')
                df.rename(columns={
                    "date": "date", "open": "open", "high": "high", "low": "low",
                    "close": "close", "volume": "volume", "amount": "amount",
                    "pctChg": "pct_change", "turn": "turnover"
                }, inplace=True)
                df['date'] = pd.to_datetime(df['date'])
                df.set_index('date', inplace=True)
                df_qlib = df.reset_index().rename(columns={"date": "trade_date"})
                append_daily_data(code, df_qlib)
                n = _write_daily_price(code, df)
                if verbose:
                    print(f"  [{code}] baostock OK (+{n} 行)")
                return True
        except Exception as e:
            baostock_failed = True
            msg = str(e)
            is_socket_err = any(kw in msg for kw in (
                '10038', '10053', '10054', '非套接字', 'socket',
                '网络接收错误', '接收数据异常', 'network',
            ))
            if bs_attempt == 0 and is_socket_err:
                if verbose:
                    print(f"  [{code}] baostock socket 断开，重连...")
                try:
                    bs.logout()
                except Exception:
                    pass
                continue
            if verbose:
                print(f"  [{code}] baostock 失败: {msg[:60]}")
            if auto_login:
                try:
                    bs.logout()
                except Exception:
                    pass
            break

    # 备用数据源：akshare
    if baostock_failed:
        try:
            ok = _sync_one_stock_akshare(code, start_date, end_date, verbose)
            if ok:
                return True
        except Exception as e:
            if verbose:
                print(f"  [{code}] akshare 也失败: {str(e)[:60]}")

        # 第三数据源：腾讯财经
        try:
            ok = _sync_one_stock_tencent(code, start_date, end_date, verbose)
            if ok:
                return True
        except Exception as e:
            if verbose:
                print(f"  [{code}] 腾讯财经 也失败: {str(e)[:60]}")

    return False


def _sync_one_stock_akshare(code: str, start_date: str, end_date: str, verbose: bool = False) -> bool:
    """akshare 备用数据源"""
    import akshare as ak
    ak_code = code.replace('.SH', '').replace('.SZ', '')
    df = ak.stock_zh_a_hist(
        symbol=ak_code, period="daily",
        start_date=start_date, end_date=end_date, adjust="qfq"
    )
    if df is not None and not df.empty:
        df.rename(columns={
            "日期":"date","开盘":"open","收盘":"close","最高":"high","最低":"low",
            "成交量":"volume","成交额":"amount","涨跌幅":"pct_change","换手率":"turnover"
        }, inplace=True)
        df['date'] = pd.to_datetime(df['date'])
        df.set_index('date', inplace=True)
        df_qlib = df.reset_index().rename(columns={"date": "trade_date"})
        append_daily_data(code, df_qlib)
        n = _write_daily_price(code, df)
        if verbose:
            print(f"  [{code}] akshare OK (+{n} 行)")
        return True
    return False


def _format_code_for_tencent(code: str) -> str:
    """转换股票代码为腾讯格式 (sh600000 / sz000001)"""
    code = code.replace('.SH', '').replace('.SZ', '').replace('sh.', '').replace('sz.', '').strip()
    if code.startswith(('600', '601', '603', '605', '688', '689')):
        return f"sh{code}"
    else:
        return f"sz{code}"


def _sync_one_stock_tencent(code: str, start_date: str, end_date: str, verbose: bool = False) -> bool:
    """腾讯财经备用数据源（akshare stock_zh_a_hist_tx）"""
    import akshare as ak
    tx_code = _format_code_for_tencent(code)
    df = ak.stock_zh_a_hist_tx(
        symbol=tx_code,
        start_date=start_date, end_date=end_date, adjust="qfq"
    )
    if df is not None and not df.empty:
        # 腾讯源列名: date, open, close, high, low, amount
        # 补充缺失列
        if 'volume' not in df.columns:
            df['volume'] = 0.0
        if 'turnover' not in df.columns:
            df['turnover'] = 0.0
        if 'pct_change' not in df.columns:
            # 用 close 和 open 估算涨跌幅
            df['pct_change'] = ((df['close'] - df['open']) / df['open'].replace(0, float('nan')) * 100).fillna(0)
        df.rename(columns={
            "date": "date", "open": "open", "close": "close",
            "high": "high", "low": "low", "volume": "volume",
            "amount": "amount", "pct_change": "pct_change", "turnover": "turnover"
        }, inplace=True)
        df['date'] = pd.to_datetime(df['date'])
        df.set_index('date', inplace=True)
        df_qlib = df.reset_index().rename(columns={"date": "trade_date"})
        append_daily_data(code, df_qlib)
        n = _write_daily_price(code, df)
        if verbose:
            print(f"  [{code}] 腾讯财经 OK (+{n} 行)")
        return True
    return False


# ─────────────────────────────────────────────
# 批量初始化：拉取所有历史数据
# ─────────────────────────────────────────────
def initial_sync(limit: int = None, verbose: bool = True) -> dict:
    """
    首次初始化：拉取所有股票历史数据
    baostock 无频率限制，速度极快
    支持断点续传（已有数据的股票自动跳过）
    """
    init_db()
    _init_qlib()  # Ensure Qlib is initialized before data operations
    stocks_df = get_all_stocks()

    if stocks_df.empty:
        print("[WARN] 股票列表为空，先自动拉取列表...")
        update_stock_list()
        stocks_df = get_all_stocks()
        if stocks_df.empty:
            print("[ERROR] 股票列表为空，请检查网络")
            return {"total": 0, "success": 0, "failed": 0}

    stocks = list(zip(stocks_df["code"], stocks_df["name"]))
    if limit:
        stocks = stocks[:limit]

    total     = len(stocks)
    success_n = 0
    failed_n  = 0

    if verbose:
        print(f"\n>>> 开始初始化历史数据（共 {total} 只股票，baostock 主数据源）")
        print(f">>> 支持断点续传，已有数据的股票自动跳过\n")

    start_time = time.time()

    for i, (code, name) in enumerate(stocks):
        if _should_skip(code, name):
            continue

        # 打印进度
        elapsed = time.time() - start_time
        speed   = (success_n + failed_n + 1) / max(elapsed, 1)
        eta_s   = int((total - i) / max(speed, 0.01))
        eta_str = f"{eta_s//60:02d}分{eta_s%60:02d}秒"

        print(f"\r[{i+1:4d}/{total}] {code} {name[:8]:<8} | "
              f"成功:{success_n:4d} 失败:{failed_n:3d} | "
              f"速度:{speed:.1f}只/s | 预计剩余:{eta_str}",
              end="", flush=True)

        # 批次间隔
        if (i + 1) % BATCH_SIZE == 0:
            pct = round((i + 1) / total * 100, 1)
            print(f"\n[{i+1:4d}/{total}] {pct:.1f}% 完成...")
            time.sleep(INTER_BATCH_DELAY)

        ok = sync_one_stock(code, HISTORY_START, verbose=False)
        if ok:
            success_n += 1
        else:
            failed_n += 1

        time.sleep(BASE_INTERVAL)

    if verbose:
        elapsed = time.time() - start_time
        print(f"\n\n>>> 初始化完成  耗时: {elapsed:.0f}秒")
        print(f"    成功: {success_n}  失败: {failed_n}")
        s = db_stats()
        print(f"    数据库: {s['有行情股票数']} 只股票，{s['行情记录总数']} 条行情")

    log_sync("initial_sync", total, success_n, failed_n,
             time.time() - start_time, f"baostock 初始化")

    return {"total": total, "success": success_n, "failed": failed_n}


# ─────────────────────────────────────────────
# 增量同步：每日更新（多线程版本）
# ─────────────────────────────────────────────
def _sync_worker(args):
    """同步单个股票的工作函数（用于多线程）"""
    code, name, start_date, end_date = args
    if _should_skip(code, name):
        return (code, name, None)  # None 表示跳过
    
    try:
        # 转换代码格式为 baostock 格式
        bs_code = _format_code_for_baostock(code)
        ok = sync_one_stock(bs_code, start_date, end_date, verbose=False)
        if ok:
            # 同步成功后计算策略分（使用原始代码）
            sync_strategy_score(code, verbose=False)
            return (code, name, True)
        else:
            return (code, name, False)
    except Exception:
        return (code, name, False)


def _get_sync_cache_file(start_date: str, end_date: str) -> str:
    """获取同步缓存文件路径"""
    cache_dir = os.path.join(os.path.dirname(__file__), '..', '.sync_cache')
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, f'sync_{start_date}_{end_date}.json')


def _load_sync_cache(start_date: str, end_date: str) -> set:
    """加载已同步的股票代码集合"""
    cache_file = _get_sync_cache_file(start_date, end_date)
    if os.path.exists(cache_file):
        try:
            import json
            with open(cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return set(data.get('synced_codes', []))
        except Exception:
            pass
    return set()


def _save_sync_cache(start_date: str, end_date: str, synced_codes: set):
    """保存已同步的股票代码集合"""
    cache_file = _get_sync_cache_file(start_date, end_date)
    try:
        import json
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump({
                'start_date': start_date,
                'end_date': end_date,
                'synced_codes': list(synced_codes),
                'updated_at': datetime.now().isoformat()
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] 保存同步缓存失败: {e}")


def _clear_sync_cache(start_date: str, end_date: str):
    """清除同步缓存"""
    cache_file = _get_sync_cache_file(start_date, end_date)
    if os.path.exists(cache_file):
        try:
            os.remove(cache_file)
        except Exception:
            pass


def daily_sync(verbose: bool = True, progress_callback=None, max_workers: int = 1,
               min_coverage: float = 0.9) -> dict:
    """
    每天运行一次：增量拉取当日行情（单线程版本，baostock 不支持多线程）

    Args:
        verbose: 是否打印详细日志
        progress_callback: 进度回调函数，接收(current, total, success, failed)参数
        max_workers: 已废弃，保持单线程
        min_coverage: 覆盖率阈值，默认0.9（90%），数据源不完整时可降低
    """
    import baostock as bs

    init_db()
    _init_qlib()  # Ensure Qlib is initialized before data operations
    stocks_df = get_all_stocks()

    if stocks_df.empty:
        print("[WARN] 股票列表为空，先自动更新...")
        update_stock_list()
        stocks_df = get_all_stocks()

    stocks = list(zip(stocks_df["code"], stocks_df["name"]))

    # 重置跳过统计
    _sync_stats.update({"total": len(stocks), "success": 0, "failed": 0,
                        "skipped_bse": 0, "skipped_st": 0})

    # 确定增量日期范围
    today = date.today()
    latest = get_latest_date_all()

    if not latest:
        start_date = (today - timedelta(days=5)).strftime("%Y-%m-%d")
        end_date = today.strftime("%Y-%m-%d")
    else:
        latest_date = datetime.strptime(latest, "%Y-%m-%d").date()
        # 全市场只有部分股票有最新日期时，逐日往前找覆盖>=min_coverage的日期
        candidate = latest_date
        five_days_ago = today - timedelta(days=5)
        while candidate >= five_days_ago:
            cand_str = candidate.strftime("%Y-%m-%d")
            total_in_db = get_stock_count_in_db()
            with get_conn() as conn:
                covered = conn.execute(
                    "SELECT COUNT(DISTINCT code) FROM daily_price WHERE trade_date = ?",
                    (cand_str,)
                ).fetchone()[0]
            if covered >= total_in_db * min_coverage:
                break
            candidate -= timedelta(days=1)

        if candidate < five_days_ago:
            start_date = five_days_ago.strftime("%Y-%m-%d")
            if verbose:
                print(f"[WARN] 近5日任一日期覆盖均不足{int(min_coverage*100)}%，执行全量补齐")
        else:
            start_date = (candidate + timedelta(days=1)).strftime("%Y-%m-%d")
        
        # 确定结束日期：如果今天是周末/节假日，则取最近一个交易日
        end_date = today
        for _ in range(7):  # 往前找最多7天
            if is_trading_day(end_date.strftime('%Y-%m-%d')):
                break
            end_date -= timedelta(days=1)
        end_date = end_date.strftime("%Y-%m-%d")

        # Always re-sync at least the last 5 calendar days to catch bad data
        min_start = (today - timedelta(days=5)).strftime("%Y-%m-%d")
        if start_date > min_start:
            start_date = min_start

    # 加载已同步的缓存
    synced_codes = _load_sync_cache(start_date, end_date)
    
    # 过滤掉已同步的股票和需要跳过的股票
    stocks_to_sync = [(code, name) for code, name in stocks 
                      if code not in synced_codes and not _should_skip(code, name)]
    
    if not stocks_to_sync:
        if verbose:
            print(f">>> 所有股票已同步完成（{start_date} ~ {end_date}）")
        _clear_sync_cache(start_date, end_date)
        return {"total": len(stocks), "success": len(stocks), "failed": 0}
    
    if verbose:
        if synced_codes:
            print(f"\n>>> 增量同步 [{start_date} ~ {end_date}]（共 {len(stocks)} 只，已同步 {len(synced_codes)} 只，剩余 {len(stocks_to_sync)} 只，单线程）")
        else:
            print(f"\n>>> 增量同步 [{start_date} ~ {end_date}]（共 {len(stocks)} 只，单线程）")

    # 单线程同步，只登录一次 baostock
    lg = bs.login()
    bs_available = (lg.error_code == '0')
    if not bs_available:
        if verbose:
            print(f"[WARN] baostock 登录失败: {lg.error_msg}，将使用备用数据源")

    start_time = time.time()
    success_n = len(synced_codes)  # 已同步的也算成功
    failed_n = 0
    skipped_n = 0
    processed_n = len(synced_codes)  # 从已同步数量开始
    total_n = len(stocks)

    def _fallback_sync(code: str, start_date: str, end_date: str) -> bool:
        """尝试 akshare → 腾讯财经 两级备用数据源"""
        try:
            if _sync_one_stock_akshare(code, start_date, end_date, verbose=False):
                return True
        except Exception:
            pass
        try:
            if _sync_one_stock_tencent(code, start_date, end_date, verbose=False):
                return True
        except Exception:
            pass
        return False

    try:
        for idx, (code, name) in enumerate(stocks_to_sync):
            processed_n += 1

            # 打印进度
            if verbose and processed_n % 10 == 0:
                elapsed = time.time() - start_time
                speed = (idx + 1) / max(elapsed, 1)
                eta_s = int((len(stocks_to_sync) - idx - 1) / max(speed, 0.01))
                eta_str = f"{eta_s//60:02d}分{eta_s%60:02d}秒"
                print(f"\r[{processed_n:4d}/{total_n}] {code} {name[:8]:<8} | "
                      f"成功:{success_n:4d} 失败:{failed_n:3d} 跳过:{skipped_n:3d} | "
                      f"速度:{speed:.1f}只/s | 预计剩余:{eta_str}",
                      end="", flush=True)

            # 调用进度回调
            if progress_callback:
                progress_callback(processed_n, total_n, success_n, failed_n)

            # 北交所跳过
            bs_code = _format_code_for_baostock(code)
            if bs_code.startswith('bj.'):
                skipped_n += 1
                continue

            synced_ok = False

            # 主数据源：baostock
            if bs_available:
                try:
                    rs = bs.query_history_k_data_plus(
                        bs_code,
                        "date,code,open,high,low,close,preclose,volume,amount,turn,pctChg",
                        start_date=start_date,
                        end_date=end_date,
                        frequency="d",
                        adjustflag="2"
                    )

                    if rs is not None and rs.error_code == '0':
                        data_list = []
                        while rs.next():
                            row = rs.get_row_data()
                            if row[0]:
                                try:
                                    data_list.append({
                                        'date': pd.to_datetime(row[0]),
                                        'open': float(row[2]) if row[2] else None,
                                        'high': float(row[3]) if row[3] else None,
                                        'low': float(row[4]) if row[4] else None,
                                        'close': float(row[5]) if row[5] else None,
                                        'volume': float(row[7]) if row[7] else 0,
                                        'amount': float(row[8]) if row[8] else 0,
                                        'turnover': float(row[9]) if row[9] else 0,
                                        'pct_change': float(row[10]) if row[10] else 0
                                    })
                                except (ValueError, TypeError):
                                    continue
                        if data_list:
                            df = pd.DataFrame(data_list)
                            df.set_index('date', inplace=True)
                            df_qlib = df.reset_index().rename(columns={"date": "trade_date"})
                            append_daily_data(code, df_qlib)
                            _write_daily_price(code, df)
                            synced_ok = True
                except Exception:
                    pass

            # baostock 失败或不可用 → 备用数据源
            if not synced_ok:
                synced_ok = _fallback_sync(code, start_date, end_date)

            if synced_ok:
                success_n += 1
                _sync_stats["success"] = success_n
                synced_codes.add(code)
                if len(synced_codes) % 10 == 0:
                    _save_sync_cache(start_date, end_date, synced_codes)
            else:
                failed_n += 1
                _sync_stats["failed"] = failed_n
                if verbose and failed_n % 100 == 0:
                    print(f"\n  [{code}] 所有数据源均失败")
    finally:
        # 确保登出
        if bs_available:
            try:
                bs.logout()
            except Exception:
                pass

    # 同步完成后清除缓存（全部完成才清除，部分完成保留用于断点续传）
    if len(synced_codes) >= len(stocks):
        _clear_sync_cache(start_date, end_date)
    else:
        _save_sync_cache(start_date, end_date, synced_codes)
    
    elapsed = time.time() - start_time
    if verbose:
        print(f"\n>>> 增量同步完成  耗时: {elapsed:.0f}秒")
        print(f"    成功: {success_n}  失败: {failed_n}  跳过: {skipped_n}")

    # 同步主要指数（供大盘择时用）—— 始终同步到 end_date，不受股票覆盖率影响
    if verbose:
        print(f"\n>>> 同步主要指数...")
    end_date_nohyphen = end_date.replace("-", "")
    index_results = sync_all_indices(start_date=end_date_nohyphen, end_date=end_date_nohyphen, verbose=verbose)
    if verbose:
        total_idx = sum(index_results.values())
        print(f">>> 指数同步完成，共写入 {total_idx} 行")

    log_sync("daily_sync", len(stocks), success_n, failed_n,
             elapsed, f"增量 {start_date}~{end_date} 单线程")

    return {"total": len(stocks), "success": success_n, "failed": failed_n}


# ─────────────────────────────────────────────
# 策略分计算并写入数据库
# ─────────────────────────────────────────────

def sync_strategy_score(code: str, verbose: bool = False) -> bool:
    """
    对指定股票的全量历史计算5策略独立评分 + 融合分，
    并批量写入数据库（覆盖所有历史日期，不再只存最新一天）。
    这样回测时可以直接从数据库读取历史评分，无需重新计算。
    """
    try:
        df = get_daily_price(code)
        if df is None or len(df) < 30:
            return False
        s1 = strategy_volume_breakout(df)
        s2 = strategy_ma_convergence(df)
        s3 = strategy_price_volume_divergence(df)
        s4 = strategy_bottom_fishing(df)
        s5 = strategy_whale_accumulation(df)
        fused = fuse_signals([s1, s2, s3, s4, s5], weights=DEFAULT_WEIGHTS)
        # 写入数据库（全量历史）
        update_strategy_scores_batch(code, fused)
        if verbose:
            last = fused.iloc[-1]
            fs = round(float(last.get("FUSION_SCORE", 0)), 1)
            print(f"  [{code}] 策略分已存 {len(fused)} 天，综合分={fs}/50")
        return True
    except Exception as e:
        if verbose:
            print(f"  [{code}] 策略分计算失败: {e}")
        return False


# ─────────────────────────────────────────────
# 快速拉取指定股票（用于前端实时调用）
# ─────────────────────────────────────────────
def fetch_and_save(code: str, start_date: str = HISTORY_START) -> bool:
    """快速拉取并保存单只股票（供 app.py 调用）"""
    return sync_one_stock(code, start_date, verbose=True)


# ─────────────────────────────────────────────
# 市值数据同步
# ─────────────────────────────────────────────
def sync_market_cap(codes: list = None, progress_cb=None) -> int:
    """
    批量同步市值数据（总股本/流通股本）到 stock_info 表。
    codes: 指定股票代码列表，None=全市场。
    progress_cb: 进度回调 fn(code, i, total)
    返回: 成功更新的数量
    """
    from core.data_fetcher import fetch_all_market_cap
    from core.db import batch_update_market_cap

    records = fetch_all_market_cap(codes, progress_cb)
    if records:
        batch_update_market_cap(records)
    return len(records)


# ─────────────────────────────────────────────
# 全市场重算打分（异步友好版本）
# ─────────────────────────────────────────────
def recalc_all_scores(progress_callback=None):
    """
    对数据库中所有股票的历史评分进行全量补算，
    同时将每日融合分高于阈值的日期写入 stock_signal 表。

    :param progress_callback: 进度回调 fn(current, total, success, failed), 可选
    :return: dict with total, success, failed, signals, elapsed_s
    """
    import json as _json

    STOP_LOSS_SC = -0.06
    TAKE_PROFIT_SC = 0.20
    START_CAPITAL_SC = 10000
    POSITION_PER_SC = 0.5
    SIG_THRESHOLD = 15.0

    stocks_df = get_all_stocks()
    if stocks_df.empty:
        return {"success": 0, "failed": 0, "days": 0, "signals": 0, "elapsed_s": 0}

    stocks = list(zip(stocks_df["code"], stocks_df["name"]))
    success = 0
    failed = 0
    total_days = 0
    sig_records = []
    start = time.time()
    total = len(stocks)

    for i, (code, name) in enumerate(stocks):
        if (i + 1) % 200 == 0:
            print(f"  补算进度: {i+1}/{total}  成功:{success}  失败:{failed}")

        ok = sync_strategy_score(code, verbose=False)
        if ok:
            success += 1
            df = get_daily_price(code)
            if df is not None:
                total_days += len(df)
        else:
            failed += 1
            continue

        try:
            df = get_daily_price(code)
            if df is None or len(df) < 30:
                continue
            s1 = strategy_volume_breakout(df)
            s2 = strategy_ma_convergence(df)
            s3 = strategy_price_volume_divergence(df)
            s4 = strategy_bottom_fishing(df)
            s5 = strategy_whale_accumulation(df)
            fused = fuse_signals([s1, s2, s3, s4, s5], weights=DEFAULT_WEIGHTS)
            for _, row in fused.iterrows():
                fs = float(row.get("FUSION_SCORE", 0) or 0)
                if fs < SIG_THRESHOLD:
                    continue
                trade_date = str(row.name.date()) if hasattr(row.name, "date") else str(row.name)[:10]
                price = round(float(row["close"]), 2)
                buy_money = int(START_CAPITAL_SC * POSITION_PER_SC)
                buy_volume = int(buy_money // (price * 100) * 100)
                stop_loss = round(price * (1 + STOP_LOSS_SC), 2)
                take_profit = round(price * (1 + TAKE_PROFIT_SC), 2)
                trigger_list = []
                if float(row.get("VOL_SCORE", 0) or 0) >= 2.0:
                    trigger_list.append("放量突破")
                if float(row.get("MA_SCORE", 0) or 0) >= 2.0:
                    trigger_list.append("均线粘合")
                if float(row.get("DIVERGE_SCORE", 0) or 0) >= 2.0:
                    trigger_list.append("量价背离")
                if float(row.get("BOTTOM_SCORE", 0) or 0) >= 2.0:
                    trigger_list.append("抄底")
                if float(row.get("WHALE_SCORE", 0) or 0) >= 2.0:
                    trigger_list.append("主力建仓")
                sig_records.append({
                    "scan_date": trade_date,
                    "trade_date": trade_date,
                    "code": code,
                    "name": name or code,
                    "price": price,
                    "fusion_score": round(fs, 2),
                    "vol_score": round(float(row.get("VOL_SCORE", 0) or 0), 1),
                    "ma_score": round(float(row.get("MA_SCORE", 0) or 0), 1),
                    "diverge_score": round(float(row.get("DIVERGE_SCORE", 0) or 0), 1),
                    "bottom_score": round(float(row.get("BOTTOM_SCORE", 0) or 0), 1),
                    "whale_score": round(float(row.get("WHALE_SCORE", 0) or 0), 1),
                    "trigger_list": _json.dumps(trigger_list, ensure_ascii=False),
                    "buy_price": price,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "buy_volume": buy_volume,
                    "buy_money": buy_money,
                    "sent_wechat": 0,
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
        except Exception as e:
            print(f"  [{code}] stock_signal 写入失败: {e}")

        if progress_callback and i % 50 == 0:
            progress_callback(i, "正在重算 " + str(i) + "/" + str(total))

    # Batch insert with single transaction
    if sig_records:
        with get_conn() as conn:
            conn.execute("BEGIN TRANSACTION")
            conn.executemany("""
                INSERT OR REPLACE INTO stock_signal
                  (scan_date, trade_date, code, name, price, fusion_score,
                   vol_score, ma_score, diverge_score, bottom_score, whale_score,
                   trigger_list, buy_price, stop_loss, take_profit,
                   buy_volume, buy_money, sent_wechat, created_at)
                VALUES
                  (:scan_date, :trade_date, :code, :name, :price, :fusion_score,
                   :vol_score, :ma_score, :diverge_score, :bottom_score, :whale_score,
                   :trigger_list, :buy_price, :stop_loss, :take_profit,
                   :buy_volume, :buy_money, :sent_wechat, :created_at)
            """, sig_records)
            conn.commit()
        print(f"  stock_signal 写入完成: {len(sig_records)} 条记录")

    elapsed = time.time() - start
    print(f"历史评分补算完成: {success} 只成功 / {failed} 只失败，{len(sig_records)} 条推荐写入 stock_signal，耗时 {elapsed:.0f}秒")

    if progress_callback:
        progress_callback(total, "重算完成: " + str(len(sig_records)) + " 条信号")

    return {
        "success": success,
        "failed": failed,
        "days": total_days,
        "signals": len(sig_records),
        "elapsed_s": round(elapsed, 1),
    }


# ─────────────────────────────────────────────
# 命令行接口
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    init_db()

    cmd = sys.argv[1] if len(sys.argv) > 1 else "daily"

    if cmd == "list":
        update_stock_list()
    elif cmd == "init":
        initial_sync(verbose=True)
    elif cmd == "init_test":
        initial_sync(limit=50, verbose=True)
    elif cmd == "daily":
        daily_sync(verbose=True)
    elif cmd == "stat":
        s = db_stats()
        for k, v in s.items():
            print(f"{k:15s}: {v}")
    elif cmd == "test":
        # 快速测试
        print("测试同步: 000001 平安银行")
        ok = sync_one_stock("000001", "20250101", verbose=True)
        print("结果:", "成功" if ok else "失败")
    else:
        print(f"未知命令: {cmd}")
        print("可用: list / init / init_test / daily / stat / test")
