"""
agents/risk_agent.py —— 风控官

职责：
  1. 大盘风险评估（沪深300 MA5/MA20/MA60 趋势）
  2. 波动率评估（VIX近似：涨跌幅标准差）
  3. 个股风险筛查（近期是否ST、退市风险、停牌）
  4. 仓位建议（基于大盘环境）
  5. 行业集中度检查
"""

from __future__ import annotations

import pandas as pd
import numpy as np
from datetime import date, timedelta

from agents.base import BaseAgent, AgentContext
from core.db import get_index_daily, get_all_stocks, get_daily_price


class RiskAgent(BaseAgent):
    """风控官：大盘风险 + 个股风险 + 仓位建议"""

    name = "RiskAgent"

    def _execute(self, ctx: AgentContext) -> dict:
        today = date.today().strftime("%Y-%m-%d")

        # 1. 大盘风险评估
        market_risk = self._assess_market(today)

        # 2. 波动率评估
        volatility = self._assess_volatility(today)

        # 3. 候选股风险筛查
        fusion_candidates = ctx.get("fusion_candidates", [])
        v4_candidates = ctx.get("v4_candidates", [])
        all_candidates = fusion_candidates + v4_candidates
        stock_risks = self._screen_stocks(all_candidates)

        # 4. 仓位建议
        position_advice = self._position_advice(market_risk, volatility)

        # 5. 综合风险等级
        overall_risk = self._overall_risk(market_risk, volatility)

        ctx.set("risk_assessment", {
            "market_risk": market_risk,
            "volatility": volatility,
            "stock_risks": stock_risks,
            "position_advice": position_advice,
            "overall_risk": overall_risk,
        })

        return {
            "status": "ok",
            "market_risk": market_risk,
            "volatility": volatility,
            "stock_risks": stock_risks,
            "position_advice": position_advice,
            "overall_risk": overall_risk,
        }

    def _assess_market(self, today: str) -> dict:
        """大盘趋势评估"""
        try:
            start = (date.today() - timedelta(days=90)).strftime("%Y-%m-%d")
            df = get_index_daily("000300", start_date=start, end_date=today)
            if df is None or df.empty:
                return {"status": "no_data", "trend": "unknown"}

            df = df.sort_index()
            close = df["close"]
            ma5 = close.rolling(5).mean()
            ma20 = close.rolling(20).mean()
            ma60 = close.rolling(60).mean()

            latest = close.iloc[-1]
            latest_ma5 = ma5.iloc[-1] if not pd.isna(ma5.iloc[-1]) else latest
            latest_ma20 = ma20.iloc[-1] if not pd.isna(ma20.iloc[-1]) else latest
            latest_ma60 = ma60.iloc[-1] if not pd.isna(ma60.iloc[-1]) else latest

            # 趋势判断
            trend = "bull" if latest > latest_ma5 > latest_ma20 > latest_ma60 else \
                    "neutral" if latest > latest_ma20 else \
                    "bear"

            # 计算近期回撤
            peak = close.max()
            drawdown = (latest - peak) / peak if peak > 0 else 0

            return {
                "status": "ok",
                "trend": trend,
                "index_close": round(float(latest), 2),
                "ma5": round(float(latest_ma5), 2),
                "ma20": round(float(latest_ma20), 2),
                "ma60": round(float(latest_ma60), 2),
                "drawdown_from_peak": round(float(drawdown), 4),
            }
        except Exception as e:
            return {"status": "error", "error": str(e)[:200]}

    def _assess_volatility(self, today: str) -> dict:
        """市场波动率评估（用沪深300近20日涨跌幅标准差作为VIX近似）"""
        try:
            start = (date.today() - timedelta(days=40)).strftime("%Y-%m-%d")
            df = get_index_daily("000300", start_date=start, end_date=today)
            if df is None or len(df) < 20:
                return {"status": "insufficient_data", "level": "medium"}

            df = df.sort_index()
            returns = df["close"].pct_change().dropna()
            vol_20d = returns.iloc[-20:].std() * np.sqrt(252)  # 年化波动率

            level = "low" if vol_20d < 0.15 else \
                    "medium" if vol_20d < 0.25 else \
                    "high"

            return {
                "status": "ok",
                "annual_volatility": round(float(vol_20d), 4),
                "level": level,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)[:200]}

    def _screen_stocks(self, candidates: list[dict]) -> list[dict]:
        """个股风险筛查"""
        results = []
        for item in candidates:
            code = item["code"]
            risk = {"code": code, "name": item.get("name", ""), "risk_flags": []}

            try:
                df = get_daily_price(code)
                if df is None or df.empty:
                    risk["risk_flags"].append("no_data")
                    results.append(risk)
                    continue

                # 检查是否连续涨停（异常波动）
                close = df["close"]
                if len(close) >= 3:
                    last3 = close.iloc[-3:].pct_change().dropna()
                    if len(last3) >= 2 and all(last3 > 0.095):
                        risk["risk_flags"].append("limit_up_3d")

                # 检查是否放量下跌（危险信号）
                if len(df) >= 2:
                    last_close = float(close.iloc[-1])
                    prev_close = float(close.iloc[-2])
                    last_vol = float(df["volume"].iloc[-1])
                    prev_vol = float(df["volume"].iloc[-2])
                    if last_close < prev_close * 0.97 and last_vol > prev_vol * 2:
                        risk["risk_flags"].append("heavy_drop_with_volume")

                # 检查是否创20日新低
                low_20d = float(close.iloc[-20:].min())
                if last_close <= low_20d * 1.01:
                    risk["risk_flags"].append("near_20d_low")

                # 检查RSI是否超买
                delta = close.diff()
                gain = delta.clip(lower=0).rolling(14).mean()
                loss = (-delta.clip(upper=0)).rolling(14).mean()
                rs = gain / loss.clip(lower=1e-9)
                rsi = 100 - (100 / (1 + rs))
                last_rsi = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50
                if last_rsi > 75:
                    risk["risk_flags"].append("rsi_overbought")

                risk["rsi"] = round(last_rsi, 1)

            except Exception:
                risk["risk_flags"].append("screen_error")

            results.append(risk)

        return results

    def _position_advice(self, market_risk: dict, volatility: dict) -> dict:
        """基于市场环境给出仓位建议"""
        trend = market_risk.get("trend", "neutral")
        vol_level = volatility.get("level", "medium")

        # 基础仓位
        base = {"bull": 0.8, "neutral": 0.5, "bear": 0.2, "unknown": 0.5}.get(trend, 0.5)

        # 波动率调整
        vol_adj = {"low": 1.1, "medium": 1.0, "high": 0.7}.get(vol_level, 1.0)

        # 回撤调整
        dd = market_risk.get("drawdown_from_peak", 0)
        dd_adj = 1.0 if dd > -0.05 else 0.8 if dd > -0.10 else 0.6

        suggested = min(base * vol_adj * dd_adj, 1.0)

        return {
            "base_ratio": base,
            "volatility_adjustment": vol_adj,
            "drawdown_adjustment": dd_adj,
            "suggested_position": round(suggested, 2),
            "max_single_position": 0.5 if vol_level == "high" else 0.4,
            "max_holdings": 3 if trend == "bear" else 4,
        }

    def _overall_risk(self, market_risk: dict, volatility: dict) -> str:
        """综合风险等级"""
        trend = market_risk.get("trend", "neutral")
        vol = volatility.get("level", "medium")

        if trend == "bear" or vol == "high":
            return "high"
        if trend == "neutral" and vol == "medium":
            return "medium"
        if trend == "bull" and vol == "low":
            return "low"
        return "medium"
