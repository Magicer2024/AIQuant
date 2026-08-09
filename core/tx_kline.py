"""
core/tx_kline.py —— 腾讯财经日 K 直连（独立兜底数据源）
=========================================================
背景：盘后同步链路 东财直连(快,易反爬) → baostock(稳,慢) → akshare(底层多为东财,同源失效)。
腾讯财经接口（web.ifzq.gtimg.cn）与东财完全异构、免费无 token、盘后当天可用，
作为 baostock 之外的第二个独立兜底（接入 sync.py `_fallback_sync`，优先级高于 akshare）。

接口：GET https://web.ifzq.gtimg.cn/appstock/app/fqkline/get
      ?param={sh|sz|bj}{code},day,{start},{end},640,qfq
返回：data.{tencent_code}.qfqday = [[date, open, close, high, low, volume(手)], ...]

口径约定（与库内 baostock 数据一致）：
  - 复权：qfq 前复权（sync.py daily_sync 主源 baostock 用 adjustflag="2" 前复权）
  - volume：接口单位为"手"(1手=100股) → 输出转"股"
  - amount：接口不直接给 → 用 volume(股)×close 估算（元），下游缺失时会自行估算，
    此处显式给出避免 amount=0 破坏质量过滤（amt20）
输出：DataFrame[trade_date, open, close, high, low, volume(股), amount(元)]，失败返回 None。
"""
from __future__ import annotations

import time
import urllib.request
import urllib.parse
import json

import pandas as pd

_FQKLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _tx_code(code: str) -> str | None:
    """6 位 A 股代码 → 腾讯前缀代码（sh/sz/bj）；北交所/无法识别返回 None"""
    code = str(code).strip().zfill(6)
    if code.startswith(("60", "68", "90", "11", "13", "51", "56", "58")):
        return f"sh{code}"
    if code.startswith(("00", "30", "20", "15", "16", "18", "12")):
        return f"sz{code}"
    if code.startswith(("43", "83", "87", "88", "92")):
        return f"bj{code}"
    return None


def fetch_kline_range(code: str, start_date: str, end_date: str,
                      timeout: float = 10.0) -> pd.DataFrame | None:
    """拉单只股票 [start_date, end_date] 日 K（前复权），失败返回 None。

    :param code: 6 位数字代码（如 "600519"）
    :param start_date/end_date: "YYYY-MM-DD"
    :return: DataFrame[trade_date, open, close, high, low, volume(股), amount(元)]
             按 trade_date 升序；空数据返回空 DataFrame；请求/解析失败返回 None。
    """
    tx = _tx_code(code)
    if tx is None:
        return None
    param = f"{tx},day,{start_date},{end_date},640,qfq"
    url = f"{_FQKLINE_URL}?param={urllib.parse.quote(param)}"
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Referer": "https://gu.qq.com/"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    try:
        node = payload["data"][tx]
        rows = node.get("qfqday") or node.get("day") or []
    except (KeyError, TypeError):
        return None

    if not rows:
        return pd.DataFrame(columns=["trade_date", "open", "close", "high", "low", "volume", "amount"])

    records = []
    for r in rows:
        # 腾讯日 K 字段序：[date, open, close, high, low, volume(手)]
        try:
            date_str = str(r[0])
            o, c, h, l = (float(r[1]), float(r[2]), float(r[3]), float(r[4]))
            v_lot = float(r[5]) if len(r) > 5 else 0.0
        except (ValueError, TypeError, IndexError):
            continue
        vol_share = v_lot * 100.0                     # 手 → 股
        amount = vol_share * c if c > 0 else 0.0      # 估算成交额（元）
        records.append({
            "trade_date": date_str,
            "open": o, "close": c, "high": h, "low": l,
            "volume": vol_share, "amount": amount,
        })
    if not records:
        return pd.DataFrame(columns=["trade_date", "open", "close", "high", "low", "volume", "amount"])
    df = pd.DataFrame(records)
    df = df[df["trade_date"] >= start_date].copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df.sort_values("trade_date").reset_index(drop=True)


def fetch_daily_batch(trade_date: str, codes: list[str],
                      progress_cb=None, sleep_s: float = 0.1) -> pd.DataFrame:
    """按日批量拉取（逐股，用于东财/baostock 均不可用时的整体兜底）。

    :param trade_date: "YYYY-MM-DD"
    :param codes: 6 位代码列表
    :return: DataFrame[trade_date, code, open, close, high, low, volume(股), amount(元)]
    """
    out = []
    total = len(codes)
    for i, code in enumerate(codes):
        if progress_cb:
            progress_cb(i + 1, total)
        df = fetch_kline_range(code, trade_date, trade_date)
        if df is not None and not df.empty:
            df = df[df["trade_date"] == pd.Timestamp(trade_date)]
            if not df.empty:
                df.insert(0, "code", code)
                out.append(df)
        time.sleep(sleep_s)  # 温和限速，防反爬
    if not out:
        return pd.DataFrame()
    return pd.concat(out, ignore_index=True)
