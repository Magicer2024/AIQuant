"""
db.py  ——  本地 SQLite 数据库模块
========================================
表结构：
  stock_info      股票基本信息（代码、名称、市场）
  daily_price     每日行情（OHLCV + 指标）
  signal_records  策略筛选记录（每次扫描结果）
  sync_log        数据同步日志
"""

import sqlite3
import os
import math
import json
import warnings
import pandas as pd
from datetime import datetime, date
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quant.db")


# ─────────────────────────────────────────────
# 连接管理
# ─────────────────────────────────────────────

@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")   # 写时复制，提升并发
    conn.execute("PRAGMA synchronous=NORMAL") # 性能与安全平衡
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─────────────────────────────────────────────
# 迁移辅助：安全地给表添加列（列已存在时自动跳过）
# ─────────────────────────────────────────────
def _safe_add_column(conn, table: str, column: str, col_type: str):
    """给表添加列，如果列已存在则什么都不做（SQLite 不支持 IF NOT EXISTS for columns）"""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    except sqlite3.OperationalError:
        pass  # 列已存在


# ─────────────────────────────────────────────
# 建表（首次运行自动创建）
# ─────────────────────────────────────────────

def init_db():
    """初始化数据库，创建所有表"""
    with get_conn() as conn:
        # ── 1. 所有建表/建索引 SQL（纯 SQL，无 Python 代码）──────────
        conn.executescript("""
-- 股票基本信息
CREATE TABLE IF NOT EXISTS stock_info (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    market      TEXT,
    is_active   INTEGER DEFAULT 1,
    updated_at  TEXT
);

-- 每日行情数据
CREATE TABLE IF NOT EXISTS daily_price (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    NOT NULL,
    trade_date  TEXT    NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      REAL,
    amount      REAL,
    pct_change  REAL,
    turnover    REAL,
    vol_score    REAL DEFAULT 0,
    ma_score     REAL DEFAULT 0,
    diverge_score REAL DEFAULT 0,
    bottom_score REAL DEFAULT 0,
    whale_score  REAL DEFAULT 0,
    fusion_score REAL DEFAULT 0,
    UNIQUE(code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_daily_code_date ON daily_price(code, trade_date);
CREATE INDEX IF NOT EXISTS idx_daily_date      ON daily_price(trade_date);

-- 指数每日行情（沪深300/中证500/中证1000等，供大盘择时用）
CREATE TABLE IF NOT EXISTS index_daily (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    NOT NULL,
    trade_date  TEXT    NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      REAL,
    amount       REAL,
    pct_change  REAL,
    UNIQUE(code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_index_code_date ON index_daily(code, trade_date);
CREATE INDEX IF NOT EXISTS idx_index_date      ON index_daily(trade_date);

-- 每日策略扫描推荐记录
CREATE TABLE IF NOT EXISTS stock_signal (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_date       TEXT    NOT NULL,
    trade_date      TEXT    NOT NULL,
    code            TEXT    NOT NULL,
    name            TEXT,
    price           REAL,
    fusion_score    REAL,
    vol_score       REAL,
    ma_score        REAL,
    diverge_score   REAL,
    bottom_score    REAL,
    whale_score     REAL,
    trigger_list    TEXT,
    buy_price       REAL,
    stop_loss       REAL,
    take_profit     REAL,
    buy_volume      INTEGER,
    buy_money       REAL,
    sent_wechat     INTEGER DEFAULT 0,
    created_at      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sig_scan_trade_code ON stock_signal(scan_date, code);
CREATE INDEX IF NOT EXISTS idx_sig_trade_date ON stock_signal(trade_date);

-- 策略筛选记录（旧表，兼容用）
CREATE TABLE IF NOT EXISTS signal_records (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_time    TEXT NOT NULL,
    trade_date   TEXT NOT NULL,
    code         TEXT NOT NULL,
    name         TEXT,
    price        REAL,
    score        INTEGER,
    stop_loss    REAL,
    take_profit  REAL,
    buy_volume   INTEGER,
    buy_money    REAL,
    sent_wechat  INTEGER DEFAULT 0,
    note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_signal_date ON signal_records(trade_date);

-- 数据同步日志
CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_time   TEXT NOT NULL,
    sync_type   TEXT,
    total       INTEGER DEFAULT 0,
    success     INTEGER DEFAULT 0,
    failed      INTEGER DEFAULT 0,
    duration_s  REAL,
    note        TEXT
);

-- 持仓记录
CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL,
    name            TEXT,
    entry_date      TEXT NOT NULL,
    entry_price     REAL NOT NULL,
    shares          INTEGER NOT NULL,
    current_price   REAL,
    stop_loss       REAL,
    take_profit     REAL,
    position_type   TEXT DEFAULT 'long',
    status          TEXT DEFAULT 'holding',
    strategy        TEXT,
    note            TEXT,
    created_at      TEXT,
    updated_at      TEXT,
    commission      REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_position_code    ON positions(code);
CREATE INDEX IF NOT EXISTS idx_position_status  ON positions(status);

-- 交易记录
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id     INTEGER,
    code            TEXT NOT NULL,
    name            TEXT,
    trade_date      TEXT NOT NULL,
    direction       TEXT NOT NULL,
    price           REAL NOT NULL,
    shares          INTEGER NOT NULL,
    amount          REAL,
    commission      REAL DEFAULT 0,
    slippage        REAL DEFAULT 0,
    reason          TEXT,
    note            TEXT,
    created_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_trade_code ON trades(code);
CREATE INDEX IF NOT EXISTS idx_trade_date ON trades(trade_date);

-- 账户资产快照
CREATE TABLE IF NOT EXISTS account_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date   TEXT NOT NULL,
    total_assets    REAL NOT NULL,
    cash            REAL NOT NULL,
    position_value  REAL NOT NULL,
    positions_count INTEGER DEFAULT 0,
    total_cost      REAL,
    total_pnl       REAL,
    pnl_pct         REAL,
    created_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshot_date ON account_snapshots(snapshot_date);

-- 融资融券汇总
CREATE TABLE IF NOT EXISTS stock_margin (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date          TEXT NOT NULL,
    sse_margin_balance  REAL,
    sse_margin_buy      REAL,
    sse_short_balance   REAL,
    sse_short_volume    REAL,
    szse_margin_balance REAL,
    szse_margin_buy     REAL,
    szse_short_balance  REAL,
    szse_short_volume   REAL,
    szse_margin_ratio   REAL,
    total_margin_balance REAL,
    total_margin_buy    REAL,
    total_short_balance REAL,
    total_margin        REAL,
    UNIQUE(trade_date)
);
CREATE INDEX IF NOT EXISTS idx_margin_date ON stock_margin(trade_date);

-- 融资融券明细
CREATE TABLE IF NOT EXISTS stock_margin_detail (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    code                TEXT NOT NULL,
    trade_date          TEXT NOT NULL,
    name                TEXT,
    exchange            TEXT,
    margin_balance       REAL,
    margin_buy          REAL,
    margin_repay        REAL,
    short_volume        REAL,
    short_sell          REAL,
    short_repay         REAL,
    short_balance       REAL,
    total_balance       REAL,
    UNIQUE(code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_md_code_date ON stock_margin_detail(code, trade_date);

-- 沪深港通北向资金
CREATE TABLE IF NOT EXISTS stock_hsgt_north (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date              TEXT NOT NULL,
    north_net_buy           REAL,
    north_buy_amt           REAL,
    north_sell_amt           REAL,
    north_cum_net           REAL,
    north_daily_flow        REAL,
    north_balance           REAL,
    north_market_cap        REAL,
    hgt_net_buy             REAL,
    hgt_buy_amt             REAL,
    hgt_sell_amt            REAL,
    hgt_cum_net             REAL,
    hgt_daily_flow          REAL,
    sgt_net_buy             REAL,
    sgt_buy_amt             REAL,
    sgt_sell_amt            REAL,
    sgt_cum_net             REAL,
    sgt_daily_flow          REAL,
    UNIQUE(trade_date)
);
CREATE INDEX IF NOT EXISTS idx_hsgt_date ON stock_hsgt_north(trade_date);

-- 指数期货
CREATE TABLE IF NOT EXISTS index_futures (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date      TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    variety         TEXT NOT NULL,
    open            REAL,
    high            REAL,
    low             REAL,
    close           REAL,
    volume           REAL,
    open_interest   REAL,
    turnover         REAL,
    settle           REAL,
    pre_settle       REAL,
    UNIQUE(trade_date, symbol)
);
CREATE INDEX IF NOT EXISTS idx_if_date       ON index_futures(trade_date);
CREATE INDEX IF NOT EXISTS idx_if_date_var  ON index_futures(trade_date, variety);

-- 龙虎榜明细
CREATE TABLE IF NOT EXISTS stock_lhb_detail (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date      TEXT NOT NULL,
    code            TEXT NOT NULL,
    name            TEXT,
    close_price     REAL,
    pct_change      REAL,
    net_buy         REAL,
    buy_amt         REAL,
    sell_amt        REAL,
    total_amt       REAL,
    mkt_total_amt   REAL,
    net_buy_ratio   REAL,
    turnover_rate   REAL,
    float_mkt_cap   REAL,
    reason          TEXT,
    perf_1d         REAL,
    perf_2d         REAL,
    perf_5d         REAL,
    perf_10d        REAL,
    UNIQUE(trade_date, code)
);
CREATE INDEX IF NOT EXISTS idx_lhb_date ON stock_lhb_detail(trade_date);
CREATE INDEX IF NOT EXISTS idx_lhb_code ON stock_lhb_detail(code);

-- 高管增减持
CREATE TABLE IF NOT EXISTS stock_mgmt_holding (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date          TEXT NOT NULL,
    code                TEXT NOT NULL,
    name                TEXT,
    changer             TEXT,
    change_shares       REAL,
    avg_price           REAL,
    change_amount       REAL,
    change_reason       TEXT,
    change_ratio        REAL,
    shares_after        REAL,
    share_type          TEXT,
    executive_name      TEXT,
    position            TEXT,
    relation            TEXT,
    UNIQUE(trade_date, code, changer, change_shares, avg_price)
);
CREATE INDEX IF NOT EXISTS idx_mh_date ON stock_mgmt_holding(trade_date);
CREATE INDEX IF NOT EXISTS idx_mh_code ON stock_mgmt_holding(code);

-- 审计日志
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action      TEXT NOT NULL,
    resource    TEXT,
    detail      TEXT,
    ip_address  TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(created_at DESC);

-- 策略规则库
CREATE TABLE IF NOT EXISTS strategy_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_name       TEXT NOT NULL UNIQUE,
    rule_type       TEXT NOT NULL,
    encoding        TEXT NOT NULL,
    conditions      TEXT,
    sell_conditions TEXT,
    holding_min     INTEGER DEFAULT 3,
    holding_max     INTEGER DEFAULT 20,
    source          TEXT DEFAULT 'template',
    generation      INTEGER DEFAULT 0,
    fitness         REAL DEFAULT 0,
    annual_return   REAL DEFAULT 0,
    win_rate        REAL DEFAULT 0,
    sharpe_ratio    REAL DEFAULT 0,
    max_drawdown    REAL DEFAULT 0,
    total_trades    INTEGER DEFAULT 0,
    signal_overlap  REAL DEFAULT 0,
    is_active       INTEGER DEFAULT 1,
    degraded_at     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_rules_type ON strategy_rules(rule_type);
CREATE INDEX IF NOT EXISTS idx_rules_active ON strategy_rules(is_active);
CREATE INDEX IF NOT EXISTS idx_rules_fitness ON strategy_rules(fitness DESC);

-- 每日信号触发日志
CREATE TABLE IF NOT EXISTS strategy_signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date      TEXT NOT NULL,
    code            TEXT NOT NULL,
    rule_id         INTEGER NOT NULL,
    rule_name       TEXT,
    confidence      REAL,
    raw_score       REAL,
    features_json   TEXT,
    label_return    REAL,
    is_win          INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (rule_id) REFERENCES strategy_rules(id)
);
CREATE INDEX IF NOT EXISTS idx_signals_date ON strategy_signals(trade_date);
CREATE INDEX IF NOT EXISTS idx_signals_code ON strategy_signals(code);
CREATE INDEX IF NOT EXISTS idx_signals_rule ON strategy_signals(rule_id);
CREATE INDEX IF NOT EXISTS idx_signals_date_code ON strategy_signals(trade_date, code);

-- 当期活跃策略
CREATE TABLE IF NOT EXISTS active_strategies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    select_date     TEXT NOT NULL,
    rule_id         INTEGER NOT NULL,
    rule_name       TEXT,
    window_60_score REAL,
    window_120_score REAL,
    final_score     REAL,
    rank            INTEGER,
    is_emergency    INTEGER DEFAULT 0,
    valid_until     TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (rule_id) REFERENCES strategy_rules(id)
);
CREATE INDEX IF NOT EXISTS idx_active_date ON active_strategies(select_date);
CREATE INDEX IF NOT EXISTS idx_active_valid ON active_strategies(valid_until);

-- 每日因子值缓存
CREATE TABLE IF NOT EXISTS factor_daily (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    NOT NULL,
    trade_date  TEXT    NOT NULL,
    factor_name TEXT    NOT NULL,
    factor_value REAL,
    UNIQUE(code, trade_date, factor_name)
);
CREATE INDEX IF NOT EXISTS idx_factor_code_date ON factor_daily(code, trade_date);
CREATE INDEX IF NOT EXISTS idx_factor_name_date ON factor_daily(factor_name, trade_date);

-- 每日打分排名
CREATE TABLE IF NOT EXISTS stock_score (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date  TEXT    NOT NULL,
    code        TEXT    NOT NULL,
    name        TEXT,
    score       REAL    DEFAULT 0,
    rule_id     TEXT,
    rule_name   TEXT,
    factors_json TEXT,
    created_at  TEXT    DEFAULT (datetime('now','localtime')),
    UNIQUE(trade_date, code)
);
CREATE INDEX IF NOT EXISTS idx_stock_score_date ON stock_score(trade_date);
CREATE INDEX IF NOT EXISTS idx_stock_score_code ON stock_score(code);

-- 回测结果汇总
CREATE TABLE IF NOT EXISTS backtest_results (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id             TEXT    NOT NULL,
    rule_name           TEXT,
    start_date          TEXT    NOT NULL,
    end_date            TEXT    NOT NULL,
    annual_return       REAL    DEFAULT 0,
    cumulative_return   REAL    DEFAULT 0,
    win_rate            REAL    DEFAULT 0,
    sharpe_ratio        REAL    DEFAULT 0,
    max_drawdown        REAL    DEFAULT 0,
    total_trades        INTEGER DEFAULT 0,
    win_trades          INTEGER DEFAULT 0,
    created_at          TEXT    DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_bt_results_rule ON backtest_results(rule_id);

-- 回测逐笔交易明细
CREATE TABLE IF NOT EXISTS backtest_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id       INTEGER,
    code            TEXT    NOT NULL,
    name            TEXT,
    entry_date      TEXT    NOT NULL,
    entry_price     REAL,
    exit_date       TEXT,
    exit_price      REAL,
    holding_days    INTEGER DEFAULT 0,
    pnl_pct         REAL    DEFAULT 0,
    exit_reason     TEXT,
    FOREIGN KEY (result_id) REFERENCES backtest_results(id)
);
CREATE INDEX IF NOT EXISTS idx_bt_trades_result ON backtest_trades(result_id);
CREATE INDEX IF NOT EXISTS idx_bt_trades_code ON backtest_trades(code);
        """)

        # ── 2. 迁移：为旧版 daily_price 补充策略评分列 ────────────
        _safe_add_column(conn, "daily_price", "vol_score",     "REAL DEFAULT 0")
        _safe_add_column(conn, "daily_price", "ma_score",      "REAL DEFAULT 0")
        _safe_add_column(conn, "daily_price", "diverge_score", "REAL DEFAULT 0")
        _safe_add_column(conn, "daily_price", "bottom_score",  "REAL DEFAULT 0")
        _safe_add_column(conn, "daily_price", "whale_score",   "REAL DEFAULT 0")
        _safe_add_column(conn, "daily_price", "fusion_score",  "REAL DEFAULT 0")
        _safe_add_column(conn, "daily_price", "strategy_score",  "REAL DEFAULT 0")
        _safe_add_column(conn, "daily_price", "strategy_signal", "TEXT DEFAULT 'HOLD'")

        # ── 3. 迁移：为 positions 补充 commission 列 ─────────────
        _safe_add_column(conn, "positions", "commission", "REAL DEFAULT 0")

        # stock_info 增加市值相关字段
        _safe_add_column(conn, "stock_info", "total_shares",  "REAL")   # 总股本
        _safe_add_column(conn, "stock_info", "circ_shares",   "REAL")   # 流通股本

        # ── 4. 迁移：修正 stock_signal 唯一索引（去掉 trade_date）────────
        try:
            conn.execute("DROP INDEX IF EXISTS idx_sig_scan_trade_code")
        except Exception:
            pass
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sig_scan_trade_code "
                "ON stock_signal(scan_date, code)")
        except Exception:
            pass

        # ── 5. 建唯一索引（重复建表后补充）────────────────────────
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_code_date_uniq "
                "ON daily_price(code, trade_date)")
        except Exception:
            pass

    print(f"[DB] 数据库初始化完成: {DB_PATH}")



def upsert_stock_list(records: list[dict]):
    """
    批量写入/更新股票列表
    records: [{"code": "000001", "name": "平安银行", "market": "SZ"}, ...]
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.executemany("""
            INSERT INTO stock_info(code, name, market, updated_at)
            VALUES(:code, :name, :market, :updated_at)
            ON CONFLICT(code) DO UPDATE SET
                name=excluded.name,
                market=excluded.market,
                updated_at=excluded.updated_at
        """, [{**r, "updated_at": now} for r in records])
    print(f"[DB] stock_info 更新 {len(records)} 条")


def update_market_cap(code: str, total_shares: float, circ_shares: float):
    """更新单只股票的总股本和流通股本"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE stock_info SET total_shares=?, circ_shares=? WHERE code=?",
            (total_shares, circ_shares, code)
        )


def batch_update_market_cap(records: list[dict]):
    """
    批量更新市值数据
    records: [{"code": "000001", "total_shares": 19405918198, "circ_shares": 19405600653}, ...]
    """
    with get_conn() as conn:
        conn.executemany(
            "UPDATE stock_info SET total_shares=?, circ_shares=? WHERE code=?",
            [(r["total_shares"], r["circ_shares"], r["code"]) for r in records]
        )
    print(f"[DB] market_cap 更新 {len(records)} 条")


def get_market_cap_map() -> dict:
    """获取所有股票的股本信息 {code: {"total_shares": ..., "circ_shares": ...}}"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT code, total_shares, circ_shares FROM stock_info WHERE total_shares IS NOT NULL"
        ).fetchall()
    return {r[0]: {"total_shares": r[1], "circ_shares": r[2]} for r in rows}


def get_all_stocks(active_only=True) -> pd.DataFrame:
    """获取所有股票列表"""
    with get_conn() as conn:
        sql = "SELECT code, name, market FROM stock_info"
        if active_only:
            sql += " WHERE is_active=1"
        sql += " ORDER BY code"
        rows = conn.execute(sql).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def get_stock_name(code: str) -> str:
    """根据代码查名称"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM stock_info WHERE code=?", (code,)
        ).fetchone()
    return row["name"] if row else code


# ─────────────────────────────────────────────
# daily_price 操作
# ─────────────────────────────────────────────

def upsert_daily_price(code: str, df: pd.DataFrame) -> int:
    """
    [DEPRECATED] 将 DataFrame 行情数据写入数据库（冲突则更新）
    Use qlib_engine.data_bridge.append_daily_data() instead.
    """
    warnings.warn(
        "upsert_daily_price is deprecated — use qlib_engine.data_bridge.append_daily_data()",
        DeprecationWarning, stacklevel=2
    )
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


def update_strategy_scores_batch(code: str, scores_df: pd.DataFrame):
    """
    批量更新指定股票多个日期的策略评分（仅更新策略分，不动行情数据）。

    scores_df 需含列（index=trade_date 或含 trade_date 列）：
      vol_score / ma_score / diverge_score / bottom_score / whale_score（各 0-10）
      fusion_score（0-50）

    示例：
        scores_df = fuse_signals([s1, s2, s3, s4, s5])
        update_strategy_scores_batch("000001", scores_df)
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
    with get_conn() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        for rec in records:
            conn.execute(f"""
                UPDATE daily_price SET
                    vol_score = :vol_score,
                    ma_score = :ma_score,
                    diverge_score = :diverge_score,
                    bottom_score = :bottom_score,
                    whale_score = :whale_score,
                    fusion_score = :fusion_score
                WHERE code = :code AND trade_date = :trade_date
            """, rec)
        conn.commit()


def get_daily_price(code: str, start_date: str = None, end_date: str = None,
                    min_rows: int = 30) -> pd.DataFrame:
    """
    [DEPRECATED] 从数据库读取某只股票行情，返回 DataFrame（index=date）
    Use Qlib DataHandler or qlib.data.D.features() instead.
    """
    warnings.warn(
        "get_daily_price is deprecated — use Qlib DataHandler or qlib.data.D.features()",
        DeprecationWarning, stacklevel=2
    )
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

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame([dict(r) for r in rows])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df.set_index("trade_date", inplace=True)
    df.drop(columns=["id", "code"], errors="ignore", inplace=True)
    return df


# ─────────────────────────────────────────────
# 指数行情操作（供大盘择时用）
# ─────────────────────────────────────────────

def upsert_index_daily(code: str, df: pd.DataFrame) -> int:
    """
    [DEPRECATED] 将指数行情 DataFrame 批量写入 index_daily 表。
    Use qlib_engine.data_bridge pattern for index data.
    """
    warnings.warn(
        "upsert_index_daily is deprecated — Qlib handles index data via calendars/day.txt",
        DeprecationWarning, stacklevel=2
    )
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
    with get_conn() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executemany(f"""INSERT INTO index_daily
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
    [DEPRECATED] 从 index_daily 表读取指数行情，返回 DataFrame（index=trade_date）
    Use Qlib DataHandler or qlib.data.D.features() instead.
    """
    warnings.warn(
        "get_index_daily is deprecated — use Qlib DataHandler or qlib.data.D.features()",
        DeprecationWarning, stacklevel=2
    )
    def _fmt(d):
        if d and len(d) == 8:
            return f"{d[:4]}-{d[4:6]}-{d[6:]}"
        return d
    start_date = _fmt(start_date)
    end_date = _fmt(end_date)
    sql = "SELECT * FROM index_daily WHERE code=?"
    params = [code]
    if start_date:
        sql += " AND trade_date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND trade_date <= ?"
        params.append(end_date)
    sql += " ORDER BY trade_date ASC"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df.set_index("trade_date", inplace=True)
    df.drop(columns=["id", "code"], errors="ignore", inplace=True)
    return df


def get_latest_date(code: str) -> str | None:
    """获取某只股票在数据库中最新的交易日"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(trade_date) as d FROM daily_price WHERE code=?", (code,)
        ).fetchone()
    return row["d"] if row and row["d"] else None


def get_latest_date_all() -> str | None:
    """获取数据库中全市场最新交易日"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(trade_date) as d FROM daily_price"
        ).fetchone()
    return row["d"] if row and row["d"] else None


def has_today_data(code: str) -> bool:
    """判断某只股票今天是否已有数据"""
    today = date.today().strftime("%Y-%m-%d")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM daily_price WHERE code=? AND trade_date=?", (code, today)
        ).fetchone()
    return row is not None


def get_stock_count_in_db() -> int:
    """数据库中有行情数据的股票数量"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT code) as n FROM daily_price"
        ).fetchone()
    return row["n"] if row else 0


# ─────────────────────────────────────────────
# signal_records 操作
# ─────────────────────────────────────────────

def save_signals(signals: list[dict], sent_wechat: bool = False):
    """
    保存策略筛选结果
    signals: [{"code":..,"name":..,"price":..,"score":..,"stop_loss":..,"take_profit":..,...}]
    """
    if not signals:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = date.today().strftime("%Y-%m-%d")
    with get_conn() as conn:
        conn.executemany("""
            INSERT INTO signal_records
              (scan_time, trade_date, code, name, price, score,
               stop_loss, take_profit, buy_volume, buy_money, sent_wechat)
            VALUES
              (:scan_time, :trade_date, :code, :name, :price, :score,
               :stop_loss, :take_profit, :buy_volume, :buy_money, :sent_wechat)
        """, [{
            "scan_time":   now,
            "trade_date":  today,
            "code":        s.get("code", ""),
            "name":        s.get("name", ""),
            "price":       s.get("price", 0),
            "score":       s.get("score", 0),
            "stop_loss":   s.get("stop_loss", 0),
            "take_profit": s.get("take_profit", 0),
            "buy_volume":  s.get("buy_volume", 0),
            "buy_money":   s.get("buy_money", 0),
            "sent_wechat": 1 if sent_wechat else 0,
        } for s in signals])
    print(f"[DB] 保存 {len(signals)} 条信号记录，推送微信={'是' if sent_wechat else '否'}")


def save_scan_signals(scan_results: list[dict], sent_wechat: bool = False):
    """
    保存每日策略扫描推荐结果到 stock_signal 表。

    scan_results: analyze_stock_from_db 返回的列表，每项需含：
      code, name, price, score（融合分）, s1_vol_break, s2_ma_conv,
      s3_pv_div, s4_bottom, s5_whale, stop_loss, take_profit,
      buy_volume, buy_money, trigger_list（触发策略名列表）
    """
    if not scan_results:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = date.today().strftime("%Y-%m-%d")
    import json as _json
    records = []
    for s in scan_results:
        def _si(v):
            if v is None: return 0.0
            if isinstance(v, float) and math.isnan(v): return 0.0
            return round(float(v), 2)
        trigger = s.get("trigger_list", [])
        if isinstance(trigger, list):
            trigger_json = json.dumps(trigger, ensure_ascii=False)
        else:
            trigger_json = str(trigger or "[]")
        records.append({
            "scan_date":    today,
            "trade_date":   s.get("trade_date") or today,
            "code":         s.get("code", ""),
            "name":         s.get("name", ""),
            "price":        _si(s.get("price")),
            "fusion_score": _si(s.get("score")),
            "vol_score":    _si(s.get("s1_vol_break")),
            "ma_score":     _si(s.get("s2_ma_conv")),
            "diverge_score": _si(s.get("s3_pv_div")),
            "bottom_score":  _si(s.get("s4_bottom")),
            "whale_score":   _si(s.get("s5_whale")),
            "trigger_list":  trigger_json,
            "buy_price":     _si(s.get("price")),
            "stop_loss":     _si(s.get("stop_loss")),
            "take_profit":   _si(s.get("take_profit")),
            "buy_volume":    int(s.get("buy_volume", 0)),
            "buy_money":     _si(s.get("buy_money")),
            "sent_wechat":   1 if sent_wechat else 0,
            "created_at":    now,
        })
    with get_conn() as conn:
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
        """, records)
    print(f"[DB] 保存 {len(scan_results)} 条扫描推荐到 stock_signal")


def get_signals(trade_date: str = None, limit: int = 100) -> list[dict]:
    """查询筛选记录"""
    sql = "SELECT * FROM signal_records"
    params = []
    if trade_date:
        sql += " WHERE trade_date=?"
        params.append(trade_date)
    sql += " ORDER BY scan_time DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_today_signals() -> list[dict]:
    """获取今日筛选记录"""
    return get_signals(trade_date=date.today().strftime("%Y-%m-%d"))


# ─────────────────────────────────────────────
# sync_log 操作
# ─────────────────────────────────────────────

def log_sync(sync_type: str, total: int, success: int, failed: int,
             duration_s: float, note: str = ""):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO sync_log(sync_time, sync_type, total, success, failed, duration_s, note)
            VALUES(?, ?, ?, ?, ?, ?, ?)
        """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              sync_type, total, success, failed, round(duration_s, 1), note))


def get_sync_logs(limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sync_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 数据库状态统计
# ─────────────────────────────────────────────

def db_stats() -> dict:
    """返回数据库概要统计信息"""
    with get_conn() as conn:
        stock_cnt  = conn.execute("SELECT COUNT(*) FROM stock_info WHERE is_active=1").fetchone()[0]
        price_cnt  = conn.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0]
        code_cnt   = conn.execute("SELECT COUNT(DISTINCT code) FROM daily_price").fetchone()[0]
        signal_cnt = conn.execute("SELECT COUNT(*) FROM signal_records").fetchone()[0]
        latest_d   = conn.execute("SELECT MAX(trade_date) FROM daily_price").fetchone()[0]
        earliest_d = conn.execute("SELECT MIN(trade_date) FROM daily_price").fetchone()[0]
        db_size    = os.path.getsize(DB_PATH) / 1024 / 1024  # MB
    return {
        "股票列表数":     stock_cnt,
        "有行情股票数":   code_cnt,
        "行情记录总数":   price_cnt,
        "信号记录总数":   signal_cnt,
        "最早日期":       earliest_d or "—",
        "最新日期":       latest_d or "—",
        "数据库大小(MB)": round(db_size, 2),
    }


def update_position_price(position_id: int, current_price: float):
    """更新持仓的当前价格"""
    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute("""
            UPDATE positions SET current_price = ?, updated_at = ? WHERE id = ?
        """, (current_price, now, position_id))


def get_positions(status: str = "holding") -> list[dict]:
    """获取持仓列表，同时从 daily_price 表补全最新价格和日期"""
    # 子查询：每只股票在 daily_price 中的最新收盘价和日期
    latest_price_sql = """
        SELECT code, close, trade_date FROM daily_price a
        WHERE trade_date = (
            SELECT MAX(trade_date) FROM daily_price b WHERE b.code = a.code
        )
    """
    sql = f"""
        SELECT p.id, p.code, p.entry_date, p.entry_price, p.shares, p.current_price,
               p.stop_loss, p.take_profit, p.position_type, p.status, p.strategy, p.note,
               p.created_at, p.updated_at,
               lp.close AS latest_price, lp.trade_date AS price_date,
               COALESCE(NULLIF(p.name, ''), s.name) AS name
        FROM positions p
        LEFT JOIN ({latest_price_sql}) lp ON lp.code = p.code
        LEFT JOIN stock_info s ON s.code = p.code
    """
    params = []
    if status:
        sql += " WHERE p.status = ?"
        params.append(status)
    sql += " ORDER BY p.entry_date DESC"

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    result = [dict(r) for r in rows]
    # 优先用实时最新价，其次用持仓记录中存储的 current_price
    for r in result:
        r["current_price"] = r.get("latest_price") or r.get("current_price") or 0
    return result


def get_position_by_id(position_id: int) -> dict | None:
    """根据ID获取持仓"""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
    return dict(row) if row else None


def get_position_by_code(code: str, status: str = "holding") -> dict | None:
    """根据股票代码获取持仓"""
    sql = "SELECT * FROM positions WHERE code = ?"
    params = [code]
    if status:
        sql += " AND status = ?"
        params.append(status)
    with get_conn() as conn:
        row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def delete_position(position_id: int):
    """删除持仓记录"""
    with get_conn() as conn:
        conn.execute("DELETE FROM positions WHERE id = ?", (position_id,))


# ─────────────────────────────────────────────
# trades 操作（交易记录）
# ─────────────────────────────────────────────

def add_trade(position_id: int, code: str, name: str, trade_date: str,
             direction: str, price: float, shares: int,
             commission: float = 0, reason: str = "", note: str = "") -> int:
    """新增交易记录"""
    amount = price * shares
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        cursor = conn.execute("""
            INSERT INTO trades
              (position_id, code, name, trade_date, direction, price, shares,
               amount, commission, reason, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (position_id, code, name, trade_date, direction, price, shares,
              amount, commission, reason, note, now))
        return cursor.lastrowid


def get_trades(code: str = None, limit: int = 100) -> list[dict]:
    """获取交易记录"""
    sql = "SELECT * FROM trades"
    params = []
    if code:
        sql += " WHERE code = ?"
        params.append(code)
    sql += " ORDER BY trade_date DESC LIMIT ?"
    params.append(limit)

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# account_snapshots 操作（账户快照）
# ─────────────────────────────────────────────

def save_account_snapshot(total_assets: float, cash: float, position_value: float,
                         positions_count: int, total_cost: float, total_pnl: float,
                         pnl_pct: float):
    """保存账户快照"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = date.today().strftime("%Y-%m-%d")

    with get_conn() as conn:
        # 检查今天是否已有快照
        existing = conn.execute(
            "SELECT id FROM account_snapshots WHERE snapshot_date = ?", (today,)
        ).fetchone()

        if existing:
            conn.execute("""
                UPDATE account_snapshots SET
                    total_assets = ?, cash = ?, position_value = ?,
                    positions_count = ?, total_cost = ?, total_pnl = ?,
                    pnl_pct = ?
                WHERE snapshot_date = ?
            """, (total_assets, cash, position_value, positions_count,
                  total_cost, total_pnl, pnl_pct, today))
        else:
            conn.execute("""
                INSERT INTO account_snapshots
                  (snapshot_date, total_assets, cash, position_value, positions_count,
                   total_cost, total_pnl, pnl_pct, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (today, total_assets, cash, position_value, positions_count,
                  total_pnl, pnl_pct, now))


def get_account_snapshots(days: int = 30) -> list[dict]:
    """获取账户快照历史"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM account_snapshots
            ORDER BY snapshot_date DESC
            LIMIT ?
        """, (days,)).fetchall()
    return [dict(r) for r in rows]


def get_latest_snapshot() -> dict | None:
    """获取最新账户快照"""
    with get_conn() as conn:
        row = conn.execute("""
            SELECT * FROM account_snapshots
            ORDER BY snapshot_date DESC LIMIT 1
        """).fetchone()
    return dict(row) if row else None


# ─────────────────────────────────────────────
# 持仓统计
# ─────────────────────────────────────────────

def get_position_summary() -> dict:
    """获取持仓汇总统计（市值实时从 daily_price 表获取）"""
    # 子查询：每只股票在 daily_price 中的最新收盘价
    latest_price_sql = """
        SELECT code, close FROM daily_price a
        WHERE trade_date = (
            SELECT MAX(trade_date) FROM daily_price b WHERE b.code = a.code
        )
    """
    with get_conn() as conn:
        # 持仓中数量
        holding_count = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE status='holding'"
        ).fetchone()[0]

        # 总持仓市值（实时从 daily_price 获取最新价）
        holding_value = conn.execute(f"""
            SELECT COALESCE(SUM(lp.close * p.shares), 0)
            FROM positions p
            LEFT JOIN ({latest_price_sql}) lp ON lp.code = p.code
            WHERE p.status='holding'
        """).fetchone()[0]

        # 总持仓成本
        total_cost = conn.execute("""
            SELECT COALESCE(SUM(entry_price * shares), 0) FROM positions WHERE status='holding'
        """).fetchone()[0]

        # 总盈亏
        total_pnl = holding_value - total_cost
        pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0

        # 已平仓盈亏（平仓时 current_price 被更新为 exit_price）
        closed_pnl = conn.execute("""
            SELECT COALESCE(SUM(
                (current_price - entry_price) * shares
            ), 0) FROM positions WHERE status='closed' AND current_price IS NOT NULL
        """).fetchone()[0]

    return {
        "holding_count": holding_count,
        "holding_value": round(holding_value, 2),
        "total_cost": round(total_cost, 2),
        "total_pnl": round(total_pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "closed_pnl": round(closed_pnl, 2),
    }


# ─────────────────────────────────────────────
# Repository 兼容层（新代码优先从 core.repository 导入）
# ─────────────────────────────────────────────

from core.repository.stock_repo import (
    upsert_stock_list, update_market_cap, batch_update_market_cap,
    get_market_cap_map, get_all_stocks, get_stock_name,
)
from core.repository.price_repo import (
    upsert_daily_price, update_strategy_scores_batch, get_daily_price,
    upsert_index_daily, get_index_daily,
    get_latest_date, get_latest_date_all, has_today_data, get_stock_count_in_db,
)
from core.repository.signal_repo import (
    save_signals, save_scan_signals, get_signals, get_today_signals,
)
from core.repository.position_repo import (
    add_position, close_position, partial_close_position,
    update_position_price, get_positions, get_position_by_id,
    get_position_by_code, delete_position, get_position_summary,
)
from core.repository.trade_repo import (
    add_trade, get_trades,
    save_account_snapshot, get_account_snapshots, get_latest_snapshot,
)
from core.repository.sync_repo import (
    log_sync, get_sync_logs, db_stats,
)
from core.repository.margin_repo import (
    upsert_market_margin, get_market_margin, get_latest_margin_date,
    upsert_margin_detail, get_margin_detail,
)
from core.repository.north_repo import (
    upsert_north_money, get_north_money, get_latest_hsgt_date,
)
from core.repository.futures_repo import (
    upsert_index_futures, get_index_futures, get_latest_futures_date,
)
from core.repository.lhb_repo import (
    upsert_lhb_detail, get_lhb_detail, get_latest_lhb_date,
)
from core.repository.mgmt_repo import (
    upsert_mgmt_holding, get_mgmt_holding, get_latest_mgmt_holding_date,
)


def upsert_strategy_rule(rule: dict) -> int:
    """插入或更新策略规则，返回 rule_id"""
    def _py(val):
        return val.item() if hasattr(val, "item") else val

    with get_conn() as conn:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cur = conn.execute("""
            INSERT INTO strategy_rules
                (rule_name, rule_type, encoding, conditions, sell_conditions,
                 holding_min, holding_max, source, generation, fitness,
                 annual_return, win_rate, sharpe_ratio, max_drawdown,
                 total_trades, signal_overlap, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(rule_name) DO UPDATE SET
                encoding=excluded.encoding, conditions=excluded.conditions,
                sell_conditions=excluded.sell_conditions, fitness=excluded.fitness,
                annual_return=excluded.annual_return, win_rate=excluded.win_rate,
                sharpe_ratio=excluded.sharpe_ratio, max_drawdown=excluded.max_drawdown,
                total_trades=excluded.total_trades, signal_overlap=excluded.signal_overlap,
                updated_at=?
        """, (
            rule["rule_name"], rule["rule_type"], rule.get("encoding", "[]"),
            rule.get("conditions", "[]"), rule.get("sell_conditions", "[]"),
            _py(rule.get("holding_min", 3)), _py(rule.get("holding_max", 20)),
            rule.get("source", "template"), _py(rule.get("generation", 0)),
            _py(rule.get("fitness", 0)), _py(rule.get("annual_return", 0)),
            _py(rule.get("win_rate", 0)), _py(rule.get("sharpe_ratio", 0)),
            _py(rule.get("max_drawdown", 0)), _py(rule.get("total_trades", 0)),
            _py(rule.get("signal_overlap", 0)), now,
        ))
        return cur.lastrowid


def get_active_rules(rule_type: str = None, min_fitness: float = 0.0, limit: int = 200) -> list:
    """获取活跃策略规则列表"""
    with get_conn() as conn:
        sql = "SELECT * FROM strategy_rules WHERE is_active=1"
        params = []
        if rule_type:
            sql += " AND rule_type=?"
            params.append(rule_type)
        if min_fitness > 0:
            sql += " AND fitness>=?"
            params.append(min_fitness)
        sql += " ORDER BY fitness DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def degrade_rule(rule_id: int):
    """降级策略规则"""
    with get_conn() as conn:
        conn.execute("""
            UPDATE strategy_rules SET is_active=0, degraded_at=?
            WHERE id=?
        """, (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), rule_id))


def save_strategy_signal(signal: dict):
    """保存单条信号触发记录"""
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO strategy_signals
                (trade_date, code, rule_id, rule_name, confidence,
                 raw_score, features_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            signal["trade_date"], signal["code"], signal["rule_id"],
            signal.get("rule_name", ""), signal.get("confidence"),
            signal.get("raw_score"), signal.get("features_json", "{}"),
        ))


def get_signals_for_training(start_date: str, end_date: str) -> list:
    """获取带标注的信号用于 LightGBM 训练"""
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT * FROM strategy_signals
            WHERE trade_date BETWEEN ? AND ? AND label_return IS NOT NULL
            ORDER BY trade_date
        """, (start_date, end_date)).fetchall()]


def update_signal_labels(updates: list):
    """批量更新信号的事后标注（未来20日超额收益）"""
    with get_conn() as conn:
        conn.executemany("""
            UPDATE strategy_signals SET label_return=?, is_win=?
            WHERE id=?
        """, updates)


def upsert_active_strategies(selections: list):
    """保存当期活跃策略选择结果"""
    with get_conn() as conn:
        conn.executemany("""
            INSERT INTO active_strategies
                (select_date, rule_id, rule_name, window_60_score,
                 window_120_score, final_score, rank, valid_until)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, selections)


def get_current_active_strategies() -> list:
    """获取当前有效的活跃策略"""
    with get_conn() as conn:
        today = datetime.now().strftime('%Y-%m-%d')
        return [dict(r) for r in conn.execute("""
            SELECT * FROM active_strategies
            WHERE valid_until >= ? AND is_emergency=0
            ORDER BY rank
        """, (today,)).fetchall()]


# ─────────────────────────────────────────────
# 入口：直接运行时打印状态
# ─────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    stats = db_stats()
    print("\n[DB] 当前数据库状态：")
    for k, v in stats.items():
        print(f"  {k}: {v}")
