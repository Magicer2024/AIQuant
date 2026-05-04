"""
llm/advisor.py —— 量化策略 AI 顾问

职责：
  1. 基于 LLM 提供策略建议（买入/卖出/持有分析）
  2. 市场分析（大盘情绪、板块轮动）
  3. 将自然语言策略意图通过 LLM 增强解析
  4. 生成交易决策的理由说明

降级机制：
  LLM 不可用时，回退到本地规则引擎（strategy/intent/parser.py）
"""

import json
from dataclasses import asdict

from llm.client import LLMClient, get_llm_client
from strategy.intent.parser import StrategyIntentParser, get_intent_parser


class StrategyAdvisor:
    """策略 AI 顾问"""

    SYSTEM_PROMPT_STRATEGY = """你是一位专业的 A 股量化策略分析师。
请基于用户提供的市场数据和策略意图，给出结构化的分析建议。

输出格式必须是 JSON：
{
  "recommendation": "buy|sell|hold|watch",
  "confidence": 0-1,
  "reasoning": "详细分析理由",
  "risk_factors": ["风险1", "风险2"],
  "suggested_position": "light|normal|heavy",
  "time_horizon": "short|medium|long"
}

注意：
- A股中红色代表上涨，绿色代表下跌
- 回答需简洁专业，不要套话
- 若数据不足，明确说明
"""

    SYSTEM_PROMPT_MARKET = """你是一位 A 股市场情绪分析师。
请基于提供的市场数据，给出大盘情绪和板块分析。

输出 JSON 格式：
{
  "market_sentiment": "bullish|bearish|neutral",
  "sentiment_score": 0-100,
  "hot_sectors": ["板块1", "板块2"],
  "cold_sectors": ["板块1", "板块2"],
  "key_events": ["事件1", "事件2"],
  "advice": "操作建议"
}
"""

    SYSTEM_PROMPT_INTENT = """你是一位量化交易系统中的策略解析专家。
请将用户的自然语言策略描述解析为结构化的量化条件。

输出 JSON 格式：
{
  "intent_type": "oversold_rebound|volume_breakout|trend_following|mean_reversion|momentum|custom",
  "conditions": [
    {"type": "条件类型", "params": {}, "description": "条件描述"}
  ],
  "params": {"days": 20, "threshold": 0.05},
  "explanation": "策略解释"
}

常见条件类型：
- price_change（涨跌幅）
- rsi（RSI指标）
- ma_cross（均线交叉）
- volume_ratio（量比）
- consecutive_up（连续上涨）
- price_near_ma（价格接近均线）
"""

    def __init__(self, llm_client: LLMClient = None):
        self.llm = llm_client or get_llm_client()

    def analyze_strategy(self, code: str, market_data: dict, user_query: str = "") -> dict:
        """
        分析个股策略建议
        :param code: 股票代码
        :param market_data: 包含技术指标的字典
        :param user_query: 用户额外问题
        :return: 结构化建议
        """
        prompt = f"""请分析股票 {code} 的交易策略：

市场数据：
{json.dumps(market_data, ensure_ascii=False, indent=2)}

用户问题：{user_query or "请给出买入/卖出/持有建议"}
"""
        resp = self.llm.chat([
            {"role": "system", "content": self.SYSTEM_PROMPT_STRATEGY},
            {"role": "user", "content": prompt},
        ], temperature=0.3)

        if not resp.success:
            return {
                "recommendation": "unknown",
                "confidence": 0,
                "reasoning": f"LLM 分析失败: {resp.error}",
                "risk_factors": [],
                "fallback": True,
            }

        return self._safe_parse_json(resp.content)

    def analyze_market(self, market_summary: dict) -> dict:
        """
        分析大盘情绪
        :param market_summary: 大盘数据摘要
        :return: 情绪分析
        """
        prompt = f"""请分析当前 A 股市场情绪：

市场摘要：
{json.dumps(market_summary, ensure_ascii=False, indent=2)}
"""
        resp = self.llm.chat([
            {"role": "system", "content": self.SYSTEM_PROMPT_MARKET},
            {"role": "user", "content": prompt},
        ], temperature=0.4)

        if not resp.success:
            return {
                "market_sentiment": "unknown",
                "sentiment_score": 50,
                "advice": f"分析失败: {resp.error}",
                "fallback": True,
            }

        return self._safe_parse_json(resp.content)

    def parse_intent_with_llm(self, text: str) -> dict:
        """
        使用 LLM 解析策略意图（比本地正则更智能）
        :param text: 自然语言描述
        :return: 结构化意图
        """
        resp = self.llm.chat([
            {"role": "system", "content": self.SYSTEM_PROMPT_INTENT},
            {"role": "user", "content": f"请解析以下策略描述：\n{text}"},
        ], temperature=0.2, max_tokens=1024)

        if not resp.success:
            # 降级到本地解析器
            parser = get_intent_parser()
            intent = parser.parse(text)
            return {
                "intent_type": intent.intent_type,
                "conditions": intent.conditions,
                "params": intent.params,
                "confidence": intent.confidence,
                "explanation": intent.explanation,
                "fallback": True,
                "fallback_reason": resp.error,
            }

        result = self._safe_parse_json(resp.content)
        result["llm_enhanced"] = True
        return result

    def generate_trade_reason(self, order_info: dict, market_context: dict) -> str:
        """
        生成交易决策的理由说明
        :param order_info: 订单信息
        :param market_context: 市场上下文
        :return: 自然语言理由
        """
        prompt = f"""请为以下交易决策生成一段简洁的理由说明（50字以内）：

订单：{json.dumps(order_info, ensure_ascii=False)}
市场背景：{json.dumps(market_context, ensure_ascii=False)}
"""
        resp = self.llm.chat([
            {"role": "system", "content": "你是量化交易系统的决策解释器。用简洁专业的中文说明交易理由。"},
            {"role": "user", "content": prompt},
        ], temperature=0.5, max_tokens=200)

        if resp.success:
            return resp.content.strip()
        return "基于系统策略信号自动执行"

    def _safe_parse_json(self, text: str) -> dict:
        """安全解析 JSON，容错处理"""
        # 尝试提取 JSON 块
        text = text.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 尝试从文本中找 JSON 部分
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1:
                try:
                    return json.loads(text[start:end+1])
                except json.JSONDecodeError:
                    pass
            return {
                "raw_response": text,
                "parse_error": True,
            }


# 全局单例
_advisor: StrategyAdvisor | None = None


def get_advisor() -> StrategyAdvisor:
    """获取顾问单例"""
    global _advisor
    if _advisor is None:
        _advisor = StrategyAdvisor()
    return _advisor
