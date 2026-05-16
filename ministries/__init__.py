"""
ministries/ -- 六部职能模块

六部分工：
  吏部(personnel) -- 账户管理、用户权限
  户部(revenue)   -- 资金管理、资产核算、收益统计
  礼部(rites)     -- 数据服务、数据源接入、数据清洗、报告生成
  兵部(war)       -- 交易执行、订单管理、委托回报
  刑部(justice)   -- 风控规则、审计日志、合规检查
  工部(works)     -- 基础设施、配置管理、监控告警、策略部署
"""

MINISTRY_REGISTRY = {
    "personnel": {
        "name": "吏部",
        "description": "账户管理、用户权限",
        "module": "ministries.personnel",
    },
    "revenue": {
        "name": "户部",
        "description": "资金管理、资产核算、收益统计",
        "module": "ministries.revenue",
    },
    "rites": {
        "name": "礼部",
        "description": "数据服务、数据源接入、报告生成",
        "module": "ministries.rites",
    },
    "war": {
        "name": "兵部",
        "description": "交易执行、订单管理",
        "module": "ministries.war",
    },
    "justice": {
        "name": "刑部",
        "description": "风控规则、审计日志、合规检查",
        "module": "ministries.justice",
    },
    "works": {
        "name": "工部",
        "description": "基础设施、监控告警、策略部署",
        "module": "ministries.works",
    },
}


def get_ministry_info(name: str) -> dict | None:
    """获取 ministry 信息"""
    return MINISTRY_REGISTRY.get(name)


def list_ministries() -> list[dict]:
    """列出所有六部"""
    return [{"id": k, **v} for k, v in MINISTRY_REGISTRY.items()]
