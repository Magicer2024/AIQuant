"""
A股数据获取模块
======================
主数据源：baostock（免费、稳定、无频率限制）
备用数据源：akshare（多接口降级）

baostock 股票代码格式：
  sh.600519  →  上交所：sh. 前缀
  sz.000001  →  深交所：sz. 前缀
  bj.830000  →  北交所：bj. 前缀
"""
import baostock as bs
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import os
import json
import threading
import requests
import time

DATA_CACHE_DIR = "data_cache"
os.makedirs(DATA_CACHE_DIR, exist_ok=True)

# ─────────────────────────────────────────────
# baostock 连接管理（全局单例，线程安全）
# ─────────────────────────────────────────────
_bs_lock = threading.Lock()
_bs_logged_in = False

def _bs_login():
    """确保 baostock 已登录"""
    global _bs_logged_in
    with _bs_lock:
        if not _bs_logged_in:
            lg = bs.login()
            if lg.error_code == '0':
                _bs_logged_in = True
            else:
                raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")

def _bs_ensure_login():
    """调用前确保已登录"""
    _bs_login()


def _bs_relogin():
    """强制重新登录（socket 断开后恢复）"""
    global _bs_logged_in
    with _bs_lock:
        try:
            bs.logout()
        except Exception:
            pass
        _bs_logged_in = False
    _bs_login()


def _is_socket_error(exc: Exception) -> bool:
    """检测是否为 socket 断开错误（含 baostock 中文错误信息）"""
    msg = str(exc).lower()
    return any(kw in msg for kw in (
        '10038', '10053', '10054', '10060', '10061',
        '非套接字', 'socket', 'connection', 'timeout',
        'reset', 'aborted', 'refused',
        '网络接收错误', '接收数据异常', 'network',
    ))

# ─────────────────────────────────────────────
# 代码格式转换
# ─────────────────────────────────────────────

def _to_bs_code(symbol: str) -> str:
    """
    将纯数字代码转换为 baostock 格式
    000001 → sz.000001
    600519 → sh.600519
    830000 → bj.830000
    """
    if '.' in symbol:
        return symbol  # 已经是 sh.xxx 格式
    code = symbol.strip()
    if code.startswith(('600', '601', '603', '605', '688', '900')):
        return f'sh.{code}'
    elif code.startswith(('000', '001', '002', '003', '200', '300', '301')):
        return f'sz.{code}'
    elif code.startswith(('4', '8', '83', '87', '430', '830', '870', '871', '872', '873', '874', '875', '876', '877', '878', '879')):
        return f'bj.{code}'
    else:
        # 默认 sz
        return f'sz.{code}'

def _from_bs_code(bs_code: str) -> str:
    """sh.600519 → 600519"""
    return bs_code.split('.')[-1] if '.' in bs_code else bs_code


# ─────────────────────────────────────────────
# 核心：获取历史行情（baostock 主数据源）
# ─────────────────────────────────────────────

def _fetch_baostock(symbol: str, start_date: str, end_date: str,
                    adjust: str = "qfq") -> pd.DataFrame:
    """
    用 baostock 拉取日线行情
    adjust: qfq=前复权(adjustflag=2), hfq=后复权(adjustflag=1), none=不复权(adjustflag=3)
    """
    for attempt in range(2):
        try:
            _bs_ensure_login()

            adjustflag_map = {"qfq": "2", "hfq": "1", "": "3", "none": "3"}
            adjustflag = adjustflag_map.get(adjust, "2")

            bs_code = _to_bs_code(symbol)
            # baostock 日期格式 YYYY-MM-DD
            start = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}" if len(start_date) == 8 else start_date
            end   = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"       if len(end_date) == 8   else end_date

            fields = "date,open,high,low,close,volume,amount,turn,pctChg"
            rs = bs.query_history_k_data_plus(
                bs_code, fields,
                start_date=start, end_date=end,
                frequency='d', adjustflag=adjustflag
            )
            if rs.error_code != '0':
                raise RuntimeError(f"baostock query failed: {rs.error_msg}")

            data = []
            while rs.next():
                data.append(rs.get_row_data())

            if not data:
                return pd.DataFrame()

            df = pd.DataFrame(data, columns=rs.fields)

            # 数据类型转换
            numeric_cols = ['open', 'high', 'low', 'close', 'volume', 'amount', 'turn', 'pctChg']
            for col in numeric_cols:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')

            df['date'] = pd.to_datetime(df['date'])
            df.set_index('date', inplace=True)
            df.sort_index(inplace=True)

            # 统一列名
            df.rename(columns={
                'turn':    'turnover',
                'pctChg':  'pct_change',
            }, inplace=True)

            # 补充 amount 为 0 时的处理
            df.dropna(subset=['close'], inplace=True)
            df = df[df['close'] > 0]

            return df
        except Exception as e:
            if attempt == 0 and _is_socket_error(e):
                print(f"  [baostock] socket 断开 ({e})，重连中...")
                _bs_relogin()
                continue
            raise


def _fetch_akshare_fallback(symbol: str, start_date: str, end_date: str,
                             adjust: str = "qfq") -> pd.DataFrame:
    """akshare 备用接口（东方财富可能限速，自动降级新浪/腾讯）"""
    import akshare as ak

    for fetch_fn, name in [
        (lambda: ak.stock_zh_a_hist(
            symbol=symbol, period="daily",
            start_date=start_date, end_date=end_date, adjust=adjust
        ), "东方财富"),
        (lambda: ak.stock_zh_a_daily(symbol=symbol, start_date=start_date, end_date=end_date), "新浪"),
    ]:
        try:
            df = fetch_fn()
            if df is None or df.empty:
                continue
            # 标准化列名
            df.rename(columns={
                "日期":"date","开盘":"open","收盘":"close","最高":"high","最低":"low",
                "成交量":"volume","成交额":"amount","涨跌幅":"pct_change","换手率":"turnover",
                "trade_date":"date",
            }, inplace=True)
            if 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date'])
                df.set_index('date', inplace=True)
            df.sort_index(inplace=True)
            df.dropna(subset=['close'], inplace=True)
            return df
        except Exception:
            continue
    return pd.DataFrame()


# ─────────────────────────────────────────────
# 公开接口
# ─────────────────────────────────────────────

def get_stock_history(symbol: str, start_date: str = None, end_date: str = None,
                      adjust: str = "qfq") -> pd.DataFrame:
    """
    获取A股历史行情（前复权）
    优先 baostock，失败自动降级 akshare

    :param symbol: 纯代码 "000001" 或带前缀 "sz.000001"
    :param start_date: "20230101" 或 "2023-01-01"
    :param end_date:   同上，默认今天
    :param adjust: qfq / hfq / none
    """
    if end_date is None:
        end_date = datetime.today().strftime("%Y%m%d")
    if start_date is None:
        start_date = (datetime.today() - timedelta(days=365 * 2)).strftime("%Y%m%d")

    # 统一为 YYYYMMDD
    start_date = start_date.replace('-', '')
    end_date   = end_date.replace('-', '')
    pure_code  = _from_bs_code(symbol)

    cache_file = os.path.join(DATA_CACHE_DIR, f"{pure_code}_{adjust}_{start_date}_{end_date}.csv")
    if os.path.exists(cache_file):
        try:
            df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
            df.index = pd.to_datetime(df.index)
            if len(df) > 5:
                return df
        except Exception:
            pass

    # 1. 主数据源：baostock
    try:
        df = _fetch_baostock(pure_code, start_date, end_date, adjust)
        if not df.empty:
            df.to_csv(cache_file)
            return df
    except Exception as e:
        print(f"  [baostock] {pure_code} 失败: {e}，尝试 akshare...")

    # 2. 备用数据源：akshare
    try:
        df = _fetch_akshare_fallback(pure_code, start_date, end_date, adjust)
        if not df.empty:
            df.to_csv(cache_file)
            return df
    except Exception as e:
        pass

    raise RuntimeError(f"所有数据源均无法获取 {symbol} 数据，请检查网络")


def get_stock_info(symbol: str) -> dict:
    """
    获取股票基本信息（名称、行业等）
    baostock 主，akshare 备
    """
    pure_code = _from_bs_code(symbol)
    bs_code   = _to_bs_code(pure_code)

    try:
        _bs_ensure_login()
        rs = bs.query_stock_basic(code=bs_code)
        if rs.error_code == '0' and rs.next():
            row = rs.get_row_data()
            fields = rs.fields
            info = dict(zip(fields, row))
            return {
                "code":     pure_code,
                "name":     info.get("code_name", ""),
                "ipo_date": info.get("ipoDate", ""),
                "status":   info.get("status", ""),
                "type":     info.get("type", ""),
            }
    except Exception:
        pass

    # akshare 备用
    try:
        import akshare as ak
        df = ak.stock_individual_info_em(symbol=pure_code)
        info = dict(zip(df.iloc[:, 0], df.iloc[:, 1]))
        return info
    except Exception:
        return {"code": pure_code, "name": "未知"}


def get_realtime_price(symbol: str) -> dict:
    """
    获取实时价格（baostock 最新收盘价）
    """
    try:
        end = datetime.today().strftime("%Y%m%d")
        start = (datetime.today() - timedelta(days=5)).strftime("%Y%m%d")
        df = _fetch_baostock(symbol, start, end, "qfq")
        if not df.empty:
            last = df.iloc[-1]
            return {
                "price":      float(last["close"]),
                "pct_change": float(last.get("pct_change", 0)),
                "volume":     float(last.get("volume", 0)),
                "date":       str(df.index[-1].date()),
            }
    except Exception:
        pass
    return {"price": 0, "pct_change": 0, "volume": 0}


def get_stock_list_bs() -> pd.DataFrame:
    """
    用 baostock 获取全市场股票列表
    返回 DataFrame: code, name, market, type, status
    """
    _bs_ensure_login()
    rs = bs.query_stock_basic(code='', code_name='')
    data = []
    while rs.error_code == '0' and rs.next():
        data.append(rs.get_row_data())
    df = pd.DataFrame(data, columns=rs.fields)

    # 只保留普通A股（type=1 股票，status=1 上市）
    df = df[(df['type'] == '1') & (df['status'] == '1')].copy()
    df['market'] = df['code'].apply(lambda x: x.split('.')[0].upper())
    df['pure_code'] = df['code'].apply(_from_bs_code)
    df.rename(columns={'code_name': 'name', 'pure_code': 'code_6'}, inplace=True)
    return df


def search_stocks(keyword: str) -> list:
    """搜索股票"""
    try:
        _bs_ensure_login()
        # 用 baostock 搜索名称
        rs = bs.query_stock_basic(code='', code_name=keyword)
        data = []
        while rs.error_code == '0' and rs.next():
            row = rs.get_row_data()
            fields = rs.fields
            r = dict(zip(fields, row))
            data.append({
                "代码": _from_bs_code(r.get("code", "")),
                "名称": r.get("code_name", ""),
                "最新价": 0,
                "涨跌幅": 0,
            })
        if data:
            return data[:20]
    except Exception:
        pass

    # akshare 备用
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        mask = df["代码"].str.contains(keyword, na=False) | df["名称"].str.contains(keyword, na=False)
        result = df[mask][["代码", "名称", "最新价", "涨跌幅"]].head(20)
        return result.to_dict("records")
    except Exception:
        return []


def get_hot_stocks(n: int = 20) -> list:
    """
    获取热门/主要股票
    用沪深300成分股，失败返回蓝筹备用列表
    """
    try:
        import akshare as ak
        df = ak.index_stock_cons_weight_csindex(symbol="000300")
        result = df[["成分券代码", "成分券名称", "权重"]].head(n).rename(
            columns={"成分券代码": "code", "成分券名称": "name", "权重": "weight"}
        ).to_dict("records")
        # 补充当前价格
        for item in result:
            try:
                p = get_realtime_price(item["code"])
                item["price"]      = p.get("price", 0)
                item["pct_change"] = p.get("pct_change", 0)
            except Exception:
                item["price"]      = 0
                item["pct_change"] = 0
        return result
    except Exception:
        pass

    # 备用静态列表
    return [
        {"code": "600519", "name": "贵州茅台",  "weight": 5.0, "price": 0, "pct_change": 0},
        {"code": "000001", "name": "平安银行",  "weight": 2.0, "price": 0, "pct_change": 0},
        {"code": "601318", "name": "中国平安",  "weight": 3.0, "price": 0, "pct_change": 0},
        {"code": "600036", "name": "招商银行",  "weight": 2.5, "price": 0, "pct_change": 0},
        {"code": "000858", "name": "五粮液",    "weight": 2.0, "price": 0, "pct_change": 0},
        {"code": "300750", "name": "宁德时代",  "weight": 4.0, "price": 0, "pct_change": 0},
        {"code": "601166", "name": "兴业银行",  "weight": 1.5, "price": 0, "pct_change": 0},
        {"code": "600900", "name": "长江电力",  "weight": 1.8, "price": 0, "pct_change": 0},
    ]


def get_market_index(index_code: str = "sh000001", start_date: str = None,
                     end_date: str = None) -> pd.DataFrame:
    """
    获取大盘指数数据
    index_code: sh000001 上证指数 / sz399001 深证成指 / sh000300 沪深300
    """
    if end_date is None:
        end_date = datetime.today().strftime("%Y%m%d")
    if start_date is None:
        start_date = (datetime.today() - timedelta(days=365 * 2)).strftime("%Y%m%d")

    # baostock 指数代码格式：sh.000001
    if '.' not in index_code:
        market = index_code[:2]
        code   = index_code[2:]
        bs_idx = f"{market}.{code}"
    else:
        bs_idx = index_code

    try:
        _bs_ensure_login()
        start = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
        end   = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
        rs = bs.query_history_k_data_plus(
            bs_idx, "date,open,high,low,close,volume,amount,pctChg",
            start_date=start, end_date=end, frequency='d', adjustflag='3'
        )
        data = []
        while rs.error_code == '0' and rs.next():
            data.append(rs.get_row_data())
        if data:
            df = pd.DataFrame(data, columns=rs.fields)
            for c in ['open','high','low','close','volume','amount','pctChg']:
                df[c] = pd.to_numeric(df[c], errors='coerce')
            df['date'] = pd.to_datetime(df['date'])
            df.set_index('date', inplace=True)
            df.rename(columns={'pctChg': 'pct_change'}, inplace=True)
            return df.sort_index()
    except Exception as e:
        pass

    # akshare 备用
    try:
        import akshare as ak
        df = ak.index_zh_a_hist(symbol=index_code[2:] if '.' not in index_code else index_code.replace('.', ''),
                                 period="daily", start_date=start_date, end_date=end_date)
        df.rename(columns={"日期":"date","开盘":"open","收盘":"close","最高":"high",
                            "最低":"low","成交量":"volume","成交额":"amount","涨跌幅":"pct_change"}, inplace=True)
        df['date'] = pd.to_datetime(df['date'])
        df.set_index('date', inplace=True)
        return df.sort_index()
    except Exception as e:
        raise RuntimeError(f"获取指数 {index_code} 失败: {e}")


# ─────────────────────────────────────────────
# 个股资金流向（akshare 主数据源）
# ─────────────────────────────────────────────

def _to_akshare_market(symbol: str) -> tuple[str, str]:
    """
    将纯数字代码转换为 akshare 的 (pure_code, market)
    market: sh=上海, sz=深圳, bj=北京
    """
    pure_code = _from_bs_code(symbol)
    if pure_code.startswith(("600", "601", "603", "605", "688", "900")):
        return pure_code, "sh"
    elif pure_code.startswith(("000", "001", "002", "003", "200", "300", "301")):
        return pure_code, "sz"
    elif pure_code.startswith(("4", "8", "83", "87", "430", "830", "870", "871", "872", "873", "874", "875", "876", "877", "878", "879")):
        return pure_code, "bj"
    else:
        return pure_code, "sz"  # 默认深圳


_em_session = requests.Session()
_em_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://data.eastmoney.com/",
})

_em_market_map = {"sh": "1", "sz": "0", "bj": "0"}



# ─────────────────────────────────────────────
# 市值数据获取（baostock 换手率反推流通股本）
# ─────────────────────────────────────────────

def _to_bs_code(code: str) -> str:
    """将纯数字代码 000001 转为 baostock 格式 sz.000001 / sh.600519"""
    if code.startswith("sh.") or code.startswith("sz.") or code.startswith("bj."):
        return code
    if code.startswith("6"):
        return f"sh.{code}"
    if code.startswith("8") or code.startswith("4"):
        return f"bj.{code}"
    return f"sz.{code}"


def _calc_circ_shares_from_turn(bs_code: str, ref_date: str = None) -> float | None:
    """
    用 baostock 日线换手率反推流通股本：
        流通股本(股) = 成交量(股) / (换手率(%) / 100)
    取最近 5 个交易日均值，提高稳健性。
    ref_date: 'YYYY-MM-DD'，默认取今天
    """
    import datetime
    if ref_date is None:
        ref_date = datetime.date.today().strftime("%Y-%m-%d")
    # 往前取 10 个自然日，大概率覆盖 5 个交易日
    start = (datetime.datetime.strptime(ref_date, "%Y-%m-%d") - datetime.timedelta(days=20)).strftime("%Y-%m-%d")
    try:
        rs = bs.query_history_k_data_plus(
            code=bs_code,
            fields="date,volume,turn,tradestatus",
            start_date=start,
            end_date=ref_date,
            frequency="d",
            adjustflag="3",
        )
        data = rs.get_data()
        if data is None or data.empty:
            return None
        # 只保留正常交易日（tradestatus=1）且换手率 > 0
        data = data[data["tradestatus"] == "1"].copy()
        data["volume"] = pd.to_numeric(data["volume"], errors="coerce")
        data["turn"]   = pd.to_numeric(data["turn"],   errors="coerce")
        data = data[(data["turn"] > 0) & (data["volume"] > 0)].tail(5)
        if data.empty:
            return None
        # 逐日计算流通股本，取中位数
        data["circ"] = data["volume"] / (data["turn"] / 100.0)
        return float(data["circ"].median())
    except Exception:
        return None


def fetch_market_cap(code: str) -> dict | None:
    """
    获取单只股票的流通股本（股）
    使用 baostock 换手率反推，无频率限制
    返回: {"code": "000001", "total_shares": float, "circ_shares": float} 或 None
    """
    pure_code = _from_bs_code(code)
    bs_code   = _to_bs_code(pure_code)
    try:
        bs.login()
        circ = _calc_circ_shares_from_turn(bs_code)
        bs.logout()
        if circ and circ > 0:
            return {
                "code":         pure_code,
                "total_shares": circ,
                "circ_shares":  circ,
            }
    except Exception as e:
        print(f"  [market_cap] {pure_code} 失败: {e}")
    return None


def fetch_all_market_cap(codes: list[str] = None, progress_cb=None) -> list[dict]:
    """
    批量获取市值数据（baostock 换手率反推，无频率限制，全市场约1~3分钟）
    codes: 股票代码列表，None 则获取全市场
    progress_cb: 进度回调 fn(code, i, total)
    返回: [{"code": ..., "total_shares": ..., "circ_shares": ...}, ...]
    """
    if codes is None:
        from core.db import get_all_stocks
        df = get_all_stocks(active_only=True)
        codes = df["code"].tolist()

    results = []
    total   = len(codes)

    import datetime
    ref_date = datetime.date.today().strftime("%Y-%m-%d")

    # baostock 只登录一次，全量查询
    try:
        bs.login()
        for i, code in enumerate(codes):
            if progress_cb:
                progress_cb(code, i, total)
            pure_code = _from_bs_code(code)
            bs_code   = _to_bs_code(pure_code)
            circ = _calc_circ_shares_from_turn(bs_code, ref_date=ref_date)
            if circ and circ > 0:
                results.append({
                    "code":         pure_code,
                    "total_shares": circ,
                    "circ_shares":  circ,
                })
            # 无需 sleep，baostock 无频率限制
        bs.logout()
    except Exception as e:
        print(f"  [market_cap] baostock 批量查询异常: {e}")
        try:
            bs.logout()
        except Exception:
            pass

    return results

    return results
