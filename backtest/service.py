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
from config.personal_config import RESEARCH_CAPITAL


# ──────────── 内存任务状态 ────────────
_TASKS: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.RLock()

# 任务池容量与 TTL（防止长期运行内存泄漏）
_MAX_TASKS = 50
_TASK_TTL_SECONDS = 2 * 3600  # 完成后 2 小时自动清理


def _purge_stale_tasks():
    """清理已完成且超过 TTL 的任务，以及超出容量上限的旧任务。"""
    now = datetime.now()
    with _LOCK:
        # 1) 清理超 TTL 的已完成任务
        stale_ids = []
        for tid, t in _TASKS.items():
            if t["status"] in ("done", "error", "cancelled"):
                started = t.get("started_at", "")
                try:
                    started_dt = datetime.fromisoformat(started)
                    if (now - started_dt).total_seconds() > _TASK_TTL_SECONDS:
                        stale_ids.append(tid)
                except (ValueError, TypeError):
                    stale_ids.append(tid)
        for tid in stale_ids:
            del _TASKS[tid]

        # 2) 容量上限：删除最早的非运行中任务
        if len(_TASKS) > _MAX_TASKS:
            finished = [
                (tid, t.get("started_at", ""))
                for tid, t in _TASKS.items()
                if t["status"] != "running"
            ]
            finished.sort(key=lambda x: x[1])
            overflow = len(_TASKS) - _MAX_TASKS
            for tid, _ in finished[:overflow]:
                del _TASKS[tid]


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
        initial_cash=float(p.get("initial_cash", RESEARCH_CAPITAL)),
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


def _execution_config(p: Optional[BacktestParams]) -> Optional[Dict[str, Any]]:
    """执行配置快照：T+1、入场方式、止损判定时点、费用、滑点、整手规则（方案 E1/E2）。

    不同执行配置分别比较，不能把日内触价回测与收盘止损推荐称为相同实验。
    费用取当前参数快照并随结果持久化，供恢复后展示当时假设。
    """
    if p is None:
        return None
    return {
        "settlement": "T+1",
        "buy_timing": p.buy_timing,
        "entry_price_basis": "next_day_open" if p.buy_timing == "next_day_open" else "current_close",
        "exit_check_timing": "close",   # 现引擎按收盘价判定止损/止盈/到期
        "stop_loss_pct": p.stop_loss_pct,
        "take_profit_pct": p.take_profit_pct,
        "max_hold_days": p.max_hold_days,
        "commission_rate": p.commission_rate,
        "min_commission": 5.0,
        "stamp_tax_rate": p.stamp_tax_rate,
        "slippage_rate": p.slippage_rate,
        "lot_size": 100,
        "risk_free_rate": p.risk_free_rate,
    }


def _save_result(
    result: Dict[str, Any],
    rule_name: str,
    *,
    payload: Optional[Dict[str, Any]] = None,
    actual_rule_id: Optional[int] = None,
    params: Optional[BacktestParams] = None,
    data_version: Optional[str] = None,
) -> Optional[int]:
    """写入 backtest_results（含完整结果 JSON）与 backtest_trades，返回 result_id。

    仅持久化成功才返回有效 id；调用方据此决定任务是否标 done（方案 E1）。
    result_json 保存权益曲线/月度收益/未平仓记录/指标；交易明细写 backtest_trades。
    写入失败返回 None —— 调用方不得标成功、不得回写规则 fitness。
    """
    payload = payload or {}
    try:
        task_id = result.get("task_id", "") or ""
        exec_config = _execution_config(params)
        conditions = payload.get("conditions")
        params_dict = payload.get("params")
        # 结果 JSON：剔除已单独入库的 trades，避免重复膨胀；其余（权益曲线/月度/持仓/指标）全留
        result_json_obj = {k: v for k, v in result.items() if k != "trades"}
        with get_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO backtest_results (
                    rule_id, rule_name, start_date, end_date,
                    annual_return, cumulative_return, win_rate,
                    sharpe_ratio, max_drawdown, total_trades, win_trades,
                    task_id, actual_rule_id, params_json, conditions_json,
                    execution_config_json, data_version, result_json, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    # rule_id 旧列（NOT NULL）：不再混写 task_id，改存真实规则 id 或空串；
                    # 历史记录的 task_id 混写用途仅在读取端兼容（list_history 回退 rule_id）。
                    str(actual_rule_id) if actual_rule_id else "",
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
                    task_id,
                    actual_rule_id,
                    json.dumps(params_dict, ensure_ascii=False) if params_dict is not None else None,
                    json.dumps(conditions, ensure_ascii=False) if conditions is not None else None,
                    json.dumps(exec_config, ensure_ascii=False) if exec_config is not None else None,
                    data_version,
                    json.dumps(result_json_obj, ensure_ascii=False, default=str),
                    "done",
                ),
            )
            result_id = cur.lastrowid
            for t in (result.get("trades") or []):
                conn.execute(
                    """
                    INSERT INTO backtest_trades (
                        result_id, code, name, entry_date, entry_price,
                        exit_date, exit_price, holding_days, pnl_pct, exit_reason,
                        shares, pnl
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        t.get("shares"),
                        t.get("pnl"),
                    ),
                )
            conn.commit()
            return result_id
    except Exception:
        traceback.print_exc()
        return None


# ──────────── 持久化恢复（内存清空/重启后从 DB 重建）────────────

def _load_result_row(task_id: str) -> Optional[Dict[str, Any]]:
    """按 task_id 读取 backtest_results 行（兼容旧记录：task_id 列为空时回退 rule_id）。"""
    if not task_id:
        return None
    try:
        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT id, rule_id, rule_name, start_date, end_date,
                       annual_return, cumulative_return, win_rate, sharpe_ratio,
                       max_drawdown, total_trades, win_trades, created_at,
                       task_id, actual_rule_id, params_json, conditions_json,
                       execution_config_json, data_version, result_json, status
                FROM backtest_results
                WHERE task_id = ? OR (task_id IS NULL AND rule_id = ?)
                ORDER BY id DESC LIMIT 1
                """,
                (task_id, task_id),
            ).fetchone()
        return dict(row) if row else None
    except Exception:
        traceback.print_exc()
        return None


def _load_trades_rows(result_id: int) -> List[Dict[str, Any]]:
    """按 result_id 读取交易明细，重建为引擎 result['trades'] 同构字典。"""
    try:
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT code, name, entry_date, entry_price, exit_date, exit_price,
                       holding_days, pnl_pct, exit_reason, shares, pnl
                FROM backtest_trades WHERE result_id = ? ORDER BY id
                """,
                (result_id,),
            ).fetchall()
    except Exception:
        traceback.print_exc()
        return []
    out = []
    for r in rows:
        out.append({
            "code": r["code"], "name": r["name"],
            "buy_date": r["entry_date"], "buy_price": r["entry_price"],
            "sell_date": r["exit_date"], "sell_price": r["exit_price"],
            "shares": r["shares"], "pnl": r["pnl"], "pnl_pct": r["pnl_pct"],
            "hold_days": r["holding_days"], "exit_reason": r["exit_reason"],
        })
    return out


def _rebuild_result_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """从持久化行重建完整 result（含权益曲线/未平仓/指标）；缺失字段保持 None 不凭空补。"""
    result: Dict[str, Any] = {}
    rj = row.get("result_json")
    if rj:
        try:
            result = json.loads(rj)
        except (ValueError, TypeError):
            result = {}
    # 汇总指标以独立列为准（旧记录无 result_json 时仍可读）
    result.setdefault("start_date", row.get("start_date"))
    result.setdefault("end_date", row.get("end_date"))
    result["task_id"] = row.get("task_id") or row.get("rule_id")
    result["annual_return"] = row.get("annual_return")
    result["total_return"] = row.get("cumulative_return")
    result["win_rate"] = row.get("win_rate")
    result["sharpe_ratio"] = row.get("sharpe_ratio")
    result["max_drawdown"] = row.get("max_drawdown")
    result["total_trades"] = row.get("total_trades")
    result["win_trades"] = row.get("win_trades")
    # 交易明细从 backtest_trades 重建（result_json 不含 trades）
    result["trades"] = _load_trades_rows(row["id"])
    # 明确标记字段缺失：旧结果没有保存的权益曲线/参数不凭空补出
    result["_recovered"] = True
    result["_db_id"] = row["id"]
    result["_data_version"] = row.get("data_version")
    result["_execution_config"] = (
        json.loads(row["execution_config_json"]) if row.get("execution_config_json") else None
    )
    result["_status"] = row.get("status")
    if not result.get("equity_curve"):
        result["_missing_fields"] = sorted(
            {"equity_curve", "monthly_returns", "positions"} - set(result.keys())
        )
    return result


# ──────────── 任务 API ────────────

def create_task(payload: Dict[str, Any]) -> str:
    """创建任务并异步执行；返回 task_id"""
    _purge_stale_tasks()
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
        if task:
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
    # 内存已清空/重启：从持久化结果重建任务视图（方案 E1，详情/存规则可恢复）
    row = _load_result_row(task_id)
    if not row:
        return None
    status = row.get("status") or "done"
    return {
        "id": task_id,
        "status": status,
        "progress": 100 if status == "done" else 0,
        "stage": "recovered",
        "message": "回测完成（从持久化恢复）" if status == "done" else status,
        "started_at": row.get("created_at", ""),
        "cancelled": False,
        "error": None,
        "_recovered": True,
    }


def get_task_result(task_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        task = _TASKS.get(task_id)
        if task:
            return task.get("result")
    # 内存无结果：从 backtest_results + backtest_trades 完整重建
    row = _load_result_row(task_id)
    if not row:
        return None
    return _rebuild_result_from_row(row)


def get_task_payload(task_id: str) -> Optional[Dict[str, Any]]:
    """返回任务的原始请求 payload（含 conditions 与 params），供“存为规则”读取。"""
    with _LOCK:
        task = _TASKS.get(task_id)
        if task:
            return task.get("params")
    # 内存清空后从持久化的 params_json/conditions_json 恢复（存为规则可继续）
    row = _load_result_row(task_id)
    if not row:
        return None
    payload: Dict[str, Any] = {}
    if row.get("params_json"):
        try:
            payload["params"] = json.loads(row["params_json"])
        except (ValueError, TypeError):
            pass
    if row.get("conditions_json"):
        try:
            payload["conditions"] = json.loads(row["conditions_json"])
        except (ValueError, TypeError):
            pass
    if row.get("actual_rule_id"):
        payload["rule_id"] = row["actual_rule_id"]
    return payload or None


def get_task_trades(task_id: str, page: int = 1, page_size: int = 50) -> Optional[Dict[str, Any]]:
    with _LOCK:
        task = _TASKS.get(task_id)
        if task and task.get("result"):
            all_trades = task["result"].get("trades", []) or []
        else:
            all_trades = None
    if all_trades is None:
        # 内存无结果：从 backtest_trades 分页恢复（分页/导出重启后仍可用）
        row = _load_result_row(task_id)
        if not row:
            return None
        all_trades = _load_trades_rows(row["id"])
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
    """从 backtest_results 读最近 N 条历史回测。

    id 优先取新 task_id 列；旧记录（task_id 为空、task_id 混写在 rule_id）回退 rule_id，
    再回退自增 id，保证历史列表点击仍能按 task_id 恢复详情（方案 E1 兼容读取）。
    """
    try:
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT id, rule_id, rule_name, start_date, end_date,
                       annual_return, cumulative_return, win_rate,
                       sharpe_ratio, max_drawdown, total_trades, win_trades,
                       created_at, task_id, status
                FROM backtest_results
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r["task_id"] or r["rule_id"] or str(r["id"]),
                "db_id": r["id"],
                "task_id": r["task_id"] or r["rule_id"],
                "status": r["status"] or "done",
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
    """返回导出内容（dict 含 filename/content/mime）；内存无结果时从持久化恢复。"""
    with _LOCK:
        task = _TASKS.get(task_id)
        result = task.get("result") if task else None
    if not result:
        # 重启/内存清空后仍可导出（方案 E1 验收：导出可恢复）
        row = _load_result_row(task_id)
        if not row:
            return None
        result = _rebuild_result_from_row(row)
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

def _current_data_version() -> Optional[str]:
    """行情数据快照版本：daily_price 最新交易日（best-effort）。

    失败返回 None，明确标记数据版本未知，不阻断回测（方案 E1）。
    """
    try:
        with get_conn() as conn:
            row = conn.execute("SELECT MAX(trade_date) AS d FROM daily_price").fetchone()
        return row["d"] if row and row["d"] else None
    except Exception:
        return None


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

        # 先判定运行结果（取消/失败/成功）；仅成功路径才持久化。
        cancelled = bool(result.get("cancelled"))
        run_failed = not result.get("success", True)
        result_id = None
        if not cancelled and not run_failed:
            with _LOCK:
                t0 = _TASKS.get(task_id)
                payload = t0.get("params") if t0 else None
            # 只有结果持久化成功才将任务标为成功（方案 E1）；DB 写在锁外，减少持锁时间。
            result_id = _save_result(
                result, rule_name, payload=payload,
                actual_rule_id=int(rule_id) if rule_id else None,
                params=params, data_version=_current_data_version())

        writeback = False
        with _LOCK:
            t = _TASKS.get(task_id)
            if not t:
                return
            t["result"] = result
            if cancelled:
                t["status"] = "cancelled"
            elif run_failed:
                t["status"] = "failed"
                t["error"] = result.get("error")
            elif result_id is None:
                # 持久化失败：不标成功、不回写规则 fitness（方案 E1 验收）
                t["status"] = "failed"
                t["error"] = "结果持久化失败，未生成有效成绩"
            else:
                t["status"] = "done"
                t["result_id"] = result_id
                writeback = bool(rule_id)
            t["progress"] = 100
            t["message"] = (
                "回测完成" if t["status"] == "done" else
                "已取消" if t["status"] == "cancelled" else
                f"失败: {t['error']}"
            )
            t["finished_at"] = datetime.now().isoformat(timespec="seconds")
        # 推荐-回测闭环：规则绩效回写（仅持久化成功后，锁外执行）
        if writeback:
            _writeback_rule_fitness(int(rule_id), result)
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
