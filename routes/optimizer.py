"""
routes/optimizer.py —— 策略优化器接口（诊断/建议/采纳/回滚）

suggest 模式：优化器只产出建议，本模块提供人工确认入口：
  GET  /api/optimizer/latest    最新诊断+寻优报告、待采纳建议、参数状态、调参历史
  POST /api/optimizer/apply     采纳建议 {id}
  POST /api/optimizer/dismiss   忽略建议 {id}
  POST /api/optimizer/rollback  回滚参数 {param_key}
  POST /api/optimizer/run       手动触发一轮 诊断+寻优（异步，kind="optimize"）
  GET  /api/optimizer/task/<id> 手动触发任务状态
"""
from flask import Blueprint, request

from utils.api import ok, fail
from core.task_queue import submit_task, get_task_status, is_any_running

optimizer_bp = Blueprint("optimizer", __name__, url_prefix="/api/optimizer")


@optimizer_bp.route("/latest", methods=["GET"])
def latest():
    """最新优化器状态（诊断/寻优/建议/参数/历史）"""
    from strategy.optimizer import get_latest_state
    try:
        return ok(get_latest_state())
    except Exception as e:
        return fail(f"读取优化器状态失败: {e}", 500)


@optimizer_bp.route("/apply", methods=["POST"])
def apply():
    """采纳一条待处理建议，写入参数覆盖层并记录审计日志"""
    from strategy.optimizer import apply_suggestion
    body = request.get_json(silent=True) or {}
    sid = body.get("id")
    if not isinstance(sid, int):
        return fail("缺少建议 id")
    try:
        result = apply_suggestion(sid)
    except ValueError as e:
        return fail(str(e), 404)
    except Exception as e:
        return fail(f"采纳失败: {e}", 500)
    return ok(result)


@optimizer_bp.route("/dismiss", methods=["POST"])
def dismiss():
    """忽略一条待处理建议"""
    from strategy.optimizer import dismiss_suggestion
    body = request.get_json(silent=True) or {}
    sid = body.get("id")
    if not isinstance(sid, int):
        return fail("缺少建议 id")
    try:
        return ok(dismiss_suggestion(sid))
    except ValueError as e:
        return fail(str(e), 404)


@optimizer_bp.route("/rollback", methods=["POST"])
def rollback():
    """回滚参数到最近一次采纳之前的值"""
    from strategy.optimizer import rollback_param
    body = request.get_json(silent=True) or {}
    key = body.get("param_key")
    if not key:
        return fail("缺少 param_key")
    try:
        return ok(rollback_param(key))
    except ValueError as e:
        return fail(str(e), 404)
    except Exception as e:
        return fail(f"回滚失败: {e}", 500)


@optimizer_bp.route("/run", methods=["POST"])
def run_now():
    """手动触发一轮 诊断+寻优（异步执行，忽略周五限制）"""
    if is_any_running(kind="optimize"):
        return fail("已有优化任务在运行", 409)

    def _job():
        from strategy.optimizer import run_daily_diagnosis, run_weekly_tuning
        diag = run_daily_diagnosis()
        tune = run_weekly_tuning()
        sug = tune.get("suggestion")
        return (f"诊断结论 {len(diag.get('findings') or [])} 条；"
                f"寻优建议: {sug['label'] if sug else tune.get('skipped', '无')}")

    task_id = submit_task(_job, kind="optimize")
    return ok({"task_id": task_id})


@optimizer_bp.route("/task/<task_id>", methods=["GET"])
def task_status(task_id: str):
    """查询手动触发任务的状态"""
    status = get_task_status(task_id)
    if status is None:
        return fail("任务不存在", 404)
    return ok(status)
