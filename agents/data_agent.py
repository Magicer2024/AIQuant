"""
agents/data_agent.py —— 数据管家（Qlib 集成版）

职责：
  1. Qlib 数据完整性验证
  2. 交易日判断（跳过周末/节假日）
  3. 全市场行情同步（收盘后全量 / 盘中增量）
  4. 指数数据同步
  5. 数据质量检查（缺失率、异常值检测）
  6. 清理旧缓存
"""

from __future__ import annotations

import pandas as pd
from datetime import date, datetime

from agents.base import BaseAgent, AgentContext
from core.db import init_db, get_latest_date_all, db_stats
from core.sync import (
    daily_sync, sync_one_stock, sync_all_indices, sync_strategy_score,
    is_trading_day, is_after_market_close,
)
from ministries.rites.data_source_manager import get_data_source_manager


class DataAgent(BaseAgent):
    """数据管家（太子院·数据官）：每日数据同步与质量检查"""

    name = "DataAgent"
    governance_role = "太子院·数据官"

    def _execute(self, ctx: AgentContext) -> dict:
        today = ctx.run_date
        run_dt = datetime.strptime(today, "%Y-%m-%d").date()
        ctx.set("today", today)

        # 0. Qlib data integrity check
        qlib_status = self._check_qlib_data()
        ctx.set("qlib_status", qlib_status)

        # 1. 交易日判断
        if not is_trading_day(today):
            init_db()
            latest_in_db = get_latest_date_all(before_date=today)
            weekday_names = ["一", "二", "三", "四", "五", "六", "日"]
            wday = run_dt.weekday()
            msg = (
                "今天是周六" if wday == 5 else
                "今天是周日" if wday == 6 else
                f"今天是节假日（{weekday_names[wday]}）"
            )
            return {
                "trading_day": False,
                "skip_reason": msg,
                "today": today,
                "latest_in_db": latest_in_db,
                "qlib_status": qlib_status,
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
                "qlib_status": qlib_status,
            }

        # 2.5 历史日期
        is_historical = run_dt < date.today()
        if is_historical:
            if latest_in_db and latest_in_db >= today:
                stats = db_stats()
                return {
                    "trading_day": True,
                    "already_latest": True,
                    "historical": True,
                    "today": today,
                    "latest_in_db": latest_in_db,
                    "stock_count": stats.get("有行情股票数", 0),
                    "record_count": stats.get("行情记录总数", 0),
                    "qlib_status": qlib_status,
                }
            else:
                return {
                    "trading_day": True,
                    "already_latest": False,
                    "historical": True,
                    "data_insufficient": True,
                    "today": today,
                    "latest_in_db": latest_in_db,
                    "skip_reason": f"数据库中最新数据为 {latest_in_db}，早于请求日期 {today}，无法为历史日期拉取实时数据",
                    "qlib_status": qlib_status,
                }

        # 3. 执行同步（仅当天）
        is_after_close = is_after_market_close()

        if not is_after_close:
            print(f"  [DataAgent] 市场未收盘，跳过同步，使用已有数据 (最新: {latest_in_db})", flush=True)
            ctx.log("info", f"市场未收盘，跳过同步，使用已有数据 (最新: {latest_in_db})")
            stats = db_stats()
            return {
                "trading_day": True,
                "already_latest": True,
                "today": today,
                "latest_in_db": latest_in_db,
                "stock_count": stats.get("有行情股票数", 0),
                "record_count": stats.get("行情记录总数", 0),
                "note": "market not closed, sync skipped",
                "qlib_status": qlib_status,
            }

        sync_report = self._run_sync(today, full=is_after_close, ctx=ctx)

        # 4. 数据质量检查
        quality = self._check_quality(today)

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
            "qlib_status": qlib_status,
        }

    def _check_qlib_data(self) -> dict:
        """Validate Qlib data integrity: features exist, calendars populated."""
        try:
            from qlib_engine import init_qlib, get_provider_uri
            from qlib.data import D
            import os

            init_qlib()

            provider_uri = get_provider_uri()
            features_dir = os.path.join(provider_uri, "features")
            feature_count = 0
            if os.path.exists(features_dir):
                feature_count = len(os.listdir(features_dir))

            try:
                calendar = D.calendar()
                instruments = D.instruments(market="all")
                calendar_count = len(calendar) if calendar is not None else 0
                instruments_count = len(instruments) if instruments is not None else 0
            except Exception:
                calendar_count = 0
                instruments_count = 0

            ctx = {
                "available": feature_count > 0,
                "feature_dirs": feature_count,
                "calendar_days": calendar_count,
                "instruments": instruments_count,
            }

            if calendar_count > 0:
                ctx["latest_date"] = str(calendar[-1])

            return ctx
        except Exception as e:
            return {"available": False, "error": str(e)}

    def _run_sync(self, today: str, full: bool, ctx: AgentContext = None) -> dict:
        """执行数据同步，返回统计"""
        if full:
            import baostock as bs
            stocks = get_data_source_manager().get_stock_list_df()
            success_n, fail_n = 0, 0
            total = len(stocks)

            lg = bs.login()
            if lg.error_code != '0':
                if ctx:
                    ctx.log("error", f"baostock 登录失败: {lg.error_msg}")
                print(f"  [DataAgent] baostock 登录失败: {lg.error_msg}")
                return {
                    "mode": "full",
                    "stocks_success": 0,
                    "stocks_fail": total,
                    "stocks_total": total,
                    "index_records": 0,
                }

            try:
                for i, row in stocks.iterrows():
                    code = row["code"]
                    ok = sync_one_stock(code, today, today, verbose=False, auto_login=False)
                    if ok:
                        success_n += 1
                        try:
                            sync_strategy_score(code, verbose=False)
                        except Exception:
                            pass
                    else:
                        fail_n += 1
                    if (i + 1) % 500 == 0:
                        if ctx:
                            ctx.log("info", f"同步进度: {i+1}/{total}")
                        print(f"  [DataAgent] 同步进度: {i+1}/{total}", flush=True)
            finally:
                bs.logout()

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

    def _check_quality(self, today: str = None) -> dict:
        """数据质量检查：缺失率、停牌检测"""
        if today is None:
            today = date.today().strftime("%Y-%m-%d")
        try:
            stocks = get_data_source_manager().get_stock_list_df()
            total = len(stocks)
            if total == 0:
                return {"status": "no_data", "missing_ratio": 1.0}

            sample = stocks.sample(min(100, total)) if total > 100 else stocks
            missing_count = 0
            stale_count = 0

            for _, row in sample.iterrows():
                try:
                    df = get_data_source_manager().get_daily_price_df(row["code"])
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
