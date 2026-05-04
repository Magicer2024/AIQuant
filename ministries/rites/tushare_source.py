"""
ministries/rites/tushare_source.py —— Tushare 数据源适配器

职责：
  1. 通过 tushare 获取A股数据
  2. 标准化数据格式
"""

import os
import tushare as ts
from datetime import datetime

from ministries.rites.data_source_manager import BaseDataSource, DataSourceStatus, DataSourceType, DataQuality


class TushareDataSource(BaseDataSource):
    """Tushare 数据源"""

    def __init__(self):
        self.pro = None
        self._init_api()

    def _init_api(self):
        """初始化 Tushare API"""
        token = os.environ.get("TUSHARE_TOKEN", "")
        if token:
            ts.set_token(token)
            self.pro = ts.pro_api()

    def get_name(self) -> str:
        return "tushare"

    def check_health(self) -> DataSourceStatus:
        """健康检查"""
        if self.pro is None:
            return DataSourceStatus(
                source_type=DataSourceType.TUSHARE,
                name="tushare",
                is_available=False,
                last_check=datetime.now().isoformat(),
                quality=DataQuality.UNAVAILABLE,
            )

        try:
            df = self.pro.daily(ts_code="000001.SZ", start_date="20240101", end_date="20240105")
            if df is not None and not df.empty:
                return DataSourceStatus(
                    source_type=DataSourceType.TUSHARE,
                    name="tushare",
                    is_available=True,
                    last_check=datetime.now().isoformat(),
                    quality=DataQuality.GOOD,
                )
        except Exception:
            pass

        return DataSourceStatus(
            source_type=DataSourceType.TUSHARE,
            name="tushare",
            is_available=False,
            last_check=datetime.now().isoformat(),
            quality=DataQuality.UNAVAILABLE,
        )

    def get_daily_price(self, code: str, start_date: str = None, end_date: str = None) -> list[dict]:
        """获取日线数据"""
        if self.pro is None:
            return []

        try:
            # tushare 代码格式：带交易所后缀
            if "." not in code:
                symbol = f"{code}.SZ" if code.startswith(("000", "001", "002", "003", "300")) else f"{code}.SH"
            else:
                symbol = code

            # 格式化日期
            sd = start_date.replace("-", "") if start_date else "20230101"
            ed = end_date.replace("-", "") if end_date else datetime.now().strftime("%Y%m%d")

            df = self.pro.daily(ts_code=symbol, start_date=sd, end_date=ed)

            if df is None or df.empty:
                return []

            # 标准化列名
            df = df.rename(columns={
                "trade_date": "trade_date",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "vol": "volume",
                "amount": "amount",
                "pct_chg": "pct_change",
            })

            df["trade_date"] = df["trade_date"].astype(str)

            return df.to_dict("records")
        except Exception as e:
            print(f"[Tushare] 获取 {code} 数据失败: {e}")
            return []

    def get_stock_list(self) -> list[dict]:
        """获取股票列表"""
        if self.pro is None:
            return []

        try:
            df = self.pro.stock_basic(exchange="", list_status="L")
            if df is None or df.empty:
                return []

            df = df.rename(columns={
                "ts_code": "code",
                "name": "name",
            })

            # 去掉后缀
            df["code"] = df["code"].str.split(".").str[0]

            return df[["code", "name"]].to_dict("records")
        except Exception as e:
            print(f"[Tushare] 获取股票列表失败: {e}")
            return []

    def get_index_daily(self, code: str = "000300", start_date: str = None, end_date: str = None) -> list[dict]:
        """获取指数日线"""
        if self.pro is None:
            return []

        try:
            # 指数代码映射
            index_map = {
                "000300": "000300.SH",  # 沪深300
                "000001": "000001.SH",  # 上证指数
                "399001": "399001.SZ",  # 深证成指
                "399006": "399006.SZ",  # 创业板指
            }
            symbol = index_map.get(code, f"{code}.SH")

            sd = start_date.replace("-", "") if start_date else "20230101"
            ed = end_date.replace("-", "") if end_date else datetime.now().strftime("%Y%m%d")

            df = self.pro.index_daily(ts_code=symbol, start_date=sd, end_date=ed)

            if df is None or df.empty:
                return []

            df = df.rename(columns={
                "trade_date": "trade_date",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "vol": "volume",
                "amount": "amount",
                "pct_chg": "pct_change",
            })
            df["trade_date"] = df["trade_date"].astype(str)

            return df.to_dict("records")
        except Exception as e:
            print(f"[Tushare] 获取指数 {code} 数据失败: {e}")
            return []
