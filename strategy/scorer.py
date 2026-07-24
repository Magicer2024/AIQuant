"""
scorer.py —— 回测/因子流式数据加载工具

原「每日打分」引擎已下线（stock_score 表已废弃）。此模块仅保留被
backtest/rule_engine.py 复用的流式行情加载函数，避免为回测重复造轮子。
"""
import pandas as pd

from core.db import get_conn


def _load_stock_data_streaming(trade_date, batch_size=100):
    """流式加载股票数据，逐股 yield (code, DataFrame)，减少内存峰值。

    从 daily_price 取截至 trade_date 的全部活跃股票 OHLCV，按 code 聚合，
    每只股票产出一个以 trade_date 为索引的 DataFrame。
    """
    with get_conn() as conn:
        cursor = conn.execute("""
            SELECT dp.code, dp.trade_date, dp.open, dp.high, dp.low, dp.close,
                   dp.volume, dp.amount, dp.turnover
            FROM daily_price dp
            INNER JOIN stock_info si ON dp.code = si.code AND si.is_active = 1
            WHERE dp.trade_date <= ?
            ORDER BY dp.code, dp.trade_date
        """, (trade_date,))

        batch = []
        current_code = None

        for row in cursor:
            if row["code"] != current_code:
                if batch:
                    yield current_code, pd.DataFrame(batch).set_index("trade_date")
                current_code = row["code"]
                batch = []
            batch.append(dict(row))

        if batch:
            yield current_code, pd.DataFrame(batch).set_index("trade_date")
