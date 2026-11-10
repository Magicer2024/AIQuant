"""
core/stock_flags.py —— 个股交易状态标记（停牌 / 涨跌停 / ST），仅供只读研究
==========================================================================
背景：入场端 alpha 切片研究需要"当天到底能不能成交"的可查询字段
（docs/review 数据可用性结论：停牌/涨跌停/ST 无落库标记，为立项前置项）。

口径与 utils/trade_constraints.py 同源（tol=0.002）：
  - 涨停封死  close >= prev_close*(1+limit_pct)*(1-tol)
  - 一字涨停  high==low 且涨停
  - 开盘封板  open 已达涨停/跌停价（买/卖当天开盘即不可成交方向）
  - 停牌      交易日历内该股当日无 daily_price 行，或 volume<=0
与生产 board_limit_pct 的唯一差异：补上北交所（4/8 开头 → ±30%），
生产函数因白名单不含北交所未定义该档，研究侧需要完整覆盖。

**红线：只写新表 stock_daily_flags，不碰 daily_price、不写 stock_signal、
不进任何策略参数。** 建表/回补由 tools/build_stock_daily_flags.py 驱动。
"""
from __future__ import annotations

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS stock_daily_flags (
    code            TEXT NOT NULL,
    trade_date      TEXT NOT NULL,
    limit_pct       REAL NOT NULL,      -- 当日适用涨跌停幅度（0.05/0.10/0.20/0.30）
    is_st           INTEGER NOT NULL DEFAULT 0,
    suspended       INTEGER NOT NULL DEFAULT 0,   -- 日历内缺行或 volume<=0
    limit_up_sealed INTEGER NOT NULL DEFAULT 0,   -- 收盘封涨停
    limit_down_sealed INTEGER NOT NULL DEFAULT 0, -- 收盘封跌停
    one_word_up     INTEGER NOT NULL DEFAULT 0,   -- 一字涨停（全天 high==low 且封板）
    one_word_down   INTEGER NOT NULL DEFAULT 0,
    open_sealed_up  INTEGER NOT NULL DEFAULT 0,   -- 开盘价已达涨停（buy 不可执行）
    open_sealed_down INTEGER NOT NULL DEFAULT 0,  -- 开盘价已达跌停（sell 不可执行）
    prev_close      REAL,
    PRIMARY KEY (code, trade_date)
)
"""

LIMIT_TOL = 0.002  # 与 trade_constraints 一致


def research_limit_pct(code: str, is_st: bool = False) -> float:
    """研究侧涨跌停幅度：ST ±5%；科创/创业(30/68) ±20%；北交所(4/8) ±30%；其余 ±10%。"""
    c = str(code or "").strip()
    if is_st:
        return 0.05
    if c.startswith(("30", "68")):
        return 0.20
    if c.startswith(("4", "8")):
        return 0.30
    return 0.10


def compute_flags_for_row(open_, high, low, close, prev_close, volume,
                          limit_pct: float, tol: float = LIMIT_TOL) -> dict:
    """单行行情 → 0/1 标记。prev_close 为该股上一有行情交易日收盘（跳过停牌日）。
    缺价/零量 → suspended。与 trade_constraints._fill_decision 的判据等价。"""
    flags = dict(limit_pct=limit_pct, is_st=1 if limit_pct == 0.05 else 0,
                 suspended=0, limit_up_sealed=0, limit_down_sealed=0,
                 one_word_up=0, one_word_down=0,
                 open_sealed_up=0, open_sealed_down=0, prev_close=None)
    valid = (close and close > 0)
    if not valid or (volume is not None and volume <= 0):
        flags["suspended"] = 1
        return flags
    flags["prev_close"] = prev_close if (prev_close and prev_close > 0) else None
    if flags["prev_close"] is None:
        return flags  # 首日/复牌无基准，涨跌停不判（保守全 0）
    pc = flags["prev_close"]
    up = pc * (1 + limit_pct)
    down = pc * (1 - limit_pct)
    one_price = (high and low and high > 0 and abs(high - low) < 1e-9)
    if close >= up * (1 - tol):
        flags["limit_up_sealed"] = 1
        if open_ and open_ >= up * (1 - tol):
            flags["open_sealed_up"] = 1
        if one_price:
            flags["one_word_up"] = 1
    if close <= down * (1 + tol):
        flags["limit_down_sealed"] = 1
        if open_ and open_ <= down * (1 + tol):
            flags["open_sealed_down"] = 1
        if one_price:
            flags["one_word_down"] = 1
    return flags


def rebuild(conn, days: int = 250) -> dict:
    """重算最近 days 个交易日的标记（幂等 upsert）。

    :param conn: sqlite3 连接（指向 quant.db），需已有 daily_price/stock_info。
    """
    import pandas as pd
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM daily_price ORDER BY trade_date DESC LIMIT ?",
        (days,)).fetchall()]
    if not dates:
        return {"rows": 0, "dates": 0}
    dates = sorted(dates)
    dmin, dmax = dates[0], dates[-1]
    df = pd.read_sql_query(
        "SELECT code, trade_date, open, high, low, close, volume "
        "FROM daily_price WHERE trade_date BETWEEN ? AND ? ORDER BY code, trade_date",
        conn, params=(dmin, dmax))
    st_codes = {r[0] for r in conn.execute(
        "SELECT code FROM stock_info WHERE name LIKE '%ST%'").fetchall()}
    cal = dates  # 交易日历 = daily_price 全市场有行的日期（本库口径）
    calset = set(cal)

    out = []
    for code, g in df.groupby("code", sort=False):
        lp = research_limit_pct(code, is_st=code in st_codes)
        g = g.sort_values("trade_date")
        prev_close = None
        seen = set()
        for r in g.itertuples(index=False):
            f = compute_flags_for_row(r.open, r.high, r.low, r.close,
                                      prev_close, r.volume, lp)
            out.append((code, r.trade_date, f["limit_pct"], f["is_st"],
                        f["suspended"], f["limit_up_sealed"], f["limit_down_sealed"],
                        f["one_word_up"], f["one_word_down"],
                        f["open_sealed_up"], f["open_sealed_down"], f["prev_close"]))
            prev_close = r.close if (r.close and r.close > 0) else prev_close
            seen.add(r.trade_date)
        # 日历内缺行的交易日 = 停牌
        for d in calset - seen:
            out.append((code, d, lp, 1 if code in st_codes else 0,
                        1, 0, 0, 0, 0, 0, 0, None))

    conn.executescript(TABLE_DDL)
    conn.executemany(
        """INSERT INTO stock_daily_flags
           (code, trade_date, limit_pct, is_st, suspended, limit_up_sealed,
            limit_down_sealed, one_word_up, one_word_down, open_sealed_up,
            open_sealed_down, prev_close)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(code, trade_date) DO UPDATE SET
             limit_pct=excluded.limit_pct, is_st=excluded.is_st,
             suspended=excluded.suspended, limit_up_sealed=excluded.limit_up_sealed,
             limit_down_sealed=excluded.limit_down_sealed,
             one_word_up=excluded.one_word_up, one_word_down=excluded.one_word_down,
             open_sealed_up=excluded.open_sealed_up,
             open_sealed_down=excluded.open_sealed_down,
             prev_close=excluded.prev_close""", out)
    conn.commit()
    return {"rows": len(out), "dates": len(cal), "range": [dmin, dmax]}
