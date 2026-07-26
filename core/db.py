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
import pandas as pd
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
    conn.execute("PRAGMA busy_timeout=5000")  # 等待锁释放而非立即报错
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
-- 注意：stock_signal 的唯一索引为 (scan_date, code, horizon) 三列，
-- 允许同一交易日同一股票并存短/中/长三条信号。
-- 该索引在下方“三周期迁移”中添加（需先有 horizon 列），此处不再建两列旧索引。
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

CREATE TABLE IF NOT EXISTS strategy_rule_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    conditions TEXT,
    sell_conditions TEXT,
    holding_min INTEGER,
    holding_max INTEGER,
    fitness REAL,
    annual_return REAL,
    win_rate REAL,
    sharpe_ratio REAL,
    max_drawdown REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (rule_id) REFERENCES strategy_rules(id)
);
CREATE INDEX IF NOT EXISTS idx_versions_rule ON strategy_rule_versions(rule_id);

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

-- 每日打分排名表 stock_score 已随「每日打分」功能下线（改用 daily_price.fusion_score）。
-- 既有库的残留表在下方迁移段一次性 DROP。

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

-- 最新行情物化表（消除 JOIN 时的 N+1 关联子查询）
CREATE TABLE IF NOT EXISTS latest_price (
    code        TEXT PRIMARY KEY,
    trade_date  TEXT,
    close       REAL,
    pct_change  REAL,
    high        REAL,
    low         REAL,
    volume      REAL,
    amount      REAL,
    fusion_score REAL
);

-- 推荐结果闭环追踪
CREATE TABLE IF NOT EXISTS recommend_outcome (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL,
    scan_date       TEXT NOT NULL,
    horizon         TEXT DEFAULT 'short',
    strategy        TEXT,
    entry_price     REAL,
    stop_loss       REAL,
    take_profit     REAL,
    fusion_score    REAL,
    t1_return       REAL,
    t3_return       REAL,
    t5_return       REAL,
    t10_return      REAL,
    max_return      REAL,
    min_return      REAL,
    hit_stop        INTEGER DEFAULT 0,
    hit_tp          INTEGER DEFAULT 0,
    exit_reason     TEXT,
    exit_date       TEXT,
    exit_return     REAL,
    evaluated_at    TEXT,
    UNIQUE(code, scan_date, horizon)
);
CREATE INDEX IF NOT EXISTS idx_outcome_scan ON recommend_outcome(scan_date);
CREATE INDEX IF NOT EXISTS idx_outcome_code ON recommend_outcome(code);
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

        # ── 4. 迁移：三周期（horizon）维度 ────────────────────
        # strategy_rules 加 horizon；stock_signal 加 horizon/strategy
        _safe_add_column(conn, "strategy_rules", "horizon", "TEXT")
        _safe_add_column(conn, "stock_signal", "horizon", "TEXT DEFAULT 'short'")
        _safe_add_column(conn, "stock_signal", "strategy", "TEXT")
        # 一次性回填 strategy_rules.horizon（按持仓期推导：<=10 短期，<=60 中期，否则长期）
        try:
            conn.execute("""
                UPDATE strategy_rules SET horizon = CASE
                    WHEN COALESCE(holding_max, 20) <= 10 THEN 'short'
                    WHEN COALESCE(holding_max, 20) <= 60 THEN 'mid'
                    ELSE 'long'
                END
                WHERE horizon IS NULL OR horizon = ''
            """)
        except Exception:
            pass

        # ── 5. 迁移：stock_signal 唯一索引升级为 (scan_date, code, horizon)，
        #    允许同一交易日同一股票同时存在短/中/长三条信号 ────────
        try:
            conn.execute("DROP INDEX IF EXISTS idx_sig_scan_trade_code")
        except Exception:
            pass
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sig_scan_code_horizon "
                "ON stock_signal(scan_date, code, horizon)")
        except Exception:
            pass

        # ── 5. 建唯一索引（重复建表后补充）────────────────────────
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_code_date_uniq "
                "ON daily_price(code, trade_date)")
        except Exception:
            pass

        # ── 6. 迁移：下线「每日打分」，清除残留 stock_score 表 ──────
        try:
            conn.execute("DROP TABLE IF EXISTS stock_score")
        except Exception:
            pass

    print(f"[DB] 数据库初始化完成: {DB_PATH}")


# ─────────────────────────────────────────────
# 自选股（personal_watchlist）查询辅助
# ─────────────────────────────────────────────
def get_watchlist_codes() -> list[str]:
    """
    返回所有自选股代码（去重、按 created_at 升序，便于展示稳定）
    表不存在或为空时返回空列表，绝不抛异常（让同步层走降级分支）
    """
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT code FROM personal_watchlist "
                "WHERE code IS NOT NULL AND code != '' "
                "GROUP BY code ORDER BY MIN(created_at)"
            ).fetchall()
    except Exception:
        # 表尚未创建（init_personal_tables 未跑）—— 视作空自选
        return []
    return [r[0] for r in rows]


def get_watchlist_with_names() -> pd.DataFrame:
    """
    返回自选股的 code / name / market / created_at / note。
    LEFT JOIN stock_info，name 缺失时退化为 code。
    """
    try:
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT w.code, COALESCE(s.name, w.code) AS name,
                       s.market, w.created_at, w.note
                FROM personal_watchlist w
                LEFT JOIN stock_info s ON s.code = w.code
                GROUP BY w.code
                ORDER BY MIN(w.created_at)
            """).fetchall()
    except Exception:
        return pd.DataFrame(columns=["code", "name", "market", "created_at", "note"])
    return pd.DataFrame([dict(r) for r in rows])


def has_watchlist_data(code: str) -> bool:
    """判断 daily_price 中该 code 是否至少有 1 条记录"""
    if not code:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM daily_price WHERE code=? LIMIT 1", (code,)
        ).fetchone()
    return row is not None


def count_watchlist() -> int:
    """返回自选股去重后的数量"""
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT code) FROM personal_watchlist"
            ).fetchone()
    except Exception:
        return 0
    return int(row[0] or 0)


def get_latest_date_for_codes(codes: list[str]) -> str | None:
    """
    返回给定代码子集在 daily_price 中最新的 trade_date（'YYYY-MM-DD'），
    子集为空或全无数据时返回 None。
    """
    if not codes:
        return None
    placeholders = ",".join("?" for _ in codes)
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT MAX(trade_date) FROM daily_price WHERE code IN ({placeholders})",
            codes,
        ).fetchone()
    return row[0] if row and row[0] else None


def get_stock_count_in_db_for_codes(codes: list[str]) -> int:
    """返回子集中在 daily_price 至少有一条数据的 code 数"""
    if not codes:
        return 0
    placeholders = ",".join("?" for _ in codes)
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT COUNT(DISTINCT code) FROM daily_price WHERE code IN ({placeholders})",
            codes,
        ).fetchone()
    return int(row[0] or 0)


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
    refresh_latest_price,
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
from core.repository.rule_repo import (
    upsert_strategy_rule, get_active_rules, degrade_rule,
    save_strategy_signal, get_signals_for_training, update_signal_labels,
    upsert_active_strategies, get_current_active_strategies,
)


# ─────────────────────────────────────────────
# 入口：直接运行时打印状态
# ─────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    stats = db_stats()
    print("\n[DB] 当前数据库状态：")
    for k, v in stats.items():
        print(f"  {k}: {v}")
