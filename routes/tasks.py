"""
routes/tasks.py —— 任务队列API

路由：
  POST /api/tasks/submit          → 提交任务
  GET  /api/tasks/<id>            → 查询任务
  GET  /api/tasks                 → 任务列表
  POST /api/tasks/<id>/cancel     → 取消任务
  POST /api/tasks/clear           → 清理已完成任务
  GET  /api/tasks/stats           → 队列统计
"""

from flask import Blueprint, jsonify, request

from core.task_queue import get_task_queue

tasks_bp = Blueprint("tasks", __name__, url_prefix="/api/tasks")
_queue = get_task_queue()

# ── 预定义任务函数 ──────────────────────────────

import time


def demo_task(duration: float = 2.0, should_fail: bool = False):
    """演示任务"""
    time.sleep(duration)
    if should_fail:
        raise ValueError("演示任务失败")
    return {"message": f"任务完成，耗时 {duration}s"}


def run_backtest_task(strategy: str, start_date: str, end_date: str):
    """回测任务（异步执行）"""
    from backtest.engine import run_backtest
    result = run_backtest(strategy=strategy, start_date=start_date, end_date=end_date)
    return result


def batch_score_task(stocks: list):
    """批量评分任务"""
    from governance.chancellery.scoring_engine import get_scoring_engine
    from ministries.rites.data_source_manager import get_data_source_manager
    import pandas as pd

    engine = get_scoring_engine()
    ds = get_data_source_manager()
    results = []

    for stock in stocks:
        code = stock.get("code", "").strip()
        name = stock.get("name", code)
        if not code:
            continue

        try:
            from datetime import datetime, timedelta
            end = datetime.now()
            start = end - timedelta(days=150)
            raw_data = ds.get_daily_price(code, start_date=start.strftime("%Y-%m-%d"), end_date=end.strftime("%Y-%m-%d"))
            if not raw_data or len(raw_data) < 20:
                continue

            df = pd.DataFrame(raw_data)
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
                df = df.sort_values("date")

            result = engine.score(code, name, df)
            results.append({
                "code": result.code,
                "name": result.name,
                "score": result.total_score,
                "grade": result.grade,
            })
        except Exception as e:
            print(f"[BatchScore] {code} 失败: {e}")
            continue

    results.sort(key=lambda x: x["score"], reverse=True)
    return {"evaluated": len(results), "results": results[:50]}


def run_phase1_mine_task():
    """Phase 1: 模板穷举规则挖掘"""
    from services.strategy_lab_service import run_phase1_mine
    return run_phase1_mine()


def run_phase2_evolve_task(rule_ids: list = None, generations: int = 20):
    """Phase 2: 遗传进化"""
    from services.strategy_lab_service import run_phase2_evolve
    return run_phase2_evolve(rule_ids=rule_ids, generations=generations)


def run_phase4_select_task():
    """Phase 4: 动态策略选择"""
    from services.strategy_lab_service import run_phase4_select
    return run_phase4_select()


def run_lgbm_train_task():
    """Phase 3: LightGBM 训练"""
    from services.strategy_lab_service import run_lgbm_train
    return run_lgbm_train()


def run_full_pipeline_task():
    """一键执行: P1 → P2 → P3 → P4"""
    from services.strategy_lab_service import run_full_pipeline
    return run_full_pipeline()


TASK_REGISTRY = {
    "demo": demo_task,
    "backtest": run_backtest_task,
    "batch_score": batch_score_task,
    "phase1_mine": run_phase1_mine_task,
    "phase2_evolve": run_phase2_evolve_task,
    "phase4_select": run_phase4_select_task,
    "lgbm_train": run_lgbm_train_task,
    "full_pipeline": run_full_pipeline_task,
}


# ── API 路由 ────────────────────────────────────

@tasks_bp.route("/submit", methods=["POST"])
def submit_task():
    """提交异步任务"""
    data = request.get_json() or {}
    task_type = data.get("type", "")
    name = data.get("name", task_type)
    params = data.get("params", {})

    fn = TASK_REGISTRY.get(task_type)
    if not fn:
        return jsonify({
            "success": False,
            "error": f"未知任务类型: {task_type}，可用: {list(TASK_REGISTRY.keys())}",
        }), 400

    task_id = _queue.submit(name, fn, **params)
    return jsonify({
        "success": True,
        "task_id": task_id,
        "status": "pending",
    })


@tasks_bp.route("/<task_id>", methods=["GET"])
def get_task(task_id: str):
    """查询任务状态"""
    task = _queue.get_task_dict(task_id)
    if not task:
        return jsonify({"success": False, "error": "任务不存在"}), 404
    return jsonify({"success": True, "task": task})


@tasks_bp.route("", methods=["GET"])
def list_tasks():
    """获取任务列表"""
    status = request.args.get("status")
    limit = request.args.get("limit", 100, type=int)
    tasks = _queue.list_tasks(status=status, limit=limit)
    return jsonify({
        "success": True,
        "count": len(tasks),
        "tasks": tasks,
    })


@tasks_bp.route("/<task_id>/cancel", methods=["POST"])
def cancel_task(task_id: str):
    """取消任务"""
    ok = _queue.cancel(task_id)
    return jsonify({
        "success": ok,
        "message": "已取消" if ok else "取消失败（任务已开始或不存在）",
    })


@tasks_bp.route("/clear", methods=["POST"])
def clear_tasks():
    """清理已完成任务"""
    count = _queue.clear_completed()
    return jsonify({
        "success": True,
        "cleared": count,
    })


@tasks_bp.route("/stats", methods=["GET"])
def get_stats():
    """获取队列统计"""
    return jsonify({
        "success": True,
        "stats": _queue.get_stats(),
    })


@tasks_bp.route("/types", methods=["GET"])
def get_task_types():
    """获取支持的任务类型"""
    return jsonify({
        "success": True,
        "types": [
            {"id": "demo", "name": "演示任务", "description": "用于测试队列功能"},
            {"id": "backtest", "name": "回测任务", "description": "异步执行策略回测"},
            {"id": "batch_score", "name": "批量评分", "description": "批量对股票列表进行多因子评分"},
            {"id": "phase1_mine", "name": "Phase1 规则挖掘", "description": "模板穷举 + IC筛选 + 聚类去重 + 全市场回测"},
            {"id": "phase2_evolve", "name": "Phase2 遗传进化", "description": "遗传算法进化策略规则（交叉/变异/选择）"},
            {"id": "phase4_select", "name": "Phase4 动态选股", "description": "双窗口回测评分 + 动态策略选择 + 三级后备"},
            {"id": "lgbm_train", "name": "LGBM 训练", "description": "LightGBM 融合模型训练"},
            {"id": "full_pipeline", "name": "一键执行流水线", "description": "P1 规则挖掘 → P2 遗传进化 → P3 LGBM训练 → P4 动态选股"},
        ],
    })
