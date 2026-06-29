"""
routes/backtest.py —— 智能可视化回测 API 路由

提供：
  GET  /api/backtest/conditions          选股条件元数据
  POST /api/backtest/run                 提交回测任务
  GET  /api/backtest/<id>/status         轮询任务状态
  GET  /api/backtest/<id>/result         获取完整结果
  GET  /api/backtest/<id>/trades         交易明细分页
  POST /api/backtest/<id>/cancel         取消任务
  GET  /api/backtest/history             历史回测列表
  GET  /api/backtest/<id>/export         导出 CSV/JSON
"""
from __future__ import annotations

import traceback

from flask import Blueprint, Response, jsonify, request

from utils.api import ok, fail
from backtest.conditions import get_catalog
from backtest import service as bt_service


backtest_bp = Blueprint("backtest", __name__, url_prefix="/api/backtest")


# ──────────── 选股条件元数据 ────────────
@backtest_bp.route("/conditions", methods=["GET"])
def conditions():
    """返回前端配置面板所需的指标字典"""
    return ok({"indicators": get_catalog()})


# ──────────── 提交回测 ────────────
@backtest_bp.route("/run", methods=["POST"])
def run_backtest():
    payload = request.get_json(force=True, silent=True) or {}
    params = payload.get("params") or {}
    if not params.get("start_date") or not params.get("end_date"):
        return fail("请填写回测起止日期")
    if not payload.get("conditions"):
        return fail("请至少添加一条选股条件")
    try:
        task_id = bt_service.create_task(payload)
        return ok({"task_id": task_id})
    except Exception as e:
        traceback.print_exc()
        return fail(str(e), http=500)


# ──────────── 任务状态 ────────────
@backtest_bp.route("/<task_id>/status", methods=["GET"])
def task_status(task_id: str):
    task = bt_service.get_task(task_id)
    if not task:
        return fail("任务不存在", http=404)
    return ok(task)


# ──────────── 任务结果 ────────────
@backtest_bp.route("/<task_id>/result", methods=["GET"])
def task_result(task_id: str):
    result = bt_service.get_task_result(task_id)
    if result is None:
        task = bt_service.get_task(task_id)
        if not task:
            return fail("任务不存在", http=404)
        if task.get("status") == "running":
            return fail("任务尚未完成", http=202)
        if task.get("status") == "failed":
            return fail(task.get("error") or "回测失败", http=500)
        return fail("结果不可用", http=404)
    return ok({"data": result})


# ──────────── 交易明细分页 ────────────
@backtest_bp.route("/<task_id>/trades", methods=["GET"])
def task_trades(task_id: str):
    page = int(request.args.get("page", 1))
    page_size = int(request.args.get("page_size", 50))
    data = bt_service.get_task_trades(task_id, page=page, page_size=page_size)
    if data is None:
        return fail("任务不存在或尚未完成", http=404)
    return ok(data)


# ──────────── 取消任务 ────────────
@backtest_bp.route("/<task_id>/cancel", methods=["POST"])
def task_cancel(task_id: str):
    if not bt_service.cancel_task(task_id):
        return fail("任务不存在或已结束", http=400)
    return ok()


# ──────────── 历史回测列表 ────────────
@backtest_bp.route("/history", methods=["GET"])
def task_history():
    limit = int(request.args.get("limit", 20))
    return ok({"items": bt_service.list_history(limit=limit)})


# ──────────── 导出 ────────────
@backtest_bp.route("/<task_id>/export", methods=["GET"])
def task_export(task_id: str):
    fmt = request.args.get("format", "csv")
    payload = bt_service.export_task_result(task_id, fmt=fmt)
    if not payload:
        return fail("任务不存在或没有结果", http=404)
    return Response(
        payload["content"],
        mimetype=payload["mime"],
        headers={"Content-Disposition": f'attachment; filename="{payload["filename"]}"'},
    )
