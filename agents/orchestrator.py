"""
agents/orchestrator.py —— 三省六部制流水线编排器

职责：
  1. 管理三省六部执行顺序和依赖关系
  2. 支持串行和并行执行
  3. 错误处理和重试机制
  4. 状态持久化（供API查询）
  5. 定时触发集成

执行图（三省六部制）：
    太子院(DataAgent) → 中书省(SignalAgent) → [尚书省(BacktestAgent), 门下省(RiskAgent)] → 尚书省(ReportAgent)
    
    门下省一票否决：
        如果 RiskAgent 风控结果为 BLOCK，则拦截流水线，跳过 ReportAgent
"""

from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Callable

from agents.base import AgentContext, AgentResult
from agents.governance_mapping import get_governance_role, format_agent_display, get_pipeline_flow

# 延迟导入Agent类，避免缺失外部依赖（如baostock）影响编排器加载
# 各Agent在 _run_agent 中按需实例化


class PipelineOrchestrator:
    """三省六部制流水线编排器"""

    def __init__(self):
        self._lock = threading.Lock()
        self._history: list[dict] = []  # 最近执行记录
        self._current_ctx: AgentContext | None = None
        self._running = False
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

        # Agent注册表（延迟解析类）
        self._agent_modules: dict[str, tuple[str, str]] = {
            "DataAgent": ("agents.data_agent", "DataAgent"),
            "SignalAgent": ("agents.signal_agent", "SignalAgent"),
            "BacktestAgent": ("agents.backtest_agent", "BacktestAgent"),
            "RiskAgent": ("agents.risk_agent", "RiskAgent"),
            "ReportAgent": ("agents.report_agent", "ReportAgent"),
        }

        # 分步执行会话
        self._stepwise_session: dict | None = None

    # 分步执行阶段定义
    PIPELINE_STAGES: list[dict] = [
        {"stage": 1, "agents": ["DataAgent"],                   "parallel": False, "label": "Stage 1/4  太子院·数据官"},
        {"stage": 2, "agents": ["SignalAgent"],                 "parallel": False, "label": "Stage 2/4  中书省·策略官"},
        {"stage": 3, "agents": ["BacktestAgent", "RiskAgent"],  "parallel": True,  "label": "Stage 3/4  并行 门下省·风控官+尚书省·回测官"},
        {"stage": 4, "agents": ["ReportAgent"],                 "parallel": False, "label": "Stage 4/4  尚书省·报表官", "skip_on_risk_block": True},
    ]

    def _get_agent_class(self, agent_name: str):
        """延迟导入Agent类"""
        module_name, class_name = self._agent_modules[agent_name]
        mod = __import__(module_name, fromlist=[class_name])
        return getattr(mod, class_name)

    # ─────────────────────────────────────────────
    # 状态查询
    # ─────────────────────────────────────────────

    def is_running(self) -> bool:
        return self._running

    def get_current_status(self) -> dict | None:
        """获取当前执行状态"""
        if self._current_ctx is None:
            return None
        return self._current_ctx.summary()

    def get_history(self, limit: int = 10) -> list[dict]:
        """获取最近执行历史"""
        return self._history[-limit:]

    # ─────────────────────────────────────────────
    # 核心执行流程
    # ─────────────────────────────────────────────

    def run_pipeline(
        self,
        pipeline_id: str | None = None,
        run_date: str | None = None,
        on_agent_start: Callable[[str], None] | None = None,
        on_agent_finish: Callable[[str, AgentResult], None] | None = None,
    ) -> AgentContext:
        """
        执行完整流水线
        :param pipeline_id: 流水线ID（默认自动生成）
        :param run_date: 目标日期 YYYY-MM-DD，默认今天
        :param on_agent_start: Agent开始回调 (agent_name) -> None
        :param on_agent_finish: Agent完成回调 (agent_name, result) -> None
        :return: 执行上下文（含所有结果）
        """
        run_date = run_date or date.today().strftime("%Y-%m-%d")
        run_dt = datetime.strptime(run_date, "%Y-%m-%d").date()
        if run_dt > date.today():
            raise ValueError(f"不能为未来日期运行流水线: {run_date}")

        with self._lock:
            if self._running:
                raise RuntimeError("流水线已在运行中")
            self._running = True

        pipeline_id = pipeline_id or f"pipe-{run_date}-{uuid.uuid4().hex[:8]}"
        ctx = AgentContext(
            pipeline_id=pipeline_id,
            run_date=run_date,
        )
        self._current_ctx = ctx

        from scheduler.state import PIPELINE_STATUS, append_log

        # 新流水线启动时清空旧日志
        PIPELINE_STATUS["logs"].clear()

        started_at = datetime.now()
        hdr = f"\n{'='*60}"
        print(hdr, flush=True)
        print(f"[Orchestrator] 流水线启动  ID={pipeline_id}", flush=True)
        print(f"  日期: {run_date}", flush=True)
        print(f"{'='*60}\n", flush=True)
        append_log("info", f"流水线启动  ID={pipeline_id}  日期:{run_date}")

        try:
            # ── Stage 1: 太子院·数据官（串行，必须先完成）──
            print(f"\n{'-'*60}", flush=True)
            print(f"[Orchestrator] ▶ 当前阶段: Stage 1/4  太子院·数据官 (DataAgent)", flush=True)
            print(f"{'-'*60}\n", flush=True)
            append_log("info", "▶ Stage 1/4  太子院·数据官 (DataAgent)")
            self._run_agent("DataAgent", ctx, on_agent_start, on_agent_finish)

            data_result = ctx.get_result("DataAgent")
            if not data_result or not data_result.success:
                print(f"[Orchestrator] 太子院·数据官 失败，流水线中止", flush=True)
                append_log("error", "太子院·数据官 失败，流水线中止")
                return ctx

            # 非交易日处理：如有历史数据，继续执行（使用最近交易日数据）
            if not data_result.data.get("trading_day", True):
                latest_in_db = data_result.data.get("latest_in_db")
                if latest_in_db:
                    reason = data_result.data.get('skip_reason', '非交易日')
                    print(f"[Orchestrator] {reason}，使用最近交易日数据: {latest_in_db}，继续执行流水线", flush=True)
                    append_log("warn", f"{reason}，使用最近交易日数据: {latest_in_db}")
                    ctx.set("effective_trade_date", latest_in_db)
                    ctx.set("is_trading_day", False)
                    # 继续执行，不 return
                else:
                    print(f"[Orchestrator] 非交易日且数据库无历史数据，跳过后续阶段", flush=True)
                    append_log("warn", "非交易日且数据库无历史数据，跳过后续阶段")
                    return ctx

            # ── Stage 2: 中书省·策略官（依赖太子院）──
            print(f"\n{'-'*60}", flush=True)
            print(f"[Orchestrator] ▶ 当前阶段: Stage 2/4  中书省·策略官 (SignalAgent)", flush=True)
            print(f"{'-'*60}\n", flush=True)
            append_log("info", "▶ Stage 2/4  中书省·策略官 (SignalAgent)")
            self._run_agent("SignalAgent", ctx, on_agent_start, on_agent_finish)

            signal_result = ctx.get_result("SignalAgent")
            if not signal_result or not signal_result.success:
                print(f"[Orchestrator] 中书省·策略官 失败，流水线中止", flush=True)
                append_log("error", "中书省·策略官 失败，流水线中止")
                return ctx

            # ── Stage 3: 门下省·风控官 + 尚书省·回测官（并行）──
            print(f"\n{'-'*60}", flush=True)
            print(f"[Orchestrator] ▶ 当前阶段: Stage 3/4  并行执行 门下省·风控官 + 尚书省·回测官", flush=True)
            print(f"{'-'*60}\n", flush=True)
            append_log("info", "▶ Stage 3/4  并行执行 门下省·风控官 + 尚书省·回测官")
            self._run_parallel(["BacktestAgent", "RiskAgent"], ctx, on_agent_start, on_agent_finish)

            # ── 门下省一票否决（风控拦截点）──
            risk_status = ctx.get("risk_status")
            if risk_status and risk_status.overall_level.value == "block":
                reason = risk_status.block_reason
                print(f"[Orchestrator] ⚠️ 门下省风控拦截: {reason}", flush=True)
                print(f"[Orchestrator] 流水线中止，跳过尚书省·报表官", flush=True)
                append_log("error", f"⚠️ 门下省风控拦截: {reason}，流水线中止")
                # 记录风控拦截事件到上下文
                ctx.set("pipeline_interrupted_by_risk", True)
                return ctx

            # ── Stage 4: 尚书省·报表官（依赖前面所有）──
            print(f"\n{'-'*60}", flush=True)
            print(f"[Orchestrator] ▶ 当前阶段: Stage 4/4  尚书省·报表官 (ReportAgent)", flush=True)
            print(f"{'-'*60}\n", flush=True)
            append_log("info", "▶ Stage 4/4  尚书省·报表官 (ReportAgent)")
            self._run_agent("ReportAgent", ctx, on_agent_start, on_agent_finish)

        except Exception as e:
            print(f"[Orchestrator] 流水线异常: {e}", flush=True)
            append_log("error", f"流水线异常: {e}")
            traceback.print_exc()
        finally:
            finished_at = datetime.now()
            elapsed = (finished_at - started_at).total_seconds()

            summary = ctx.summary()
            summary["elapsed_total_sec"] = round(elapsed, 2)
            summary["finished_at"] = finished_at.strftime("%Y-%m-%d %H:%M:%S")

            self._history.append(summary)
            # 保留最近30条记录
            if len(self._history) > 30:
                self._history = self._history[-30:]

            self._running = False
            print(f"\n{'='*60}", flush=True)
            print(f"[Orchestrator] 流水线结束  耗时={elapsed:.1f}s  成功={summary['overall_success']}", flush=True)
            print(f"{'='*60}\n", flush=True)
            append_log("info", f"流水线结束  耗时={elapsed:.1f}s  成功={summary['overall_success']}")

        return ctx

    # ─────────────────────────────────────────────
    # Agent执行辅助
    # ─────────────────────────────────────────────

    def _run_agent(
        self,
        agent_name: str,
        ctx: AgentContext,
        on_start: Callable[[str], None] | None = None,
        on_finish: Callable[[str, AgentResult], None] | None = None,
    ) -> AgentResult:
        """执行单个Agent（含心跳打印，防止日志看起来卡死）"""
        if agent_name not in self._agent_modules:
            raise ValueError(f"未知Agent: {agent_name}")
        agent_cls = self._get_agent_class(agent_name)

        # 获取三省六部制角色名称
        display_name = format_agent_display(agent_name)

        from scheduler.state import append_log

        if on_start:
            on_start(agent_name)
        print(f"[Orchestrator] → 启动 {display_name}", flush=True)
        append_log("info", f"→ 启动 {display_name}")

        # ── 启动心跳线程：每30秒打印一次"仍在执行" ──
        self._heartbeat_stop.clear()
        _agent_start_time = time.time()

        def _heartbeat():
            while not self._heartbeat_stop.wait(timeout=30):
                elapsed = int(time.time() - _agent_start_time)
                print(f"[Orchestrator] ⏳ {display_name} 执行中... (已运行 {elapsed}s)", flush=True)

        self._heartbeat_thread = threading.Thread(target=_heartbeat, daemon=True)
        self._heartbeat_thread.start()

        # ── 执行 Agent ──
        agent = agent_cls()
        result: AgentResult | None = None
        try:
            result = agent.run(ctx)
        finally:
            # 确保心跳线程被停止
            self._heartbeat_stop.set()
            if self._heartbeat_thread:
                self._heartbeat_thread.join(timeout=1)
                self._heartbeat_thread = None

        # ── 打印结果 ──
        status = "✅ 成功" if result and result.success else "❌ 失败"
        elapsed = int(time.time() - _agent_start_time)
        print(f"[Orchestrator] ← {display_name} 完成  {status}  耗时={result.elapsed_sec:.1f}s (实际 {elapsed}s)", flush=True)
        log_level = "info" if result and result.success else "error"
        append_log(log_level, f"← {display_name} 完成  {status}  耗时={result.elapsed_sec:.1f}s")
        if result and result.error:
            print(f"  错误: {result.error[:200]}", flush=True)
            append_log("error", f"  错误: {result.error[:200]}")

        if on_finish:
            on_finish(agent_name, result)

        return result

    def _run_parallel(
        self,
        agent_names: list[str],
        ctx: AgentContext,
        on_start: Callable[[str], None] | None = None,
        on_finish: Callable[[str, AgentResult], None] | None = None,
    ) -> dict[str, AgentResult]:
        """并行执行多个Agent"""
        results = {}

        def _run_one(name: str):
            r = self._run_agent(name, ctx, on_start, on_finish)
            results[name] = r

        print(f"[Orchestrator] → 并行启动: {', '.join(agent_names)}", flush=True)
        threads = []
        for name in agent_names:
            t = threading.Thread(target=_run_one, args=(name,))
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

        print(f"[Orchestrator] ← 并行完成: {', '.join(agent_names)}", flush=True)
        return results

    # ─────────────────────────────────────────────
    # 单Agent快捷执行
    # ─────────────────────────────────────────────

    def run_single(self, agent_name: str, run_date: str | None = None) -> AgentResult:
        """单独执行一个Agent（用于调试/手动触发）"""
        run_date = run_date or date.today().strftime("%Y-%m-%d")
        run_dt = datetime.strptime(run_date, "%Y-%m-%d").date()
        if run_dt > date.today():
            raise ValueError(f"不能为未来日期运行: {run_date}")

        with self._lock:
            if self._running:
                raise RuntimeError("流水线已在运行中")
            self._running = True

        ctx = AgentContext(
            pipeline_id=f"single-{agent_name}-{uuid.uuid4().hex[:6]}",
            run_date=run_date,
        )
        self._current_ctx = ctx

        try:
            result = self._run_agent(agent_name, ctx)
            self._history.append(ctx.summary())
            if len(self._history) > 30:
                self._history = self._history[-30:]
            return result
        finally:
            self._running = False

    # ─────────────────────────────────────────────
    # 分步执行流水线（共享AgentContext）
    # ─────────────────────────────────────────────

    def start_stepwise(self, run_date: str | None = None) -> dict:
        """初始化分步执行会话，创建共享AgentContext"""
        run_date = run_date or date.today().strftime("%Y-%m-%d")
        run_dt = datetime.strptime(run_date, "%Y-%m-%d").date()
        if run_dt > date.today():
            raise ValueError(f"不能为未来日期运行流水线: {run_date}")

        with self._lock:
            if self._running:
                raise RuntimeError("流水线已在运行中")
            self._running = True

        from scheduler.state import PIPELINE_STATUS, append_log

        pipeline_id = f"stepwise-{run_date}-{uuid.uuid4().hex[:8]}"
        ctx = AgentContext(pipeline_id=pipeline_id, run_date=run_date)
        self._current_ctx = ctx

        PIPELINE_STATUS["logs"].clear()

        self._stepwise_session = {
            "session_id": pipeline_id,
            "ctx": ctx,
            "run_date": run_date,
            "current_stage": 0,
            "stage_running": False,
            "stage_complete": True,
            "pipeline_complete": False,
            "cancelled": False,
            "risk_blocked": False,
            "error": None,
        }

        print(f"[Orchestrator] 分步流水线启动  ID={pipeline_id}  日期={run_date}", flush=True)
        append_log("info", f"分步流水线启动  ID={pipeline_id}  日期:{run_date}")

        return {
            "session_id": pipeline_id,
            "run_date": run_date,
            "total_stages": len(self.PIPELINE_STAGES),
            "current_stage": 0,
        }

    def stepwise_next(self) -> dict:
        """执行分步流水线的下一个阶段（后台线程）"""
        session = self._stepwise_session
        if not session:
            raise RuntimeError("没有活跃的分步执行会话，请先调用 start")
        if session["stage_running"]:
            raise RuntimeError("当前阶段仍在执行中，请等待完成")
        if session["pipeline_complete"]:
            raise RuntimeError("流水线已执行完毕")
        if session["cancelled"]:
            raise RuntimeError("流水线已取消")

        from scheduler.state import append_log, update_pipeline_status

        session["current_stage"] += 1
        stage_idx = session["current_stage"] - 1
        stage_def = self.PIPELINE_STAGES[stage_idx]
        ctx = session["ctx"]

        # 风控拦截检查：若 RiskAgent BLOCK 则跳过 ReportAgent
        if stage_def.get("skip_on_risk_block"):
            risk_status = ctx.get("risk_status")
            if risk_status and risk_status.overall_level.value == "block":
                reason = risk_status.block_reason
                print(f"[Orchestrator] 门下省风控拦截: {reason}，跳过Stage 4", flush=True)
                append_log("error", f"门下省风控拦截: {reason}，跳过尚书省·报表官")
                ctx.set("pipeline_interrupted_by_risk", True)
                session["risk_blocked"] = True
                session["pipeline_complete"] = True
                session["stage_complete"] = True
                self._finalize_stepwise(ctx)
                return {"stage": stage_def["stage"], "skipped": True, "reason": f"风控拦截: {reason}"}

        session["stage_running"] = True
        session["stage_complete"] = False

        def _on_start(name: str):
            update_pipeline_status(agent_name=name, agent_state="running")

        def _on_finish(name: str, result):
            state = "success" if result.success else "failed"
            update_pipeline_status(agent_name=name, agent_state=state)

        def _run_stage():
            try:
                print(f"\n{'-'*60}", flush=True)
                print(f"[Orchestrator] {stage_def['label']}  ({', '.join(stage_def['agents'])})", flush=True)
                print(f"{'-'*60}\n", flush=True)
                append_log("info", f"▶ {stage_def['label']}")

                if stage_def["parallel"]:
                    self._run_parallel(stage_def["agents"], ctx, on_start=_on_start, on_finish=_on_finish)
                else:
                    for agent_name in stage_def["agents"]:
                        self._run_agent(agent_name, ctx, on_start=_on_start, on_finish=_on_finish)

                # Stage 1 后检查非交易日
                if stage_def["stage"] == 1:
                    data_result = ctx.get_result("DataAgent")
                    if data_result and data_result.success:
                        if not data_result.data.get("trading_day", True):
                            latest_in_db = data_result.data.get("latest_in_db")
                            if not latest_in_db:
                                append_log("warn", "非交易日且数据库无历史数据，流水线中止")
                                session["pipeline_complete"] = True
                                self._finalize_stepwise(ctx)
                                return

                # 最后阶段 → 完成
                if stage_idx >= len(self.PIPELINE_STAGES) - 1:
                    session["pipeline_complete"] = True
                    self._finalize_stepwise(ctx)

            except Exception as e:
                print(f"[Orchestrator] 分步执行异常: {e}", flush=True)
                traceback.print_exc()
                append_log("error", f"分步执行异常: {e}")
                session["error"] = str(e)
                session["pipeline_complete"] = True
                self._finalize_stepwise(ctx)
            finally:
                session["stage_running"] = False
                session["stage_complete"] = True

        t = threading.Thread(target=_run_stage, daemon=True)
        t.start()

        return {
            "stage": stage_def["stage"],
            "label": stage_def["label"],
            "agents": stage_def["agents"],
            "parallel": stage_def["parallel"],
        }

    def stepwise_cancel(self) -> dict:
        """取消当前分步执行会话"""
        session = self._stepwise_session
        if not session:
            raise RuntimeError("没有活跃的分步执行会话")

        from scheduler.state import append_log
        append_log("warn", "分步流水线已取消")
        session["cancelled"] = True
        session["pipeline_complete"] = True
        self._finalize_stepwise(session["ctx"])
        return {"cancelled": True, "session_id": session["session_id"]}

    def get_stepwise_status(self) -> dict | None:
        """获取当前分步会话状态"""
        session = self._stepwise_session
        if not session:
            return None
        return {
            "session_id": session["session_id"],
            "run_date": session["run_date"],
            "current_stage": session["current_stage"],
            "total_stages": len(self.PIPELINE_STAGES),
            "stage_running": session["stage_running"],
            "stage_complete": session["stage_complete"],
            "pipeline_complete": session["pipeline_complete"],
            "cancelled": session["cancelled"],
            "risk_blocked": session["risk_blocked"],
            "error": session["error"],
        }

    def _finalize_stepwise(self, ctx: AgentContext):
        """结束分步执行，写入历史，重置状态"""
        from scheduler.state import update_pipeline_status

        summary = ctx.summary()
        finished_at = datetime.now()
        summary["finished_at"] = finished_at.strftime("%Y-%m-%d %H:%M:%S")

        self._history.append(summary)
        if len(self._history) > 30:
            self._history = self._history[-30:]

        update_pipeline_status(running=False, result=summary)
        self._running = False

        print(f"\n{'='*60}", flush=True)
        print(f"[Orchestrator] 分步流水线结束  成功={summary['overall_success']}", flush=True)
        print(f"{'='*60}\n", flush=True)


# 全局单例
_orchestrator: PipelineOrchestrator | None = None


def get_orchestrator() -> PipelineOrchestrator:
    """获取全局Orchestrator单例"""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = PipelineOrchestrator()
    return _orchestrator
