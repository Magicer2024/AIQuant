"""llm/ —— 大语言模型接入模块

支持 OpenAI 兼容格式（含 DeepSeek、Moonshot、通义千问等国内模型）
统一封装，自动降级到本地规则引擎。
"""

from llm.client import LLMClient, get_llm_client
from llm.advisor import StrategyAdvisor, get_advisor

__all__ = ["LLMClient", "get_llm_client", "StrategyAdvisor", "get_advisor"]
