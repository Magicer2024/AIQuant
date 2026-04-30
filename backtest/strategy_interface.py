"""
backtest/strategy_interface.py —— 策略插件接口定义
===============================================
所有回测策略（放量突破、超跌反弹v4、均线粘合等）都实现 Strategy 接口。
回测引擎只接收 Strategy 实例列表，不关心具体策略类型。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional, Any
import pandas as pd


@dataclass
class Signal:
    """单条交易信号"""
    date: str
    buy: bool
    sell: bool
    score: float = 0.0
    metadata: Dict[str, Any] = None


class Strategy(ABC):
    """
    策略插件基类。

    子类只需实现：
      - name: 策略名称
      - generate(df): 输入 OHLCV DataFrame，输出含 BUY_SIGNAL/SELL_SIGNAL/SCORE 的 DataFrame
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """策略唯一标识名"""
        ...

    @abstractmethod
    def generate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        根据行情数据生成交易信号。

        Args:
            df: OHLCV 数据，index 为日期，含 open/high/low/close/volume 列

        Returns:
            原 DataFrame 附加以下列：
                - BUY_SIGNAL (bool): 是否触发买入
                - SELL_SIGNAL (bool): 是否触发卖出
                - SCORE (float): 信号强度（0-10 或 0-50，引擎不依赖具体范围）
                - 可附加其他 metadata 列
        """
        ...

    def params(self) -> Dict[str, Any]:
        """返回策略当前参数（用于回测报告记录）"""
        return {}


# ── 适配器：将现有函数式策略包装为 Strategy 类 ──────────────

class FuncStrategy(Strategy):
    """
    将现有的函数式策略包装为 Strategy 接口。
    用于过渡期，无需重写原有策略函数。
    """

    def __init__(self, name: str, func, **kwargs):
        self._name = name
        self._func = func
        self._kwargs = kwargs

    @property
    def name(self) -> str:
        return self._name

    def generate(self, df: pd.DataFrame) -> pd.DataFrame:
        result = self._func(df, **self._kwargs)
        # 统一列名映射（兼容不同策略的输出习惯）
        if "BUY_SCORE" in result.columns and "SCORE" not in result.columns:
            result["SCORE"] = result["BUY_SCORE"]
        return result

    def params(self) -> Dict[str, Any]:
        return dict(self._kwargs)
