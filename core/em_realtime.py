"""
core/em_realtime.py —— 东方财富全市场行情直连
============================================
盘前/盘后推荐场景专用：
  - fetch_realtime_all()    一次 HTTP 拉全 A 最新行情（5000+ 只）
  - fetch_realtime(codes)   按代码批量查询最新价
  - fetch_klines_by_date()  按交易日拉全市场日线（盘后批量同步）

特点：
  - 零 akshare 依赖，直接调东财 push2 接口
  - 单次 5000 只 < 3s
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
_FIELDS_REALTIME = "f12,f14,f2,f3,f4,f5,f6,f7,f15,f16,f17,f8"


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
    from core.em_guard import cached_fetch, GUARD_CONFIG

    # 默认 TTL 120s（盘前 9:00 / 盘后 16:00 各刷一次就够了）
    cfg = GUARD_CONFIG.get("realtime_all", {})
    ttl = cfg.get("default_ttl", 120)

    def _do_fetch():
        # 优先：东财 clist（直接 HTTP，最快 2-3s 拉 5000 只）
        try:
            df = _fetch_realtime_all_em(progress_cb)
            if not df.empty:
                return df
        except Exception as e:
            logger.info("东财 clist 失败，降级 akshare: %s", e)
        # 降级：akshare（底层是东财，但 UA 不同）
        return _fetch_realtime_all_akshare(progress_cb)

    params = {"page_size": page_size}
    df, source = cached_fetch("realtime_all", params, _do_fetch, ttl=ttl)

    if source == "fresh":
        logger.info("[fetch_realtime_all] 缓存命中, 0 网络请求")
    elif source == "stale":
        logger.warning("[fetch_realtime_all] 配额超限，使用 stale 缓存（防反爬）")
    elif source == "empty":
        logger.error("[fetch_realtime_all] 无数据可用")
    return df if df is not None else pd.DataFrame()


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
                "fields": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f14,f15,f16,f17",
                "_": str(int(time.time() * 1000)),
            }
            resp = _SESSION.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            payload = (data or {}).get("data") or {}
            diff_list = payload.get("diff") or []
            total = int(payload.get("total") or 0)

            for r in diff_list:
                all_rows.append({
                    "code":       r.get("f12", ""),
                    "name":       r.get("f14", ""),
                    "price":      _to_float(r.get("f2")),
                    "change":     _to_float(r.get("f4")),
                    "pct_change": _to_float(r.get("f3")),
                    "open":       _to_float(r.get("f5")),
                    "preclose":   _to_float(r.get("f6")),
                    "high":       _to_float(r.get("f7")),
                    "low":        _to_float(r.get("f8")),
                    "volume":     _to_float(r.get("f15")),
                    "amount":     _to_float(r.get("f16")),
                    "turnover":   _to_float(r.get("f17")),
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
    from core.em_guard import cached_fetch, GUARD_CONFIG

    cfg = GUARD_CONFIG.get("klines_by_date", {})
    ttl = cfg.get("default_ttl", 86400)  # 日线数据，1 天 1 拉

    def _do_fetch():
        # 优先：东财（之前直连的尝试在 30 天后会被反爬挡）
        try:
            df = _fetch_klines_by_date_em(trade_date, progress_cb)
            if not df.empty:
                return df
        except Exception as e:
            logger.info("东财 hist 失败，降级 akshare: %s", e)
        return _fetch_klines_by_date_akshare(trade_date, progress_cb)

    params = {"trade_date": trade_date, "page_size": page_size}
    df, source = cached_fetch("klines_by_date", params, _do_fetch, ttl=ttl)

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
                all_rows.append({
                    "code":       r.get("f12", ""),
                    "name":       r.get("f14", ""),
                    "trade_date": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}",
                    "open":       _to_float(r.get("f5")),
                    "close":      _to_float(r.get("f2")),
                    "high":       _to_float(r.get("f7")),
                    "low":        _to_float(r.get("f8")),
                    "volume":     _to_float(r.get("f15")),
                    "amount":     _to_float(r.get("f16")),
                    "pct_change": _to_float(r.get("f3")),
                    "change":     _to_float(r.get("f4")),
                    "turnover":   _to_float(r.get("f10")),
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
