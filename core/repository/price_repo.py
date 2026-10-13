"""
price_repo.py —— daily_price / index_daily 表的数据访问层
"""
import pandas as pd
from datetime import date


def _get_conn():
    from core.db import get_conn
    return get_conn()


def upsert_daily_price(code: str, df: pd.DataFrame) -> int:
    """
    将 DataFrame 行情数据写入数据库（冲突则更新）
    df 需含列: open/high/low/close/volume/amount/pct_change/turnover
    index 为 datetime
    返回实际写入行数
    """
    if df.empty:
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
    with _get_conn() as conn:
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


def update_strategy_scores_batch(code: str, scores_df: pd.DataFrame):
    """
    批量更新指定股票多个日期的策略评分（仅更新策略分，不动行情数据）。
    scores_df 需含列（index=trade_date 或含 trade_date 列）：
      vol_score / ma_score / diverge_score / bottom_score / whale_score（各 0-10）
      fusion_score（0-50）
    """
    if scores_df.empty:
        return
    records = []
    for idx, row in scores_df.iterrows():
        if isinstance(idx, pd.Timestamp):
            dt_str = str(idx.date())
        elif hasattr(idx, "date"):
            dt_str = str(idx.date())
        else:
            dt_str = str(idx)[:10]
        records.append({
            "code":          code,
            "trade_date":    dt_str,
            "vol_score":     round(float(row.get("VOL_SCORE", 0) or 0), 4),
            "ma_score":      round(float(row.get("MA_SCORE", 0) or 0), 4),
            "diverge_score": round(float(row.get("DIVERGE_SCORE", 0) or 0), 4),
            "bottom_score":  round(float(row.get("BOTTOM_SCORE", 0) or 0), 4),
            "whale_score":   round(float(row.get("WHALE_SCORE", 0) or 0), 4),
            "fusion_score":  round(float(row.get("FUSION_SCORE", 0) or 0), 4),
        })
    if not records:
        return
    with _get_conn() as conn:
        conn.executemany("""
            UPDATE daily_price SET
                vol_score = :vol_score,
                ma_score = :ma_score,
                diverge_score = :diverge_score,
                bottom_score = :bottom_score,
                whale_score = :whale_score,
                fusion_score = :fusion_score
            WHERE code = :code AND trade_date = :trade_date
        """, records)


def get_daily_price(code: str, start_date: str = None, end_date: str = None,
                    min_rows: int = 30, conn=None) -> pd.DataFrame:
    """从数据库读取某只股票行情，返回 DataFrame（index=date）。

    start_date / end_date: "YYYY-MM-DD" 或 "YYYYMMDD"；
    conn: 可选共享连接（复用调用方事务视角，避免 WAL checkpoint-on-close，
          T5-2 2026-10 实测每次 close ≈0.7s → 224 次 close ≈160s 被砍掉）。
    """
    def _fmt(d):
        if d and len(d) == 8:
            return f"{d[:4]}-{d[4:6]}-{d[6:]}"
        return d

    start_date = _fmt(start_date)
    end_date   = _fmt(end_date)

    sql = "SELECT * FROM daily_price WHERE code=?"
    params = [code]
    if start_date:
        sql += " AND trade_date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND trade_date <= ?"
        params.append(end_date)
    sql += " ORDER BY trade_date ASC"

    # 有共享连接 → 直接用，避免 WAL checkpoint-on-close 成本
    if conn is not None:
        rows = conn.execute(sql, params).fetchall()
    else:
        with _get_conn() as cn:
            rows = cn.execute(sql, params).fetchall()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame([dict(r) for r in rows])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df.set_index("trade_date", inplace=True)
    df.drop(columns=["id", "code"], errors="ignore", inplace=True)
    return df


def get_price_frames(codes: list, conn=None) -> dict:
    """批量取多只股票的日线 DataFrame（单条 code IN (...) SQL，消除逐票 N+1）。

    返回 {code: DataFrame(index=trade_date, [open, close, high, low]) 或 None}；
    无行情数据的 code 为 None，结构与原逐票查询逐值一致（T4，2026-10）。
    传入 conn 可复用调用方连接（同一事务视角）；不传则自开连接。
    """
    out = {c: None for c in codes}
    uniq = list(dict.fromkeys(codes))       # 去重保序
    if not uniq:
        return out
    placeholders = ",".join("?" * len(uniq))

    def _load(cn):
        # 游标迭代按 code 分段建帧（SQL 已 ORDER BY code）：同一时刻只驻留
        # 一只股票的行，避免 fetchall 把全量行物化进内存（峰值内存约束）
        cur = cn.execute(
            f"SELECT code, trade_date, open, close, high, low FROM daily_price "
            f"WHERE code IN ({placeholders}) ORDER BY code, trade_date ASC", uniq
        )
        cols = ["trade_date", "open", "close", "high", "low"]
        cur_code, recs = None, []

        def _flush():
            if cur_code is not None:
                out[cur_code] = pd.DataFrame(recs, columns=cols).set_index("trade_date")

        for r in cur:
            if r["code"] != cur_code:
                _flush()
                cur_code, recs = r["code"], []
            recs.append((r["trade_date"], r["open"], r["close"], r["high"], r["low"]))
        _flush()

    if conn is not None:
        _load(conn)
    else:
        with _get_conn() as cn:
            _load(cn)
    return out


# ── 指数行情 ──────────────────────────────────

def upsert_index_daily(code: str, df: pd.DataFrame) -> int:
    """
    将指数行情 DataFrame 批量写入 index_daily 表。
    """
    if df.empty:
        return 0
    records = []
    for idx, row in df.iterrows():
        if hasattr(idx, 'strftime'):
            dt_str = idx.strftime("%Y-%m-%d")
        else:
            dt_str = str(idx)[:10]
        records.append({
            "code": code,
            "trade_date": dt_str,
            "open": row.get("open"),
            "high": row.get("high"),
            "low": row.get("low"),
            "close": row.get("close"),
            "volume": row.get("volume"),
            "amount": row.get("amount"),
            "pct_change": row.get("pct_change"),
        })
    with _get_conn() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executemany("""INSERT INTO index_daily
            (code, trade_date, open, high, low, close, volume, amount, pct_change)
            VALUES (:code, :trade_date, :open, :high, :low, :close, :volume, :amount, :pct_change)
            ON CONFLICT(code, trade_date) DO UPDATE SET
                open=:open, high=:high, low=:low, close=:close,
                volume=:volume, amount=:amount, pct_change=:pct_change
        """, records)
        conn.commit()
    return len(records)


def get_index_daily(code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """
    从 index_daily 表读取指数行情，返回 DataFrame（index=trade_date）
    """
    def _fmt(d):
        if d and len(d) == 8:
            return f"{d[:4]}-{d[4:6]}-{d[6:]}"
        return d
    start_date = _fmt(start_date)
    end_date   = _fmt(end_date)
    sql = "SELECT * FROM index_daily WHERE code=?"
    params = [code]
    if start_date:
        sql += " AND trade_date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND trade_date <= ?"
        params.append(end_date)
    sql += " ORDER BY trade_date ASC"
    with _get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df.set_index("trade_date", inplace=True)
    df.drop(columns=["id", "code"], errors="ignore", inplace=True)
    return df


# ── 通用日期查询 ──────────────────────────────

def get_latest_date(code: str) -> str | None:
    """获取某只股票在数据库中最新的交易日"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(trade_date) as d FROM daily_price WHERE code=?", (code,)
        ).fetchone()
    return row["d"] if row and row["d"] else None


def get_latest_date_all(before_date: str = None) -> str | None:
    """获取数据库中全市场最新交易日

    :param before_date: 可选，只返回该日期之前的交易日（非交易日时排除当天）
    """
    where = "WHERE trade_date < ?" if before_date else ""
    params = (before_date,) if before_date else ()
    with _get_conn() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT trade_date FROM daily_price {where} ORDER BY trade_date DESC LIMIT 30",
            params,
        ).fetchall()
    if not rows:
        return None
    candidates = [r["trade_date"] for r in rows]
    try:
        from core.trade_calendar import get_trade_days
        days = get_trade_days()
        if days is not None:
            for d in candidates:
                if d in days:
                    return d
    except Exception:
        pass
    from datetime import datetime as _dt
    for d in candidates:
        if _dt.strptime(d, "%Y-%m-%d").weekday() < 5:
            return d
    return candidates[0]


def has_today_data(code: str) -> bool:
    """判断某只股票今天是否已有数据"""
    today = date.today().strftime("%Y-%m-%d")
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM daily_price WHERE code=? AND trade_date=?", (code, today)
        ).fetchone()
    return row is not None


def get_stock_count_in_db() -> int:
    """数据库中有行情数据的股票数量"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT code) as n FROM daily_price"
        ).fetchone()
    return row["n"] if row else 0


def has_price_data(code: str) -> bool:
    """判断 daily_price 中该 code 是否至少有 1 条记录（任意日期）。

    T10 自 core/db.py 迁入，原名 has_watchlist_data（自选股同步场景命名，
    实际只查 daily_price），迁入 price_repo 时按真实语义正名。
    """
    if not code:
        return False
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM daily_price WHERE code=? LIMIT 1", (code,)
        ).fetchone()
    return row is not None


def get_latest_date_for_codes(codes: list[str]) -> str | None:
    """给定代码子集在 daily_price 中最新的 trade_date（'YYYY-MM-DD'）。

    子集为空或全无数据时返回 None。
    """
    if not codes:
        return None
    placeholders = ",".join("?" for _ in codes)
    with _get_conn() as conn:
        row = conn.execute(
            f"SELECT MAX(trade_date) FROM daily_price WHERE code IN ({placeholders})",
            codes,
        ).fetchone()
    return row[0] if row and row[0] else None


def get_stock_count_in_db_for_codes(codes: list[str]) -> int:
    """子集中在 daily_price 至少有一条数据的 code 数"""
    if not codes:
        return 0
    placeholders = ",".join("?" for _ in codes)
    with _get_conn() as conn:
        row = conn.execute(
            f"SELECT COUNT(DISTINCT code) FROM daily_price WHERE code IN ({placeholders})",
            codes,
        ).fetchone()
    return int(row[0] or 0)


# ── latest_price 物化表维护 ─────────────────────

def refresh_latest_price(codes: list[str] = None):
    """刷新 latest_price 物化表（每只股票取最新一行）。

    - codes 为 None 时全量刷新；否则仅刷新指定 code 子集。
    - 应在每次数据同步完成后调用。
    """
    with _get_conn() as conn:
        if codes:
            placeholders = ",".join("?" for _ in codes)
            conn.execute(f"DELETE FROM latest_price WHERE code IN ({placeholders})", codes)
            conn.execute(f"""
                INSERT INTO latest_price (code, trade_date, close, pct_change, high, low, volume, amount, fusion_score)
                SELECT code, trade_date, close, pct_change, high, low, volume, amount, fusion_score
                FROM daily_price
                WHERE code IN ({placeholders})
                  AND trade_date = (
                      SELECT MAX(trade_date) FROM daily_price dp2 WHERE dp2.code = daily_price.code
                  )
            """, codes)
        else:
            conn.execute("DELETE FROM latest_price")
            conn.execute("""
                INSERT INTO latest_price (code, trade_date, close, pct_change, high, low, volume, amount, fusion_score)
                SELECT code, trade_date, close, pct_change, high, low, volume, amount, fusion_score
                FROM daily_price
                WHERE (code, trade_date) IN (
                    SELECT code, MAX(trade_date) FROM daily_price GROUP BY code
                )
            """)
