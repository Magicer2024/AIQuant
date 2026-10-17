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
from contextlib import contextmanager
from contextvars import ContextVar

_input_connection = ContextVar("frozen_input_connection", default=None)


@contextmanager
def input_connection(conn):
    """计算器借用只读快照连接；上下文内不得另连当前库或提交写入。"""
    if not conn.execute("PRAGMA query_only").fetchone()[0]:
        raise ValueError("冻结输入连接必须为只读")
    token = _input_connection.set(conn)
    try:
        yield conn
    finally:
        _input_connection.reset(token)

from pathlib import Path
from config.settings import DB_PATH  # 兼容别名，测试可 monkeypatch；配置只有一个来源


# ─────────────────────────────────────────────
# 连接管理
# ─────────────────────────────────────────────

def connect_db(*, readonly=False, path=None):
    """统一连接配置；只读连接不创建文件、不设置 WAL、不迁移。"""
    target = Path(path or DB_PATH).resolve()
    conn = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=30) if readonly else sqlite3.connect(str(target), timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        if readonly:
            conn.execute("PRAGMA query_only=ON")
        else:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        return conn
    except Exception:
        conn.close()
        raise


@contextmanager
def get_conn(*, readonly=False, path=None):
    frozen = _input_connection.get()
    if frozen is not None:
        if path is not None:
            raise ValueError("冻结计算禁止绕过输入快照连接")
        yield frozen
        return
    conn = connect_db(readonly=readonly, path=path)
    try:
        yield conn
        if not readonly:
            conn.commit()
    except Exception:
        if not readonly:
            conn.rollback()
        raise
    finally:
        conn.close()


# ─────────────────────────────────────────────
# 迁移辅助：安全地给表添加列（列已存在时自动跳过）
# ─────────────────────────────────────────────
def _safe_add_column(conn, table: str, column: str, col_type: str):
    """给表添加列，如果列已存在则什么都不做（SQLite 不支持 IF NOT EXISTS for columns）"""
    import re
    if not all(re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", s) for s in (table, column)):
        raise ValueError("非法表名或列名")
    columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
    if column not in columns:
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {col_type}')


# ─────────────────────────────────────────────
# 建表（首次运行自动创建）
# ─────────────────────────────────────────────

def _apply_versioned_migration(conn, version, sql):
    """在调用方事务内逐条执行；包括触发器，不使用隐式提交的 executescript。"""
    if conn.execute("SELECT 1 FROM schema_migration WHERE version=?", (version,)).fetchone():
        return
    statement = ""
    for line in sql.splitlines():
        statement += line + "\n"
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError(f"迁移 {version} 包含不完整 SQL")
    conn.execute("INSERT INTO schema_migration(version) VALUES (?)", (version,))


_UNIFIED_MODEL_SQL = """
CREATE TABLE signal_run (
    id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
    scan_date TEXT NOT NULL, as_of TEXT NOT NULL,
    run_type TEXT NOT NULL CHECK(run_type IN ('live','replay','legacy_import')),
    scope TEXT NOT NULL, scope_json TEXT NOT NULL,
    code_version_json TEXT NOT NULL, params_json TEXT NOT NULL,
    params_hash TEXT NOT NULL, input_manifest_json TEXT NOT NULL,
    input_hash TEXT NOT NULL, quality_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','complete','partial','failed')),
    content_hash TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);
CREATE INDEX idx_run_date_scope ON signal_run(scan_date,scope,status);
CREATE TABLE signal_event (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES signal_run(id),
    scan_date TEXT NOT NULL, code TEXT NOT NULL, horizon TEXT NOT NULL,
    strategy_key TEXT NOT NULL, strategy TEXT, signal_kind TEXT NOT NULL,
    signal_reference_price REAL, reference_kind TEXT NOT NULL,
    entry_target REAL, stop_loss REAL, take_profit REAL,
    raw_score REAL, score_scale TEXT NOT NULL,
    payload_json TEXT NOT NULL, content_hash TEXT NOT NULL,
    UNIQUE(run_id,code,horizon,strategy_key,signal_kind)
);
CREATE INDEX idx_event_date_code_strategy ON signal_event(scan_date,code,strategy_key);
CREATE TABLE recommendation_batch (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES signal_run(id),
    scan_date TEXT NOT NULL, list_key TEXT NOT NULL,
    cohort TEXT NOT NULL CHECK(cohort IN ('production','observation','shadow')),
    experiment_id TEXT NOT NULL DEFAULT '', policy_json TEXT NOT NULL,
    policy_hash TEXT NOT NULL, market_state TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1, published_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('draft','published','superseded','comparison')),
    source TEXT NOT NULL, exclusions_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE(run_id,list_key,cohort,experiment_id,policy_hash)
);
CREATE UNIQUE INDEX idx_batch_active ON recommendation_batch(scan_date,list_key,cohort,experiment_id)
    WHERE status='published';
CREATE INDEX idx_batch_status ON recommendation_batch(status,scan_date,list_key);
CREATE TABLE recommendation_item (
    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES recommendation_batch(id),
    signal_id TEXT NOT NULL REFERENCES signal_event(id), code TEXT NOT NULL,
    horizon TEXT NOT NULL, rank INTEGER NOT NULL CHECK(rank>0),
    reason_json TEXT NOT NULL, plan_json TEXT NOT NULL, support_json TEXT NOT NULL,
    content_hash TEXT NOT NULL, UNIQUE(batch_id,code,horizon), UNIQUE(batch_id,rank)
);
CREATE TABLE signal_outcome (
    signal_id TEXT NOT NULL REFERENCES signal_event(id), definition_version TEXT NOT NULL,
    reference_price REAL, t1_return REAL, t2_return REAL, t3_return REAL,
    t5_return REAL, t10_return REAL, evaluated_as_of TEXT,
    status TEXT NOT NULL DEFAULT 'data_pending',
    PRIMARY KEY(signal_id,definition_version)
);
CREATE TABLE simulated_trade (
    id TEXT PRIMARY KEY, first_recommendation_id TEXT NOT NULL UNIQUE REFERENCES recommendation_item(id),
    code TEXT NOT NULL, horizon TEXT NOT NULL, list_key TEXT NOT NULL,
    cohort TEXT NOT NULL CHECK(cohort IN ('production','observation','shadow')),
    experiment_id TEXT NOT NULL DEFAULT '', execution_json TEXT NOT NULL,
    merge_policy TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('watching','holding','closed','expired','cancelled','data_pending')),
    exec_entry_date TEXT, exec_entry_price REAL, exec_exit_date TEXT, exec_exit_price REAL,
    gross_return REAL, net_return REAL, last_evaluated_as_of TEXT, evaluation_version TEXT,
    state_json TEXT NOT NULL DEFAULT '{}',
    CHECK(exec_entry_price IS NOT NULL OR (gross_return IS NULL AND net_return IS NULL)),
    CHECK(status NOT IN ('holding','closed') OR
          (exec_entry_date IS NOT NULL AND exec_entry_price IS NOT NULL AND exec_entry_price>0)),
    CHECK(status!='closed' OR
          (exec_exit_date IS NOT NULL AND exec_exit_price IS NOT NULL
           AND exec_exit_price>0 AND exec_exit_date>exec_entry_date))
);
CREATE INDEX idx_simulated_status_asof ON simulated_trade(status,last_evaluated_as_of);
CREATE TABLE simulated_trade_event (
    id TEXT PRIMARY KEY, trade_id TEXT NOT NULL REFERENCES simulated_trade(id),
    recommendation_id TEXT REFERENCES recommendation_item(id),
    event_date TEXT NOT NULL, event_key TEXT NOT NULL UNIQUE,
    event_kind TEXT NOT NULL, payload_json TEXT NOT NULL, content_hash TEXT NOT NULL
);
CREATE INDEX idx_trade_event_date ON simulated_trade_event(trade_id,event_date);
CREATE TRIGGER protect_finished_run_update BEFORE UPDATE ON signal_run
WHEN OLD.status!='running'
BEGIN SELECT RAISE(ABORT,'已结束运行不可修改'); END;
CREATE TRIGGER protect_run_delete BEFORE DELETE ON signal_run
BEGIN SELECT RAISE(ABORT,'运行记录不可删除'); END;
CREATE TRIGGER protect_signal_insert BEFORE INSERT ON signal_event
WHEN (SELECT status FROM signal_run WHERE id=NEW.run_id)!='running'
BEGIN SELECT RAISE(ABORT,'已结束运行不可追加信号'); END;
CREATE TRIGGER protect_signal_update BEFORE UPDATE ON signal_event
BEGIN SELECT RAISE(ABORT,'信号不可修改'); END;
CREATE TRIGGER protect_signal_delete BEFORE DELETE ON signal_event
BEGIN SELECT RAISE(ABORT,'信号不可删除'); END;
CREATE TRIGGER protect_item_insert BEFORE INSERT ON recommendation_item
WHEN (SELECT status FROM recommendation_batch WHERE id=NEW.batch_id)!='draft'
BEGIN SELECT RAISE(ABORT,'冻结批次不可追加推荐'); END;
CREATE TRIGGER protect_item_update BEFORE UPDATE ON recommendation_item
BEGIN SELECT RAISE(ABORT,'推荐条目不可修改'); END;
CREATE TRIGGER protect_item_delete BEFORE DELETE ON recommendation_item
BEGIN SELECT RAISE(ABORT,'推荐条目不可删除'); END;
CREATE TRIGGER protect_batch_delete BEFORE DELETE ON recommendation_batch
BEGIN SELECT RAISE(ABORT,'批次不可删除'); END;
CREATE TRIGGER protect_batch_content BEFORE UPDATE ON recommendation_batch
WHEN NEW.run_id IS NOT OLD.run_id OR NEW.scan_date IS NOT OLD.scan_date
 OR NEW.list_key IS NOT OLD.list_key OR NEW.cohort IS NOT OLD.cohort
 OR NEW.experiment_id IS NOT OLD.experiment_id OR NEW.policy_json IS NOT OLD.policy_json
 OR NEW.policy_hash IS NOT OLD.policy_hash OR NEW.content_hash IS NOT OLD.content_hash
 OR NEW.market_state IS NOT OLD.market_state OR NEW.source IS NOT OLD.source
 OR NEW.exclusions_json IS NOT OLD.exclusions_json OR NEW.revision IS NOT OLD.revision
 OR NOT ((OLD.status='draft' AND NEW.status IN ('published','comparison'))
         OR (OLD.status='published' AND NEW.status='superseded'))
BEGIN SELECT RAISE(ABORT,'批次内容或状态变更不合法'); END;
CREATE TRIGGER validate_signal_scope BEFORE INSERT ON signal_event
WHEN NEW.scan_date IS NOT (SELECT scan_date FROM signal_run WHERE id=NEW.run_id)
BEGIN SELECT RAISE(ABORT,'信号日期与运行不一致'); END;
CREATE TRIGGER validate_batch_scope BEFORE INSERT ON recommendation_batch
WHEN NEW.scan_date IS NOT (SELECT scan_date FROM signal_run WHERE id=NEW.run_id)
BEGIN SELECT RAISE(ABORT,'批次日期与运行不一致'); END;
CREATE TRIGGER validate_item_identity BEFORE INSERT ON recommendation_item
WHEN NOT EXISTS (
    SELECT 1 FROM signal_event s JOIN recommendation_batch b ON b.run_id=s.run_id
    WHERE s.id=NEW.signal_id AND b.id=NEW.batch_id
      AND s.code=NEW.code AND s.horizon=NEW.horizon)
BEGIN SELECT RAISE(ABORT,'推荐与信号身份不一致'); END;
CREATE TRIGGER protect_trade_event_update BEFORE UPDATE ON simulated_trade_event
BEGIN SELECT RAISE(ABORT,'交易事件不可修改'); END;
CREATE TRIGGER protect_trade_event_delete BEFORE DELETE ON simulated_trade_event
BEGIN SELECT RAISE(ABORT,'交易事件不可删除'); END;
"""


# 003 —— 任务持久化、租约与流水线阶段编排（方案 D）。
# 设计不变式：
#   - background_task 是所有异步/定时/CLI 任务的唯一状态来源；旧内存 _tasks 仅作轻量线程池缓存。
#   - 状态固定枚举；已结束任务不可复活（重试创建新任务并以 retry_of 关联，不改旧任务）。
#   - resource_group='pipeline_write' 串行化对主数据的并发修改；租约用心跳续期、过期阈值回收，
#     owner_token 防止失去租约的旧执行器继续写入。
#   - task_stage 记录每阶段状态/耗时/输入输出数量/数据日期/失败原因，支撑 partial_success 与
#     「仅重跑失败阶段及下游」。阶段可更新（重试），但不可删除（审计留痕）。
_TASK_ORCHESTRATION_SQL = """
CREATE TABLE background_task (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE,
    task_type TEXT NOT NULL,
    parent_id TEXT REFERENCES background_task(id),
    resource_group TEXT,
    input_json TEXT NOT NULL DEFAULT '{}',
    input_version TEXT,
    status TEXT NOT NULL CHECK(status IN
        ('pending','running','cancel_requested','success','partial_success',
         'error','cancelled','interrupted')),
    stage TEXT,
    progress REAL NOT NULL DEFAULT 0,
    message TEXT,
    owner_token TEXT,
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    result_json TEXT,
    error TEXT,
    retry_of TEXT REFERENCES background_task(id),
    retry_count INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX idx_task_status ON background_task(status);
CREATE INDEX idx_task_resource ON background_task(resource_group,status);
CREATE INDEX idx_task_type_status ON background_task(task_type,status);
CREATE TABLE scheduled_job (
    id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    schedule_kind TEXT NOT NULL CHECK(schedule_kind IN ('daily','interval','once')),
    at_time TEXT,
    interval_seconds INTEGER,
    input_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_at TEXT,
    last_status TEXT,
    last_task_id TEXT REFERENCES background_task(id),
    next_run_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT,
    UNIQUE(job_type,at_time)
);
CREATE INDEX idx_job_enabled ON scheduled_job(enabled,schedule_kind);
CREATE TABLE task_stage (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES background_task(id),
    stage_key TEXT NOT NULL,
    seq INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','running','success','skipped','error')),
    input_hash TEXT,
    output_count INTEGER,
    data_date TEXT,
    error TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT,
    finished_at TEXT,
    elapsed_ms INTEGER,
    UNIQUE(task_id,stage_key)
);
CREATE INDEX idx_stage_task ON task_stage(task_id,seq);
CREATE TRIGGER protect_finished_task_update BEFORE UPDATE ON background_task
WHEN OLD.status IN ('success','partial_success','error','cancelled','interrupted')
 AND NEW.status IN ('pending','running','cancel_requested')
BEGIN SELECT RAISE(ABORT,'已结束任务不可复活'); END;
CREATE TRIGGER protect_stage_delete BEFORE DELETE ON task_stage
BEGIN SELECT RAISE(ABORT,'阶段记录不可删除'); END;
"""

# 005 —— JH4 纪律装置包 + L09' 封存盲核（T2/T4，spec: shared/specs/JH4_spec.md §7）。
#   不可逆设计：payload 字段一经写入不可改（触发器逐列 IS NOT 白名单），
#   仅状态机推进字段可更新；修订 = 新行 + parent_id。哈希链见 core/discipline.py。
_005_JH4_SQL = """
CREATE TABLE trade_plan (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_id INTEGER, revision INTEGER NOT NULL DEFAULT 1,
  code TEXT NOT NULL, name TEXT,
  status TEXT NOT NULL CHECK(status IN ('draft','alive','invalidated','stale','closed_won','closed_dead')),
  thesis TEXT NOT NULL CHECK(length(thesis) BETWEEN 15 AND 500),
  delta_note TEXT,
  entry_trigger_json TEXT NOT NULL,
  expected_return_pct REAL NOT NULL,
  expected_horizon_days INTEGER NOT NULL CHECK(expected_horizon_days BETWEEN 1 AND 60),
  confidence REAL NOT NULL CHECK(confidence BETWEEN 0.05 AND 0.95),
  position_pct REAL NOT NULL CHECK(position_pct > 0 AND position_pct <= 1),
  risk_params_json TEXT NOT NULL,
  params_source TEXT CHECK(params_source IN ('calibrated','default','manual')),
  calibration_id INTEGER,
  regime_at_plan TEXT, atr_stop_at_plan REAL,
  content_hash TEXT NOT NULL, prev_hash TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at TEXT
);
CREATE INDEX idx_plan_code_status ON trade_plan(code, status);
CREATE TABLE plan_anchor (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id INTEGER NOT NULL REFERENCES trade_plan(id),
  anchor_kind TEXT NOT NULL CHECK(anchor_kind IN
    ('price_below','drawdown_from_entry','giveback_from_peak','deadline','indicator_below','unlock_event')),
  indicator TEXT, comparator TEXT,
  threshold_num REAL NOT NULL,
  unit TEXT,
  state TEXT NOT NULL DEFAULT 'alive' CHECK(state IN ('alive','dead','pending_data')),
  died_at TEXT, last_checked_at TEXT,
  check_expr_json TEXT NOT NULL
);
CREATE INDEX idx_anchor_plan_state ON plan_anchor(plan_id, state);
CREATE TABLE deviation_ticket (
  id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER,
  code TEXT NOT NULL, action TEXT NOT NULL, qty INTEGER NOT NULL,
  price_ref REAL NOT NULL, amount_ref REAL NOT NULL,
  deviation_rules_json TEXT NOT NULL,
  tier TEXT NOT NULL DEFAULT '24h' CHECK(tier IN ('24h','soft_hold')),
  intent_reason TEXT,
  submitted_at TEXT NOT NULL, release_at TEXT NOT NULL, expire_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN
    ('pending','released','expired','voided_drift','withdrawn','executed','abandoned')),
  retry_of INTEGER, drift_pct REAL, void_reason TEXT, executed_trade_id INTEGER,
  content_hash TEXT NOT NULL, prev_hash TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_ticket_status_submitted ON deviation_ticket(status, submitted_at);
CREATE INDEX idx_ticket_code ON deviation_ticket(code, submitted_at);
CREATE TABLE case_file (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id INTEGER NOT NULL REFERENCES deviation_ticket(id),
  code TEXT NOT NULL, outcome TEXT NOT NULL,
  intent_reason TEXT, ref_price REAL, ref_amount REAL, qty INTEGER,
  exec_ret_5 REAL, exec_ret_10 REAL, exec_ret_20 REAL,
  counterfactual_ret_5 REAL, counterfactual_ret_10 REAL, counterfactual_ret_20 REAL,
  worst_adverse_ret REAL, regime_at_submit TEXT,
  archived_at TEXT NOT NULL, content_hash TEXT NOT NULL, prev_hash TEXT
);
CREATE INDEX idx_case_ticket ON case_file(ticket_id);
CREATE TABLE discipline_violation (
  id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, ticket_id INTEGER,
  kind TEXT NOT NULL, detected_at TEXT NOT NULL, note TEXT
);
CREATE TABLE risk_param_calibration (
  id INTEGER PRIMARY KEY AUTOINCREMENT, as_of TEXT NOT NULL,
  regime_stratum TEXT NOT NULL, n_samples INTEGER NOT NULL,
  m_p25 REAL, m_p50 REAL, m_p75 REAL, a_p50 REAL,
  stop_loss_pct REAL, max_single_position_pct REAL, median_cut_loss_yuan REAL,
  sample_ids_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('calibrated','pooled','low_sample','uncalibrated')),
  params_hash TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE sealed_decision (
  id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, ticket_id INTEGER,
  code TEXT NOT NULL, action TEXT NOT NULL, qty INTEGER NOT NULL, ref_price REAL NOT NULL,
  triggers_json TEXT NOT NULL,
  thesis_snapshot TEXT NOT NULL,
  expected_return_pct REAL NOT NULL, expected_horizon_days INTEGER NOT NULL,
  confidence REAL NOT NULL CHECK(confidence BETWEEN 0.05 AND 0.95),
  worst_case_pct REAL NOT NULL,
  falsifier TEXT NOT NULL CHECK(length(falsifier) >= 15),
  seal_note TEXT,
  status TEXT NOT NULL DEFAULT 'sealed' CHECK(status IN ('sealed','due','blinding','reconciled')),
  due_on TEXT NOT NULL,
  sealed_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  content_hash TEXT NOT NULL, prev_hash TEXT
);
CREATE INDEX idx_seal_status_due ON sealed_decision(status, due_on);
CREATE TABLE sealed_blind_check (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sealed_id INTEGER NOT NULL REFERENCES sealed_decision(id),
  result_metrics_json TEXT NOT NULL,
  recall_verdict TEXT NOT NULL CHECK(recall_verdict IN ('yes','no','partial')),
  recall_expected_return_pct REAL NOT NULL, recall_reason TEXT NOT NULL,
  answered_at TEXT NOT NULL,
  gap_expected REAL, confidence_hit INTEGER, worst_case_hit INTEGER,
  explanation_diverged REAL, revealed_at TEXT,
  UNIQUE(sealed_id, answered_at)
);
CREATE TRIGGER tq_trade_plan_update BEFORE UPDATE ON trade_plan
WHEN NEW.parent_id IS NOT OLD.parent_id OR NEW.revision IS NOT OLD.revision
  OR NEW.code IS NOT OLD.code OR NEW.name IS NOT OLD.name
  OR NEW.thesis IS NOT OLD.thesis OR NEW.delta_note IS NOT OLD.delta_note
  OR NEW.entry_trigger_json IS NOT OLD.entry_trigger_json
  OR NEW.expected_return_pct IS NOT OLD.expected_return_pct
  OR NEW.expected_horizon_days IS NOT OLD.expected_horizon_days
  OR NEW.confidence IS NOT OLD.confidence OR NEW.position_pct IS NOT OLD.position_pct
  OR NEW.risk_params_json IS NOT OLD.risk_params_json
  OR NEW.params_source IS NOT OLD.params_source
  OR NEW.calibration_id IS NOT OLD.calibration_id
  OR NEW.regime_at_plan IS NOT OLD.regime_at_plan
  OR NEW.atr_stop_at_plan IS NOT OLD.atr_stop_at_plan
  OR NEW.content_hash IS NOT OLD.content_hash OR NEW.prev_hash IS NOT OLD.prev_hash
  OR NEW.created_at IS NOT OLD.created_at
  OR NOT (
      OLD.status = NEW.status
      OR (OLD.status='draft' AND NEW.status='alive')
      OR (OLD.status='alive' AND NEW.status IN ('invalidated','stale','closed_won','closed_dead'))
      OR (OLD.status='stale' AND NEW.status IN ('alive','invalidated','closed_won','closed_dead'))
      OR (OLD.status='invalidated' AND NEW.status='closed_dead')
  )
BEGIN SELECT RAISE(ABORT, 'trade_plan 业务字段不可变，修订请插入新 revision 行'); END;
CREATE TRIGGER tq_trade_plan_delete BEFORE DELETE ON trade_plan
BEGIN SELECT RAISE(ABORT, 'trade_plan 不可删除（append-only）'); END;
CREATE TRIGGER tq_plan_anchor_update BEFORE UPDATE ON plan_anchor
WHEN NEW.plan_id IS NOT OLD.plan_id OR NEW.anchor_kind IS NOT OLD.anchor_kind
  OR NEW.indicator IS NOT OLD.indicator OR NEW.comparator IS NOT OLD.comparator
  OR NEW.threshold_num IS NOT OLD.threshold_num OR NEW.unit IS NOT OLD.unit
  OR NEW.check_expr_json IS NOT OLD.check_expr_json
  OR NOT (
      OLD.state = NEW.state
      OR (OLD.state='alive' AND NEW.state IN ('dead','pending_data'))
      OR (OLD.state='pending_data' AND NEW.state IN ('alive','dead'))
  )
BEGIN SELECT RAISE(ABORT, 'plan_anchor 定义不可变，仅存活状态可推进'); END;
CREATE TRIGGER tq_plan_anchor_delete BEFORE DELETE ON plan_anchor
BEGIN SELECT RAISE(ABORT, 'plan_anchor 不可删除'); END;
CREATE TRIGGER tq_ticket_update BEFORE UPDATE ON deviation_ticket
WHEN NEW.plan_id IS NOT OLD.plan_id OR NEW.code IS NOT OLD.code
  OR NEW.action IS NOT OLD.action OR NEW.qty IS NOT OLD.qty
  OR NEW.price_ref IS NOT OLD.price_ref OR NEW.amount_ref IS NOT OLD.amount_ref
  OR NEW.deviation_rules_json IS NOT OLD.deviation_rules_json
  OR NEW.tier IS NOT OLD.tier OR NEW.intent_reason IS NOT OLD.intent_reason
  OR NEW.submitted_at IS NOT OLD.submitted_at OR NEW.release_at IS NOT OLD.release_at
  OR NEW.expire_at IS NOT OLD.expire_at OR NEW.retry_of IS NOT OLD.retry_of
  OR (NEW.drift_pct IS NOT OLD.drift_pct AND OLD.drift_pct IS NOT NULL)
  OR (NEW.void_reason IS NOT OLD.void_reason AND OLD.void_reason IS NOT NULL)
  OR NEW.content_hash IS NOT OLD.content_hash OR NEW.prev_hash IS NOT OLD.prev_hash
  OR NEW.created_at IS NOT OLD.created_at
  OR NOT (
      OLD.status = NEW.status
      OR (OLD.status='pending' AND NEW.status IN ('released','expired','voided_drift','withdrawn'))
      OR (OLD.status='released' AND NEW.status IN ('executed','abandoned','voided_drift'))
  )
  OR (NEW.executed_trade_id IS NOT OLD.executed_trade_id
      AND NOT (OLD.executed_trade_id IS NULL AND NEW.status='executed'))
BEGIN SELECT RAISE(ABORT, 'deviation_ticket 内容不可变，仅状态机推进与 drift/void 一次性回填'); END;
CREATE TRIGGER tq_ticket_delete BEFORE DELETE ON deviation_ticket
BEGIN SELECT RAISE(ABORT, 'deviation_ticket 不可删除'); END;
CREATE TRIGGER tq_case_update BEFORE UPDATE ON case_file
WHEN NEW.ticket_id IS NOT OLD.ticket_id OR NEW.code IS NOT OLD.code
  OR NEW.outcome IS NOT OLD.outcome OR NEW.intent_reason IS NOT OLD.intent_reason
  OR NEW.ref_price IS NOT OLD.ref_price OR NEW.ref_amount IS NOT OLD.ref_amount
  OR NEW.qty IS NOT OLD.qty OR NEW.regime_at_submit IS NOT OLD.regime_at_submit
  OR NEW.archived_at IS NOT OLD.archived_at
  OR NEW.content_hash IS NOT OLD.content_hash OR NEW.prev_hash IS NOT OLD.prev_hash
  OR NOT (
      (NEW.exec_ret_5 IS NOT OLD.exec_ret_5 AND OLD.exec_ret_5 IS NULL)
      OR (NEW.exec_ret_10 IS NOT OLD.exec_ret_10 AND OLD.exec_ret_10 IS NULL)
      OR (NEW.exec_ret_20 IS NOT OLD.exec_ret_20 AND OLD.exec_ret_20 IS NULL)
      OR (NEW.counterfactual_ret_5 IS NOT OLD.counterfactual_ret_5 AND OLD.counterfactual_ret_5 IS NULL)
      OR (NEW.counterfactual_ret_10 IS NOT OLD.counterfactual_ret_10 AND OLD.counterfactual_ret_10 IS NULL)
      OR (NEW.counterfactual_ret_20 IS NOT OLD.counterfactual_ret_20 AND OLD.counterfactual_ret_20 IS NULL)
      OR (NEW.worst_adverse_ret IS NOT OLD.worst_adverse_ret AND OLD.worst_adverse_ret IS NULL)
      OR (NEW.exec_ret_5 IS OLD.exec_ret_5 AND NEW.exec_ret_10 IS OLD.exec_ret_10
          AND NEW.exec_ret_20 IS OLD.exec_ret_20 AND NEW.counterfactual_ret_5 IS OLD.counterfactual_ret_5
          AND NEW.counterfactual_ret_10 IS OLD.counterfactual_ret_10 AND NEW.counterfactual_ret_20 IS OLD.counterfactual_ret_20
          AND NEW.worst_adverse_ret IS OLD.worst_adverse_ret)
  )
BEGIN SELECT RAISE(ABORT, 'case_file 仅反事实/盈亏列可从 NULL 回填，其余不可变'); END;
CREATE TRIGGER tq_case_delete BEFORE DELETE ON case_file
BEGIN SELECT RAISE(ABORT, 'case_file 不可删除（判例档案）'); END;
CREATE TRIGGER tq_violation_update BEFORE UPDATE ON discipline_violation
BEGIN SELECT RAISE(ABORT, 'discipline_violation 只增不改'); END;
CREATE TRIGGER tq_violation_delete BEFORE DELETE ON discipline_violation
BEGIN SELECT RAISE(ABORT, 'discipline_violation 只增不删'); END;
CREATE TRIGGER tq_calibration_update BEFORE UPDATE ON risk_param_calibration
BEGIN SELECT RAISE(ABORT, '校准行不可修改，重算请写新行'); END;
CREATE TRIGGER tq_calibration_delete BEFORE DELETE ON risk_param_calibration
BEGIN SELECT RAISE(ABORT, '校准历史不可删除'); END;
CREATE TRIGGER tq_sealed_update BEFORE UPDATE ON sealed_decision
WHEN NEW.plan_id IS NOT OLD.plan_id OR NEW.ticket_id IS NOT OLD.ticket_id
  OR NEW.code IS NOT OLD.code OR NEW.action IS NOT OLD.action
  OR NEW.qty IS NOT OLD.qty OR NEW.ref_price IS NOT OLD.ref_price
  OR NEW.triggers_json IS NOT OLD.triggers_json
  OR NEW.thesis_snapshot IS NOT OLD.thesis_snapshot
  OR NEW.expected_return_pct IS NOT OLD.expected_return_pct
  OR NEW.expected_horizon_days IS NOT OLD.expected_horizon_days
  OR NEW.confidence IS NOT OLD.confidence OR NEW.worst_case_pct IS NOT OLD.worst_case_pct
  OR NEW.falsifier IS NOT OLD.falsifier OR NEW.seal_note IS NOT OLD.seal_note
  OR NEW.due_on IS NOT OLD.due_on OR NEW.sealed_at IS NOT OLD.sealed_at
  OR NEW.content_hash IS NOT OLD.content_hash OR NEW.prev_hash IS NOT OLD.prev_hash
  OR NOT (
      OLD.status = NEW.status
      OR (OLD.status='sealed' AND NEW.status='due')
      OR (OLD.status='due' AND NEW.status='blinding')
      OR (OLD.status='blinding' AND NEW.status='reconciled')
  )
BEGIN SELECT RAISE(ABORT, 'sealed_decision 封存内容不可变，仅状态可推进'); END;
CREATE TRIGGER tq_sealed_delete BEFORE DELETE ON sealed_decision
BEGIN SELECT RAISE(ABORT, 'sealed_decision 不可删除'); END;
CREATE TRIGGER tq_blind_update BEFORE UPDATE ON sealed_blind_check
WHEN NEW.sealed_id IS NOT OLD.sealed_id OR NEW.result_metrics_json IS NOT OLD.result_metrics_json
  OR NEW.recall_verdict IS NOT OLD.recall_verdict
  OR NEW.recall_expected_return_pct IS NOT OLD.recall_expected_return_pct
  OR NEW.recall_reason IS NOT OLD.recall_reason OR NEW.answered_at IS NOT OLD.answered_at
  OR NOT (
      (NEW.gap_expected IS NOT OLD.gap_expected AND OLD.gap_expected IS NULL)
      OR (NEW.confidence_hit IS NOT OLD.confidence_hit AND OLD.confidence_hit IS NULL)
      OR (NEW.worst_case_hit IS NOT OLD.worst_case_hit AND OLD.worst_case_hit IS NULL)
      OR (NEW.explanation_diverged IS NOT OLD.explanation_diverged AND OLD.explanation_diverged IS NULL)
      OR (NEW.revealed_at IS NOT OLD.revealed_at AND OLD.revealed_at IS NULL)
      OR (NEW.gap_expected IS OLD.gap_expected AND NEW.confidence_hit IS OLD.confidence_hit
          AND NEW.worst_case_hit IS OLD.worst_case_hit AND NEW.explanation_diverged IS OLD.explanation_diverged
          AND NEW.revealed_at IS OLD.revealed_at)
  )
BEGIN SELECT RAISE(ABORT, '盲核作答不可改，仅对照列可一次写入'); END;
CREATE TRIGGER tq_blind_delete BEFORE DELETE ON sealed_blind_check
BEGIN SELECT RAISE(ABORT, '盲核记录不可删除'); END;
"""

# 006 —— L06 30 分钟验证闭环存储（T3，spec: shared/specs/L06_spec.md §8）。
#   分表分域：纪律装置管下单决策，L06 管想法验证，统计口径互不污染。
#   triage_log 计时留痕一次写入不可改（elapsed 服务端算，R4 防客户端伪报）。
_006_L06_SQL = """
CREATE TABLE idea (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL CHECK(length(title) BETWEEN 4 AND 120),
  hypothesis TEXT NOT NULL,
  conditions_json TEXT NOT NULL,
  indicators_json TEXT NOT NULL,
  implied_hold_days INTEGER,
  status TEXT NOT NULL DEFAULT 'new'
    CHECK(status IN ('new','triaging','passed','failed','deferred','timeout','run_complete')),
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_idea_status_created ON idea(status, created_at);
CREATE TABLE triage_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idea_id INTEGER NOT NULL REFERENCES idea(id),
  triage_attempt INTEGER NOT NULL DEFAULT 1 CHECK(triage_attempt BETWEEN 1 AND 2),
  started_at TEXT NOT NULL, decided_at TEXT NOT NULL,
  elapsed_seconds INTEGER NOT NULL,
  verdict TEXT NOT NULL CHECK(verdict IN ('pass','fail','defer','timeout')),
  death_step TEXT, checks_json TEXT NOT NULL,
  defer_reason TEXT, overlap_rule_id INTEGER,
  UNIQUE(idea_id, triage_attempt)
);
CREATE TABLE idea_run_bundle (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idea_id INTEGER NOT NULL REFERENCES idea(id),
  triage_id INTEGER REFERENCES triage_log(id),
  combos_json TEXT NOT NULL,
  death_step TEXT NOT NULL, conclusion_text TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  report_md_path TEXT, report_json_path TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_bundle_idea ON idea_run_bundle(idea_id);
CREATE TRIGGER tq_triage_update BEFORE UPDATE ON triage_log
BEGIN SELECT RAISE(ABORT, 'triage_log 计时留痕不可修改'); END;
CREATE TRIGGER tq_triage_delete BEFORE DELETE ON triage_log
BEGIN SELECT RAISE(ABORT, 'triage_log 不可删除'); END;
CREATE TRIGGER tq_idea_content BEFORE UPDATE ON idea
WHEN NEW.title IS NOT OLD.title OR NEW.hypothesis IS NOT OLD.hypothesis
  OR NEW.conditions_json IS NOT OLD.conditions_json
  OR NEW.indicators_json IS NOT OLD.indicators_json
  OR NEW.implied_hold_days IS NOT OLD.implied_hold_days
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'idea 内容不可变，仅 status 可推进'); END;
CREATE TRIGGER tq_idea_delete BEFORE DELETE ON idea
BEGIN SELECT RAISE(ABORT, 'idea 不可删除'); END;
CREATE TRIGGER tq_bundle_update BEFORE UPDATE ON idea_run_bundle
WHEN NEW.idea_id IS NOT OLD.idea_id OR NEW.triage_id IS NOT OLD.triage_id
  OR NEW.death_step IS NOT OLD.death_step OR NEW.conclusion_text IS NOT OLD.conclusion_text
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'bundle 死因结论一经落定不可改'); END;
CREATE TRIGGER tq_bundle_delete BEFORE DELETE ON idea_run_bundle
BEGIN SELECT RAISE(ABORT, 'idea_run_bundle 不可删除'); END;
"""

# L06 运行态（T3，量化策略工程师）：006 已给分诊留痕与 bundle 终稿两张不可变表，
# 但缺「在途」载体——bundle 的 death_step/conclusion_text 一经写入不可改（正确），
# 所以 bundle 行只能在三跑全部就绪后一次性插入。本迁移补两张表：
#   l06_triage_session：每个 attempt 的计时起点（§2.1：attempt=2 有全新 1800s，
#     起点不能取 idea.created_at，也不能信客户端传来，必须由服务端首次记录）
#   l06_run：一题三跑的在途任务清单（combos 一经写入不可改，只允许推进 status/finished_at）
# 只新增表与新迁移，不改 005/006 既有表与触发器，历史数字零触碰。
_007_L06_RUN_STATE_SQL = """
CREATE TABLE l06_triage_session (
  idea_id INTEGER NOT NULL REFERENCES idea(id),
  attempt INTEGER NOT NULL CHECK(attempt IN (1,2)),
  started_at TEXT NOT NULL,
  deadline_at TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (idea_id, attempt)
);
CREATE INDEX idx_l06_session_deadline ON l06_triage_session(deadline_at);
CREATE TRIGGER tq_l06_session_update BEFORE UPDATE ON l06_triage_session
BEGIN SELECT RAISE(ABORT, 'l06 分诊计时起点一经写入不可改'); END;
CREATE TRIGGER tq_l06_session_delete BEFORE DELETE ON l06_triage_session
BEGIN SELECT RAISE(ABORT, 'l06 分诊计时留痕不可删除'); END;
CREATE TABLE l06_run (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idea_id INTEGER NOT NULL REFERENCES idea(id),
  triage_id INTEGER REFERENCES triage_log(id),
  combos_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running'
    CHECK(status IN ('running','ready','partial_failed')),
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  finished_at TEXT
);
CREATE INDEX idx_l06_run_idea ON l06_run(idea_id,id);
CREATE TRIGGER tq_l06_run_content BEFORE UPDATE ON l06_run
WHEN NEW.idea_id IS NOT OLD.idea_id OR NEW.triage_id IS NOT OLD.triage_id
  OR NEW.combos_json IS NOT OLD.combos_json OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'l06_run 任务清单不可变，仅可推进 status/finished_at'); END;
CREATE TRIGGER tq_l06_run_delete BEFORE DELETE ON l06_run
BEGIN SELECT RAISE(ABORT, 'l06_run 不可删除'); END;
-- timeout / auto_defer 是「系统替我推进的状态」，不是我的分诊结论：
-- 记在事件流里，不占 triage_log 的 attempt 名额（否则超时未收口会吃掉第二次预算机会）。
CREATE TABLE l06_idea_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idea_id INTEGER NOT NULL REFERENCES idea(id),
  attempt INTEGER NOT NULL,
  event TEXT NOT NULL CHECK(event IN ('timeout','auto_defer')),
  occurred_at TEXT NOT NULL,
  reason TEXT
);
CREATE UNIQUE INDEX uq_l06_event ON l06_idea_event(idea_id, attempt, event);
CREATE INDEX idx_l06_event_occurred ON l06_idea_event(occurred_at);
CREATE TRIGGER tq_l06_event_update BEFORE UPDATE ON l06_idea_event
BEGIN SELECT RAISE(ABORT, 'l06 事件流不可修改'); END;
CREATE TRIGGER tq_l06_event_delete BEFORE DELETE ON l06_idea_event
BEGIN SELECT RAISE(ABORT, 'l06 事件流不可删除'); END;
"""

# 任务1c（2026-10-17，Yu Fang 批准的数据层修复包）：
#   source 列=推荐来源标记（live=当日链路 / backfill=事后回填观察池），
#     仅 recommend_outcome；默认 'live' 保证旧进程写入自动落默认值。
#   exit_code 列=出场原因结构化枚举（stop_loss/trailing_stop/fixed_tp/
#     max_hold_days/no_fill/expired/other），exit_reason 保留为展示文本；
#     shadow 表同步加列——两表共用评估函数，列必须对齐。
#   recommend_outcome_archive=窗口先清后重写前的同事务快照（append-only，
#     主键含 policy_version：同一参数版本内最新快照覆盖旧快照，跨版本全保留）。
#   存量归类：反转首日 scan_date<2026-09-24（stock_signal 该线最早 9/24）→backfill；
#     自由文本 exit_reason 按前缀规则一次性映射（规则与 core.outcome_tracker
#     ::exit_code_for_reason 一致，中文模板来自 exit_advisor 的 f-string）。
_TASK1C_OUTCOME_SQL = """
ALTER TABLE recommend_outcome ADD COLUMN source TEXT NOT NULL DEFAULT 'live';
ALTER TABLE recommend_outcome ADD COLUMN exit_code TEXT;
ALTER TABLE recommend_outcome_shadow ADD COLUMN exit_code TEXT;
CREATE TABLE recommend_outcome_archive (
    policy_version TEXT NOT NULL,
    archived_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    code           TEXT NOT NULL,
    scan_date      TEXT NOT NULL,
    horizon        TEXT NOT NULL,
    strategy       TEXT,
    entry_price    REAL,
    stop_loss      REAL,
    take_profit    REAL,
    fusion_score   REAL,
    t1_return      REAL,
    t2_return      REAL,
    t3_return      REAL,
    t5_return      REAL,
    t10_return     REAL,
    max_return     REAL,
    min_return     REAL,
    hit_stop       INTEGER,
    hit_tp         INTEGER,
    exit_reason    TEXT,
    exit_code      TEXT,
    exit_date      TEXT,
    exit_return    REAL,
    evaluated_at   TEXT,
    source         TEXT,
    PRIMARY KEY (code, scan_date, horizon, strategy, policy_version)
);
CREATE INDEX idx_outcome_archive_scan ON recommend_outcome_archive(scan_date);
UPDATE recommend_outcome SET source = 'backfill'
 WHERE strategy = '反转首日' AND scan_date < '2026-09-24';
UPDATE recommend_outcome SET exit_code = CASE
    WHEN exit_reason IN ('stop_loss','trailing_stop','max_hold_days','no_fill',
                         'expired','holding','watching','data_pending','cancelled')
        THEN exit_reason
    WHEN exit_reason LIKE '收盘跌破止损价%' THEN 'stop_loss'
    WHEN exit_reason LIKE '移动止盈%' THEN 'trailing_stop'
    WHEN exit_reason LIKE '收盘达到止盈价%' THEN 'fixed_tp'
    WHEN exit_reason LIKE '持仓满%' THEN 'max_hold_days'
    WHEN exit_reason IS NOT NULL THEN 'other'
    ELSE NULL END
  WHERE exit_code IS NULL AND exit_reason IS NOT NULL;
UPDATE recommend_outcome_shadow SET exit_code = CASE
    WHEN exit_reason IN ('stop_loss','trailing_stop','max_hold_days','no_fill',
                         'expired','holding','watching','data_pending','cancelled')
        THEN exit_reason
    WHEN exit_reason LIKE '收盘跌破止损价%' THEN 'stop_loss'
    WHEN exit_reason LIKE '移动止盈%' THEN 'trailing_stop'
    WHEN exit_reason LIKE '收盘达到止盈价%' THEN 'fixed_tp'
    WHEN exit_reason LIKE '持仓满%' THEN 'max_hold_days'
    WHEN exit_reason IS NOT NULL THEN 'other'
    ELSE NULL END
  WHERE exit_code IS NULL AND exit_reason IS NOT NULL;
"""


def init_db():
    """初始化数据库，创建所有表"""
    with get_conn() as conn:
        # ── 1. 所有建表/建索引 SQL（纯 SQL，无 Python 代码）──────────
        conn.executescript("""
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS schema_migration (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- 个人持仓、自选及真实交易独立保留。
CREATE TABLE IF NOT EXISTS personal_position (
    id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, name TEXT,
    shares INTEGER NOT NULL, cost_price REAL NOT NULL, stop_loss REAL,
    take_profit REAL, note TEXT, opened_at TEXT, closed_at TEXT,
    status TEXT DEFAULT 'holding', created_at TEXT, updated_at TEXT,
    UNIQUE(code, opened_at)
);
CREATE INDEX IF NOT EXISTS idx_pp_code ON personal_position(code);
CREATE INDEX IF NOT EXISTS idx_pp_status ON personal_position(status);
CREATE TABLE IF NOT EXISTS personal_watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE,
    note TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS personal_trade (
    id INTEGER PRIMARY KEY AUTOINCREMENT, position_id INTEGER,
    code TEXT NOT NULL, name TEXT, action TEXT NOT NULL,
    shares INTEGER NOT NULL, price REAL NOT NULL, cost_price REAL NOT NULL,
    pnl REAL NOT NULL, pnl_pct REAL NOT NULL, traded_at TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pt_code ON personal_trade(code);
CREATE INDEX IF NOT EXISTS idx_pt_traded_at ON personal_trade(traded_at);
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

-- 龙虎榜「机构专用席位」买卖统计（akshare stock_lhb_jgmmtj_em 东方财富）
--
-- ⚠ 口径警告（实测确认，2026-09-22）：
--   同一 (trade_date, code) 会因不同「上榜原因」并存多行，而 reason 决定统计窗口：
--     · 「日涨幅偏离值达7%」「日换手率达到20%」等 → 单日口径，
--        金额四舍五入等于当日值（接口市场总成交额 / daily_price.amount == 1.00x）
--     · 「连续三个交易日内…」→ N 日累计口径（实测 2.0~4.3x）
--   故金额类字段（jg_buy_amt / jg_sell_amt / jg_net_buy / market_total_amt）
--   **绝不可跨 reason 相加**，也不能按 (trade_date, code) 聚合（会把 3 日额当单日额）。
--   相反，close_price / pct_change / turnover_rate / float_mkt_cap 恒为当日值，
--   两种口径下完全一致，可直接使用。
--   主键含 reason 就是为了保留全部口径；筛选单日动向用 window_days = 1。
CREATE TABLE IF NOT EXISTS stock_lhb_jg_detail (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date          TEXT NOT NULL,
    code                TEXT NOT NULL,
    name                TEXT,
    close_price         REAL,
    pct_change          REAL,
    buyer_jg_count      INTEGER,
    seller_jg_count     INTEGER,
    jg_buy_amt          REAL,
    jg_sell_amt         REAL,
    jg_net_buy          REAL,
    market_total_amt    REAL,
    jg_net_ratio        REAL,
    turnover_rate       REAL,
    float_mkt_cap       REAL,
    reason              TEXT NOT NULL,
    window_days         INTEGER DEFAULT 1,
    UNIQUE(trade_date, code, reason)
);
CREATE INDEX IF NOT EXISTS idx_lhjg_date   ON stock_lhb_jg_detail(trade_date);
CREATE INDEX IF NOT EXISTS idx_lhjg_code   ON stock_lhb_jg_detail(code);
CREATE INDEX IF NOT EXISTS idx_lhjg_netbuy ON stock_lhb_jg_detail(trade_date, jg_net_buy);

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
    t2_return       REAL,
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

-- 筹码优先排序 · 前向记录旁路表（2026-09-16，步骤③）
-- ─────────────────────────────────────────────────────────────
-- 目的：26k 历史样本已被多轮挖掘，IS/OOS 切分不再干净，且筹码 Δ 对样本期敏感
--（子样本 Δ +0.87%→+0.47%，成因是样本构成而非口径）。故不做历史回填，
-- 改为**前向对照**：每日 × core/outcome_tracker.insert_new_outcomes 里，用与
-- 线上完全相同的 WHERE（门槛/T1 闸门/大盘门控/观察线/gap guard/板块过滤），
-- 只把 ORDER BY 换成 chip_conc 优先，取同样 top_n 写本表。
-- 两组群体天然对齐 ⇒ 唯一变量是排序键。
--
-- ⚠ 本表**不参与任何线上推荐/出场跟踪/复盘胜率**，纯记录。
-- 列名与 recommend_outcome 保持一致，以便复用同一套评估函数
--（evaluate_outcomes(table=...)），出场数学 import 线上 _short_exit_sim、不重写。
CREATE TABLE IF NOT EXISTS recommend_outcome_shadow (
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
    t2_return       REAL,
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
    -- 影子组专属
    chip_conc       REAL,               -- 排序键（越小越集中）
    in_baseline     INTEGER DEFAULT 0,  -- 是否也在线上基线 top_n 里（重叠度）
    UNIQUE(code, scan_date, horizon)
);
CREATE INDEX IF NOT EXISTS idx_outcome_shadow_scan
    ON recommend_outcome_shadow(scan_date);

-- 策略参数运行时覆盖层（优化器建议被采纳/手动调整后写入，代码常量退化为默认值）
CREATE TABLE IF NOT EXISTS strategy_param_override (
    param_key   TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    reason      TEXT,
    source      TEXT DEFAULT 'manual',
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 参数调整审计日志（记录旧值/新值/依据指标，支持一键回滚）
CREATE TABLE IF NOT EXISTS param_tune_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    param_key    TEXT NOT NULL,
    old_value    TEXT,
    new_value    TEXT NOT NULL,
    action       TEXT NOT NULL,
    reason       TEXT,
    metrics_json TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_tune_log_key ON param_tune_log(param_key, created_at DESC);

-- 优化器报告（每日诊断 diagnosis / 周五寻优 tuning，同日同类型只留最新一份）
CREATE TABLE IF NOT EXISTS optimizer_report (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date  TEXT NOT NULL,
    report_type  TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(report_date, report_type)
);

-- 优化器产出的待采纳参数建议（suggest 模式：人工确认后才生效）
CREATE TABLE IF NOT EXISTS param_suggestion (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_date  TEXT NOT NULL,
    param_key     TEXT NOT NULL,
    current_value TEXT,
    suggest_value TEXT NOT NULL,
    reason        TEXT,
    metrics_json  TEXT,
    status        TEXT DEFAULT 'pending',
    decided_at    TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_suggestion_status ON param_suggestion(status, created_date DESC);
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

        _safe_add_column(conn, "stock_info", "industry", "TEXT")
        _safe_add_column(conn, "personal_watchlist", "target_price", "REAL")
        _safe_add_column(conn, "personal_watchlist", "alert_dir", "TEXT")
        _safe_add_column(conn, "personal_trade", "position_id", "INTEGER")
        # stock_info 增加市值相关字段
        _safe_add_column(conn, "stock_info", "total_shares",  "REAL")   # 总股本
        _safe_add_column(conn, "stock_info", "circ_shares",   "REAL")   # 流通股本

        # recommend_outcome 补充 T+2 收益列
        _safe_add_column(conn, "recommend_outcome", "t2_return", "REAL")

        # ── 3b. 迁移：优化器建议/调参追溯列（方案 E3）─────────────
        # param_suggestion 记录「建议生成时」的基线参数哈希与版本，采纳前校验：
        #   基线已变化 ⇒ 建议过期，防止用旧寻优结论覆盖新参数（方案 E3 bullet 7）。
        #   list_key 记录该建议面向的正式信号线；sample_n 记录评估样本数（可追溯）。
        _safe_add_column(conn, "param_suggestion", "baseline_hash", "TEXT")
        _safe_add_column(conn, "param_suggestion", "baseline_version", "INTEGER")
        _safe_add_column(conn, "param_suggestion", "list_key", "TEXT")
        _safe_add_column(conn, "param_suggestion", "sample_n", "INTEGER")
        # param_tune_log 记录每次采纳/回滚生成的新参数版本（单调递增，供诊断按版本分组）。
        _safe_add_column(conn, "param_tune_log", "new_version", "INTEGER")

        # ── 4. 迁移：三周期（horizon）维度 ────────────────────
        # strategy_rules 加 horizon；stock_signal 加 horizon/strategy
        _safe_add_column(conn, "strategy_rules", "horizon", "TEXT")
        _safe_add_column(conn, "stock_signal", "horizon", "TEXT DEFAULT 'short'")
        _safe_add_column(conn, "personal_position", "horizon", "TEXT DEFAULT 'short'")
        _safe_add_column(conn, "stock_signal", "strategy", "TEXT")
        # 短线扩展度列：价相对 MA20 偏离（short 组低扩展度排序用，S4 口径）
        _safe_add_column(conn, "stock_signal", "pct_above_ma20", "REAL")
        # 筹码集中度影子列（2026-09-16，步骤②影子模式）：conc=(P90-P10)/(P90+P10)，
        # 越小越集中。**只写列、不改排序、不回填历史行** —— 排序开关
        # short_chip_sort_prioritize 默认 0，历史行为 NULL（排序时 COALESCE 到 9.9 排最后）。
        # 填库走 core/sync.py::_build_signal_records（short 组），与回测同口径
        # decay=0.65 / decay_floor=0.003 / warmup≥1300 交易日。
        # 依据：docs/chip-peak-mktcap-verification.md（conc 作第一排序键时样本外 +0.91%→+1.34%、
        # 止损率 37%→23%；作过滤器无效）。⚠ 只验过短线 T+5，勿移植 stock_deep。
        _safe_add_column(conn, "stock_signal", "chip_conc", "REAL")
        # 长线「波动收敛度」排序键列（2026-09-19）：60 日年化波动 ÷ 250 日年化波动，
        # 越小＝相对自身常态越收敛。
        # 依据 tools/_diag_long_rescore.py（重放 scan_long_term 打分逻辑，2015-2026
        # 共 62.3 万信号 / 2488 采样日）+ _diag_long_key_eval.py（分年 + 剔除单年压力）：
        #   横截面五分位 rho = −1.00 **完美单调**（10 个候选键中唯一），Δ = −4.13%
        #   组合口径「vol_ratio 升序取 5×topN 池 → ext_ma20 升序取 topN」top4 等权 T+60：
        #     均值 +5.23% / 中位 +2.52% / 胜率 58.3%
        #     随机基线 +1.83% / +0.93% / 55.1%   ⇒ Δ = +3.41%
        #     剔除任一单年后 Δ 恒正 [+3.01%, +3.85%]
        # ⚠ 只作**排序键**，不作过滤器（当过滤器会大幅缩样本，未验证）。
        # ⚠ 不能当**单键**用：纯 vol_ratio 升序虽全期 Δ 高达 +7.51%，但分年在
        #   2018/2025/2026 为负（2026 −5.62%），是过拟合到 2016-2021 的强市段。
        #   必须与 ext_ma20 组成两层结构（见 outcome_tracker.long_sort_clause）。
        # 只写 long 组；short/mid 未验证 → NULL（排序时 COALESCE 到 9.9 排最后）。
        _safe_add_column(conn, "stock_signal", "vol_ratio", "REAL")
        # ── 长线选股重设计（2026-09-20）────────────────────────────────────────
        # 背景：原「score>=2.0」票池长期**负 alpha** —— 相对全市场等权超额
        #   -0.92%、t=-7.30（tools/_diag_long_pool.py，2015-2026 / 2416 采样日，
        #   11 年里 8 年为负）。逐 mask 拆解（_diag_long_mask.py）定位到负 alpha 主体：
        #     mask=11（趋势+斜率+浅回撤但**高波动**）日均 165 只，超额 -0.90% t=-6.54
        #     mask=8 / 12 / 4（含「回撤<40%」但缺趋势）超额 -1.07~-1.72%，t 最低 -14.90
        #   而 mask=7 / 15（趋势+斜率+低波）超额 +0.53~+0.56%，日胜率 ~60%
        # ⇒ 把「低波」从事后加分改为**票池必需条件**，并用连续键排序。
        #
        # long_mask：四条件 bitmask（bit0 趋势 / bit1 斜率 / bit2 低波 / bit3 回撤受控），
        #   票池过滤用 `(long_mask & 7) = 7`（趋势+斜率+低波三项必需）。
        # vol60 / dd250：连续特征，供横截面百分位加权排序键 long_rank_key 使用。
        # long_rank_key：0.75×pctile(vol60) + 0.25×pctile(dd250)，升序取 topN。
        #   依据 tools/_diag_long_redesign.py（带真实出场纪律，498 采样日 / 1992 笔）：
        #     本方案  全期 均值 +2.67% / 中位 +1.85% / 胜率 57.0% / 止损率 12.2%
        #     现状    全期 均值 +2.16% / 中位 +0.68% / 胜率 52.7% / 止损率 13.4%
        #     2026 年 本方案 +1.60%/胜率52.2%  现状 -0.38%/胜率53.3%
        #     分年 11 年里 10 年为正、10 年胜率>=50%（2018 熊市 -6.82% 是唯一负值）
        #   ⚠ 不能用**两层硬切池**近似（vol60 取 2×topN 池 → dd250 取 topN）：
        #     全期看着更好（+2.84%/57.2%）但 2026 胜率掉到 48.9%，是软加权才稳。
        _safe_add_column(conn, "stock_signal", "long_mask", "INTEGER")
        _safe_add_column(conn, "stock_signal", "vol60", "REAL")
        _safe_add_column(conn, "stock_signal", "dd250", "REAL")
        _safe_add_column(conn, "stock_signal", "long_rank_key", "REAL")
        # ── E1：回测结果可恢复（方案 E1，2026-09-25）──────────────────────────
        # backtest_results 原本把 task_id 混写进 rule_id 列，且只存汇总指标；
        # 内存 _TASKS 清空或进程重启后，权益曲线/参数/条件/未平仓记录全部丢失，
        # 详情/分页/导出/存为规则都无法恢复。新增独立列做完整持久化：
        #   task_id             独立任务 ID（新写入口，rule_id 旧混合用途仅兼容读取）
        #   actual_rule_id      规则回测时的真实 strategy_rules.id（可视化回测为 NULL）
        #   params_json         完整回测参数（BacktestParams 全字段）
        #   conditions_json     完整选股条件
        #   execution_config_json 执行配置（T+1/入场方式/止损判定时点/费用/滑点/整手规则）
        #   data_version        数据版本（行情快照标识，缺省 NULL 明确标记未知）
        #   result_json         结果 JSON（权益曲线/月度收益/指标/未平仓记录）
        #   status              done/failed/cancelled —— 仅持久化成功才写 done
        # 旧结果没有保存的权益曲线/参数不凭空补出：读取时字段缺失即标记 None。
        _safe_add_column(conn, "backtest_results", "task_id", "TEXT")
        _safe_add_column(conn, "backtest_results", "actual_rule_id", "INTEGER")
        _safe_add_column(conn, "backtest_results", "params_json", "TEXT")
        _safe_add_column(conn, "backtest_results", "conditions_json", "TEXT")
        _safe_add_column(conn, "backtest_results", "execution_config_json", "TEXT")
        _safe_add_column(conn, "backtest_results", "data_version", "TEXT")
        _safe_add_column(conn, "backtest_results", "result_json", "TEXT")
        _safe_add_column(conn, "backtest_results", "status", "TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bt_results_task "
                     "ON backtest_results(task_id)")
        # backtest_trades 补 shares/pnl：导出 CSV 与盈亏统计需要成交量与绝对盈亏，
        # 原表只存 pnl_pct，重启后无法完整恢复交易明细（E1 可恢复要求）。
        _safe_add_column(conn, "backtest_trades", "shares", "INTEGER")
        _safe_add_column(conn, "backtest_trades", "pnl", "REAL")
        # 一次性回填 strategy_rules.horizon（按持仓期推导：<=10 短期，<=60 中期，否则长期）
        conn.execute("""
            UPDATE strategy_rules SET horizon = CASE
                WHEN COALESCE(holding_max, 20) <= 10 THEN 'short'
                WHEN COALESCE(holding_max, 20) <= 60 THEN 'mid'
                ELSE 'long'
            END
            WHERE horizon IS NULL OR horizon = ''
        """)
        conn.execute("DROP INDEX IF EXISTS idx_sig_scan_trade_code")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sig_scan_code_horizon "
            "ON stock_signal(scan_date, code, horizon)")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_code_date_uniq "
            "ON daily_price(code, trade_date)")
        # 旧表保留作历史证据；所有步骤成功后才记录迁移完成。
        conn.execute("INSERT INTO schema_migration(version) VALUES ('001_safe_baseline') "
                     "ON CONFLICT(version) DO NOTHING")
        _apply_versioned_migration(conn, "002_unified_signal_model", _UNIFIED_MODEL_SQL)
        _apply_versioned_migration(conn, "003_task_orchestration", _TASK_ORCHESTRATION_SQL)
        _apply_versioned_migration(conn, "004_task1c_outcome_columns", _TASK1C_OUTCOME_SQL)
        _apply_versioned_migration(conn, "005_jh4_discipline", _005_JH4_SQL)
        _apply_versioned_migration(conn, "006_l06_triage", _006_L06_SQL)
        _apply_versioned_migration(conn, "007_l06_run_state", _007_L06_RUN_STATE_SQL)

    print(f"[DB] 数据库初始化完成: {DB_PATH}")


# ─────────────────────────────────────────────
# Repository 兼容层（新代码优先从 core.repository 导入）
# T10：自住业务函数已全部迁入 repository/，本层仅保留 re-export 兼容旧导入路径
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
    # T10 迁入（自选股同步用的 daily_price 子集查询）
    has_price_data, get_latest_date_for_codes, get_stock_count_in_db_for_codes,
)
from core.repository.watchlist_repo import (
    # T10 迁入（personal_watchlist 只读查询）
    get_watchlist_codes, get_watchlist_with_names, count_watchlist,
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
    upsert_lhb_detail, get_lhb_detail, get_latest_lhb_date, get_lhb_map_for_date,
    upsert_lhb_jg_detail, get_latest_lhb_jg_date, get_lhb_jg_rows,
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
