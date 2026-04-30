"""
agents/data_agent.py —— 数据管家

职责：
  1. 交易日判断（跳过周末/节假日）
  2. 全市场行情同步（收盘后全量 / 盘中增量）
  3. 指数数据同步
  4. 数据质量检查（缺失率、异常值检测）
  5. 清理旧缓存
"""

from __future__ import annotations

import pandas as pd
from datetime import date, datetime

from agents.base import BaseAgent, AgentContext
from core.db import init_db, get_all_stocks, get_latest_date_all, get_stock_count_in_db, db_stats
from core.sync import (
    daily_sync, sync_one_stock, sync_all_indices, sync_strategy_score,
    is_trading_day, is_after_market_close,
)


class DataAgent(BaseAgent):
    """数据管家：每日数据同步与质量检查"""

    name = "DataAgent"

    def _execute(self, ctx: AgentContext) -> dict:
        today = date.today().strftime("%Y-%m-%d")
        ctx.set("today", today)

        # 1. 交易日判断
        if not is_trading_day(today):
            weekday_names = ["一", "二", "三", "四", "五", "六", "日"]
            wday = date.today().weekday()
            msg = (
                "今天是周六" if wday == 5 else
                "今天是周日" if wday == 6 else
                f"今天是节假日（{weekday_names[wday]}）"
            )
            return {
                "trading_day": False,
                "skip_reason": msg,
                "today": today,
            }

        init_db()
        latest_in_db = get_latest_date_all()

        # 2. 数据已最新
        if latest_in_db and latest_in_db >= today:
            stats = db_stats()
            return {
                "trading_day": True,
                "already_latest": True,
                "today": today,
                "latest_in_db": latest_in_db,
                "stock_count": stats.get("有行情股票数", 0),
                "record_count": stats.get("行情记录总数", 0),
            }

        # 3. 执行同步
        is_after_close = is_after_market_close()
        sync_report = self._run_sync(today, full=is_after_close)

        # 4. 数据质量检查
        quality = self._check_quality()

        # 5. 清理旧缓存
        cache_cleaned = self._cleanup_cache()

        return {
            "trading_day": True,
            "already_latest": False,
            "today": today,
            "is_after_market_close": is_after_close,
            "sync": sync_report,
            "quality": quality,
            "cache_cleaned": cache_cleaned,
        }

    def _run_sync(self, today: str, full: bool) -> dict:
        """执行数据同步，返回统计"""
        if full:
            stocks = get_all_stocks()
            success_n, fail_n = 0, 0
            total = len(stocks)
            for i, row in stocks.iterrows():
                code = row["code"]
                ok = sync_one_stock(code, today, today, verbose=False)
                if ok:
                    success_n += 1
                    try:
                        sync_strategy_score(code, verbose=False)
                    except Exception:
                        pass
                else:
                    fail_n += 1
                if (i + 1) % 500 == 0:
                    print(f"  [DataAgent] 同步进度: {i+1}/{total}")

            index_results = sync_all_indices(start_date=today, end_date=today, verbose=False)
            total_idx = sum(index_results.values()) if index_results else 0

            return {
                "mode": "full",
                "stocks_success": success_n,
                "stocks_fail": fail_n,
                "stocks_total": total,
                "index_records": total_idx,
            }
        else:
            daily_sync(verbose=False)
            return {"mode": "incremental"}

    def _check_quality(self) -> dict:
        """数据质量检查：缺失率、停牌检测"""
        try:
            stocks = get_all_stocks()
            total = len(stocks)
            if total == 0:
                return {"status": "no_data", "missing_ratio": 1.0}

            # 随机抽查100只股票，检查最新数据日期
            sample = stocks.sample(min(100, total)) if total > 100 else stocks
            from core.db import get_daily_price
            missing_count = 0
            stale_count = 0
            today = date.today().strftime("%Y-%m-%d")

            for _, row in sample.iterrows():
                try:
                    df = get_daily_price(row["code"])
                    if df is None or df.empty:
                        missing_count += 1
                        continue
                    latest = str(df.index[-1])[:10]
                    if latest < today:
                        stale_count += 1
                except Exception:
                    missing_count += 1

            return {
                "status": "checked",
                "sample_size": len(sample),
                "missing_count": missing_count,
                "stale_count": stale_count,
                "healthy_ratio": round(1 - (missing_count + stale_count) / len(sample), 3),
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def _cleanup_cache(self) -> dict:
        """清理旧缓存文件"""
        try:
            from core.data_fetcher import cleanup_cache
            removed = cleanup_cache(days_old=7)
            return {"removed_files": removed}
        except Exception as e:
            return {"removed_files": 0, "error": str(e)}
