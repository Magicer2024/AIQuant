"""
agents/base.py —— Agent基类与上下文定义

所有Agent继承 BaseAgent，统一接口：
    - run(ctx: AgentContext) -> AgentResult
    - 自动记录执行时间、异常、耗时
"""

from __future__ import annotations

import abc
import json
import time
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any


@dataclass
class AgentResult:
    """Agent执行结果"""
    agent_name: str
    success: bool
    data: dict = field(default_factory=dict)
    error: str = ""
    started_at: str = ""
    finished_at: str = ""
    elapsed_sec: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, default=str)


@dataclass
class AgentContext:
    """
    流水线共享上下文
    各Agent通过 ctx.get(key) / ctx.set(key, value) 读写数据
    """
    pipeline_id: str = ""
    run_date: str = ""
    # 各Agent结果存储
    results: dict[str, AgentResult] = field(default_factory=dict)
    # 共享数据区
    _store: dict[str, Any] = field(default_factory=dict, repr=False)

    def set(self, key: str, value: Any):
        self._store[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)

    def set_result(self, result: AgentResult):
        self.results[result.agent_name] = result

    def get_result(self, agent_name: str) -> AgentResult | None:
        return self.results.get(agent_name)

    def all_success(self, *agent_names: str) -> bool:
        """检查指定Agent是否全部成功"""
        for name in agent_names:
            r = self.results.get(name)
            if r is None or not r.success:
                return False
        return True

    def summary(self) -> dict:
        """生成流水线执行摘要"""
        return {
            "pipeline_id": self.pipeline_id,
            "run_date": self.run_date,
            "agents": {
                name: {
                    "success": r.success,
                    "elapsed_sec": round(r.elapsed_sec, 2),
                    "error": r.error[:200] if r.error else "",
                }
                for name, r in self.results.items()
            },
            "overall_success": all(r.success for r in self.results.values()),
        }


class BaseAgent(abc.ABC):
    """Agent基类"""

    name: str = "BaseAgent"

    def run(self, ctx: AgentContext) -> AgentResult:
        """执行Agent任务，自动包装异常和计时"""
        started = datetime.now()
        result = AgentResult(
            agent_name=self.name,
            success=False,
            started_at=started.strftime("%Y-%m-%d %H:%M:%S"),
        )
        try:
            t0 = time.perf_counter()
            data = self._execute(ctx)
            result.success = True
            result.data = data or {}
            result.elapsed_sec = time.perf_counter() - t0
        except Exception as e:
            result.success = False
            result.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            result.elapsed_sec = time.perf_counter() - t0 if 't0' in locals() else 0
        finally:
            result.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ctx.set_result(result)
        return result

    @abc.abstractmethod
    def _execute(self, ctx: AgentContext) -> dict:
        """子类实现的具体业务逻辑"""
        raise NotImplementedError
