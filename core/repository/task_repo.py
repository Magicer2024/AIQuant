"""task_repo.py —— background_task / scheduled_job / task_stage 持久化（方案 D1）。

职责边界与不变式：
  - 所有异步/定时/CLI 写任务的唯一状态来源；旧内存 task_queue._tasks 仅作轻量线程池缓存。
  - 提交幂等与资源占用在同一数据库事务（BEGIN IMMEDIATE）内判定，不再「先查询再无锁启动」。
    同一 idempotency_key 的重复提交返回已有任务；同一 resource_group 已有活跃任务时返回冲突。
  - 租约：owner_token + lease_expires_at。心跳每 HEARTBEAT_INTERVAL_SECONDS 秒续期，
    超过 LEASE_TTL_SECONDS 未续期视为失效。失效的 running/cancel_requested 任务是僵尸，
    可被 submit/recover 标记 interrupted；owner_token 防止失去租约的旧执行器继续写入。
  - 状态机：pending→running→(success|partial_success|error|cancelled)。cancel_requested 是
    运行中收到取消请求的中间态（协作式，在阶段/批次边界检查）。进程中断且租约失效→interrupted。
    已结束任务不可复活（DB 触发器保护）；重试创建新任务并以 retry_of 关联旧任务。
  - 阶段：task_stage 记录每阶段 status/耗时/输入输出数量/数据日期/失败原因，支撑 partial_success
    与「仅重跑失败阶段及下游」。阶段可更新（重试），不可删除（审计留痕）。

时间戳统一为 UTC、固定格式 `%Y-%m-%dT%H:%M:%S.%f`，可按字典序比较（等价于时间序）。
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

from core.db import get_conn

# 资源组：串行化对主数据的并发修改（同步/重算/历史修正/深度扫描/回补）。
RESOURCE_PIPELINE_WRITE = "pipeline_write"

TASK_STATUSES = {"pending", "running", "cancel_requested", "success",
                 "partial_success", "error", "cancelled", "interrupted"}
ACTIVE_STATUSES = ("pending", "running", "cancel_requested")
TERMINAL_STATUSES = ("success", "partial_success", "error", "cancelled", "interrupted")
STAGE_STATUSES = {"pending", "running", "success", "skipped", "error"}

HEARTBEAT_INTERVAL_SECONDS = 5
LEASE_TTL_SECONDS = 60

_TS_FMT = "%Y-%m-%dT%H:%M:%S.%f"


def _now_iso():
    return datetime.now(timezone.utc).strftime(_TS_FMT)


def _plus_ttl(seconds, now_iso=None):
    base = datetime.strptime(now_iso, _TS_FMT) if now_iso else datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return (base + timedelta(seconds=seconds)).strftime(_TS_FMT)


def _decode(row):
    if row is None:
        return None
    d = dict(row)
    for key in ("input_json", "result_json"):
        if d.get(key):
            try:
                d[key[:-5]] = json.loads(d[key])
            except (ValueError, TypeError):
                d[key[:-5]] = None
        else:
            d[key[:-5]] = None
    return d


# ─────────────────────────── 提交（幂等 + 资源占用） ───────────────────────────

def submit_task(*, task_type, idempotency_key=None, resource_group=None,
                input=None, input_version=None, parent_id=None, retry_of=None,
                now_iso=None):
    """在单事务内创建任务或返回已存在/冲突任务。

    返回 dict：
      {task_id, status, reused, conflict, holder_id}
      - reused=True：命中同 idempotency_key 的已有任务（不新建）。
      - conflict=True：同 resource_group 已有活跃任务（返回其 holder_id，调用方可 409 或复用）。
      - 两者皆 False：新建 pending 任务。
    提交时顺带回收本资源组内租约已失效的僵尸任务（标记 interrupted），避免永久阻塞。
    """
    now = now_iso or _now_iso()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if idempotency_key is not None:
            prev = conn.execute(
                "SELECT id,status FROM background_task WHERE idempotency_key=?",
                (idempotency_key,)).fetchone()
            if prev:
                return {"task_id": prev["id"], "status": prev["status"],
                        "reused": True, "conflict": False, "holder_id": prev["id"]}
        if resource_group:
            _reclaim_zombies(conn, resource_group, now)
            holder = conn.execute(
                """SELECT id FROM background_task
                   WHERE resource_group=? AND status IN ('pending','running','cancel_requested')
                   ORDER BY created_at LIMIT 1""", (resource_group,)).fetchone()
            if holder:
                return {"task_id": holder["id"], "status": "conflict",
                        "reused": False, "conflict": True, "holder_id": holder["id"]}
        task_id = uuid.uuid4().hex
        conn.execute(
            """INSERT INTO background_task(
                id,idempotency_key,task_type,parent_id,resource_group,
                input_json,input_version,status,retry_of,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (task_id, idempotency_key, task_type, parent_id, resource_group,
             json.dumps(input or {}, ensure_ascii=False, sort_keys=True), input_version,
             "pending", retry_of, now))
        return {"task_id": task_id, "status": "pending",
                "reused": False, "conflict": False, "holder_id": task_id}


def _reclaim_zombies(conn, resource_group, now):
    """把本资源组内租约已失效的 running/cancel_requested 任务标记 interrupted（自愈）。"""
    conn.execute(
        """UPDATE background_task
           SET status='interrupted', finished_at=?, error='租约失效，判定为中断',
               owner_token=NULL, lease_expires_at=NULL
           WHERE resource_group=? AND status IN ('running','cancel_requested')
             AND (lease_expires_at IS NULL OR lease_expires_at < ?)""",
        (now, resource_group, now))


# ─────────────────────────── 租约：认领 / 心跳 / 释放 ───────────────────────────

def claim_task(task_id, owner_token, *, ttl=LEASE_TTL_SECONDS, now_iso=None):
    """认领一个 pending 任务：取得租约并置 running。

    仅认领 pending；同 resource_group 已有持有效租约的执行器时拒绝（互斥）。
    running/cancel_requested 的僵尸任务不在此接管（由 recover_interrupted 标记 interrupted），
    符合「无法验证存活的任务不接管」。
    返回 {claimed: bool, reason?: str, holder_id?: str, status: str}。
    """
    now = now_iso or _now_iso()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status,resource_group FROM background_task WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return {"claimed": False, "reason": "not_found", "status": None}
        if row["status"] != "pending":
            return {"claimed": False, "reason": "not_pending", "status": row["status"]}
        if row["resource_group"]:
            _reclaim_zombies(conn, row["resource_group"], now)
            holder = conn.execute(
                """SELECT id FROM background_task
                   WHERE resource_group=? AND id!=? AND status IN ('running','cancel_requested')
                     AND lease_expires_at IS NOT NULL AND lease_expires_at>=?
                   LIMIT 1""", (row["resource_group"], task_id, now)).fetchone()
            if holder:
                return {"claimed": False, "reason": "resource_busy",
                        "holder_id": holder["id"], "status": row["status"]}
        conn.execute(
            """UPDATE background_task
               SET status='running', owner_token=?, lease_expires_at=?, heartbeat_at=?,
                   started_at=COALESCE(started_at,?)
               WHERE id=? AND status='pending'""",
            (owner_token, _plus_ttl(ttl, now), now, now, task_id))
        return {"claimed": True, "status": "running"}


def renew_lease(task_id, owner_token, *, ttl=LEASE_TTL_SECONDS, now_iso=None):
    """心跳续期。仅当 owner_token 匹配且任务仍活跃时成功；失去租约返回 False。"""
    now = now_iso or _now_iso()
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE background_task SET lease_expires_at=?, heartbeat_at=?
               WHERE id=? AND owner_token=? AND status IN ('running','cancel_requested')""",
            (_plus_ttl(ttl, now), now, task_id, owner_token))
        return cur.rowcount > 0


def release_lease(task_id, owner_token):
    """主动释放租约（任务结束或让出资源）；仅当持有者匹配时清除。"""
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE background_task SET owner_token=NULL, lease_expires_at=NULL
               WHERE id=? AND owner_token=? AND status IN ('running','cancel_requested')""",
            (task_id, owner_token))
        return cur.rowcount > 0


def owns_lease(task_id, owner_token, *, now_iso=None):
    """校验执行器是否仍持有有效租约（写库前的所有权令牌检查）。"""
    now = now_iso or _now_iso()
    with get_conn(readonly=True) as conn:
        row = conn.execute(
            """SELECT owner_token, lease_expires_at FROM background_task
               WHERE id=? AND status IN ('running','cancel_requested')""", (task_id,)).fetchone()
    return bool(row and row["owner_token"] == owner_token
                and row["lease_expires_at"] and row["lease_expires_at"] >= now)


# ─────────────────────────── 状态 / 进度 / 取消 ───────────────────────────

def set_status(task_id, status, *, owner_token=None, result=None, error=None,
               stage=None, now_iso=None):
    """更新任务状态；owner_token 给定时校验所有权（防止失去租约的旧执行器写入）。

    进入终态时清除租约并写 finished_at。返回是否更新成功。
    """
    if status not in TASK_STATUSES:
        raise ValueError(f"非法任务状态：{status}")
    now = now_iso or _now_iso()
    sets = ["status=?"]
    params = [status]
    if stage is not None:
        sets.append("stage=?")
        params.append(stage)
    if result is not None:
        sets.append("result_json=?")
        params.append(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if error is not None:
        sets.append("error=?")
        params.append(error)
    if status in TERMINAL_STATUSES:
        sets.append("finished_at=?")
        params.append(now)
        sets.append("owner_token=NULL")
        sets.append("lease_expires_at=NULL")
    where = "id=?"
    params.append(task_id)
    if owner_token is not None:
        where += " AND owner_token=?"
        params.append(owner_token)
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE background_task SET {','.join(sets)} WHERE {where}", params)
        return cur.rowcount > 0


def update_progress(task_id, progress, message=None, *, stage=None, owner_token=None):
    """更新进度（0-100）与当前阶段/消息；owner_token 给定时校验所有权。"""
    sets = ["progress=?"]
    params = [float(progress)]
    if message is not None:
        sets.append("message=?")
        params.append(message)
    if stage is not None:
        sets.append("stage=?")
        params.append(stage)
    where = "id=? AND status IN ('running','cancel_requested')"
    params.append(task_id)
    if owner_token is not None:
        where += " AND owner_token=?"
        params.append(owner_token)
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE background_task SET {','.join(sets)} WHERE {where}", params)
        return cur.rowcount > 0


def request_cancel(task_id):
    """协作式取消请求：置 cancel_requested 标志并把活跃任务转 cancel_requested 状态。"""
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE background_task SET cancel_requested=1,
                 status=CASE WHEN status IN ('pending','running') THEN 'cancel_requested'
                             ELSE status END
               WHERE id=? AND status IN ('pending','running','cancel_requested')""",
            (task_id,))
        return cur.rowcount > 0


def should_cancel(task_id, owner_token=None):
    """阶段/批次边界检查点：是否已请求取消（或已失去租约）。"""
    with get_conn(readonly=True) as conn:
        row = conn.execute(
            "SELECT cancel_requested, status FROM background_task WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return True
    if row["cancel_requested"] or row["status"] == "cancel_requested":
        return True
    # 失去租约的旧执行器不得继续写入，视同应停止。
    if owner_token is not None and row["status"] == "running" and not owns_lease(task_id, owner_token):
        return True
    return False


# ─────────────────────────── 中断恢复 ───────────────────────────

def recover_interrupted(*, now_iso=None, resource_group=None):
    """启动时回收：把租约已失效的活跃任务标记 interrupted（无法验证存活即不接管）。

    pending 任务没有租约，不在此处理（等待被认领）。返回被标记的任务 ID 列表。
    """
    now = now_iso or _now_iso()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        sql = ("SELECT id FROM background_task WHERE status IN ('running','cancel_requested') "
               "AND (lease_expires_at IS NULL OR lease_expires_at < ?)")
        params = [now]
        if resource_group:
            sql += " AND resource_group=?"
            params.append(resource_group)
        ids = [r["id"] for r in conn.execute(sql, params).fetchall()]
        if ids:
            conn.execute(
                f"""UPDATE background_task SET status='interrupted', finished_at=?,
                       error='进程中断，租约失效', owner_token=NULL, lease_expires_at=NULL
                   WHERE id IN ({','.join('?' for _ in ids)})""",
                [now, *ids])
        return ids


# ─────────────────────────── 查询 ───────────────────────────

def get_task(task_id):
    with get_conn(readonly=True) as conn:
        return _decode(conn.execute(
            "SELECT * FROM background_task WHERE id=?", (task_id,)).fetchone())


def get_task_by_key(idempotency_key):
    with get_conn(readonly=True) as conn:
        return _decode(conn.execute(
            "SELECT * FROM background_task WHERE idempotency_key=?", (idempotency_key,)).fetchone())


def list_tasks(*, status=None, task_type=None, resource_group=None, active_only=False, limit=100):
    sql = "SELECT * FROM background_task"
    clauses, params = [], []
    if status is not None:
        clauses.append("status=?")
        params.append(status)
    if active_only:
        clauses.append("status IN ('pending','running','cancel_requested')")
    if task_type is not None:
        clauses.append("task_type=?")
        params.append(task_type)
    if resource_group is not None:
        clauses.append("resource_group=?")
        params.append(resource_group)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with get_conn(readonly=True) as conn:
        return [_decode(r) for r in conn.execute(sql, params).fetchall()]


# ─────────────────────────── 阶段记录（D2 流水线用） ───────────────────────────

def init_stages(task_id, stage_keys):
    """为一件事登记全部阶段（幂等：已存在则跳过）。seq 按传入顺序。"""
    with get_conn() as conn:
        for seq, key in enumerate(stage_keys):
            conn.execute(
                """INSERT INTO task_stage(id,task_id,stage_key,seq,status,detail_json)
                   VALUES (?,?,?,?, 'pending', '{}')
                   ON CONFLICT(task_id,stage_key) DO NOTHING""",
                (uuid.uuid4().hex, task_id, key, seq))


def start_stage(task_id, stage_key, *, input_hash=None, data_date=None, now_iso=None):
    now = now_iso or _now_iso()
    with get_conn() as conn:
        conn.execute(
            """UPDATE task_stage SET status='running', started_at=?, input_hash=COALESCE(?,input_hash),
                   data_date=COALESCE(?,data_date)
               WHERE task_id=? AND stage_key=?""",
            (now, input_hash, data_date, task_id, stage_key))
        conn.execute("UPDATE background_task SET stage=? WHERE id=?", (stage_key, task_id))


def finish_stage(task_id, stage_key, status, *, output_count=None, error=None,
                 detail=None, now_iso=None):
    """收束阶段：status ∈ success/skipped/error；计算 elapsed_ms。"""
    if status not in ("success", "skipped", "error"):
        raise ValueError(f"非法阶段结束状态：{status}")
    now = now_iso or _now_iso()
    with get_conn() as conn:
        row = conn.execute("SELECT started_at FROM task_stage WHERE task_id=? AND stage_key=?",
                           (task_id, stage_key)).fetchone()
        elapsed = None
        if row and row["started_at"]:
            try:
                started = datetime.strptime(row["started_at"], _TS_FMT).replace(tzinfo=timezone.utc)
                ended = datetime.strptime(now, _TS_FMT).replace(tzinfo=timezone.utc)
                elapsed = int((ended - started).total_seconds() * 1000)
            except (ValueError, TypeError):
                elapsed = None
        conn.execute(
            """UPDATE task_stage SET status=?, finished_at=?, elapsed_ms=?, output_count=?,
                   error=?, detail_json=?
               WHERE task_id=? AND stage_key=?""",
            (status, now, elapsed, output_count, error,
             json.dumps(detail or {}, ensure_ascii=False, sort_keys=True), task_id, stage_key))


def mark_stage_skipped(task_id, stage_key, *, reason=None):
    """登记阶段跳过（如非交易日、依赖未就绪、已成功复用）。"""
    finish_stage(task_id, stage_key, "skipped", detail={"reason": reason} if reason else None)


def get_stages(task_id):
    with get_conn(readonly=True) as conn:
        rows = conn.execute(
            """SELECT stage_key,seq,status,input_hash,output_count,data_date,error,
                      started_at,finished_at,elapsed_ms,detail_json
               FROM task_stage WHERE task_id=? ORDER BY seq""", (task_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["detail"] = json.loads(d.pop("detail_json") or "{}")
        except (ValueError, TypeError):
            d["detail"] = {}
        out.append(d)
    return out


# ─────────────────────────── 定时任务（持久化安排） ───────────────────────────

def upsert_scheduled_job(*, job_type, schedule_kind, at_time=None, interval_seconds=None,
                         input=None, enabled=True, job_id=None):
    """按 (job_type, at_time) 唯一登记定时安排；重复登记更新参数而不新增。"""
    if schedule_kind not in ("daily", "interval", "once"):
        raise ValueError(f"非法调度类型：{schedule_kind}")
    jid = job_id or f"{job_type}_{(at_time or '').replace(':', '')}"
    now = _now_iso()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO scheduled_job(id,job_type,schedule_kind,at_time,interval_seconds,
                   input_json,enabled,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 schedule_kind=excluded.schedule_kind, at_time=excluded.at_time,
                 interval_seconds=excluded.interval_seconds, input_json=excluded.input_json,
                 enabled=excluded.enabled, updated_at=excluded.updated_at""",
            (jid, job_type, schedule_kind, at_time, interval_seconds,
             json.dumps(input or {}, ensure_ascii=False, sort_keys=True),
             1 if enabled else 0, now, now))
    return jid


def get_scheduled_job(job_id):
    with get_conn(readonly=True) as conn:
        row = conn.execute("SELECT * FROM scheduled_job WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        d["input"] = json.loads(d.pop("input_json") or "{}")
    except (ValueError, TypeError):
        d["input"] = {}
    return d


def list_scheduled_jobs(*, enabled_only=True):
    sql = "SELECT * FROM scheduled_job"
    params = []
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY job_type, at_time"
    with get_conn(readonly=True) as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["input"] = json.loads(d.pop("input_json") or "{}")
        except (ValueError, TypeError):
            d["input"] = {}
        out.append(d)
    return out


def set_job_enabled(job_id, enabled):
    with get_conn() as conn:
        cur = conn.execute("UPDATE scheduled_job SET enabled=?, updated_at=? WHERE id=?",
                           (1 if enabled else 0, _now_iso(), job_id))
        return cur.rowcount > 0


def record_job_run(job_id, *, task_id=None, status=None, now_iso=None):
    """登记一次触发结果（供自动同步「依据最近应完成交易日和阶段结果判断」）。"""
    now = now_iso or _now_iso()
    with get_conn() as conn:
        conn.execute(
            """UPDATE scheduled_job SET last_run_at=?, last_status=COALESCE(?,last_status),
                   last_task_id=COALESCE(?,last_task_id), updated_at=?
               WHERE id=?""",
            (now, status, task_id, now, job_id))
