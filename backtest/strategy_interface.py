"""strategy_interface.py ? Strategy base class and adapters for backtest engine."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable
import pandas as pd


@dataclass
class Signal:
    """A single trading signal."""
    date: str
    action: str  # "buy" or "sell"
    price: float
    score: float = 0.0
    reason: str = ""


class Strategy(ABC):
    """Base class for all strategies."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def generate(self, df: pd.DataFrame) -> pd.DataFrame:
        """Generate signals for the given dataframe."""
        ...


class FuncStrategy(Strategy):
    """Adapter that wraps a function as a Strategy."""

    def __init__(self, name: str, func: Callable):
        self._name = name
        self._func = func

    @property
    def name(self) -> str:
        return self._name

    def generate(self, df: pd.DataFrame) -> pd.DataFrame:
        result = self._func(df.copy())
        if "SCORE" not in result.columns:
            result["SCORE"] = 0.0
        return result
