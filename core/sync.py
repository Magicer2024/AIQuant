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
    log_sync, db_stats, get_conn, get_daily_price, upsert_index_daily, get_index_daily,
    # 自选股相关
    get_watchlist_codes, get_watchlist_with_names, count_watchlist,
    has_watchlist_data, get_latest_date_for_codes, get_stock_count_in_db_for_codes,
    # 龙虎榜（隔日动量信号线）
    upsert_lhb_detail, get_latest_lhb_date, get_lhb_map_for_date,
)
from qlib_engine.data_bridge import append_daily_data, batch_append_daily, append_calendar_dates
from qlib_engine import init_qlib as _init_qlib


def _write_daily_price(code: str, df: pd.DataFrame, source: str = "baostock") -> int:
    """写入 daily_price 表（与 Qlib 二进制并行写入，供仪表板/打分读取）

    单位统一约定（入库后）：
        volume     = 股
        amount     = 元
        pct_change = 百分比数值（如 1.23 表示 1.23%）
        turnover   = 百分比数值

    各数据源原始单位与换算：
        baostock : volume=股,   amount=元    → 无需换算
        akshare  : volume=手,   amount=元    → volume ×100 转股
        tencent  : 无volume,    amount=万元  → amount ×10000 转元；volume 用 amount/close 估算(股)
    """
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

    # —— 1. 按数据源统一单位（在 df 上 vectorized 操作）——
    df = df.copy()
    if source == "akshare" and "volume" in df.columns:
        # akshare stock_zh_a_hist: volume 单位是"手"(1手=100股)，转成股
        df["volume"] = df["volume"].fillna(0) * 100
    elif source == "tencent_direct":
        # 腾讯直连（core/tx_kline.py）：模块内已统一 volume=股 / amount=元，无需换算
        pass
    elif source == "tencent":
        # 腾讯 stock_zh_a_hist_tx: amount 单位是"万元"，转成元
        if "amount" in df.columns:
            df["amount"] = df["amount"].fillna(0) * 10000
        # 腾讯不返回 volume，用 成交额(元)/收盘价 估算成交量(股)
        if all(col in df.columns for col in ("volume", "amount", "close")):
            mask = (df["volume"].fillna(0) == 0) & (df["amount"] > 0) & (df["close"] > 0)
            df.loc[mask, "volume"] = df.loc[mask, "amount"] / df.loc[mask, "close"]

    # —— 2. 写入前清洗：拦截负价格/OHLC错位/负成交量等脏值 ——
    try:
        from core.data_cleaner import clean_dataframe
        df, _clean_stats = clean_dataframe(code, df, verbose=False, new_stock_skip=False)
    except Exception:
        pass  # 清洗失败不阻塞写入（兜底保护，避免清洗器异常导致数据无法同步）
    if df is None or df.empty:
        return 0

    # —— 3. 构建记录并批量写入 ——
    records = []
    for dt, row in df.iterrows():
        close = float(row.get("close", 0) or 0)
        records.append({
            "code":       code,
            "trade_date": str(dt.date()) if hasattr(dt, "date") else str(dt)[:10],
            "open":       float(row.get("open", 0) or 0),
            "high":       float(row.get("high", 0) or 0),
            "low":        float(row.get("low", 0) or 0),
            "close":      close,
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
    strategy_bottom_fishing_v2,
    strategy_whale_accumulation, strategy_oversold_rebound,
    fuse_signals
)
from strategy.mid_long import scan_mid_term, scan_long_term
from strategy.next_day_momentum import scan_next_day_momentum
from strategy.rec_filters import (
    trend_gate_series, quality_series, passes_quality,
    chase_filter_series, chase_filter, extension_filter_series,
    rsi_sweet_spot_series,
)
from config.strategy_params import SHORT_ENGINE, NEXT_DAY_MOMENTUM

# 短线抄底引擎选择：pure_bottom = v1 已反弹（线上默认，双口径回测验证组合）；
# pure_bottom_v2 = 买回踩平滑版（P1-2.1 灰度，tools/_eval_pullback.py 34 cohort：
# T+1 OC 胜率 51.5%→55.5%、均值 +0.104%→+0.211%、候选量约减半）。
# ⚠ 切换 v2 后必须跑 sync_strategy_score 全市场重算 daily_price 分数列——
#   增量信号路径（reuse_scores=True）复用 daily_price.bottom_score，不重算策略分，
#   否则 v2 引擎实际读到 v1 分数；回滚切回 pure_bottom 后同样需重算。
_BOTTOM_FISH_FN = (strategy_bottom_fishing_v2 if SHORT_ENGINE == "pure_bottom_v2"
                   else strategy_bottom_fishing)

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
    """判断指定日期是否为A股交易日（带缓存，避免每次调用打 akshare 网络接口）

    :param today: 日期字符串，默认为今天
    """
    if today is None:
        today = date.today().strftime("%Y-%m-%d")
    from core.trade_calendar import is_trading_day as _is_td
    return _is_td(today)


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
                n = _write_daily_price(code, df, source="baostock")
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
        n = _write_daily_price(code, df, source="akshare")
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
def initial_sync(limit: int = None, verbose: bool = True, target: str = "all") -> dict:
    """
    首次初始化：拉取所有股票历史数据
    baostock 无频率限制，速度极快
    支持断点续传（已有数据的股票自动跳过）

    target="all"（全市场，默认）/"watchlist"（仅自选股）
    """
    target = target if target in ("all", "watchlist") else "all"

    init_db()
    _init_qlib()  # Ensure Qlib is initialized before data operations

    if target == "watchlist":
        if count_watchlist() == 0:
            print("[WARN] 自选列表为空，initial_sync(target='watchlist') 跳过")
            return {"total": 0, "success": 0, "failed": 0, "target": "watchlist",
                    "skipped": "empty_watchlist"}
        stocks_df = get_watchlist_with_names()[["code", "name"]]
        if stocks_df.empty:
            print("[WARN] 自选股 stock_info 未匹配，请先 update_stock_list() 再试")
            return {"total": 0, "success": 0, "failed": 0, "target": "watchlist"}
    else:
        stocks_df = get_all_stocks()
        if stocks_df.empty:
            print("[WARN] 股票列表为空，先自动拉取列表...")
            update_stock_list()
            stocks_df = get_all_stocks()
            if stocks_df.empty:
                print("[ERROR] 股票列表为空，请检查网络")
                return {"total": 0, "success": 0, "failed": 0, "target": "all"}

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
               min_coverage: float = 0.9, target: str = "all",
               recalc: bool = True) -> dict:
    """
    每天运行一次：增量拉取当日行情（单线程版本，baostock 不支持多线程）

    Args:
        verbose: 是否打印详细日志
        progress_callback: 进度回调函数，接收(current, total, success, failed)参数
        max_workers: 已废弃，保持单线程
        min_coverage: 覆盖率阈值，默认0.9（90%），数据源不完整时可降低
        target: "all"（全市场，默认）/ "watchlist"（仅自选股）
        recalc: 同步完成后是否立即重算打分（默认 True）。置 True 时本函数
                自洽完成「拉行情 + 拉龙虎榜 + 重算打分」，保证隔日动量金色卡片
                无需再手动点「立即重算打分」即可出现。
    """
    import baostock as bs

    target = target if target in ("all", "watchlist") else "all"

    init_db()
    _init_qlib()  # Ensure Qlib is initialized before data operations

    # 1) 选取股票池（all = stock_info；watchlist = personal_watchlist ∩ stock_info）
    if target == "watchlist":
        wl_df = get_watchlist_with_names()
        if wl_df.empty or count_watchlist() == 0:
            print("[WARN] 自选列表为空，daily_sync(target='watchlist') 跳过")
            _sync_stats.update({"total": 0, "success": 0, "failed": 0,
                                "skipped_bse": 0, "skipped_st": 0, "target": "watchlist"})
            return {"total": 0, "success": 0, "failed": 0, "skipped": "empty_watchlist", "target": "watchlist"}
        stocks_df = wl_df[["code", "name"]].copy()
    else:
        # target == "all" —— 同步前先确保 stock_info 完整
        update_stock_list()
        stocks_df = get_all_stocks()

    if stocks_df.empty:
        print("[WARN] 股票列表为空，先自动更新...")
        update_stock_list()
        stocks_df = get_all_stocks() if target == "all" else get_watchlist_with_names()[["code", "name"]]

    stocks = list(zip(stocks_df["code"], stocks_df["name"]))

    # 重置跳过统计
    _sync_stats.update({"total": len(stocks), "success": 0, "failed": 0,
                        "skipped_bse": 0, "skipped_st": 0, "target": target})

    # 确定增量日期范围
    today = date.today()
    latest = get_latest_date_all() if target == "all" else get_latest_date_for_codes([c for c, _ in stocks])

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
            if target == "all":
                total_in_db = get_stock_count_in_db()
            else:
                total_in_db = get_stock_count_in_db_for_codes([c for c, _ in stocks]) or len(stocks)
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
        """尝试 腾讯直连 → akshare → 腾讯(akshare封装) 三级备用数据源"""
        # 腾讯直连（core/tx_kline.py）：与东财完全异构、免费无 token、盘后当天可用；
        # 优先级高于 akshare（akshare 底层多为东财，东财整体挂时同源失效）。
        try:
            from core.tx_kline import fetch_kline_range
            df = fetch_kline_range(code, start_date, end_date)
            if df is not None and not df.empty:
                # _write_daily_price 契约：df 索引为日期（与 baostock/akshare 路径一致）
                _write_daily_price(code, df.set_index("trade_date"),
                                   source="tencent_direct")
                return True
        except Exception:
            pass
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
                            _write_daily_price(code, df, source="baostock")
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

    # 同步当日龙虎榜（供隔日动量信号线）—— 抓取失败不阻断主流程
    if NEXT_DAY_MOMENTUM.get("enabled"):
        try:
            if verbose:
                print(f"\n>>> 同步龙虎榜 {start_date}~{end_date}...")
            from core.data_fetcher import fetch_lhb_detail
            lhb_df = fetch_lhb_detail(start_date, end_date)
            n_lhb = upsert_lhb_detail(lhb_df) if lhb_df is not None and not lhb_df.empty else 0
            if verbose:
                print(f">>> 龙虎榜同步完成，写入/更新 {n_lhb} 条")
        except Exception as e:
            print(f"  [WARN] 龙虎榜同步失败（不阻断同步主流程）: {e}")

    log_sync("daily_sync", len(stocks), success_n, failed_n,
             elapsed, f"增量 {start_date}~{end_date} 单线程")

    # ── 同步完成后立即重算全量打分（含龙虎榜隔日动量信号）──
    # 让「全市场同步」成为自洽流程：拉行情 + 拉龙虎榜 + 重算打分，
    # 三步同一顺序执行，隔日动量金色卡片无需再手动点「立即重算打分」。
    recalc_result = None
    if recalc and success_n > 0:
        try:
            if verbose:
                print(f"\n>>> 同步完成，开始重算打分（target={target}，含龙虎榜隔日动量）...")
            recalc_result = recalc_all_scores(target=target)
            if verbose:
                print(f">>> 打分重算完成: {recalc_result}")
        except Exception as e:
            print(f"  [WARN] 同步后重算打分失败（不影响已同步的行情/龙虎榜数据）: {e}")

    return {"total": len(stocks), "success": success_n, "failed": failed_n,
            "recalc": recalc_result}


# ─────────────────────────────────────────────
# 策略分计算并写入数据库
# ─────────────────────────────────────────────

def sync_single_stock_to_watchlist(code: str, verbose: bool = False) -> dict:
    """
    「加入自选」后即时补拉单只股的历史 + 打分。
    用于：用户加自选后无需等 18:00 调度，立即能在 watchlist 卡片看到数据。

    返回结构（永远不抛异常）：
      {
        "ok": True/False,
        "code": "000001",
        "rows_added": int,        # 新写入 daily_price 的行数（已存在时为 0）
        "score_computed": bool,   # 是否成功重算 daily_price 融合分
        "elapsed_s": float,
        "message": str,
      }
    """
    import time as _time
    started = _time.time()
    code = (code or "").strip()
    if not (isinstance(code, str) and code.isdigit() and len(code) == 6):
        return {"ok": False, "code": code, "rows_added": 0, "score_computed": False,
                "elapsed_s": 0.0, "message": "code 格式非法（需 6 位数字）"}

    try:
        # 1) 先确保 stock_info 有该 code（新上市股可能未入库）
        from core.db import get_conn
        with get_conn() as conn:
            row = conn.execute("SELECT 1 FROM stock_info WHERE code=? LIMIT 1", (code,)).fetchone()
        if row is None:
            # 尝试 update_stock_list()（拉全 A 列表，约 5~10 秒）
            try:
                update_stock_list()
            except Exception as e:
                if verbose:
                    print(f"[watchlist] {code} update_stock_list 失败: {e}")
            with get_conn() as conn:
                row = conn.execute("SELECT 1 FROM stock_info WHERE code=? LIMIT 1", (code,)).fetchone()
            if row is None:
                return {"ok": False, "code": code, "rows_added": 0, "score_computed": False,
                        "elapsed_s": round(_time.time() - started, 2),
                        "message": f"code={code} 不在 stock_info 中，请确认股票代码正确"}

        # 2) 判断是否需要补拉历史
        need_pull = not has_watchlist_data(code)
        rows_added = 0
        if need_pull:
            ok = sync_one_stock(code, HISTORY_START, end_date=None, verbose=False)
            if ok:
                # 统计本次新增行数（粗略：用 last row count 差值，这里以"全量=已存在"为基准）
                from core.db import get_conn
                with get_conn() as conn:
                    cnt = conn.execute("SELECT COUNT(*) FROM daily_price WHERE code=?", (code,)).fetchone()[0]
                rows_added = int(cnt or 0)
            else:
                return {"ok": False, "code": code, "rows_added": 0, "score_computed": False,
                        "elapsed_s": round(_time.time() - started, 2),
                        "message": f"code={code} 历史拉取失败"}

        # 3) 重算单股策略分（无数据时直接跳过）
        score_ok = False
        try:
            score_ok = bool(sync_strategy_score(code, verbose=False))
        except Exception as e:
            if verbose:
                print(f"[watchlist] {code} sync_strategy_score 异常: {e}")
            score_ok = False

        # 4) 刷新 latest_price 物化表（单股）
        try:
            from core.repository.price_repo import refresh_latest_price
            refresh_latest_price([code])
        except Exception:
            pass

        return {
            "ok": True,
            "code": code,
            "rows_added": rows_added,
            "score_computed": score_ok,
            "elapsed_s": round(_time.time() - started, 2),
            "message": "完成" if score_ok else "行情已写入，策略分计算被跳过（数据不足）",
        }
    except Exception as e:
        return {"ok": False, "code": code, "rows_added": 0, "score_computed": False,
                "elapsed_s": round(_time.time() - started, 2),
                "message": f"异常: {e}"}


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
        s4 = _BOTTOM_FISH_FN(df)
        s5 = strategy_whale_accumulation(df)
        # daily_price.fusion_score 固定为纯抄底口径（回测验证过的基准，与市场状态无关），
        # 供回测/研究脚本读取；短线推荐用的自适应口径写在 stock_signal 表。
        from config.strategy_params import PURE_BOTTOM_WEIGHTS
        fused = fuse_signals([s1, s2, s3, s4, s5],
                             weights=PURE_BOTTOM_WEIGHTS, mode="weighted_avg")
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

# stock_signal 写入 SQL（全量/增量共用，字段顺序与表结构一致）
_SIGNAL_INSERT_SQL = """
    INSERT OR REPLACE INTO stock_signal
      (scan_date, trade_date, code, name, price, fusion_score,
       vol_score, ma_score, diverge_score, bottom_score, whale_score,
       trigger_list, buy_price, stop_loss, take_profit,
       buy_volume, buy_money, sent_wechat, created_at,
       horizon, strategy, pct_above_ma20)
    VALUES
      (:scan_date, :trade_date, :code, :name, :price, :fusion_score,
       :vol_score, :ma_score, :diverge_score, :bottom_score, :whale_score,
       :trigger_list, :buy_price, :stop_loss, :take_profit,
       :buy_volume, :buy_money, :sent_wechat, :created_at,
       :horizon, :strategy, :pct_above_ma20)
"""


def _resolve_sig_threshold():
    """
    解析短线 SIG_THRESHOLD（与 recalc_all_scores 同口径）：
    自适应开启时按 20 日波动率取两档阈值，异常/关闭时回退参数覆盖层。
    返回 (threshold, market_state)，market_state 为 None 表示未启用自适应。
    """
    from config.strategy_params import get_param, ADAPTIVE_WEIGHTS_ENABLED
    if ADAPTIVE_WEIGHTS_ENABLED:
        try:
            from strategy.adaptive_weights import get_adaptive_threshold, detect_market_state
            _market_state = detect_market_state()
            return float(get_adaptive_threshold(_market_state)), _market_state
        except Exception as e:
            print(f"  [WARN] 自适应阈值加载失败，回退默认阈值: {e}")
            return float(get_param("sig_threshold")), None
    return float(get_param("sig_threshold")), None


def _build_signal_records(df, code, name, total_shares, sig_threshold,
                          stop_loss_sc, take_profit_sc, lhb_row=None,
                          scan_date=None, reuse_scores=False) -> list:
    """
    对单只股票的日线 df 生成 stock_signal 记录列表（与 recalc_all_scores 同口径）。

    短线段：
      - scan_date 为 "YYYY-MM-DD" 时，只生成该日期的记录（增量模式，每日盘后）；
      - scan_date 为 None 时，对全部历史命中日生成（全量重算模式，供回测读历史）。
    中/长线与隔日动量（龙虎榜）恒只评估 df 最新交易日（信号持续期长，逐日写库会膨胀）。

    :param reuse_scores: True 时短线复用 daily_price 已算好的分数列（仅限单日评估，
        须在 sync_strategy_score 写库之后调用，否则回退全量重算）。
    :return: list[dict]（stock_signal 行），可能为空
    """
    import json as _json
    if df is None or len(df) < 30:
        return []
    records = []
    START_CAPITAL_SC = 10000
    POSITION_PER_SC = 0.5

    # 短线扩展度（低扩展度排序用，S4 口径）：价相对 MA20 偏离
    _ma20 = df["close"].astype(float).rolling(20).mean()

    def _pct_above_ma20(ts, close_val):
        """返回价相对 MA20 偏离（小数，0.03 = 高于 MA20 3%）；MA20 缺失按 0"""
        try:
            m = float(_ma20.get(ts, float("nan")))
        except Exception:
            m = float("nan")
        if m and m > 0 and close_val:
            return round(float(close_val) / m - 1.0, 4)
        return 0.0

    if SHORT_ENGINE == "oversold_rebound":
        # ── 短线：超跌反弹v3 + 趋势闸门 + 质量过滤 ──
        reb = strategy_oversold_rebound(df)
        gate = trend_gate_series(df).reindex(reb.index).fillna(False)
        qual = quality_series(name, df, total_shares).reindex(reb.index).fillna(False)
        for dt, r in reb.iterrows():
            trade_date = str(dt.date()) if hasattr(dt, "date") else str(dt)[:10]
            if scan_date is not None and trade_date != scan_date:
                continue
            if not bool(r.get("BUY_SIGNAL", False)):
                continue
            if not bool(gate.get(dt, False)) or not bool(qual.get(dt, False)):
                continue
            score4 = float(r.get("BUY_SCORE", 0) or 0)
            fs = round(score4 / 4.0 * 50.0, 2)  # 0~4 → 0~50 量纲对齐
            price = round(float(r["close"]), 2)
            buy_money = int(START_CAPITAL_SC * POSITION_PER_SC)
            buy_volume = int(buy_money // (price * 100) * 100)
            stop_loss = round(price * (1 + stop_loss_sc), 2)
            take_profit = round(price * (1 + take_profit_sc), 2)
            trigger_list = [
                f"超跌反弹v3 评分 {score4:.1f}/4",
                "站上MA20且均线向上",
                "非ST/流动性达标",
            ]
            records.append({
                "scan_date": trade_date,
                "trade_date": trade_date,
                "code": code,
                "name": name or code,
                "price": price,
                "fusion_score": fs,
                "vol_score": 0,
                "ma_score": 0,
                "diverge_score": 0,
                "bottom_score": round(score4, 1),
                "whale_score": 0,
                "trigger_list": _json.dumps(trigger_list, ensure_ascii=False),
                "buy_price": price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "buy_volume": buy_volume,
                "buy_money": buy_money,
                "sent_wechat": 0,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "horizon": "short",
                "strategy": "超跌反弹v3",
                "pct_above_ma20": _pct_above_ma20(dt, price),
            })
    else:
        # ── 短线：纯抄底融合分（SHORT_ENGINE="pure_bottom" 一键回退）──
        # 权重固定 PURE_BOTTOM_WEIGHTS（2026-07-31 双口径回测唯一赚钱组合），
        # 与 daily_price.fusion_score 同口径；自适应只保留「阈值」这一项。
        from config.strategy_params import PURE_BOTTOM_WEIGHTS, FUSION_MODE
        _SCORE_COLS = {"vol_score", "ma_score", "diverge_score",
                       "bottom_score", "whale_score", "fusion_score"}
        if reuse_scores and scan_date is not None and _SCORE_COLS.issubset(df.columns):
            # 增量模式：复用 daily_price 分数列（sync_strategy_score 已在同流程写库，
            # 纯抄底权重下 weighted_avg 与 max 数学等价 → 与全量重算口径一致），
            # 只取目标日一行，避免对全历史重算 5 策略（全市场耗时约 2 分钟 → 秒级）。
            _target = df.index[df.index == pd.Timestamp(scan_date)]
            if len(_target) == 0:
                return records
            _r = df.loc[_target[0]]
            fused = pd.DataFrame([{
                "VOL_SCORE": float(_r.get("vol_score") or 0),
                "MA_SCORE": float(_r.get("ma_score") or 0),
                "DIVERGE_SCORE": float(_r.get("diverge_score") or 0),
                "BOTTOM_SCORE": float(_r.get("bottom_score") or 0),
                "WHALE_SCORE": float(_r.get("whale_score") or 0),
                "FUSION_SCORE": float(_r.get("fusion_score") or 0),
                "close": float(_r["close"]),
            }], index=[_target[0]])
        else:
            # 全量重算 / 分数列缺失时的回退：对全历史重算 5 策略 + 融合
            s1 = strategy_volume_breakout(df)
            s2 = strategy_ma_convergence(df)
            s3 = strategy_price_volume_divergence(df)
            s4 = _BOTTOM_FISH_FN(df)
            s5 = strategy_whale_accumulation(df)
            fused = fuse_signals([s1, s2, s3, s4, s5],
                                 weights=PURE_BOTTOM_WEIGHTS, mode=FUSION_MODE)
        qual = quality_series(name, df, total_shares).reindex(fused.index).fillna(False)
        # 趋势闸门：排除 MA20 向下/未站上 MA20 的"一路阴跌"接飞刀信号
        # （2026-07-29 回测：隔日OC平均 -0.043%→-0.003%，信号数 -69%）
        gate = trend_gate_series(df).reindex(fused.index).fillna(False)
        # 追高否决：连板天数>=3 / 涨停打开 / 近3日急涨>=25% → 不推荐
        # （2026-08-05：修复"连板启动期被闸门挡、涨停打开放量日反而高分进推荐"问题）
        chase = chase_filter_series(df).reindex(fused.index).fillna(False)
        # RSI 甜区：RSI(14)∈[lo,hi] 剔除弱势(<lo)与超买(>hi)（2026-08 P3-1，
        # backtest_enhance E3 回测：T+1 胜率 48.6%→49.3%、均值 +0.100%→+0.123%；
        # enabled=False 一键降级；口径与 tools/backtest_*.py rsi14 一致）
        rsi_ok = rsi_sweet_spot_series(df).reindex(fused.index).fillna(False)
        # 扩展度否决：价 > MA20×1.12 硬否决（2026-08 P0-1.2，diag 实测高位组
        # T+1 OC 48.3%/+0.04% 劣于低位组 52.2%/+0.28%；enabled=False 一键降级）
        ext = extension_filter_series(df).reindex(fused.index).fillna(False)
        for _, row in fused.iterrows():
            trade_date = str(row.name.date()) if hasattr(row.name, "date") else str(row.name)[:10]
            if scan_date is not None and trade_date != scan_date:
                continue
            fs = float(row.get("FUSION_SCORE", 0) or 0)
            if fs < sig_threshold:
                continue
            # 质量过滤（ST/流动性/市值），清理 picks，逻辑本身不改
            if not bool(qual.get(row.name, False)):
                continue
            if not bool(gate.get(row.name, False)):
                continue
            if not bool(chase.get(row.name, False)):
                continue
            if not bool(rsi_ok.get(row.name, False)):
                continue
            if not bool(ext.get(row.name, False)):
                continue
            price = round(float(row["close"]), 2)
            buy_money = int(START_CAPITAL_SC * POSITION_PER_SC)
            buy_volume = int(buy_money // (price * 100) * 100)
            stop_loss = round(price * (1 + stop_loss_sc), 2)
            take_profit = round(price * (1 + take_profit_sc), 2)
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
            records.append({
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
                "horizon": "short",
                "strategy": "短线融合",
                "pct_above_ma20": _pct_above_ma20(row.name, price),
            })

    # ── 中/长线信号：只评估最新交易日，命中各写一条 ──
    for _scan in (scan_mid_term, scan_long_term):
        sig = _scan(df)
        if not sig:
            continue
        # 质量过滤（ST/流动性/市值），逻辑本身不改
        if not passes_quality(name, df, total_shares):
            continue
        # 追高否决（2026-08-05：与短线同口径——连板/涨停打开/急涨后不推长线建仓，
        # 000815 三连板后的 long 41.67 分即因此不再写入）
        if not chase_filter(df):
            continue
        records.append({
            "scan_date": sig["trade_date"],
            "trade_date": sig["trade_date"],
            "code": code,
            "name": name or code,
            "price": sig["buy_price"],
            "fusion_score": sig["fusion_score"],
            "vol_score": 0,
            "ma_score": 0,
            "diverge_score": 0,
            "bottom_score": 0,
            "whale_score": 0,
            "trigger_list": _json.dumps(sig["triggers"], ensure_ascii=False),
            "buy_price": sig["buy_price"],
            "stop_loss": sig["stop_loss"],
            "take_profit": sig["take_profit"],
            "buy_volume": 0,
            "buy_money": 0,
            "sent_wechat": 0,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "horizon": sig["horizon"],
            "strategy": sig["strategy"],
            "pct_above_ma20": _pct_above_ma20(pd.Timestamp(sig["trade_date"]), sig["buy_price"]),
        })

    # ── 隔日动量（龙虎榜净买占比）：仅最新交易日、可交易子集 ──
    # 追加在短线记录之后：同 horizon 同票 INSERT OR REPLACE 时动量信号胜出
    if lhb_row:
        nd_sig = scan_next_day_momentum(
            df, lhb_row, name, total_shares, params=NEXT_DAY_MOMENTUM)
        if nd_sig and passes_quality(name, df, total_shares):
            records.append({
                "scan_date": nd_sig["trade_date"],
                "trade_date": nd_sig["trade_date"],
                "code": code,
                "name": name or code,
                "price": nd_sig["buy_price"],
                "fusion_score": nd_sig["fusion_score"],
                "vol_score": 0,
                "ma_score": 0,
                "diverge_score": 0,
                "bottom_score": 0,
                "whale_score": 0,
                "trigger_list": _json.dumps(nd_sig["triggers"], ensure_ascii=False),
                "buy_price": nd_sig["buy_price"],
                "stop_loss": nd_sig["stop_loss"],
                "take_profit": nd_sig["take_profit"],
                "buy_volume": 0,
                "buy_money": 0,
                "sent_wechat": 0,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "horizon": nd_sig["horizon"],
                "strategy": nd_sig["strategy"],
                "pct_above_ma20": _pct_above_ma20(
                    pd.Timestamp(nd_sig["trade_date"]), nd_sig["buy_price"]),
            })

    return records


def _run_post_recalc_hooks() -> dict:
    """推荐闭环钩子（best-effort）：常驻规则扫描 + outcome 追踪 + 策略优化器。

    供全量 recalc_all_scores 与每日增量 recalc_incremental_signals 共用，
    任一步失败不影响主流程。
    """
    result = {"rule_hits": 0, "outcome_updated": 0, "diagnosis_findings": 0}
    try:
        from strategy.rule_scanner import scan_active_rules
        rule_result = scan_active_rules()
        result["rule_hits"] = rule_result.get("hits", 0)
        print(f"  常驻规则扫描完成: {rule_result.get('rules', 0)} 条规则 / "
              f"{result['rule_hits']} 条命中写入 strategy_signals")
    except Exception as e:
        print(f"  [WARN] 常驻规则扫描失败（不影响主流程）: {e}")
    try:
        from core.outcome_tracker import insert_new_outcomes, evaluate_outcomes
        insert_new_outcomes()
        result["outcome_updated"] = evaluate_outcomes()
        print(f"  推荐结果追踪: {result['outcome_updated']} 条评估更新")
    except Exception as e:
        print(f"  [WARN] 推荐结果追踪失败（不影响主流程）: {e}")
    try:
        from strategy.optimizer import run_post_sync as _optimizer_run
        opt = _optimizer_run()
        _diag = opt.get("diagnosis") or {}
        result["diagnosis_findings"] = len(_diag.get("findings") or [])
        print(f"  策略优化器: 诊断结论 {result['diagnosis_findings']} 条"
              + (f"，寻优建议: {(opt.get('tuning') or {}).get('suggestion') or '无'}"
                 if opt.get("tuning") is not None else ""))
    except Exception as e:
        print(f"  [WARN] 策略优化器失败（不影响主流程）: {e}")
    return result


def recalc_incremental_signals(trade_dates: list[str] | None = None,
                               verbose: bool = True,
                               progress_callback=None,
                               target: str = "all") -> dict:
    """
    增量重算 stock_signal：只对最近交易日重新评估并重写该日信号，
    历史日的信号保留不动（供回测读取）。

    定位：每日盘后调度（daily_sync_by_date 之后）的推荐刷新入口，
    替代分钟级全量 recalc_all_scores。仅当发生数据回填/修正时，
    才需要全量 recalc_all_scores 重算历史。

    :param trade_dates: 交易日列表（'%Y%m%d' 或 '%Y-%m-%d'），取最后一个作为信号评估日
    :param target: "all"（全市场，默认）/ "watchlist"（仅自选股）
    :return: {"scan_date": ..., "signals": N, "codes": M, "elapsed_s": ...}
    """
    start = time.time()
    target = target if target in ("all", "watchlist") else "all"
    if not trade_dates:
        return {"scan_date": None, "signals": 0, "codes": 0,
                "elapsed_s": 0.0, "target": target}

    # 统一信号评估日格式为 YYYY-MM-DD（与 daily_price.trade_date 存储格式一致）
    scan_date = str(trade_dates[-1])
    if len(scan_date) == 8 and scan_date.isdigit():
        scan_date = f"{scan_date[:4]}-{scan_date[4:6]}-{scan_date[6:]}"

    # 受影响股票 = 该日有行情写入的股票（停牌股当日无行情，本就无需当日信号）
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT code FROM daily_price WHERE trade_date=?",
            (scan_date,),
        ).fetchall()
    codes = [r["code"] for r in rows]
    if target == "watchlist":
        wl_set = set(get_watchlist_codes())
        codes = [c for c in codes if c in wl_set]
    if not codes:
        if verbose:
            print(f"  [增量信号] {scan_date} 无受影响股票，跳过")
        return {"scan_date": scan_date, "signals": 0, "codes": 0,
                "elapsed_s": round(time.time() - start, 1), "target": target}

    # 名称映射（stock_signal.name 字段；缺映射时用 code 兜底，不影响过滤）
    try:
        stocks_df = get_all_stocks()
        name_map = dict(zip(stocks_df["code"], stocks_df["name"])) if not stocks_df.empty else {}
    except Exception:
        name_map = {}

    # 阈值/止损止盈（与全量 recalc_all_scores 同口径）
    from config.strategy_params import get_param
    STOP_LOSS_SC = float(get_param("short_stop_loss"))
    TAKE_PROFIT_SC = float(get_param("short_take_profit"))
    SIG_THRESHOLD, _market_state = _resolve_sig_threshold()
    if verbose and _market_state is not None:
        print(f"  [增量信号] {scan_date} 市场状态 {_market_state['detail']}，"
              f"阈值 {SIG_THRESHOLD}，止损 {STOP_LOSS_SC:.0%} 止盈 {TAKE_PROFIT_SC:.0%}")

    # 质量过滤所需总股本映射 + 龙虎榜预加载
    from core.db import get_market_cap_map
    try:
        _mktcap_map = get_market_cap_map()
    except Exception:
        _mktcap_map = {}
    _lhb_map = {}
    if NEXT_DAY_MOMENTUM.get("enabled"):
        try:
            _lhb_date = get_latest_lhb_date()
            if _lhb_date:
                _lhb_map = get_lhb_map_for_date(_lhb_date)
                if verbose:
                    print(f"  [增量信号] 预加载龙虎榜 {_lhb_date}：{len(_lhb_map)} 只")
        except Exception as e:
            if verbose:
                print(f"  [WARN] 隔日动量龙虎榜预加载失败: {e}")

    sig_records = []
    n_success = 0
    for i, code in enumerate(codes):
        if verbose and (i + 1) % 500 == 0:
            print(f"    增量信号进度: {i+1}/{len(codes)}")
        if progress_callback and (i + 1) % 200 == 0:
            progress_callback(int((i + 1) / len(codes) * 100),
                              f"增量信号 {i+1}/{len(codes)}，已生成 {len(sig_records)} 条")
        try:
            # 增量只评估最新交易日：读取 scan_date 前 ~700 自然日窗口即可
            # （覆盖长线 250 日回撤 + MA120 斜率 + 中线 80 日 + 短线滚动窗口；
            #  滚动指标在 scan_date 处与全历史读取完全一致，I/O 与计算量大幅下降）
            _start = (pd.Timestamp(scan_date) - timedelta(days=700)).strftime("%Y-%m-%d")
            df = get_daily_price(code, start_date=_start)
            if df is None or len(df) < 30:
                continue
            # 停牌密集股窗口内行数可能不足 scan_long_term 的 min_rows=250，
            # 回退全历史读取，保证长线评估口径与全量重算完全一致
            if len(df) < 260:
                df = get_daily_price(code)
                if df is None or len(df) < 30:
                    continue
            _ts = (_mktcap_map.get(code) or {}).get("total_shares")
            _recs = _build_signal_records(
                df, code, name_map.get(code) or code, _ts,
                SIG_THRESHOLD, STOP_LOSS_SC, TAKE_PROFIT_SC,
                lhb_row=_lhb_map.get(code), scan_date=scan_date,
                reuse_scores=True)
            sig_records.extend(_recs)
            n_success += 1
        except Exception as e:
            if verbose:
                print(f"  [{code}] 增量信号生成失败: {e}")

    # 重写当日信号（历史日保留不动）：
    # 当日 short/mid/long/动量全部由本次增量重新生成，语义与全量 recalc 的
    # "当日候选完全由当日过滤链决定"一致；target=all 时清当日全部，watchlist 时只清自选股。
    with get_conn() as conn:
        conn.execute("BEGIN TRANSACTION")
        if target == "all":
            conn.execute("DELETE FROM stock_signal WHERE scan_date=?", (scan_date,))
        else:
            ph = ",".join("?" * len(codes))
            conn.execute(
                f"DELETE FROM stock_signal WHERE scan_date=? AND code IN ({ph})",
                [scan_date] + codes)
        if sig_records:
            conn.executemany(_SIGNAL_INSERT_SQL, sig_records)
        conn.commit()

    elapsed = time.time() - start
    if verbose:
        print(f"  增量信号重算完成: {len(sig_records)} 条"
              f"（{scan_date}，{n_success}/{len(codes)} 只，耗时 {elapsed:.1f}s）")

    # ── 复盘闭环：导入新推荐 + 评估收益（best-effort）──
    # 与全量路径 _run_post_recalc_hooks 的 outcome 部分同口径；不在此跑规则扫描/
    # 策略优化器，保持增量路径轻量（每日盘后调度高频触发）。
    try:
        from core.outcome_tracker import insert_new_outcomes, evaluate_outcomes
        insert_new_outcomes()
        _m = evaluate_outcomes()
        if verbose:
            print(f"  复盘追踪: 推荐导入完成 / 收益评估更新 {_m} 条")
    except Exception as e:
        if verbose:
            print(f"  [WARN] 复盘追踪失败（不影响主流程）: {e}")

    return {"scan_date": scan_date, "signals": len(sig_records),
            "codes": n_success, "elapsed_s": round(elapsed, 1), "target": target}


def recalc_all_scores(progress_callback=None, target: str = "all"):
    """
    对数据库中所有股票的历史评分进行全量补算，
    同时将每日融合分高于阈值的日期写入 stock_signal 表。

    :param progress_callback: 进度回调 fn(percent, message)，percent 为 0~100 整数，可选
    :param target: "all"（全市场，默认）/ "watchlist"（仅自选股）
                   watchlist 模式下，stock_signal 也只写自选股命中
    :return: dict with total, success, failed, signals, elapsed_s, target
    """
    target = target if target in ("all", "watchlist") else "all"

    # 止损/止盈比例走参数覆盖层（优化器采纳建议后即时生效，默认 -6%/+20%）
    from config.strategy_params import get_param
    STOP_LOSS_SC = float(get_param("short_stop_loss"))
    TAKE_PROFIT_SC = float(get_param("short_take_profit"))
    START_CAPITAL_SC = 10000
    POSITION_PER_SC = 0.5

    # ── 短线融合分口径：固定纯抄底（2026-07-31 双口径回测结论）─────────────
    # tools/eval_fusion_mode.py 五组对照（长窗 2024-02~2026-07，组合口径 474~481 笔）：
    #   平均+自适应(旧线上) 胜率40.08% PF0.867 回撤-50.3% 总收益-34.6%  ← 最差
    #   平均+均衡           39.53%     0.898     -47.1%      -32.0%
    #   取最大+自适应       41.75%     0.887     -45.0%      -36.3%
    #   取最大+均衡         42.20%     0.923     -40.1%      -27.3%
    #   纯抄底              44.35%     1.066     -27.2%      +29.2%  ← 唯一赚钱
    # SHORT_ENGINE 声明的本来就是 "pure_bottom"，但历史上这条分支被自适应权重
    # 悄悄接管，线上实际跑的恰是五组里最差的一组。故权重固定 PURE_BOTTOM_WEIGHTS，
    # 与 daily_price.fusion_score 同口径（纯抄底下 weighted_avg 与 max 数学等价）。
    # 自适应只保留「阈值」这一项（高波动 15→18，仍是有效的收紧手段）。
    from config.strategy_params import PURE_BOTTOM_WEIGHTS, FUSION_MODE
    _fusion_mode = FUSION_MODE
    _active_weights = PURE_BOTTOM_WEIGHTS
    SIG_THRESHOLD, _market_state = _resolve_sig_threshold()
    if _market_state is not None:
        print(f"  [市场状态] {_market_state['detail']}")
        print(f"  [短线打分] 权重: 纯抄底 {_active_weights}，"
              f"阈值: {SIG_THRESHOLD}，融合方式: {_fusion_mode}")

    if target == "watchlist":
        if count_watchlist() == 0:
            print("[WARN] 自选列表为空，recalc_all_scores(target='watchlist') 跳过")
            return {"success": 0, "failed": 0, "days": 0, "signals": 0, "elapsed_s": 0,
                    "target": "watchlist", "skipped": "empty_watchlist"}
        stocks_df = get_watchlist_with_names()[["code", "name"]]
    else:
        stocks_df = get_all_stocks()

    if stocks_df.empty:
        return {"success": 0, "failed": 0, "days": 0, "signals": 0, "elapsed_s": 0, "target": target}

    stocks = list(zip(stocks_df["code"], stocks_df["name"]))
    success = 0
    failed = 0
    total_days = 0
    sig_records = []
    start = time.time()
    total = len(stocks)

    # 质量过滤所需的总股本映射（缺失的股票自动跳过市值项）
    from core.db import get_market_cap_map
    try:
        _mktcap_map = get_market_cap_map()
    except Exception:
        _mktcap_map = {}

    # ── 隔日动量（龙虎榜净买占比）：一次性预加载最新龙虎榜日期的映射 ──
    _lhb_map = {}
    _lhb_date = None
    if NEXT_DAY_MOMENTUM.get("enabled"):
        try:
            _lhb_date = get_latest_lhb_date()
            if _lhb_date:
                _lhb_map = get_lhb_map_for_date(_lhb_date)
                print(f"  [隔日动量] 预加载龙虎榜 {_lhb_date}：{len(_lhb_map)} 只")
        except Exception as e:
            print(f"  [WARN] 隔日动量龙虎榜预加载失败: {e}")
            _lhb_map = {}

    for i, (code, name) in enumerate(stocks):
        if (i + 1) % 200 == 0:
            print(f"  补算进度: {i+1}/{total}  成功:{success}  失败:{failed}")
        if progress_callback and (i + 1) % 50 == 0:
            progress_callback(int((i + 1) / total * 100),
                              f"正在重算 {i+1}/{total}  成功:{success} 失败:{failed}")

        ok = sync_strategy_score(code, verbose=False)
        if ok:
            success += 1
        else:
            failed += 1
            continue

        try:
            df = get_daily_price(code)
            if df is None or len(df) < 30:
                continue
            _ts = (_mktcap_map.get(code) or {}).get("total_shares")
            # 信号生成与写库口径完全复用增量路径的 _build_signal_records
            # （全量：短线逐历史日 + 中/长线/动量只评最新日）
            _recs = _build_signal_records(
                df, code, name, _ts, SIG_THRESHOLD,
                STOP_LOSS_SC, TAKE_PROFIT_SC,
                lhb_row=_lhb_map.get(code))
            sig_records.extend(_recs)
            total_days += len(df)
        except Exception as e:
            print(f"  [{code}] stock_signal 写入失败: {e}")

    # Batch insert with single transaction
    # 先清掉本轮重算范围内的短线融合旧信号再写入：闸门/阈值收紧后不再命中的
    # 历史信号必须删除，INSERT OR REPLACE 只覆盖同键行，否则旧推荐（如已跌破
    # MA20 的接飞刀票）会一直残留在今日推荐里
    _recalc_codes = [c for c, _ in stocks]
    with get_conn() as conn:
        conn.execute("BEGIN TRANSACTION")
        conn.executemany(
            "DELETE FROM stock_signal WHERE code=? AND horizon='short' "
            "AND strategy IN ('短线融合', '超跌反弹v3')",
            [(c,) for c in _recalc_codes])
        if sig_records:
            conn.executemany(_SIGNAL_INSERT_SQL, sig_records)
        conn.commit()
    print(f"  stock_signal 写入完成: {len(sig_records)} 条记录（已清理重算范围内旧短线信号）")

    elapsed = time.time() - start
    print(f"历史评分补算完成: {success} 只成功 / {failed} 只失败，{len(sig_records)} 条推荐写入 stock_signal，耗时 {elapsed:.0f}秒")

    if progress_callback:
        progress_callback(100, "重算完成: " + str(len(sig_records)) + " 条信号")

    # ── 推荐闭环：常驻规则扫描 + outcome 追踪 + 策略优化器（best-effort）──
    _hooks = _run_post_recalc_hooks()
    rule_hits = _hooks["rule_hits"]

    return {
        "success": success,
        "failed": failed,
        "days": total_days,
        "signals": len(sig_records),
        "rule_hits": rule_hits,
        "elapsed_s": round(elapsed, 1),
        "target": target,
    }


# ─────────────────────────────────────────────
# 按日批量同步（东财 push2his，一次 HTTP 拉全 A，秒级完成）
# ─────────────────────────────────────────────
def daily_sync_by_date(trade_dates: list[str] | None = None,
                        verbose: bool = True,
                        progress_callback=None,
                        target: str = "all") -> dict:
    """
    盘后/盘前推荐主入口：按交易日批量同步，一次 HTTP 拉全 A

    :param trade_dates: 交易日期列表 ['20260624', '20260625']，None 则取最近 1 个交易日
    :param verbose: 打印详细日志
    :param progress_callback: 进度回调 fn(stage, current, total)
    :param target: "all"（全市场，默认）/ "watchlist"（仅自选股）
                  拉取仍是全 A（东财接口粒度），仅在写入层按 target 过滤；
                  策略分重算范围也按 target 过滤。
    :return: {"dates": [...], "rows": N, "elapsed_s": x.x, "target": ...}
    """
    target = target if target in ("all", "watchlist") else "all"

    init_db()
    _init_qlib()
    from core.em_realtime import fetch_klines_by_date

    # 自选空校验（target=watchlist 才有意义）
    if target == "watchlist" and count_watchlist() == 0:
        if verbose:
            print("[WARN] 自选列表为空，daily_sync_by_date(target='watchlist') 跳过")
        return {"dates": [], "rows": 0, "elapsed_s": 0.0,
                "skipped": "empty_watchlist", "target": "watchlist"}

    if trade_dates is None:
        # 默认拉最近 1 个交易日
        end = date.today()
        for _ in range(7):
            if is_trading_day(end.strftime('%Y-%m-%d')):
                break
            end -= timedelta(days=1)
        trade_dates = [end.strftime('%Y%m%d')]

    t0 = time.time()
    total_rows = 0
    results = {}

    # 提前计算 watchlist codes 集合（O(1) 查找）
    wl_set = None
    if target == "watchlist":
        wl_set = set(get_watchlist_codes())

    for trade_date in trade_dates:
        if verbose:
            print(f"\n>>> 批量同步 {trade_date}（东财 push2his，一次 HTTP，target={target}）")

        try:
            df = fetch_klines_by_date(
                trade_date,
                progress_cb=lambda f, t: (
                    print(f"\r  拉取 {f}/{t}", end="", flush=True) if verbose else None
                ),
            )
        except Exception as e:
            if verbose:
                print(f"  [ERROR] 东财接口失败: {e}")
            # ── 回退：东财/akshare 同源兜底不可靠，整体降级 baostock 全市场同步 ──
            # daily_sync 自带 baostock → akshare → 腾讯 三级兜底 + 断点续传缓存，
            # 单线程逐股拉取，预计 5~15 分钟（东财正常时仍走秒级直连，不受影响）。
            # recalc=False：行情写库后由本函数统一的增量打分收尾负责，避免触发
            # daily_sync 内部的全量 recalc_all_scores（约 5 分钟，重复计算）。
            if verbose:
                print("  [FALLBACK] 东财/akshare 不可用，回退 baostock 全市场同步"
                      "（逐股拉取，预计 5~15 分钟）...")
            try:
                fb = daily_sync(verbose=verbose, target=target, recalc=False)
                fb_rows = int(fb.get("success", 0) or 0)
                results[trade_date] = {
                    "rows": fb_rows, "source": "baostock_fallback",
                    "detail": {k: fb.get(k) for k in ("total", "success", "failed")},
                }
                total_rows += fb_rows
                if progress_callback:
                    progress_callback("date_done", trade_dates.index(trade_date) + 1,
                                      len(trade_dates))
                continue
            except Exception as fb_e:
                if verbose:
                    print(f"  [ERROR] baostock 回退也失败: {fb_e}")
                results[trade_date] = {"rows": 0, "error": f"em: {e}; baostock: {fb_e}"}
                continue

        if df.empty:
            if verbose:
                print(f"  {trade_date} 无数据（可能非交易日）")
            results[trade_date] = {"rows": 0}
            continue

        # 写入层过滤：watchlist 模式只保留自选股
        if wl_set is not None and "code" in df.columns:
            before = len(df)
            df = df[df["code"].astype(str).isin(wl_set)].copy()
            if verbose and before != len(df):
                print(f"  watchlist 过滤: {before} -> {len(df)} 行")

        # 写数据库
        try:
            n = _batch_write_daily_price(df)
            total_rows += n
            if verbose:
                print(f"\n  {trade_date} 写入 {n} 行（东财）")
            results[trade_date] = {"rows": n}
        except Exception as e:
            if verbose:
                print(f"  [ERROR] 写库失败: {e}")
            results[trade_date] = {"rows": 0, "error": str(e)}

        if progress_callback:
            progress_callback("date_done", trade_dates.index(trade_date) + 1, len(trade_dates))

    # 触发策略分数重算（按 target 过滤）
    if total_rows > 0:
        try:
            _recompute_strategy_scores_for_updated(
                trade_dates, verbose=verbose, target=target
            )
        except Exception as e:
            if verbose:
                print(f"  [WARN] 策略分数重算失败: {e}")

        # 增量重算 stock_signal：只重写最新交易日信号，历史日保留
        # （替代分钟级全量 recalc_all_scores；数据回填/修正才需要全量）
        try:
            recalc_incremental_signals(trade_dates, verbose=verbose, target=target)
        except Exception as e:
            if verbose:
                print(f"  [WARN] 增量信号重算失败: {e}")

        # 推荐闭环：常驻规则扫描 + outcome 追踪 + 策略优化器（best-effort）
        try:
            _run_post_recalc_hooks()
        except Exception as e:
            if verbose:
                print(f"  [WARN] 推荐闭环钩子失败: {e}")

        # 刷新 latest_price 物化表（消除查询时的 N+1 子查询）
        try:
            from core.repository.price_repo import refresh_latest_price
            if wl_set:
                refresh_latest_price(list(wl_set))
            else:
                refresh_latest_price()
        except Exception as e:
            if verbose:
                print(f"  [WARN] latest_price 刷新失败: {e}")

    elapsed = time.time() - t0
    if verbose:
        print(f"\n>>> 批量同步完成  耗时: {elapsed:.1f}s  共 {total_rows} 行  target={target}")

    return {"dates": trade_dates, "rows": total_rows, "elapsed_s": round(elapsed, 1),
            "detail": results, "target": target}


def _batch_write_daily_price(df: pd.DataFrame) -> int:
    """
    把东财返回的全市场日线 DataFrame 写入 daily_price 表
    期望 df.columns: code, trade_date, open, close, high, low, volume, amount, pct_change
    """
    if df.empty:
        return 0

    from core.data_cleaner import validate_record

    records = []
    for _, row in df.iterrows():
        close = float(row.get("close", 0) or 0)
        o = float(row.get("open", 0) or 0)
        h = float(row.get("high", 0) or 0)
        l = float(row.get("low", 0) or 0)
        # 东财 push2 的 volume 单位是"手"(1手=100股)，统一转成股
        vol = float(row.get("volume", 0) or 0) * 100

        # 行级校验：价格上限 + OHLC关系 + 负成交量（拦截字段错位等脏值）
        if close > 3000:
            continue
        if not validate_record(o, h, l, close, vol):
            continue

        records.append({
            "code":       str(row["code"]),
            "trade_date": str(row["trade_date"]),
            "open":       o,
            "high":       h,
            "low":        l,
            "close":      close,
            "volume":     vol,
            "amount":     float(row.get("amount", 0) or 0),
            "pct_change": float(row.get("pct_change", 0) or 0),
            "turnover":   float(row.get("turnover", 0) or 0),
        })

    if not records:
        return 0

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

    # 同步到 Qlib
    try:
        from qlib_engine.data_bridge import batch_append_daily
        qlib_records = [{
            "code": r["code"],
            "trade_date": r["trade_date"],
            "open": r["open"], "high": r["high"], "low": r["low"],
            "close": r["close"], "volume": r["volume"], "amount": r["amount"],
            "pct_change": r["pct_change"], "turnover": r["turnover"],
        } for r in records]
        batch_append_daily(qlib_records)
    except Exception:
        pass  # Qlib 失败不影响主流程

    return len(records)


def _recompute_strategy_scores_for_updated(trade_dates: list[str], verbose: bool = True,
                                          target: str = "all") -> None:
    """对受影响股票重新计算策略分数（仅重算最近有更新的，不全量重算）

    target="watchlist" 时，仅对受影响 ∩ 自选股子集重算。
    """
    target = target if target in ("all", "watchlist") else "all"

    try:
        from qlib_engine.data_bridge import init_qlib as _qinit
        _qinit()
    except Exception:
        pass

    # 取所有需要重算的 code
    # trade_dates 可能是 '%Y%m%d'（daily_sync_by_date 默认构造）或 '%Y-%m-%d'，
    # 表内 trade_date 为 '%Y-%m-%d'——不归一会导致 IN 查询 0 行、策略分重算被静默跳过
    # （2026-08-10 修复：此前 08-10 写入 4496 行后 short 信号全灭即此根因）。
    tds = []
    for _t in trade_dates:
        _t = str(_t)
        if len(_t) >= 8 and _t[:8].isdigit():
            tds.append(f"{_t[:4]}-{_t[4:6]}-{_t[6:8]}")
        else:
            tds.append(_t[:10])
    with get_conn() as conn:
        placeholders = ",".join("?" * len(tds))
        rows = conn.execute(
            f"SELECT DISTINCT code FROM daily_price WHERE trade_date IN ({placeholders})",
            tds,
        ).fetchall()
    codes = [r["code"] for r in rows]
    if not codes:
        return

    # 自选视角：缩小到自选股子集
    if target == "watchlist":
        wl_set = set(get_watchlist_codes())
        codes = [c for c in codes if c in wl_set]
        if not codes:
            return

    if verbose:
        print(f"  重算策略分数: {len(codes)} 只 (target={target})")

    # 复用原有的逐只打分逻辑（已经是串行，但只重算受影响股票）
    for i, code in enumerate(codes):
        try:
            sync_strategy_score(code, verbose=False)
        except Exception:
            pass
        if verbose and (i + 1) % 100 == 0:
            print(f"    重算进度: {i+1}/{len(codes)}")


# ─────────────────────────────────────────────
# 推荐池并行拉取（盘前/盘后给推荐系统用）
# ─────────────────────────────────────────────
def fetch_recommend_pool_parallel(codes: list[str],
                                   start_date: str = None,
                                   end_date: str = None,
                                   max_workers: int = 8,
                                   progress_callback=None,
                                   use_em_cache: bool = True) -> dict:
    """
    并行拉取推荐池的 K 线（盘前/盘后推荐用）

    :param codes: 推荐股票代码列表（一般 50-200 只）
    :param max_workers: 并发数（东财接口建议 4-8）
    :return: {"success": [...], "failed": [...], "elapsed_s": x.x}
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from core.em_kline import get_stock_history

    if end_date is None:
        end_date = date.today().strftime("%Y%m%d")
    if start_date is None:
        start_date = (date.today() - timedelta(days=180)).strftime("%Y%m%d")

    success: list[str] = []
    failed: list[tuple[str, str]] = []
    t0 = time.time()
    total = len(codes)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {
            pool.submit(get_stock_history, c, start_date, end_date, "qfq", use_em_cache): c
            for c in codes
        }
        for i, fut in enumerate(as_completed(future_map)):
            code = future_map[fut]
            try:
                df = fut.result(timeout=20)
                if df is not None and not df.empty:
                    success.append(code)
                else:
                    failed.append((code, "empty"))
            except Exception as e:
                failed.append((code, str(e)[:60]))

            if progress_callback:
                progress_callback(i + 1, total, len(success), len(failed))

    elapsed = time.time() - t0
    return {
        "success": success,
        "failed": failed,
        "elapsed_s": round(elapsed, 1),
        "total": total,
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
    elif cmd == "daily_fast":
        # 按日批量同步（东财直连，1 次 HTTP 拉全 A；自动附带最新交易日信号增量重算）
        daily_sync_by_date(verbose=True)
    elif cmd == "full":
        # 全量重算历史打分 + stock_signal（数据回填/修正后使用，约 5 分钟）
        recalc_all_scores()
    elif cmd == "pool_test":
        # 推荐池并行拉取测试
        from core.db import get_all_stocks
        df = get_all_stocks()
        codes = df["code"].head(50).tolist() if not df.empty else ["000001", "600519", "300750"]
        result = fetch_recommend_pool_parallel(codes, max_workers=8, verbose=True)
        print(f"  成功: {len(result['success'])}  失败: {len(result['failed'])}  耗时: {result['elapsed_s']}s")
    elif cmd == "em_test":
        # 东财 clist 自检
        from core.em_realtime import fetch_realtime_all
        df = fetch_realtime_all()
        print(f"  全 A: {len(df)} 只")
        print(df.head(5).to_string(index=False))
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
        print("可用: list / init / init_test / daily / daily_fast / full / pool_test / em_test / stat / test")
