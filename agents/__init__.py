"""
agents/ —— 多Agent协作Alpha流水线

执行流：
    DataAgent → SignalAgent → [BacktestAgent, RiskAgent并行] → ReportAgent

用途：
    每日无人值守自动完成数据同步→策略扫描→回测验证→风险评估→生成报告

延迟导入说明：
    为避免循环导入和缺失依赖（如 baostock/akshare）影响基础模块加载，
    顶层不直接 import 具体 Agent 类。使用时按需 import：

        from agents.data_agent import DataAgent
        from agents.orchestrator import PipelineOrchestrator
"""

__all__ = [
    "base",
    "data_agent",
    "signal_agent",
    "backtest_agent",
    "risk_agent",
    "report_agent",
    "orchestrator",
]
