"""
backtest/service.py —— 任务管理与持久化

提供：
  - create_task(payload)         创建任务并异步执行
  - get_task(task_id)            任务状态
  - get_task_result(task_id)     任务结果
  - get_task_trades(task_id, page, page_size)  交易明细分页
  - cancel_task(task_id)         取消任务
  - list_history(limit)          历史回测列表（从 backtest_results 表读）
  - export_task_result(task_id, fmt)  导出（CSV/JSON）

任务状态保存在内存 _TASKS 字典中（重启即清空），
最终结果会写入 core.db.backtest_results / backtest_trades 实现持久化。
"""
from __future__ import annotations

import json
import threading
import traceback
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.db import get_conn
from backtest.engine import VisualBacktestEngine, BacktestParams


# ──────────── 内存任务状态 ────────────
_TASKS: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.RLock()


# ──────────── 工具 ────────────

def _to_float_or_none(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int_or_none(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _build_params(payload: Dict[str, Any]) -> BacktestParams:
    p = payload.get("params") or {}
    return BacktestParams(
        start_date=p.get("start_date", ""),
        end_date=p.get("end_date", ""),
        initial_cash=float(p.get("initial_cash", 1_000_000)),
        max_holdings=int(p.get("max_holdings", 5)),
        max_buy_per_day=int(p.get("max_buy_per_day", 3)),
        buy_timing=p.get("buy_timing", "next_day_open"),
        take_profit_pct=_to_float_or_none(p.get("take_profit_pct")),
        stop_loss_pct=_to_float_or_none(p.get("stop_loss_pct")),
        max_hold_days=_to_int_or_none(p.get("max_hold_days")),
        commission_rate=float(p.get("commission_rate", 0.0003)),
        stamp_tax_rate=float(p.get("stamp_tax_rate", 0.001)),
        slippage_rate=float(p.get("slippage_rate", 0.001)),
        exclude_st=bool(p.get("exclude_st", True)),
        exclude_kcb=bool(p.get("exclude_kcb", True)),
        exclude_cyb=bool(p.get("exclude_cyb", False)),
        risk_free_rate=float(p.get("risk_free_rate", 0.02)),
    )


# ──────────── 持久化 ────────────

def _writeback_rule_fitness(rule_id: int, result: Dict[str, Any]):
    """回测完成后把绩效回写到 strategy_rules（推荐-回测闭环）。

    fitness 复合分（公式可调）：年化×1.5 + 胜率 + min(Sharpe,3)×0.5 - |最大回撤|
    （均用小数形式计算，如年化 30% 计 0.30；下限 0）
    """
    try:
        annual = float(result.get("annual_return", 0) or 0)
        win = float(result.get("win_rate", 0) or 0)
        sharpe = float(result.get("sharpe_ratio", 0) or 0)
        mdd = float(result.get("max_drawdown", 0) or 0)
        trades = int(result.get("total_trades", 0) or 0)
        fitness = round(max(0.0, annual * 1.5 + win + min(sharpe, 3.0) * 0.5 - abs(mdd)), 4)
        with get_conn() as conn:
            conn.execute("""
                UPDATE strategy_rules SET
                    annual_return = ?, win_rate = ?, sharpe_ratio = ?,
                    max_drawdown = ?, total_trades = ?, fitness = ?,
                    updated_at = datetime('now','localtime')
                WHERE id = ?
            """, (
                round(annual * 100, 2), round(win * 100, 2), round(sharpe, 3),
                round(mdd * 100, 2), trades, fitness, rule_id,
            ))
        print(f"[Backtest] 规则 {rule_id} 绩效已回写: fitness={fitness}")
    except Exception:
        traceback.print_exc()


def _save_result(result: Dict[str, Any], rule_name: str) -> Optional[int]:
    """写入 backtest_results 与 backtest_trades，返回 result_id"""
    try:
        with get_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO backtest_results (
                    rule_id, rule_name, start_date, end_date,
                    annual_return, cumulative_return, win_rate,
                    sharpe_ratio, max_drawdown, total_trades, win_trades
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.get("task_id", ""),
                    rule_name,
                    result["start_date"],
                    result["end_date"],
                    result.get("annual_return", 0),
                    result.get("total_return", 0),
                    result.get("win_rate", 0),
                    result.get("sharpe_ratio", 0),
                    result.get("max_drawdown", 0),
                    result.get("total_trades", 0),
                    result.get("win_trades", 0),
                ),
            )
            result_id = cur.lastrowid
            for t in (result.get("trades") or []):
                conn.execute(
                    """
                    INSERT INTO backtest_trades (
                        result_id, code, name, entry_date, entry_price,
                        exit_date, exit_price, holding_days, pnl_pct, exit_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result_id,
                        t.get("code", ""),
                        t.get("name", ""),
                        t.get("buy_date", ""),
                        t.get("buy_price"),
                        t.get("sell_date", ""),
                        t.get("sell_price"),
                        t.get("hold_days", 0),
                        t.get("pnl_pct", 0),
                        t.get("exit_reason", ""),
                    ),
                )
            conn.commit()
            return result_id
    except Exception:
        traceback.print_exc()
        return None


# ──────────── 任务 API ────────────

def create_task(payload: Dict[str, Any]) -> str:
    """创建任务并异步执行；返回 task_id"""
    task_id = uuid.uuid4().hex[:16]
    params = _build_params(payload)
    rule_name = payload.get("rule_name") or "可视化回测"

    with _LOCK:
        _TASKS[task_id] = {
            "id": task_id,
            "status": "running",
            "progress": 0,
            "stage": "init",
            "message": "任务已创建",
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "params": payload,
            "result": None,
            "result_id": None,
            "error": None,
            "cancelled": False,
            "cancel_flag": threading.Event(),
        }

    thread = threading.Thread(
        target=_run_task,
        args=(task_id, params, rule_name),
        daemon=True,
        name=f"visual-bt-{task_id}",
    )
    thread.start()
    return task_id


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        task = _TASKS.get(task_id)
        if not task:
            return None
        return {
            "id": task["id"],
            "status": task["status"],
            "progress": task.get("progress", 0),
            "stage": task.get("stage", ""),
            "message": task.get("message", ""),
            "started_at": task.get("started_at", ""),
            "cancelled": task.get("cancelled", False),
            "error": task.get("error"),
        }


def get_task_result(task_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        task = _TASKS.get(task_id)
        if not task:
            return None
        return task.get("result")


def get_task_trades(task_id: str, page: int = 1, page_size: int = 50) -> Optional[Dict[str, Any]]:
    with _LOCK:
        task = _TASKS.get(task_id)
        if not task or not task.get("result"):
            return None
        all_trades = task["result"].get("trades", []) or []
        total = len(all_trades)
        start = (page - 1) * page_size
        end = start + page_size
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "trades": all_trades[start:end],
        }


def cancel_task(task_id: str) -> bool:
    with _LOCK:
        task = _TASKS.get(task_id)
        if not task or task["status"] != "running":
            return False
        task["cancel_flag"].set()
        task["cancelled"] = True
        return True


def list_history(limit: int = 20) -> List[Dict[str, Any]]:
    """从 backtest_results 读最近 N 条历史回测"""
    try:
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT id, rule_id, rule_name, start_date, end_date,
                       annual_return, cumulative_return, win_rate,
                       sharpe_ratio, max_drawdown, total_trades, win_trades,
                       created_at
                FROM backtest_results
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r["rule_id"] or str(r["id"]),
                "db_id": r["id"],
                "name": r["rule_name"] or "可视化回测",
                "start_date": r["start_date"],
                "end_date": r["end_date"],
                "metrics": {
                    "total_return": r["cumulative_return"] or 0,
                    "annual_return": r["annual_return"] or 0,
                    "win_rate": r["win_rate"] or 0,
                    "sharpe_ratio": r["sharpe_ratio"] or 0,
                    "max_drawdown": r["max_drawdown"] or 0,
                    "total_trades": r["total_trades"] or 0,
                },
                "created_at": r["created_at"],
            })
        return out
    except Exception:
        traceback.print_exc()
        return []


def export_task_result(task_id: str, fmt: str = "csv"):
    """返回导出内容（dict 含 filename/content/mime）"""
    with _LOCK:
        task = _TASKS.get(task_id)
        if not task or not task.get("result"):
            return None
        result = task["result"]
    if fmt == "json":
        return {
            "filename": f"backtest_{task_id}.json",
            "content": json.dumps(result, ensure_ascii=False, default=str),
            "mime": "application/json",
        }
    # csv
    import csv
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["买入日期", "股票代码", "股票名称", "买入价", "卖出日期",
                     "卖出价", "数量", "盈亏金额", "收益率", "持仓天数", "卖出原因"])
    for t in (result.get("trades") or []):
        writer.writerow([
            t.get("buy_date", ""), t.get("code", ""), t.get("name", ""),
            t.get("buy_price", ""), t.get("sell_date", ""), t.get("sell_price", ""),
            t.get("shares", ""), t.get("pnl", ""), t.get("pnl_pct", ""),
            t.get("hold_days", ""), t.get("exit_reason", ""),
        ])
    return {"filename": f"backtest_{task_id}.csv", "content": buf.getvalue(), "mime": "text/csv"}


# ──────────── 内部：执行任务 ────────────

def _run_task(task_id: str, params: BacktestParams, rule_name: str):
    with _LOCK:
        task = _TASKS.get(task_id)
        if not task:
            return
        cancel_flag = task["cancel_flag"]

    def progress_cb(info: Dict[str, Any]):
        with _LOCK:
            t = _TASKS.get(task_id)
            if not t:
                return
            t["stage"] = info.get("stage", t.get("stage"))
            t["message"] = info.get("message", t.get("message", ""))
            if "progress" in info:
                t["progress"] = int(info["progress"])

    try:
        rule_id = None
        with _LOCK:
            t0 = _TASKS.get(task_id)
            if t0:
                rule_id = (t0.get("params") or {}).get("rule_id")

        if rule_id:
            # 策略规则一键回测路径（因子规则 → 全市场组合模拟）
            from strategy.rules_store import get_rule
            from backtest.rule_engine import run_rule_backtest
            rule = get_rule(int(rule_id))
            if not rule:
                raise ValueError(f"策略规则不存在: {rule_id}")
            result = run_rule_backtest(
                rule, params, progress_cb=progress_cb, cancel_flag=cancel_flag)
        else:
            engine = VisualBacktestEngine(
                params=params,
                progress_cb=progress_cb,
                cancel_flag=cancel_flag,
            )
            result = engine.run(conditions=_get_task_conditions(task_id))
        result["task_id"] = task_id

        with _LOCK:
            t = _TASKS.get(task_id)
            if not t:
                return
            t["result"] = result
            if result.get("cancelled"):
                t["status"] = "cancelled"
            elif not result.get("success", True):
                t["status"] = "failed"
                t["error"] = result.get("error")
            else:
                t["status"] = "done"
                # 持久化
                t["result_id"] = _save_result(result, rule_name)
                # 推荐-回测闭环：规则绩效回写
                if rule_id:
                    _writeback_rule_fitness(int(rule_id), result)
            t["progress"] = 100
            t["message"] = (
                "回测完成" if t["status"] == "done" else
                "已取消" if t["status"] == "cancelled" else
                f"失败: {t['error']}"
            )
            t["finished_at"] = datetime.now().isoformat(timespec="seconds")
    except Exception as e:
        traceback.print_exc()
        with _LOCK:
            t = _TASKS.get(task_id)
            if not t:
                return
            t["status"] = "failed"
            t["error"] = str(e)
            t["message"] = f"异常: {e}"
            t["finished_at"] = datetime.now().isoformat(timespec="seconds")


def _get_task_conditions(task_id: str) -> List[Dict[str, Any]]:
    """从任务载荷中取出选股条件列表"""
    with _LOCK:
        t = _TASKS.get(task_id)
        if not t:
            return []
        return list(t.get("params", {}).get("conditions") or [])
