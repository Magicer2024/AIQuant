"""
ministries/rites/data_source_manager.py —— 礼部数据源管理器

职责：
  1. 管理多个数据源的接入
  2. 数据源主备切换
  3. 数据质量检查
  4. 统一数据接口
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class DataSourceType(Enum):
    """数据源类型"""
    AKSHARE = "akshare"
    TUSHARE = "tushare"
    BAOSTOCK = "baostock"
    YFINANCE = "yfinance"
    LOCAL = "local"


class DataQuality(Enum):
    """数据质量等级"""
    EXCELLENT = "excellent"
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"
    UNAVAILABLE = "unavailable"


@dataclass
class DataSourceStatus:
    """数据源状态"""
    source_type: DataSourceType
    name: str
    is_available: bool
    last_check: str
    latency_ms: int = 0
    error_count: int = 0
    quality: DataQuality = DataQuality.UNAVAILABLE


class BaseDataSource(ABC):
    """数据源抽象基类"""

    @abstractmethod
    def get_name(self) -> str:
        """数据源名称"""
        pass

    @abstractmethod
    def check_health(self) -> DataSourceStatus:
        """健康检查"""
        pass

    @abstractmethod
    def get_daily_price(self, code: str, start_date: str, end_date: str) -> list[dict]:
        """获取日线数据"""
        pass

    @abstractmethod
    def get_stock_list(self) -> list[dict]:
        """获取股票列表"""
        pass


class LocalDataSource(BaseDataSource):
    """本地数据库数据源"""

    def get_name(self) -> str:
        return "local_db"

    def check_health(self) -> DataSourceStatus:
        try:
            from core.db import get_stock_count_in_db
            count = get_stock_count_in_db()
            return DataSourceStatus(
                source_type=DataSourceType.LOCAL,
                name="local_db",
                is_available=True,
                last_check=datetime.now().isoformat(),
                quality=DataQuality.GOOD if count > 0 else DataQuality.POOR,
            )
        except Exception as e:
            return DataSourceStatus(
                source_type=DataSourceType.LOCAL,
                name="local_db",
                is_available=False,
                last_check=datetime.now().isoformat(),
                quality=DataQuality.UNAVAILABLE,
            )

    def get_daily_price(self, code: str, start_date: str, end_date: str) -> list[dict]:
        from core.db import get_daily_price
        df = get_daily_price(code, start_date, end_date)
        if df.empty:
            return []
        return df.reset_index().to_dict("records")

    def get_stock_list(self) -> list[dict]:
        from core.db import get_all_stocks
        df = get_all_stocks()
        return df.to_dict("records")


class DataSourceManager:
    """数据源管理器"""

    def __init__(self):
        self.sources: dict[DataSourceType, BaseDataSource] = {}
        self.primary: DataSourceType = DataSourceType.LOCAL
        self._register_default_sources()

    def _register_default_sources(self):
        """注册默认数据源"""
        self.register(DataSourceType.LOCAL, LocalDataSource())

    def register(self, source_type: DataSourceType, source: BaseDataSource):
        """注册数据源"""
        self.sources[source_type] = source

    def get_source(self, source_type: DataSourceType = None) -> BaseDataSource:
        """获取数据源（默认主数据源）"""
        st = source_type or self.primary
        return self.sources.get(st)

    def set_primary(self, source_type: DataSourceType):
        """设置主数据源"""
        self.primary = source_type

    def health_check_all(self) -> list[DataSourceStatus]:
        """检查所有数据源健康状态"""
        results = []
        for st, source in self.sources.items():
            try:
                status = source.check_health()
                results.append(status)
            except Exception as e:
                results.append(DataSourceStatus(
                    source_type=st,
                    name=source.get_name(),
                    is_available=False,
                    last_check=datetime.now().isoformat(),
                    quality=DataQuality.UNAVAILABLE,
                ))
        return results

    def get_daily_price(self, code: str, start_date: str = None, end_date: str = None,
                        source_type: DataSourceType = None) -> list[dict]:
        """获取日线数据（自动切换数据源）"""
        # 先尝试主数据源
        source = self.get_source(source_type)
        if source:
            try:
                data = source.get_daily_price(code, start_date, end_date)
                if data:
                    return data
            except Exception:
                pass

        # 主数据源失败，尝试备用
        for st, src in self.sources.items():
            if st == (source_type or self.primary):
                continue
            try:
                data = src.get_daily_price(code, start_date, end_date)
                if data:
                    return data
            except Exception:
                continue

        return []

    def get_daily_price_df(self, code: str, start_date: str = None, end_date: str = None,
                           source_type: DataSourceType = None):
        """获取日线数据，返回 pd.DataFrame 格式（供 Agent 使用）"""
        import pandas as pd
        data = self.get_daily_price(code, start_date, end_date, source_type)
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        if "trade_date" in df.columns:
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            df = df.set_index("trade_date").sort_index()
        return df

    def get_stock_list_df(self):
        """获取股票列表，返回 pd.DataFrame 格式"""
        import pandas as pd
        source = self.get_source()
        if source:
            data = source.get_stock_list()
            if data:
                return pd.DataFrame(data)
        return pd.DataFrame()

    def get_index_daily_df(self, code: str = "000300", start_date: str = None, end_date: str = None):
        """获取指数日线数据，返回 pd.DataFrame 格式"""
        from core.db import get_index_daily
        return get_index_daily(code, start_date=start_date, end_date=end_date)


# 全局单例
_data_source_manager: DataSourceManager | None = None


def get_data_source_manager() -> DataSourceManager:
    """获取数据源管理器单例"""
    global _data_source_manager
    if _data_source_manager is None:
        _data_source_manager = DataSourceManager()
    return _data_source_manager
