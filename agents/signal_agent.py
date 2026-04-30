"""
agents/signal_agent.py —— 信号猎手

职责：
  1. 运行5策略融合扫描
  2. 运行v4超跌反弹策略扫描
  3. 生成候选股票列表（按评分排序）
  4. 保存到 stock_signal 表供面板展示
"""

from __future__ import annotations

import math
from datetime import date

from agents.base import BaseAgent, AgentContext
from core.db import get_all_stocks, save_scan_signals
from strategy.strategies import (
    strategy_volume_breakout,
    strategy_ma_convergence,
    strategy_price_volume_divergence,
    strategy_bottom_fishing,
    strategy_whale_accumulation,
    strategy_oversold_rebound,
    fuse_signals,
    DEFAULT_WEIGHTS,
)
from core.db import get_daily_price

# 策略参数（与 quant.py 保持一致）
START_CAPITAL = 10000
POSITION_PER_STOCK = 0.5
STOP_LOSS = -0.06
TAKE_PROFIT = 0.20
V4_SCORE_THRESHOLD = 1.8
FUSION_THRESHOLD = 28.0
SIG_BACKFILL_THRESHOLD = 15.0
POSITION_NUM = 3


class SignalAgent(BaseAgent):
    """信号猎手：运行全部策略，生成候选列表"""

    name = "SignalAgent"

    def _execute(self, ctx: AgentContext) -> dict:
        stocks_df = get_all_stocks()
        if stocks_df.empty:
            return {"status": "no_stocks", "hits_fusion": [], "hits_v4": []}

        # 排除科创板和创业板
        stocks_df = stocks_df[
            ~stocks_df["code"].str.startswith("688") &
            ~stocks_df["code"].str.startswith("301")
        ]
        total = len(stocks_df)

        # ── 1. 5策略融合扫描 ──────────────────────────────
        fusion_hits = []
        v4_hits = []

        for i, row in stocks_df.iterrows():
            code, name = row["code"], row["name"]

            # 5策略融合
            item_f = self._analyze_fusion(code, name)
            if item_f:
                fusion_hits.append(item_f)

            # v4 超跌反弹
            item_v4 = self._analyze_v4(code, name)
            if item_v4:
                v4_hits.append(item_v4)

            if (i + 1) % 500 == 0:
                print(f"  [SignalAgent] 扫描进度: {i+1}/{total}，融合命中:{len(fusion_hits)}，v4命中:{len(v4_hits)}")

        # 排序取Top
        fusion_top = sorted(fusion_hits, key=lambda x: x["score"], reverse=True)[:POSITION_NUM]
        v4_top = sorted(v4_hits, key=lambda x: x["score"], reverse=True)[:POSITION_NUM]
        fusion_all = sorted(fusion_hits, key=lambda x: x["score"], reverse=True)
        v4_all = sorted(v4_hits, key=lambda x: x["score"], reverse=True)

        # 保存到数据库（供面板展示）
        sig_limit = 200
        if fusion_all:
            save_scan_signals(fusion_all[:sig_limit], sent_wechat=False)
        if v4_all:
            # v4也保存到 stock_signal，但用不同的标识
            for item in v4_all[:sig_limit]:
                item["strategy_type"] = "v4_oversold"
            save_scan_signals(v4_all[:sig_limit], sent_wechat=False)

        # 写入上下文供下游Agent使用
        ctx.set("fusion_candidates", fusion_top)
        ctx.set("v4_candidates", v4_top)
        ctx.set("fusion_all_count", len(fusion_hits))
        ctx.set("v4_all_count", len(v4_hits))

        return {
            "status": "ok",
            "total_scanned": total,
            "fusion_hits": len(fusion_hits),
            "v4_hits": len(v4_hits),
            "fusion_top": fusion_top,
            "v4_top": v4_top,
        }

    def _analyze_fusion(self, code: str, name: str) -> dict | None:
        """5策略融合分析"""
        try:
            import pandas as pd
            df = get_daily_price(code)
            if df.empty or len(df) < 30:
                return None

            s1 = strategy_volume_breakout(df)
            s2 = strategy_ma_convergence(df)
            s3 = strategy_price_volume_divergence(df)
            s4 = strategy_bottom_fishing(df)
            s5 = strategy_whale_accumulation(df)

            def last_score(s_df):
                v = float(s_df.iloc[-1]["BUY_SCORE"])
                return round(min(10.0, v / 3.0 * 10.0), 1) if not pd.isna(v) else 0.0

            s1_sc = last_score(s1)
            s2_sc = last_score(s2)
            s3_sc = last_score(s3)
            s4_sc = last_score(s4)
            s5_sc = last_score(s5)

            fused = fuse_signals([s1, s2, s3, s4, s5], weights=DEFAULT_WEIGHTS)
            last = fused.iloc[-1]
            fusion_score = round(float(last.get("FUSION_SCORE", 0)), 2)

            if fusion_score < FUSION_THRESHOLD:
                return None

            price = round(float(last["close"]), 2)
            trade_date = str(df.index[-1].date()) if hasattr(df.index[-1], "date") else str(df.index[-1])[:10]
            buy_money = int(START_CAPITAL * POSITION_PER_STOCK)
            buy_volume = int(buy_money // (price * 100) * 100)

            trigger_list = []
            if s1_sc >= 2.0: trigger_list.append("放量突破")
            if s2_sc >= 2.0: trigger_list.append("均线粘合")
            if s3_sc >= 2.0: trigger_list.append("量价背离")
            if s4_sc >= 2.0: trigger_list.append("抄底")
            if s5_sc >= 2.0: trigger_list.append("主力建仓")

            return {
                "code": code,
                "name": name,
                "price": price,
                "trade_date": trade_date,
                "score": fusion_score,
                "strategy": "fusion",
                "buy_money": buy_money,
                "buy_volume": buy_volume,
                "stop_loss": round(price * (1 + STOP_LOSS), 2),
                "take_profit": round(price * (1 + TAKE_PROFIT), 2),
                "s1_vol_break": s1_sc,
                "s2_ma_conv": s2_sc,
                "s3_pv_div": s3_sc,
                "s4_bottom": s4_sc,
                "s5_whale": s5_sc,
                "trigger_list": trigger_list,
            }
        except Exception:
            return None

    def _analyze_v4(self, code: str, name: str) -> dict | None:
        """v4超跌反弹策略分析"""
        try:
            import pandas as pd
            df = get_daily_price(code)
            if df.empty or len(df) < 30:
                return None

            s = strategy_oversold_rebound(df)
            last = s.iloc[-1]
            v4_score = float(last.get("BUY_SCORE", 0))
            buy_signal = bool(last.get("BUY_SIGNAL", False))

            if not buy_signal and v4_score < V4_SCORE_THRESHOLD:
                return None

            price = round(float(last["close"]), 2)
            trade_date = str(df.index[-1].date()) if hasattr(df.index[-1], "date") else str(df.index[-1])[:10]
            v4_score_scaled = round(v4_score / 4.0 * 50.0, 2)

            buy_money = int(START_CAPITAL * POSITION_PER_STOCK)
            buy_volume = int(buy_money // (price * 100) * 100)

            return {
                "code": code,
                "name": name,
                "price": price,
                "trade_date": trade_date,
                "score": v4_score_scaled,
                "raw_score": round(v4_score, 2),
                "strategy": "v4_oversold",
                "buy_money": buy_money,
                "buy_volume": buy_volume,
                "stop_loss": round(price * (1 + STOP_LOSS), 2),
                "take_profit": round(price * (1 + TAKE_PROFIT), 2),
                "trigger_list": ["超跌反弹"] if buy_signal else [],
            }
        except Exception:
            return None
