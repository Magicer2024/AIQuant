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
  GET  /api/backtest/rules               可回测的策略规则列表（供前端下拉）
  POST /api/backtest/parse_intent        自然语言策略 → 条件+参数（LLM/本地降级）
"""
from __future__ import annotations

import json
import traceback

from flask import Blueprint, Response, request

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
    # rule_id（策略规则一键回测）与 conditions（可视化条件回测）至少其一
    rule_id = payload.get("rule_id") or params.get("rule_id")
    if not rule_id and not payload.get("conditions"):
        return fail("请至少添加一条选股条件或选择策略规则")
    if rule_id:
        try:
            params["rule_id"] = int(rule_id)
            payload["params"] = params
        except (TypeError, ValueError):
            return fail("rule_id 非法")
    try:
        task_id = bt_service.create_task(payload)
        return ok({"task_id": task_id})
    except Exception as e:
        traceback.print_exc()
        return fail(str(e), http=500)


# ──────────── 可回测的策略规则列表 ────────────
@backtest_bp.route("/rules", methods=["GET"])
def list_backtest_rules():
    """返回 is_active=1 的策略规则，供前端"从策略规则导入"下拉"""
    from strategy.rules_store import list_rules
    items = [
        {
            "id": r["id"],
            "rule_name": r["rule_name"],
            "horizon": r.get("horizon") or "short",
            "fitness": r.get("fitness"),
            "win_rate": r.get("win_rate"),
            "annual_return": r.get("annual_return"),
            "holding_min": r.get("holding_min"),
            "holding_max": r.get("holding_max"),
        }
        for r in list_rules(active_only=True)
    ]
    return ok({"items": items})


# ──────────── 回测结果存为常驻规则 ────────────
@backtest_bp.route("/save_as_rule", methods=["POST"])
def save_as_rule():
    """把一次可视化条件回测的结果存为常驻策略规则（选股→回测→推荐闭环）。

    Body: {"task_id": "<回测任务 id>", "rule_name": "<规则名>"}
    仅支持可视化条件回测（含 conditions）；rule_id 型回测已是规则，直接拒绝。
    去重：同名→更新（save_rule）；同条件异名→409。
    """
    from strategy.rules_store import save_rule, find_rule_by_conditions, derive_horizon

    payload = request.get_json(force=True, silent=True) or {}
    task_id = (payload.get("task_id") or "").strip()
    rule_name = (payload.get("rule_name") or "").strip()
    if not task_id:
        return fail("缺少 task_id")
    if not rule_name:
        return fail("请填写规则名称")

    task = bt_service.get_task(task_id)
    if not task:
        return fail("回测任务不存在或已过期", 404)
    if task.get("status") != "done":
        return fail("回测尚未完成，无法存为规则")

    task_payload = bt_service.get_task_payload(task_id) or {}
    if task_payload.get("rule_id") or (task_payload.get("params") or {}).get("rule_id"):
        return fail("该回测来自已有规则，无需再次保存")

    conditions = task_payload.get("conditions")
    if not conditions:
        return fail("回测无选股条件，无法存为规则")
    conditions_json = json.dumps(conditions, ensure_ascii=False)

    result = bt_service.get_task_result(task_id)
    if not result:
        return fail("回测结果不可用")

    # 内容去重：同条件且规则名不同→409
    dup = find_rule_by_conditions(conditions_json)
    if dup and dup.get("rule_name") != rule_name:
        return fail(f"已存在相同条件的规则：{dup.get('rule_name')}", 409)

    params = task_payload.get("params") or {}
    holding_max = params.get("max_hold_days") or 20
    try:
        holding_max = int(holding_max)
    except (TypeError, ValueError):
        holding_max = 20

    annual = float(result.get("annual_return", 0) or 0)
    win = float(result.get("win_rate", 0) or 0)
    sharpe = float(result.get("sharpe_ratio", 0) or 0)
    mdd = float(result.get("max_drawdown", 0) or 0)
    trades = int(result.get("total_trades", 0) or 0)
    fitness = round(max(0.0, annual * 1.5 + win + min(sharpe, 3.0) * 0.5 - abs(mdd)), 4)

    rule = {
        "rule_name": rule_name,
        "rule_type": "visual",
        "encoding": conditions_json,
        "conditions": conditions_json,
        "sell_conditions": "",
        "holding_min": 3,
        "holding_max": holding_max,
        "horizon": derive_horizon(holding_max),
        "source": "backtest",
        "fitness": fitness,
        "annual_return": round(annual * 100, 2),
        "win_rate": round(win * 100, 2),
        "sharpe_ratio": round(sharpe, 3),
        "max_drawdown": round(mdd * 100, 2),
        "total_trades": trades,
    }
    try:
        rule_id = save_rule(rule)
    except Exception as e:
        traceback.print_exc()
        return fail(str(e), 500)
    return ok({"rule_id": rule_id, "rule_name": rule_name, "horizon": rule["horizon"]})


# ──────────── 自然语言策略解析 ────────────
@backtest_bp.route("/parse_intent", methods=["POST"])
def parse_intent():
    """自然语言策略 → 选股条件 + 回测参数

    Body: {"text": "换手率1%~20%，MA5上穿MA20，回测近一年，止损5%止盈15%"}
    主路径走 LLM（config/llm_config.yaml + 环境变量 LLM_API_KEY）；
    未配置密钥时降级为本地正则参数提取，不会硬失败。
    """
    payload = request.get_json(force=True, silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return fail("请输入策略描述")
    try:
        from backtest.nl_parser import parse_backtest_intent
        return ok(parse_backtest_intent(text))
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
