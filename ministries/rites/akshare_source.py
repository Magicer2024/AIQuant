"""
ministries/rites/akshare_source.py —— Akshare 数据源适配器

职责：
  1. 通过 akshare 获取A股数据
  2. 标准化数据格式
"""

import akshare as ak
from datetime import datetime

from ministries.rites.data_source_manager import BaseDataSource, DataSourceStatus, DataSourceType, DataQuality


class AkshareDataSource(BaseDataSource):
    """Akshare 数据源"""

    def get_name(self) -> str:
        return "akshare"

    def check_health(self) -> DataSourceStatus:
        """健康检查"""
        try:
            # 尝试获取一只股票的最新数据
            df = ak.stock_zh_a_hist(symbol="000001", period="daily", start_date="20240101", adjust="qfq")
            if df is not None and not df.empty:
                return DataSourceStatus(
                    source_type=DataSourceType.AKSHARE,
                    name="akshare",
                    is_available=True,
                    last_check=datetime.now().isoformat(),
                    quality=DataQuality.GOOD,
                )
        except Exception:
            pass

        return DataSourceStatus(
            source_type=DataSourceType.AKSHARE,
            name="akshare",
            is_available=False,
            last_check=datetime.now().isoformat(),
            quality=DataQuality.UNAVAILABLE,
        )

    def get_daily_price(self, code: str, start_date: str = None, end_date: str = None) -> list[dict]:
        """获取日线数据"""
        try:
            # akshare 代码格式：不带后缀
            symbol = code.split(".")[0] if "." in code else code

            # 格式化日期
            sd = start_date.replace("-", "") if start_date else "20230101"
            ed = end_date.replace("-", "") if end_date else datetime.now().strftime("%Y%m%d")

            df = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=sd, end_date=ed, adjust="qfq")

            if df is None or df.empty:
                return []

            # 标准化列名
            df = df.rename(columns={
                "日期": "trade_date",
                "开盘": "open",
                "最高": "high",
                "最低": "low",
                "收盘": "close",
                "成交量": "volume",
                "成交额": "amount",
                "振幅": "amplitude",
                "涨跌幅": "pct_change",
                "涨跌额": "change",
                "换手率": "turnover",
            })

            # 转换日期格式
            df["trade_date"] = df["trade_date"].astype(str)

            return df.to_dict("records")
        except Exception as e:
            print(f"[Akshare] 获取 {code} 数据失败: {e}")
            return []

    def get_stock_list(self) -> list[dict]:
        """获取股票列表"""
        try:
            df = ak.stock_zh_a_spot_em()
            if df is None or df.empty:
                return []

            # 标准化列名
            df = df.rename(columns={
                "代码": "code",
                "名称": "name",
            })

            return df[["code", "name"]].to_dict("records")
        except Exception as e:
            print(f"[Akshare] 获取股票列表失败: {e}")
            return []

    def get_index_daily(self, code: str = "000300", start_date: str = None, end_date: str = None) -> list[dict]:
        """获取指数日线"""
        try:
            sd = start_date.replace("-", "") if start_date else "20230101"
            ed = end_date.replace("-", "") if end_date else datetime.now().strftime("%Y%m%d")

            # 沪深300
            if code == "000300":
                df = ak.index_zh_a_hist(symbol="000300", period="daily", start_date=sd, end_date=ed)
            else:
                return []

            if df is None or df.empty:
                return []

            df = df.rename(columns={
                "日期": "trade_date",
                "开盘": "open",
                "最高": "high",
                "最低": "low",
                "收盘": "close",
                "成交量": "volume",
                "成交额": "amount",
                "振幅": "amplitude",
                "涨跌幅": "pct_change",
                "涨跌额": "change",
                "换手率": "turnover",
            })
            df["trade_date"] = df["trade_date"].astype(str)

            return df.to_dict("records")
        except Exception as e:
            print(f"[Akshare] 获取指数 {code} 数据失败: {e}")
            return []
