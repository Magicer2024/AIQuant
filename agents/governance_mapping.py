"""
agents/governance_mapping.py —— 三省六部制与Agent映射

职责：
  1. 定义 Agent 与三省六部的对应关系
  2. 提供统一的命名转换接口
  3. 保持向后兼容

映射关系：
  DataAgent    → 太子院·数据官 (crown_prince.data_officer)
  SignalAgent  → 中书省·策略官 (chancellery.strategy_officer)
  RiskAgent    → 门下省·风控官 (censorate.risk_officer)
  BacktestAgent → 尚书省·回测官 (secretariat.backtest_officer)
  ReportAgent  → 礼部·报表官 (rites.report_officer)
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class GovernanceRole:
    """三省六部角色定义"""
    province: str  # 省名：太子院/中书省/门下省/尚书省/礼部
    ministry: Optional[str]  # 部名（可选）：吏部/户部/礼部/兵部/刑部/工部
    role: str  # 角色名：数据官/策略官/风控官等
    agent_name: str  # 对应的Agent名称
    description: str  # 职责描述


# 三省六部角色注册表
GOVERNANCE_REGISTRY: dict[str, GovernanceRole] = {
    "DataAgent": GovernanceRole(
        province="太子院",
        ministry=None,
        role="数据官",
        agent_name="DataAgent",
        description="数据前置校验、清洗、分发"
    ),
    "SignalAgent": GovernanceRole(
        province="中书省",
        ministry=None,
        role="策略官",
        agent_name="SignalAgent",
        description="策略信号生成、评分、排序"
    ),
    "RiskAgent": GovernanceRole(
        province="门下省",
        ministry="刑部",
        role="风控官",
        agent_name="RiskAgent",
        description="风控审核、一票否决、拦截"
    ),
    "BacktestAgent": GovernanceRole(
        province="尚书省",
        ministry="兵部",
        role="回测官",
        agent_name="BacktestAgent",
        description="回测验证、交易撮合"
    ),
    "ReportAgent": GovernanceRole(
        province="尚书省",
        ministry="礼部",
        role="报表官",
        agent_name="ReportAgent",
        description="业绩报表、策略排行、可视化"
    ),
}

# 反向映射：从角色名到Agent名
ROLE_TO_AGENT: dict[str, str] = {
    "数据官": "DataAgent",
    "策略官": "SignalAgent",
    "风控官": "RiskAgent",
    "回测官": "BacktestAgent",
    "报表官": "ReportAgent",
}

# 省份到Agent的映射
PROVINCE_TO_AGENTS: dict[str, list[str]] = {
    "太子院": ["DataAgent"],
    "中书省": ["SignalAgent"],
    "门下省": ["RiskAgent"],
    "尚书省": ["BacktestAgent", "ReportAgent"],
}


def get_governance_role(agent_name: str) -> Optional[GovernanceRole]:
    """根据Agent名称获取三省六部角色"""
    return GOVERNANCE_REGISTRY.get(agent_name)


def get_agent_name(role_name: str) -> Optional[str]:
    """根据角色名称获取Agent名称"""
    return ROLE_TO_AGENT.get(role_name)


def get_agents_by_province(province: str) -> list[str]:
    """根据省份获取所属Agent列表"""
    return PROVINCE_TO_AGENTS.get(province, [])


def format_agent_display(agent_name: str) -> str:
    """格式化Agent显示名称：Agent名 (三省六部角色)"""
    role = get_governance_role(agent_name)
    if role:
        return f"{agent_name} ({role.province}·{role.role})"
    return agent_name


def get_pipeline_flow() -> list[dict]:
    """获取三省六部流水线流程"""
    return [
        {
            "stage": 1,
            "province": "太子院",
            "role": "数据官",
            "agent": "DataAgent",
            "description": "数据校验",
            "parallel": False,
        },
        {
            "stage": 2,
            "province": "中书省",
            "role": "策略官",
            "agent": "SignalAgent",
            "description": "策略生成",
            "parallel": False,
        },
        {
            "stage": 3,
            "province": "门下省",
            "role": "风控官",
            "agent": "RiskAgent",
            "description": "风控审核",
            "parallel": True,
        },
        {
            "stage": 3,
            "province": "尚书省",
            "role": "回测官",
            "agent": "BacktestAgent",
            "description": "回测验证",
            "parallel": True,
        },
        {
            "stage": 4,
            "province": "尚书省",
            "role": "报表官",
            "agent": "ReportAgent",
            "description": "报表生成",
            "parallel": False,
        },
    ]
