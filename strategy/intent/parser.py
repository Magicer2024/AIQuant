"""
strategy/intent/parser.py —— 策略意图解析器

职责：
  1. 将自然语言策略描述解析为结构化条件配置
  2. 支持常见策略模式识别
  3. 生成可执行的策略参数

示例输入：
  "找出近20日跌幅超过15%但近3日出现放量的股票"
  "MA5上穿MA20的放量突破股"
  "RSI低于30的超跌反弹股"

输出：
  结构化条件配置，可直接用于扫描和回测
"""

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class StrategyIntent:
    """解析后的策略意图"""
    original_text: str
    intent_type: str
    conditions: list[dict]
    params: dict = field(default_factory=dict)
    confidence: float = 0.0
    explanation: str = ""


class StrategyIntentParser:
    """策略意图解析器"""

    # 常见策略模式正则
    PATTERNS = {
        "oversold_rebound": [
            r"(?:超跌|跌幅|下跌).*(?:反弹|反弹股)",
            r"RSI.*?低于\s*(\d+)",
            r"(?:超卖|超跌).*?(\d+)日",
        ],
        "volume_breakout": [
            r"(?:放量|成交量).*(?:突破|放大|增加)",
            r"(?:突破|上穿).*?MA(\d+)",
            r"(?:均线|MA).*?(?:金叉|多头排列)",
        ],
        "trend_following": [
            r"(?:趋势|上涨|多头).*(?:跟随|持股)",
            r"(?:均线|MA).*?(?:多头|排列)",
            r"连续\s*(\d+)\s*日?.*?上涨",
        ],
        "mean_reversion": [
            r"(?:回调|回踩|回撤).*?(?:买入|低吸)",
            r"(?:跌破|触及).*?MA(\d+)",
            r"(?:偏离|远离).*?(?:均线|MA)",
        ],
        "momentum": [
            r"(?:动量|强势|领涨).*(?:股|票)",
            r"(?:涨幅|上涨).*?(\d+)%",
            r"(?:连板|涨停)",
        ],
    }

    # 参数提取正则
    PARAM_PATTERNS = {
        "days": r"(?:近|过去|最近|前)\s*(\d+)\s*日?",
        "drop_pct": r"(?:跌幅|下跌|回撤|回调)\s*(?:超过|大于|≥|>=?)\s*(\d+\.?\d*)%?",
        "rise_pct": r"(?:涨幅|上涨|增长)\s*(?:超过|大于|≥|>=?)\s*(\d+\.?\d*)%?",
        "volume_ratio": r"(?:量比|成交量比|放量)\s*(?:超过|大于|≥|>=?)\s*(\d+\.?\d*)",
        "rsi_threshold": r"RSI\s*(?:低于|小于|≤|<=?)\s*(\d+)",
        "ma_short": r"MA(\d+).*?(?:上穿|突破|金叉)",
        "ma_long": r"(?:上穿|突破|金叉).*?MA(\d+)",
        "consecutive_days": r"连续\s*(\d+)\s*日?",
        "price_above": r"(?:价格|股价|收盘).*?(?:高于|大于|突破).*?(\d+\.?\d*)",
        "price_below": r"(?:价格|股价|收盘).*?(?:低于|小于|跌破).*?(\d+\.?\d*)",
    }

    def parse(self, text: str) -> StrategyIntent:
        """
        解析策略意图
        :param text: 自然语言描述
        :return: 结构化策略意图
        """
        text = text.strip()
        if not text:
            return StrategyIntent(
                original_text=text,
                intent_type="unknown",
                conditions=[],
                confidence=0.0,
                explanation="输入为空",
            )

        # 1. 识别策略类型
        intent_type, type_confidence = self._detect_intent_type(text)

        # 2. 提取参数
        params = self._extract_params(text)

        # 3. 构建条件
        conditions = self._build_conditions(intent_type, params, text)

        # 4. 计算整体置信度
        confidence = self._calculate_confidence(type_confidence, params, conditions)

        # 5. 生成解释
        explanation = self._generate_explanation(intent_type, conditions, params)

        return StrategyIntent(
            original_text=text,
            intent_type=intent_type,
            conditions=conditions,
            params=params,
            confidence=confidence,
            explanation=explanation,
        )

    def _detect_intent_type(self, text: str) -> tuple[str, float]:
        """检测策略类型"""
        scores = {}
        for intent_type, patterns in self.PATTERNS.items():
            score = 0
            for pattern in patterns:
                matches = re.findall(pattern, text, re.IGNORECASE)
                score += len(matches)
            scores[intent_type] = score

        if not scores or max(scores.values()) == 0:
            return "unknown", 0.0

        best_type = max(scores, key=scores.get)
        best_score = scores[best_type]
        total_score = sum(scores.values())
        confidence = best_score / (total_score + 1)

        return best_type, min(confidence, 1.0)

    def _extract_params(self, text: str) -> dict:
        """提取参数"""
        params = {}
        for key, pattern in self.PARAM_PATTERNS.items():
            matches = re.findall(pattern, text, re.IGNORECASE)
            if matches:
                try:
                    params[key] = float(matches[0])
                except ValueError:
                    params[key] = matches[0]
        return params

    def _build_conditions(self, intent_type: str, params: dict, text: str) -> list[dict]:
        """根据类型和参数构建条件"""
        conditions = []

        if intent_type == "oversold_rebound":
            days = int(params.get("days", 20))
            drop = params.get("drop_pct", 15)
            rsi = params.get("rsi_threshold", 30)
            conditions.append({
                "type": "price_change",
                "period": days,
                "operator": "<=",
                "value": -drop,
                "description": f"近{days}日跌幅超过{drop}%",
            })
            conditions.append({
                "type": "rsi",
                "period": 14,
                "operator": "<=",
                "value": rsi,
                "description": f"RSI低于{rsi}",
            })

        elif intent_type == "volume_breakout":
            ma_short = int(params.get("ma_short", 5))
            ma_long = int(params.get("ma_long", 20))
            vol_ratio = params.get("volume_ratio", 1.5)
            conditions.append({
                "type": "ma_cross",
                "short": ma_short,
                "long": ma_long,
                "description": f"MA{ma_short}上穿MA{ma_long}",
            })
            conditions.append({
                "type": "volume_ratio",
                "operator": ">=",
                "value": vol_ratio,
                "description": f"量比超过{vol_ratio}",
            })

        elif intent_type == "trend_following":
            days = int(params.get("consecutive_days", params.get("days", 5)))
            conditions.append({
                "type": "consecutive_up",
                "period": days,
                "description": f"连续{days}日上涨",
            })
            conditions.append({
                "type": "ma_alignment",
                "short": 5,
                "mid": 20,
                "long": 60,
                "description": "均线多头排列",
            })

        elif intent_type == "mean_reversion":
            days = int(params.get("days", 10))
            ma_period = int(params.get("ma_long", 20))
            conditions.append({
                "type": "price_near_ma",
                "ma_period": ma_period,
                "tolerance": 0.02,
                "description": f"价格接近MA{ma_period}",
            })
            conditions.append({
                "type": "pullback",
                "period": days,
                "max_drop": params.get("drop_pct", 5),
                "description": f"近{days}日回调不超过{params.get('drop_pct', 5)}%",
            })

        elif intent_type == "momentum":
            days = int(params.get("days", 5))
            rise = params.get("rise_pct", 10)
            conditions.append({
                "type": "price_change",
                "period": days,
                "operator": ">=",
                "value": rise,
                "description": f"近{days}日涨幅超过{rise}%",
            })
            conditions.append({
                "type": "volume_increase",
                "operator": ">=",
                "value": 1.2,
                "description": "成交量放大",
            })

        else:
            # 通用条件：尝试提取涨跌和天数
            days = int(params.get("days", 20))
            if "drop_pct" in params:
                conditions.append({
                    "type": "price_change",
                    "period": days,
                    "operator": "<=",
                    "value": -params["drop_pct"],
                    "description": f"近{days}日跌幅超过{params['drop_pct']}%",
                })
            elif "rise_pct" in params:
                conditions.append({
                    "type": "price_change",
                    "period": days,
                    "operator": ">=",
                    "value": params["rise_pct"],
                    "description": f"近{days}日涨幅超过{params['rise_pct']}%",
                })
            else:
                conditions.append({
                    "type": "custom",
                    "description": "自定义条件，需手动配置",
                })

        return conditions

    def _calculate_confidence(self, type_confidence: float, params: dict, conditions: list) -> float:
        """计算整体置信度"""
        param_score = min(len(params) * 0.15, 0.4)
        condition_score = min(len(conditions) * 0.1, 0.3)
        return min(type_confidence + param_score + condition_score, 1.0)

    def _generate_explanation(self, intent_type: str, conditions: list, params: dict) -> str:
        """生成策略解释"""
        type_names = {
            "oversold_rebound": "超跌反弹策略",
            "volume_breakout": "放量突破策略",
            "trend_following": "趋势跟踪策略",
            "mean_reversion": "均值回归策略",
            "momentum": "动量策略",
            "unknown": "自定义策略",
        }
        name = type_names.get(intent_type, "自定义策略")
        cond_desc = "，".join([c["description"] for c in conditions])
        return f"{name}：{cond_desc}"


# 全局单例
_parser: StrategyIntentParser | None = None


def get_intent_parser() -> StrategyIntentParser:
    """获取解析器单例"""
    global _parser
    if _parser is None:
        _parser = StrategyIntentParser()
    return _parser
