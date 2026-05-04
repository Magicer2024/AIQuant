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

        # Agent注册表（延迟解析类）
        self._agent_modules: dict[str, tuple[str, str]] = {
            "DataAgent": ("agents.data_agent", "DataAgent"),
            "SignalAgent": ("agents.signal_agent", "SignalAgent"),
            "BacktestAgent": ("agents.backtest_agent", "BacktestAgent"),
            "RiskAgent": ("agents.risk_agent", "RiskAgent"),
            "ReportAgent": ("agents.report_agent", "ReportAgent"),
        }

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
        on_agent_start: Callable[[str], None] | None = None,
        on_agent_finish: Callable[[str, AgentResult], None] | None = None,
    ) -> AgentContext:
        """
        执行完整流水线
        :param pipeline_id: 流水线ID（默认自动生成）
        :param on_agent_start: Agent开始回调 (agent_name) -> None
        :param on_agent_finish: Agent完成回调 (agent_name, result) -> None
        :return: 执行上下文（含所有结果）
        """
        with self._lock:
            if self._running:
                raise RuntimeError("流水线已在运行中")
            self._running = True

        pipeline_id = pipeline_id or f"pipe-{date.today()}-{uuid.uuid4().hex[:8]}"
        ctx = AgentContext(
            pipeline_id=pipeline_id,
            run_date=date.today().strftime("%Y-%m-%d"),
        )
        self._current_ctx = ctx

        started_at = datetime.now()
        print(f"\n{'='*60}")
        print(f"[Orchestrator] 流水线启动  ID={pipeline_id}")
        print(f"{'='*60}")

        try:
            # ── Stage 1: 太子院·数据官（串行，必须先完成）──
            self._run_agent("DataAgent", ctx, on_agent_start, on_agent_finish)

            data_result = ctx.get_result("DataAgent")
            if not data_result or not data_result.success:
                print(f"[Orchestrator] 太子院·数据官 失败，流水线中止")
                return ctx

            # 非交易日处理：如有历史数据，继续执行（使用最近交易日数据）
            if not data_result.data.get("trading_day", True):
                latest_in_db = data_result.data.get("latest_in_db")
                if latest_in_db:
                    print(f"[Orchestrator] {data_result.data.get('skip_reason')}，使用最近交易日数据: {latest_in_db}，继续执行流水线")
                    ctx.set("effective_trade_date", latest_in_db)
                    ctx.set("is_trading_day", False)
                    # 继续执行，不 return
                else:
                    print(f"[Orchestrator] 非交易日且数据库无历史数据，跳过后续阶段")
                    return ctx

            # ── Stage 2: 中书省·策略官（依赖太子院）──
            self._run_agent("SignalAgent", ctx, on_agent_start, on_agent_finish)

            signal_result = ctx.get_result("SignalAgent")
            if not signal_result or not signal_result.success:
                print(f"[Orchestrator] 中书省·策略官 失败，流水线中止")
                return ctx

            # ── Stage 3: 门下省·风控官 + 尚书省·回测官（并行）──
            self._run_parallel(["BacktestAgent", "RiskAgent"], ctx, on_agent_start, on_agent_finish)

            # ── 门下省一票否决（风控拦截点）──
            risk_status = ctx.get("risk_status")
            if risk_status and risk_status.overall_level.value == "block":
                print(f"[Orchestrator] ⚠️ 门下省风控拦截: {risk_status.block_reason}")
                print(f"[Orchestrator] 流水线中止，跳过尚书省·报表官")
                # 记录风控拦截事件到上下文
                ctx.set("pipeline_interrupted_by_risk", True)
                return ctx

            # ── Stage 4: 尚书省·报表官（依赖前面所有）──
            self._run_agent("ReportAgent", ctx, on_agent_start, on_agent_finish)

        except Exception as e:
            print(f"[Orchestrator] 流水线异常: {e}")
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
            print(f"[Orchestrator] 流水线结束  耗时={elapsed:.1f}s  成功={summary['overall_success']}")
            print(f"{'='*60}\n")

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
        """执行单个Agent"""
        if agent_name not in self._agent_modules:
            raise ValueError(f"未知Agent: {agent_name}")
        agent_cls = self._get_agent_class(agent_name)

        # 获取三省六部制角色名称
        display_name = format_agent_display(agent_name)

        if on_start:
            on_start(agent_name)
        print(f"[Orchestrator] → 启动 {display_name}")

        agent = agent_cls()
        result = agent.run(ctx)

        status = "✅ 成功" if result.success else "❌ 失败"
        print(f"[Orchestrator] ← {display_name} 完成  {status}  耗时={result.elapsed_sec:.1f}s")
        if result.error:
            print(f"  错误: {result.error[:200]}")

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

        print(f"[Orchestrator] → 并行启动: {', '.join(agent_names)}")
        threads = []
        for name in agent_names:
            t = threading.Thread(target=_run_one, args=(name,))
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

        print(f"[Orchestrator] ← 并行完成: {', '.join(agent_names)}")
        return results

    # ─────────────────────────────────────────────
    # 单Agent快捷执行
    # ─────────────────────────────────────────────

    def run_single(self, agent_name: str) -> AgentResult:
        """单独执行一个Agent（用于调试/手动触发）"""
        ctx = AgentContext(
            pipeline_id=f"single-{agent_name}-{uuid.uuid4().hex[:6]}",
            run_date=date.today().strftime("%Y-%m-%d"),
        )
        return self._run_agent(agent_name, ctx)


# 全局单例
_orchestrator: PipelineOrchestrator | None = None


def get_orchestrator() -> PipelineOrchestrator:
    """获取全局Orchestrator单例"""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = PipelineOrchestrator()
    return _orchestrator
