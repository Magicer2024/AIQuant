"""
core/data_integrity.py —— 行情数据完整性：缺日自愈 + 当日行数护栏
==================================================================
背景（2026-11-10 docs/review 留档）：调度进程某天未运行时，daily_sync_by_date
只同步"当日"、无缺日追补，10-29/11-06 全市场缺行直接扭曲跨日持仓结算与
样本外统计。本模块提供：

  1. find_missing_trade_dates() —— 交易日历 × daily_price/index_daily 覆盖率 diff
  2. backfill_missing_dates()   —— 逐股东财 fqt=0（当日实际价）补拉缺日行情。
     口径与 clist 快路径逐价吻合（2026-11-09 抽样实测验证），不引入第三种价。
     注意：只补 daily_price / index_daily，**绝不补跑 stock_signal**（管理A红线：
     事后造信号会污染复盘）。
  3. check_daily_row_guard()    —— 当日行数 < 活跃股票数×ratio 时写 risk_events 告警。

网络/接口失败时全部降级为"跳过 + 日志"，不阻断主同步链。
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

import pandas as pd

logger = logging.getLogger(__name__)

PRICE_MIN_RATIO = 0.90        # 当日行数护栏：低于活跃股票数的 90% 判异常
INDEX_CODES_MIN = 5           # index_daily 每日应至少有 5 个指数代码
MAX_BACKFILL_DATES = 5        # 单次自愈最多补几个缺日（防止大缺口一次跑几小时）


def _active_stock_count(conn) -> int:
    row = conn.execute("SELECT COUNT(*) FROM stock_info WHERE is_active = 1").fetchone()
    return int(row[0]) if row and row[0] else 0


def _recent_trade_days(lookback_days: int, until: str) -> list[str]:
    """最近 lookback_days 自然日内的交易日（'YYYY-MM-DD' 升序）。
    优先 akshare 交易日历（core.trade_calendar，12h TTL 缓存）；
    日历不可用时降级为工作日近似（宁缺毋滥：降级结果只用于告警与补拉候选，
    非交易日拉到空数据即自然跳过）。"""
    end = datetime.strptime(until, "%Y-%m-%d").date()
    start = end - timedelta(days=lookback_days)
    try:
        from core.trade_calendar import get_trade_days
        days = get_trade_days()
    except Exception:
        days = None
    out = []
    d = start
    while d <= end:
        if days is not None:
            if d.isoformat() in days:
                out.append(d.isoformat())
        elif d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def find_missing_trade_dates(conn, lookback_days: int = 60,
                             until: str | None = None) -> list[dict]:
    """返回近 lookback_days 天里行情/指数覆盖不足的日子。

    :return: [{"trade_date", "price_rows", "index_rows", "expected",
               "price_missing": bool, "index_missing": bool}, ...]（升序）
    """
    until = until or date.today().strftime("%Y-%m-%d")
    expected = _active_stock_count(conn)
    if expected <= 0:
        return []
    days = _recent_trade_days(lookback_days, until)
    if not days:
        return []
    ph = ",".join("?" * len(days))
    price = dict(conn.execute(
        f"SELECT trade_date, COUNT(*) FROM daily_price WHERE trade_date IN ({ph}) "
        "GROUP BY trade_date", days).fetchall())
    idx = dict(conn.execute(
        f"SELECT trade_date, COUNT(DISTINCT code) FROM index_daily WHERE trade_date IN ({ph}) "
        "GROUP BY trade_date", days).fetchall())
    out = []
    for d in days:
        pr = int(price.get(d, 0))
        ir = int(idx.get(d, 0))
        p_miss = pr < expected * PRICE_MIN_RATIO
        i_miss = ir < INDEX_CODES_MIN
        if p_miss or i_miss:
            out.append({"trade_date": d, "price_rows": pr, "index_rows": ir,
                        "expected": expected, "price_missing": p_miss,
                        "index_missing": i_miss})
    return out


def backfill_price_date(conn, trade_date: str) -> dict:
    """逐股东财个股K线 fqt=0 补某历史交易日全市场行情（当日实际价，
    与 clist 快路径同口径）。幂等：已有该日行的股票跳过。"""
    from core.em_kline import to_em_secid, _UT
    import core.em_kline as ek
    from core.sync import _batch_write_daily_price

    d_compact = trade_date.replace("-", "")
    beg = (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=3)).strftime("%Y%m%d")
    codes = [r[0] for r in conn.execute(
        "SELECT code FROM stock_info WHERE is_active = 1").fetchall()]
    have = {r[0] for r in conn.execute(
        "SELECT DISTINCT code FROM daily_price WHERE trade_date = ?",
        (trade_date,)).fetchall()}
    todo = [c for c in codes if c not in have]
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params_tpl = {"ut": _UT, "fields1": "f1,f2,f3,f4,f5,f6",
                  "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                  "klt": "101", "fqt": "0", "beg": beg, "end": d_compact,
                  "lmt": "1000"}
    rows, failed = [], 0
    for code in todo:
        try:
            r = ek._SESSION.get(url, params=dict(params_tpl, secid=to_em_secid(code)),
                                timeout=12)
            r.raise_for_status()
            kl = ((r.json() or {}).get("data") or {}).get("klines") or []
            for k in kl:
                f = k.split(",")
                if f[0] != trade_date:
                    continue
                rows.append({
                    "code": code, "trade_date": trade_date,
                    "open": float(f[1]), "close": float(f[2]), "high": float(f[3]),
                    "low": float(f[4]), "volume": float(f[5]),  # 手，_batch_write 转股
                    "amount": float(f[6]), "pct_change": float(f[8]),
                    "turnover": float(f[10]),
                })
        except Exception:
            failed += 1
        time.sleep(0.05)
    written = _batch_write_daily_price(pd.DataFrame(rows)) if rows else 0
    return {"trade_date": trade_date, "attempted": len(todo),
            "written": written, "failed": failed, "already_had": len(have)}


def backfill_index_dates(conn, trade_dates: list[str]) -> int:
    """东财指数K线补齐 index_daily 指定日（volume 手→股 ×100 与库口径一致）。"""
    from core.em_kline import fetch_index_klines
    codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT code FROM index_daily").fetchall()] or \
        ["000001", "000300", "000852", "000905", "399001"]
    want = set(trade_dates)
    beg = (datetime.strptime(min(want), "%Y-%m-%d") - timedelta(days=5)).strftime("%Y%m%d")
    end = max(want).replace("-", "")
    ins = 0
    for code in codes:
        try:
            df = fetch_index_klines(code, beg, end)
        except Exception as e:
            logger.info("[integrity] 指数 %s 补拉失败: %s", code, e)
            continue
        if df is None or df.empty:
            continue
        for dt, row in df.iterrows():
            d = str(dt.date())
            if d not in want:
                continue
            conn.execute(
                """INSERT INTO index_daily
                   (code, trade_date, open, high, low, close, volume, amount, pct_change)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(code, trade_date) DO UPDATE SET
                     open=excluded.open, high=excluded.high, low=excluded.low,
                     close=excluded.close, volume=excluded.volume,
                     amount=excluded.amount, pct_change=excluded.pct_change""",
                (code, d, float(row["open"]), float(row["high"]), float(row["low"]),
                 float(row["close"]), float(row["volume"]) * 100,
                 float(row["amount"]), float(row["pct_change"])))
            ins += 1
    conn.commit()
    return ins


def self_heal(conn, lookback_days: int = 60,
              skip_dates: set[str] | None = None,
              until: str | None = None) -> dict:
    """缺日自愈：检测 → 补行情（daily_price + index_daily）。
    绝不触发信号重算。异常全部吞掉返回 error 字段，不阻断主同步。

    :param skip_dates: 本轮主链正在同步的日期（不重复处理）。
    """
    skip_dates = skip_dates or set()
    try:
        missing = [m for m in find_missing_trade_dates(conn, lookback_days, until)
                   if m["trade_date"] not in skip_dates]
    except Exception as e:
        return {"ok": False, "error": f"detect: {e}"}
    if not missing:
        return {"ok": True, "missing": [], "backfilled": []}
    missing = missing[:MAX_BACKFILL_DATES]
    results = []
    for m in missing:
        d = m["trade_date"]
        entry = {"trade_date": d, "detected": m}
        try:
            if m["price_missing"]:
                entry["price"] = backfill_price_date(conn, d)
            if m["index_missing"]:
                entry["index_rows"] = backfill_index_dates(conn, [d])
            entry["ok"] = True
        except Exception as e:
            entry["ok"] = False
            entry["error"] = str(e)[:200]
        results.append(entry)
        logger.info("[integrity] 自愈 %s -> %s", d, entry)
    return {"ok": True, "missing": missing, "backfilled": results}


def record_risk_event(conn, rule_name: str, message: str, *,
                      metric_value=None, threshold=None,
                      level: str = "warning", category: str = "data",
                      suggestion: str = "") -> int:
    """写 risk_events（生产表为历史遗留 schema，主树无建表代码，
    此处 IF NOT EXISTS 兜底保证新库/测试库可写）。"""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS risk_events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            pipeline_id   TEXT,
            rule_name     TEXT    NOT NULL,
            category      TEXT    NOT NULL,
            level         TEXT    NOT NULL,
            message       TEXT    NOT NULL,
            metric_value  REAL,
            threshold     REAL,
            suggestion    TEXT,
            created_at    TEXT    NOT NULL
        )""")
    cur = conn.execute(
        """INSERT INTO risk_events
           (pipeline_id, rule_name, category, level, message,
            metric_value, threshold, suggestion, created_at)
           VALUES (NULL, ?,?,?,?,?,?,?,datetime('now','localtime'))""",
        (rule_name, category, level, message,
         metric_value, threshold, suggestion))
    conn.commit()
    return int(cur.lastrowid)


def check_daily_row_guard(conn, trade_date: str, *,
                          min_ratio: float = PRICE_MIN_RATIO) -> dict:
    """当日行数护栏：daily_price[trade_date] 行数 < 活跃股票数×min_ratio
    → 写 risk_events 告警并返回 ok=False。护栏只报警不拦截。"""
    expected = _active_stock_count(conn)
    rows = conn.execute(
        "SELECT COUNT(*) FROM daily_price WHERE trade_date = ?",
        (trade_date,)).fetchone()[0]
    threshold = int(expected * min_ratio) if expected else 0
    ok = expected == 0 or rows >= threshold
    result = {"ok": ok, "trade_date": trade_date, "rows": int(rows),
              "expected": expected, "threshold": threshold}
    if not ok:
        try:
            record_risk_event(
                conn, "data_integrity_daily_rows",
                f"{trade_date} 当日行情行数 {rows} < 活跃股票数 {expected} 的 "
                f"{min_ratio:.0%}（阈值 {threshold}），疑似同步缺口",
                metric_value=float(rows), threshold=float(threshold),
                level="critical",
                suggestion="检查调度进程当日是否运行；可手动执行 "
                           "core.data_integrity.self_heal 或按日补拉")
        except Exception as e:
            logger.warning("[integrity] 护栏告警写入失败: %s", e)
    return result
