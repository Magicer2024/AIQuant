"""
core/em_realtime.py —— 东方财富全市场行情直连
============================================
盘前/盘后推荐场景专用：
  - fetch_realtime_all()    全 A 最新行情，降级链：东财 clist → 腾讯批量(500只/请求) → akshare
  - fetch_klines_by_date()  按交易日拉全市场日线（盘后批量同步）

特点：
  - 全部经 core.em_guard.cached_fetch（TTL 缓存 + 频控 + 日配额）
  - 东财存活时单次 5000 只 < 3s；被反爬断连时腾讯批量 ≈10 请求拉全 A
  - 内置 retry + 连接池
"""
from __future__ import annotations

import time
import json
import logging
from typing import Callable, Iterable

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# HTTP Session（连接池 + 自动重试）
# ─────────────────────────────────────────────
def _build_session() -> requests.Session:
    """构造带连接池和重试的 Session，跨调用复用"""
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://quote.eastmoney.com/",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    retry = Retry(
        total=3, backoff_factor=0.3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=4,
        pool_maxsize=8,
    )
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess


_SESSION: requests.Session = _build_session()

# 东财 ut 字段（动态 token，使用一个稳定值即可）
_UT = "fa5fd1943c7b386f172d6893dbfba10b"

# 全 A 股票列表 fs 参数（沪深京三市）
_FS_ALL = "m:0+t:6,m:0+t:13,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81"

# 单只行情字段：代码、名称、最新价、涨跌额、涨跌幅、今开、昨收、最高、最低、成交量、成交额、换手率、市盈率
_FIELDS_REALTIME = "f12,f14,f2,f3,f4,f5,f6,f7,f15,f16,f17,f8,f18"


# ─────────────────────────────────────────────
# 工具：股票代码 → 东财 secid
# ─────────────────────────────────────────────
def to_em_secid(code: str) -> str:
    """
    纯代码 → 东财 secid
        600519  → 1.600519  (1=沪)
        000001  → 0.000001  (0=深)
        830xxx  → 0.830xxx  (0=北)
    """
    code = code.strip().replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    if code.startswith(("600", "601", "603", "605", "688", "689", "900")):
        return f"1.{code}"
    if code.startswith(("4", "8", "92", "43", "83", "87")):
        return f"0.{code}"
    # 000/001/002/003/300/301 → 深
    return f"0.{code}"


# ─────────────────────────────────────────────
# 核心：一次拉全 A 最新行情
# ─────────────────────────────────────────────
def fetch_realtime_all(page_size: int = 5000,
                       progress_cb: Callable[[int, int], None] | None = None) -> pd.DataFrame:
    """
    一次拉全 A 股最新行情（盘前/盘后推荐主入口，带护栏）

    实现策略：先尝试东财 clist（最快）；失败/被反爬时降级到 akshare
    返回 DataFrame，如果缓存命中则不消耗网络配额
    """
    from core.em_guard import cached_fetch, GUARD_CONFIG, GuardName

    # 默认 TTL 120s（盘前 9:00 / 盘后 16:00 各刷一次就够了）
    cfg = GUARD_CONFIG.get(GuardName.REALTIME_ALL, {})
    ttl = cfg.get("default_ttl", 120)

    # 降级链（东财 clist → 腾讯批量 → akshare）整体在护栏内执行，
    # 一轮无论内部发多少分页请求都只占 1 次 realtime_all 配额
    def _do_fetch():
        return _fetch_realtime_with_fallback(progress_cb)

    params = {"page_size": page_size}
    df, source = cached_fetch(GuardName.REALTIME_ALL, params, _do_fetch, ttl=ttl)

    if source == "fresh":
        logger.info("[fetch_realtime_all] 缓存命中, 0 网络请求")
    elif source == "stale":
        logger.warning("[fetch_realtime_all] 配额超限，使用 stale 缓存（防反爬）")
    elif source == "empty":
        logger.error("[fetch_realtime_all] 无数据可用")
    return df if df is not None else pd.DataFrame()


def _fetch_realtime_with_fallback(progress_cb=None) -> pd.DataFrame:
    """
    实时全 A 快照降级链（护栏内 fetcher，整轮只占 1 次配额）：
      1. 东财 clist 直连（存活时最快，但会周期性被反爬断连）
      2. 腾讯 qt.gtimg.cn 批量（500 只/请求，全 A ≈10 请求；
         2026-10 T6 实测东财主备全断连时唯一可用通道，与 klines 腾讯通道同源）
      3. akshare（底层仍是东财编号子域，UA/子域不同，最后兜底）
    """
    try:
        df = _fetch_realtime_all_em(progress_cb)
        if not df.empty:
            return df
    except Exception as e:
        logger.info("东财实时 clist 失败，降级腾讯批量: %s", e)

    try:
        df = _fetch_realtime_all_tencent(progress_cb)
        if not df.empty:
            return df
    except Exception as e:
        logger.info("腾讯批量实时失败，降级 akshare: %s", e)

    return _fetch_realtime_all_akshare(progress_cb)


def _fetch_realtime_all_em(progress_cb=None) -> pd.DataFrame:
    """东财 push2.clist 直连：拆段拉取（3 段），避免大 fs 触发反爬"""
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    all_rows: list[dict] = []
    page_no = 1
    fs_segments = [
        "m:1+t:2,m:1+t:23",       # 沪主板 + 沪科创
        "m:0+t:6,m:0+t:13,m:0+t:80",  # 深主板+中小+创
        "m:0+t:81",                # 北交所
    ]

    for fs in fs_segments:
        page_no = 1
        while True:
            params = {
                "pn": str(page_no),
                "pz": "500",
                "po": "1",
                "np": "1",
                "ut": _UT,
                "fl": _FIELDS_REALTIME,
                "invt": "2",
                "fid": "f3",
                "fs": fs,
                "fields": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f14,f15,f16,f17,f18",
                "_": str(int(time.time() * 1000)),
            }
            resp = _SESSION.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            payload = (data or {}).get("data") or {}
            diff_list = payload.get("diff") or []
            total = int(payload.get("total") or 0)

            for r in diff_list:
                # 东财 push2 clist 字段含义（已实证验证）：
                # f2=最新价 f3=涨跌幅% f4=涨跌额 f5=成交量(手) f6=成交额(元)
                # f7=振幅% f8=换手率% f9=市盈率 f10=量比 f15=最高价
                # f16=最低价 f17=今开 f18=昨收
                all_rows.append({
                    "code":       r.get("f12", ""),
                    "name":       r.get("f14", ""),
                    "price":      _to_float(r.get("f2")),
                    "change":     _to_float(r.get("f4")),
                    "pct_change": _to_float(r.get("f3")),
                    "open":       _to_float(r.get("f17")),   # 今开
                    "preclose":   _to_float(r.get("f18")),   # 昨收
                    "high":       _to_float(r.get("f15")),   # 最高价
                    "low":        _to_float(r.get("f16")),   # 最低价
                    "volume":     _to_float(r.get("f5")),    # 成交量(手)
                    "amount":     _to_float(r.get("f6")),    # 成交额(元)
                    "turnover":   _to_float(r.get("f8")),    # 换手率%
                    "pe":         _to_float(r.get("f9")),
                })

            fetched = len(all_rows)
            if progress_cb:
                progress_cb(fetched, total)

            if not diff_list or fetched >= total or page_no >= 20:
                break
            page_no += 1
            time.sleep(0.05)

    df = pd.DataFrame(all_rows)
    if df.empty:
        return df
    df = df[df["code"].astype(str).str.match(r"^\d{6}$", na=False)].copy()
    return df.reset_index(drop=True)


def _fetch_realtime_all_akshare(progress_cb=None) -> pd.DataFrame:
    """akshare 备用：底层是东财，但 UA 不同"""
    try:
        import akshare as ak
    except ImportError:
        return pd.DataFrame()

    logger.info("使用 akshare stock_zh_a_spot_em 拉全 A 最新价")
    try:
        df = ak.stock_zh_a_spot_em()
    except Exception as e:
        logger.warning("akshare 拉取失败: %s", e)
        return pd.DataFrame()

    # 列名映射（akshare 列已是中文）
    rename = {
        "代码": "code", "名称": "name",
        "最新价": "price", "涨跌额": "change", "涨跌幅": "pct_change",
        "今开": "open", "昨收": "preclose",
        "最高": "high", "最低": "low",
        "成交量": "volume", "成交额": "amount",
        "换手率": "turnover",
    }
    df = df.rename(columns=rename)
    # 保留需要的列
    keep = ["code", "name", "price", "change", "pct_change",
            "open", "preclose", "high", "low",
            "volume", "amount", "turnover"]
    df = df[[c for c in keep if c in df.columns]].copy()
    df["pe"] = 0.0  # akshare 不直接给 PE，留空
    if progress_cb:
        progress_cb(len(df), len(df))
    return df.reset_index(drop=True)


def _parse_tencent_quote_line(line: str) -> dict | None:
    """
    解析腾讯 qt.gtimg.cn 单行快照为东财兼容字段；无效行返回 None。

    与 _fetch_klines_by_date_tencent 的区别：实时场景不按交易日过滤——
    停牌票腾讯仍返回最后交易日快照（f3>0），东财 clist 同样包含停牌票，
    结果集口径须保持一致。字段单位沿用日线腾讯通道约定：
      volume=手（写库时 *100 转股）、amount=万元×1e4 转元、turnover=%。
    """
    if "=" not in line:
        return None
    body = line.split("=", 1)[1].strip().strip('"')
    if not body:
        return None  # 停牌/无效代码的占位空串
    f = body.split("~")
    if len(f) < 40:
        return None
    code = f[2]
    if not (code.isdigit() and len(code) == 6):
        return None
    price = _to_float(f[3])
    if price <= 0:
        return None
    return {
        "code":       code,
        "name":       f[1],
        "price":      price,                         # 最新价（收盘后=当日收盘价）
        "change":     _to_float(f[31]),              # 涨跌额
        "pct_change": _to_float(f[32]),              # 涨跌幅 %
        "open":       _to_float(f[5]),               # 今开
        "preclose":   _to_float(f[4]),               # 昨收
        "high":       _to_float(f[33]),              # 最高
        "low":        _to_float(f[34]),              # 最低
        "volume":     _to_float(f[6]),               # 成交量(手)
        "amount":     _to_float(f[37]) * 10000.0,    # 成交额：万元 → 元
        "turnover":   _to_float(f[38]),              # 换手率 %
        "pe":         _to_float(f[39]),              # 市盈率(动态)
    }


def _fetch_realtime_all_tencent(progress_cb=None) -> pd.DataFrame:
    """
    腾讯 qt.gtimg.cn 批量实时快照（东财断连时的实时兜底）：
    500 只/请求（实测 1000+ 被拒），全 A 4500+ 只 ≈10 请求 ≈9s。

    股票池取本地 stock_info（与日线腾讯通道一致：仅沪深、无北交所），
    输出 13 列与 _fetch_realtime_all_em 完全同构。
    """
    try:
        from core.repository.stock_repo import get_all_stocks
        stocks_df = get_all_stocks()
    except Exception as e:
        logger.warning("腾讯实时: 取股票列表失败: %s", e)
        return pd.DataFrame()
    if stocks_df is None or stocks_df.empty:
        return pd.DataFrame()
    codes = stocks_df["code"].astype(str).tolist()

    all_rows: list[dict] = []
    total = len(codes)
    BATCH = 500  # 腾讯批量上限：实测 500 正常、1000+ 返回 0 条
    for i in range(0, total, BATCH):
        chunk = codes[i:i + BATCH]
        syms = ",".join(("sh" if c[0] in "56" else "sz") + c for c in chunk)
        try:
            resp = _SESSION.get(f"https://qt.gtimg.cn/q={syms}", timeout=15)
            resp.encoding = "gbk"  # 腾讯返回 GBK，不显式设置会乱码
        except Exception as e:
            # 单批失败不拖垮整轮（该批股票缺失，由下一轮 TTL 过期后自然补齐）
            logger.warning("腾讯实时 第 %d 批失败: %s", i // BATCH + 1, e)
            continue
        for line in resp.text.strip().split(";"):
            row = _parse_tencent_quote_line(line)
            if row is not None:
                all_rows.append(row)
        if progress_cb:
            progress_cb(len(all_rows), total)
    return pd.DataFrame(all_rows, columns=[
        "code", "name", "price", "change", "pct_change",
        "open", "preclose", "high", "low",
        "volume", "amount", "turnover", "pe",
    ]).reset_index(drop=True)


# ─────────────────────────────────────────────
# 核心：按交易日拉全 A 日线（盘后批量同步主入口）
# ─────────────────────────────────────────────
def fetch_klines_by_date(trade_date: str,
                         page_size: int = 5000,
                         progress_cb: Callable[[int, int], None] | None = None) -> pd.DataFrame:
    """
    拉取某个交易日的全 A 日线行情（盘后批量同步主入口，带护栏）

    优先东财（最近 30 天可能因反爬失败），降级 akshare stock_zh_a_hist
    """
    from core.em_guard import cached_fetch, GUARD_CONFIG, GuardName

    cfg = GUARD_CONFIG.get(GuardName.KLINES_BY_DATE, {})
    ttl = cfg.get("default_ttl", 86400)  # 日线数据，1 天 1 拉

    def _do_fetch():
        # 优先：东财（拆段拉全 A，秒级直连）
        try:
            df = _fetch_klines_by_date_em(trade_date, progress_cb)
            if not df.empty:
                return df
        except Exception as e:
            logger.info("东财 hist 失败: %s", e)
        # 2026-08-10：东财 push2his/push2 全线 404/断连、AkShare 内部东财接口
        # 同步失效，新增腾讯 qt.gtimg.cn 批量源（500 只/次 ≈1s，全 A ≈9 次 ≈10s）
        try:
            df = _fetch_klines_by_date_tencent(trade_date, progress_cb)
            if not df.empty:
                return df
        except Exception as e:
            logger.info("腾讯备用失败，降级 akshare: %s", e)
        return _fetch_klines_by_date_akshare(trade_date, progress_cb)

    params = {"trade_date": trade_date, "page_size": page_size}
    df, source = cached_fetch(GuardName.KLINES_BY_DATE, params, _do_fetch, ttl=ttl)

    if source in ("stale", "empty"):
        logger.warning("[fetch_klines_by_date] %s 走降级路径 source=%s", trade_date, source)
    return df if df is not None else pd.DataFrame()


def _fetch_klines_by_date_em(trade_date: str, progress_cb=None) -> pd.DataFrame:
    """东财 hist 直连（拆段拉取）"""
    trade_date = trade_date.replace("-", "")
    url = "https://push2his.eastmoney.com/api/qt/clist/hist"

    fs_segments = [
        "m:1+t:2,m:1+t:23",
        "m:0+t:6,m:0+t:13,m:0+t:80",
        "m:0+t:81",
    ]
    all_rows: list[dict] = []
    total = 0

    for fs in fs_segments:
        page_no = 1
        while True:
            params = {
                "pn": str(page_no),
                "pz": "500",
                "po": "1",
                "np": "1",
                "ut": _UT,
                "fl": "f12,f14,f2,f3,f5,f6,f7,f8,f15,f16,f17,f10,f9",
                "invt": "2",
                "fid": "f3",
                "fs": fs,
                "fields": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f14,f15,f16,f17",
                "date": trade_date,
                "_": str(int(time.time() * 1000)),
            }
            resp = _SESSION.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            payload = (data or {}).get("data") or {}
            diff_list = payload.get("diff") or []
            total += int(payload.get("total") or 0)

            for r in diff_list:
                # 东财 push2 clist 字段含义（已实证验证，与实时接口一致）：
                # f2=最新价 f3=涨跌幅% f5=成交量(手) f6=成交额(元) f7=振幅%
                # f8=换手率% f10=量比 f15=最高价 f16=最低价 f17=今开
                all_rows.append({
                    "code":       r.get("f12", ""),
                    "name":       r.get("f14", ""),
                    "trade_date": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}",
                    "open":       _to_float(r.get("f17")),   # 今开
                    "close":      _to_float(r.get("f2")),
                    "high":       _to_float(r.get("f15")),   # 最高价
                    "low":        _to_float(r.get("f16")),   # 最低价
                    "volume":     _to_float(r.get("f5")),    # 成交量(手)
                    "amount":     _to_float(r.get("f6")),    # 成交额(元)
                    "pct_change": _to_float(r.get("f3")),
                    "change":     _to_float(r.get("f4")),
                    "turnover":   _to_float(r.get("f8")),    # 换手率%（原误用 f10=量比）
                    "pe":         _to_float(r.get("f9")),
                })

            fetched = len(all_rows)
            if progress_cb:
                progress_cb(fetched, total)

            if not diff_list or page_no >= 20:
                break
            page_no += 1
            time.sleep(0.05)

    df = pd.DataFrame(all_rows)
    if not df.empty:
        df = df[df["code"].astype(str).str.match(r"^\d{6}$", na=False)].copy()
    return df.reset_index(drop=True)


def _fetch_klines_by_date_tencent(trade_date: str, progress_cb=None) -> pd.DataFrame:
    """
    腾讯 qt.gtimg.cn 备用：按代码批量拉当日快照（一次 ~500 只，全 A ≈9 次请求 ≈10s）

    背景（2026-08-10）：东财 push2his/push2 全线 404/断连、AkShare 内部东财接口
    同步失效，用腾讯批量源顶替"1 次 HTTP 拉全 A"的快速路径。
    实测批量上限：500 只/次正常，1000+ 只被拒（返回 0 条）。

    字段与东财直连兼容（_batch_write_daily_price 直接消费）：
      volume 单位=手（写库时 *100 转股，与东财一致）
      amount 由腾讯"万元"×10000 转元
      turnover 取字段[38]（与东财 f8 同口径）
      trade_date 取字段[30] 时间戳前 8 位；停牌/无效代码腾讯返回空行 → 跳过，
      非目标交易日（停牌返回旧日期）→ 跳过。
    """
    trade_date = trade_date.replace("-", "")
    try:
        from core.repository.stock_repo import get_all_stocks
        stocks_df = get_all_stocks()
    except Exception as e:
        logger.warning("腾讯备用: 取股票列表失败: %s", e)
        return pd.DataFrame()
    if stocks_df is None or stocks_df.empty:
        return pd.DataFrame()
    codes = stocks_df["code"].astype(str).tolist()

    def _symbol(c: str) -> str:
        # 本库股票池仅沪(6/5 开头)与深(0/3 开头)，无北交所（8/4/920）
        return ("sh" if c[0] in "56" else "sz") + c

    all_rows: list[dict] = []
    total = len(codes)
    BATCH = 500
    for i in range(0, total, BATCH):
        chunk = codes[i:i + BATCH]
        syms = ",".join(_symbol(c) for c in chunk)
        try:
            resp = _SESSION.get(f"https://qt.gtimg.cn/q={syms}", timeout=15)
            resp.encoding = "gbk"   # 腾讯返回 GBK，不显式设置会乱码
        except Exception as e:
            logger.warning("腾讯备用 第 %d 批失败: %s", i // BATCH + 1, e)
            continue
        for line in resp.text.strip().split(";"):
            if "=" not in line:
                continue
            body = line.split("=", 1)[1].strip().strip('"')
            if not body:
                continue  # 停牌/无效代码
            f = body.split("~")
            if len(f) < 40:
                continue
            ts = f[30]
            if len(ts) < 8 or ts[:8] != trade_date:
                continue  # 非目标交易日数据（停牌返回旧日期）
            code = f[2]
            if not code.isdigit() or len(code) != 6:
                continue
            close = _to_float(f[3])
            if close <= 0:
                continue
            all_rows.append({
                "code":       code,
                "name":       f[1],
                "trade_date": f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}",
                "open":       _to_float(f[5]),
                "close":      close,
                "high":       _to_float(f[33]),
                "low":        _to_float(f[34]),
                "volume":     _to_float(f[6]),              # 手（写库时 *100 转股）
                "amount":     _to_float(f[37]) * 10000.0,   # 万元 → 元
                "pct_change": _to_float(f[32]),
                "change":     _to_float(f[31]),
                "turnover":   _to_float(f[38]),
                "pe":         None,
            })
        if progress_cb:
            progress_cb(len(all_rows), total)
    return pd.DataFrame(all_rows).reset_index(drop=True)


def _fetch_klines_by_date_akshare(trade_date: str, progress_cb=None) -> pd.DataFrame:
    """
    akshare 备用：用 stock_zh_a_hist 单只拉取，并发处理
    适合 fetch_klines_by_date() 失败时降级
    """
    try:
        import akshare as ak
    except ImportError:
        return pd.DataFrame()

    from concurrent.futures import ThreadPoolExecutor, as_completed
    from datetime import datetime, timedelta

    logger.info("akshare 备用模式（并发拉 %s）", trade_date)

    # 拿全 A 代码
    try:
        spot = ak.stock_zh_a_spot_em()
        codes = spot["代码"].astype(str).tolist()
    except Exception as e:
        logger.warning("akshare 列表失败: %s", e)
        return pd.DataFrame()

    # 转为 akshare 的日期格式
    if len(trade_date) == 8:
        sd = f"{trade_date[:4]}{trade_date[4:6]}{trade_date[6:]}"
    else:
        # 尝试把 yyyy-mm-dd 转 yyyymmdd
        try:
            dt = datetime.strptime(trade_date, "%Y-%m-%d")
            sd = dt.strftime("%Y%m%d")
        except Exception:
            sd = trade_date

    # 拉前 5 天的范围（保证抓到当天）
    end_dt = datetime.strptime(sd, "%Y%m%d")
    start_dt = end_dt - timedelta(days=7)
    start_str = start_dt.strftime("%Y%m%d")
    end_str = (end_dt + timedelta(days=1)).strftime("%Y%m%d")

    all_rows: list[dict] = []
    total = len(codes)
    if progress_cb:
        progress_cb(0, total)

    def _fetch_one(code: str):
        try:
            df = ak.stock_zh_a_hist(
                symbol=code, period="daily",
                start_date=start_str, end_date=end_str, adjust="qfq",
            )
            if df is None or df.empty:
                return None
            # 找匹配日期的行
            target = f"{sd[:4]}-{sd[4:6]}-{sd[6:]}"
            row = df[df["日期"] == target]
            if row.empty:
                return None
            r = row.iloc[0]
            return {
                "code": code,
                "name": "",  # 暂不查名称
                "trade_date": target,
                "open": float(r.get("开盘", 0) or 0),
                "close": float(r.get("收盘", 0) or 0),
                "high": float(r.get("最高", 0) or 0),
                "low": float(r.get("最低", 0) or 0),
                "volume": float(r.get("成交量", 0) or 0),
                "amount": float(r.get("成交额", 0) or 0),
                "pct_change": float(r.get("涨跌幅", 0) or 0),
                "change": float(r.get("涨跌额", 0) or 0),
                "turnover": float(r.get("换手率", 0) or 0),
            }
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(_fetch_one, c) for c in codes]
        done_n = 0
        for fut in as_completed(futures):
            done_n += 1
            r = fut.result()
            if r:
                all_rows.append(r)
            if progress_cb and done_n % 100 == 0:
                progress_cb(done_n, total)

    df = pd.DataFrame(all_rows)
    if progress_cb:
        progress_cb(done_n, total)
    return df


# ─────────────────────────────────────────────
# 工具
# ─────────────────────────────────────────────
def _to_float(v) -> float:
    """东财接口 None / '' / '-' → 0.0"""
    if v in (None, "", "-", "None"):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ─────────────────────────────────────────────
# 自检入口
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    t0 = time.time()
    df = fetch_realtime_all(progress_cb=lambda f, t: print(f"\r  拉取 {f}/{t}", end=""))
    elapsed = time.time() - t0
    print(f"\n  共 {len(df)} 只，耗时 {elapsed:.2f}s")
    print(df.head(10).to_string(index=False))
