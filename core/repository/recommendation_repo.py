"""recommendation_repo.py —— 推荐批次与条目的持久化。

职责：把推荐服务选出的冻结名单落库为「草稿批次 → 发布批次」，并提供只读回取。
不变式（由 core/db.py 触发器与唯一索引共同保证，本层不重复实现约束）：
  - 同一 (run_id,list_key,cohort,experiment_id,policy_hash) 只有一个批次；重复创建幂等返回原 ID。
  - 每个 (scan_date,list_key,cohort,experiment_id) 同时只有一个 published 批次。
  - 已发布批次的条目不可增删改；批次内容字段不可改；只允许 draft→published/comparison、
    published→superseded 两类状态迁移。
  - 0 条推荐也保存批次（空榜可追溯），不因为空而丢失发布记录。
"""
import json
import uuid
from datetime import datetime

from core.db import get_conn
from core.input_snapshot import canonical_json, content_hash

COHORTS = {"production", "observation", "shadow"}
BATCH_STATUSES = {"draft", "published", "superseded", "comparison"}


class BatchConflictError(ValueError):
    """发布身份冲突：同日同榜已有发布批次，或非法状态迁移。"""


def _batch_identity(scan_date, list_key, cohort, experiment_id):
    return (scan_date, list_key, cohort, experiment_id or "")


def create_draft_batch(*, run_id, scan_date, list_key, cohort, policy,
                       market_state, source, experiment_id="", exclusions=None):
    """创建（或幂等返回）草稿批次。policy 为选择时冻结的完整配置。"""
    if cohort not in COHORTS:
        raise ValueError(f"非法分组：{cohort}")
    if not list_key:
        raise ValueError("榜单键不能为空")
    policy_json = canonical_json(policy)
    policy_hash = content_hash(policy)
    exclusions_json = canonical_json(exclusions or [])
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        prev = conn.execute(
            "SELECT id FROM recommendation_batch "
            "WHERE run_id=? AND list_key=? AND cohort=? AND experiment_id=? AND policy_hash=?",
            (run_id, list_key, cohort, experiment_id or "", policy_hash)).fetchone()
        if prev:
            return prev["id"]
        batch_id = uuid.uuid4().hex
        batch_content = content_hash({
            "run_id": run_id, "scan_date": scan_date, "list_key": list_key,
            "cohort": cohort, "experiment_id": experiment_id or "",
            "policy": policy, "market_state": market_state, "source": source,
            "exclusions": exclusions or [],
        })
        conn.execute(
            """INSERT INTO recommendation_batch(
                id,run_id,scan_date,list_key,cohort,experiment_id,policy_json,
                policy_hash,market_state,revision,status,source,exclusions_json,content_hash)
               VALUES (?,?,?,?,?,?,?,?,?,1,'draft',?,?,?)""",
            (batch_id, run_id, scan_date, list_key, cohort, experiment_id or "",
             policy_json, policy_hash, market_state, source, exclusions_json, batch_content))
        return batch_id


def add_items(batch_id, items):
    """向草稿批次写入推荐条目；批次非草稿状态直接拒绝（触发器亦会兜底）。

    items: [{signal_id, code, horizon, rank, reason, plan, support}]
    """
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status FROM recommendation_batch WHERE id=?", (batch_id,)).fetchone()
        if not row:
            raise ValueError("批次不存在")
        if row["status"] != "draft":
            raise BatchConflictError("只有草稿批次可以写入条目")
        for it in items:
            reason = canonical_json(it.get("reason") or {})
            plan = canonical_json(it.get("plan") or {})
            support = canonical_json(it.get("support") or [])
            content = content_hash({
                "signal_id": it["signal_id"], "code": it["code"], "horizon": it["horizon"],
                "rank": it["rank"], "reason": it.get("reason") or {},
                "plan": it.get("plan") or {}, "support": it.get("support") or [],
            })
            conn.execute(
                """INSERT INTO recommendation_item(
                    id,batch_id,signal_id,code,horizon,rank,reason_json,plan_json,support_json,content_hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, batch_id, it["signal_id"], it["code"], it["horizon"],
                 int(it["rank"]), reason, plan, support, content))
    return len(items)


def _has_filled_trade(conn, batch_id):
    """该批次关联的推荐是否已产生模拟成交（用于阻断重新发布）。"""
    row = conn.execute(
        """SELECT 1 FROM simulated_trade t
           JOIN recommendation_item i ON i.id=t.first_recommendation_id
           WHERE i.batch_id=? AND t.exec_entry_price IS NOT NULL LIMIT 1""",
        (batch_id,)).fetchone()
    return row is not None


def publish_batch(batch_id, *, status="published"):
    """草稿 → 发布（或旁路 comparison）。同日同榜已有其它发布批次时拒绝，需走 republish。"""
    if status not in {"published", "comparison"}:
        raise ValueError("发布状态只能是 published 或 comparison")
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM recommendation_batch WHERE id=?", (batch_id,)).fetchone()
        if not row:
            raise ValueError("批次不存在")
        if row["status"] == status:
            return batch_id  # 幂等
        if row["status"] != "draft":
            raise BatchConflictError(f"批次当前状态 {row['status']} 不可发布")
        if status == "published":
            active = conn.execute(
                "SELECT id FROM recommendation_batch "
                "WHERE scan_date=? AND list_key=? AND cohort=? AND experiment_id=? "
                "AND status='published' AND id!=?",
                (row["scan_date"], row["list_key"], row["cohort"], row["experiment_id"], batch_id)).fetchone()
            if active:
                raise BatchConflictError("当日该榜单已发布，重新发布须显式替代（republish_batch）")
        conn.execute(
            "UPDATE recommendation_batch SET status=?, published_at=? WHERE id=?",
            (status, datetime.now().isoformat(timespec="seconds"), batch_id))
        return batch_id


def republish_batch(batch_id):
    """显式重新发布：替代当日同榜旧发布批次。旧批次相关模拟单已成交时拒绝。

    仅用于「下一交易日开盘前、模拟单尚未成交」的显式修正；历史/已进入交易日的
    重算属于 replay，不应调用本函数（由上层调度保证）。
    """
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM recommendation_batch WHERE id=?", (batch_id,)).fetchone()
        if not row:
            raise ValueError("批次不存在")
        if row["status"] == "published":
            return batch_id
        if row["status"] != "draft":
            raise BatchConflictError(f"批次当前状态 {row['status']} 不可重新发布")
        olds = conn.execute(
            "SELECT id FROM recommendation_batch "
            "WHERE scan_date=? AND list_key=? AND cohort=? AND experiment_id=? AND status='published'",
            (row["scan_date"], row["list_key"], row["cohort"], row["experiment_id"])).fetchall()
        for old in olds:
            if _has_filled_trade(conn, old["id"]):
                raise BatchConflictError("旧发布批次已产生模拟成交，不可重新发布")
        # 先替代旧发布批次，再发布新批次，避免与唯一活跃索引冲突。
        for old in olds:
            conn.execute("UPDATE recommendation_batch SET status='superseded' WHERE id=?", (old["id"],))
        conn.execute(
            "UPDATE recommendation_batch SET status='published', published_at=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), batch_id))
        return batch_id


def get_published_batch(scan_date, list_key, cohort="production", experiment_id=""):
    """只读回取当日冻结的已发布名单（含条目）；不存在返回 None，绝不重新选股。"""
    with get_conn(readonly=True) as conn:
        row = conn.execute(
            "SELECT * FROM recommendation_batch "
            "WHERE scan_date=? AND list_key=? AND cohort=? AND experiment_id=? AND status='published'",
            _batch_identity(scan_date, list_key, cohort, experiment_id)).fetchone()
        if not row:
            return None
        items = conn.execute(
            "SELECT * FROM recommendation_item WHERE batch_id=? ORDER BY rank", (row["id"],)).fetchall()
    batch = dict(row)
    batch["policy"] = json.loads(batch.pop("policy_json"))
    batch["exclusions"] = json.loads(batch.pop("exclusions_json"))
    batch["items"] = [_decode_item(i) for i in items]
    return batch


def get_batch(batch_id):
    with get_conn(readonly=True) as conn:
        row = conn.execute("SELECT * FROM recommendation_batch WHERE id=?", (batch_id,)).fetchone()
        if not row:
            return None
        items = conn.execute(
            "SELECT * FROM recommendation_item WHERE batch_id=? ORDER BY rank", (batch_id,)).fetchall()
    batch = dict(row)
    batch["policy"] = json.loads(batch.pop("policy_json"))
    batch["exclusions"] = json.loads(batch.pop("exclusions_json"))
    batch["items"] = [_decode_item(i) for i in items]
    return batch


def _decode_item(row):
    item = dict(row)
    item["reason"] = json.loads(item.pop("reason_json"))
    item["plan"] = json.loads(item.pop("plan_json"))
    item["support"] = json.loads(item.pop("support_json"))
    return item
