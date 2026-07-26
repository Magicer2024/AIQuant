"""
routes/screen.py —— 指标筛选 API
"""
from flask import Blueprint, request
from utils.api import ok, fail
from core.db import get_conn
import numpy as np
import pandas as pd

screen_bp = Blueprint("screen", __name__, url_prefix="/api/screen")


@screen_bp.route("", methods=["POST"])
def screen_stocks():
    """指标筛选 POST /api/screen"""
    body = request.get_json(silent=True) or {}
    
    exclude_kcb = body.get("exclude_kcb", False)
    exclude_cyb = body.get("exclude_cyb", False)
    max_market_cap = body.get("max_market_cap", 0)
    macd_conditions = body.get("macd", [])
    macd_fast = body.get("macd_fast", 12)
    macd_slow = body.get("macd_slow", 26)
    macd_signal = body.get("macd_signal", 9)
    macd_dif_filter = body.get("macd_dif")  # {op: "gt"|"gte"|"lt"|"lte"|"eq", value: float}
    macd_dea_filter = body.get("macd_dea")  # DEA 数值范围
    macd_hist_filter = body.get("macd_hist")  # 柱状图数值范围
    rsi_conditions = body.get("rsi", [])
    kdj_conditions = body.get("kdj", [])
    ma_conditions = body.get("ma", [])
    vol_conditions = body.get("vol", [])
    turnover_filter = body.get("turnover")  # {min: float, max: float} 换手率%
    change_conditions = body.get("change", [])  # ["up","down","limit_up","limit_down"]
    
    # 获取最新交易日
    with get_conn() as conn:
        row = conn.execute("SELECT MAX(trade_date) as d FROM daily_price").fetchone()
        if not row or not row["d"]:
            return ok([], message="暂无行情数据")
        trade_date = row["d"]
    
    # 获取所有活跃股票
    with get_conn() as conn:
        stocks = conn.execute("""
            SELECT code, name, market, total_shares
            FROM stock_info WHERE is_active = 1
        """).fetchall()
    
    # 排除板块
    filtered_stocks = []
    for s in stocks:
        code = s["code"]
        if exclude_kcb and code.startswith("688"):
            continue
        if exclude_cyb and (code.startswith("300") or code.startswith("301")):
            continue
        filtered_stocks.append(dict(s))
    
    if not filtered_stocks:
        return ok([], date=trade_date, total=0)
    
    # 批量获取行情数据计算市值
    codes = [s["code"] for s in filtered_stocks]
    placeholders = ",".join(["?"] * len(codes))
    
    with get_conn() as conn:
        price_rows = conn.execute(f"""
            SELECT code, close, volume, turnover, pct_change
            FROM daily_price
            WHERE trade_date = ? AND code IN ({placeholders})
        """, [trade_date] + codes).fetchall()
    
    price_map = {r["code"]: dict(r) for r in price_rows}
    
    # 计算市值并筛选
    stocks_with_cap = []
    for s in filtered_stocks:
        price_info = price_map.get(s["code"])
        if not price_info or not price_info["close"]:
            continue
        market_cap = price_info["close"] * (s.get("total_shares") or 0) / 1e8
        s["market_cap"] = market_cap
        s["close"] = price_info["close"]
        s["turnover"] = price_info.get("turnover")
        s["pct_change"] = price_info.get("pct_change")
        if max_market_cap > 0 and market_cap > max_market_cap:
            continue
        stocks_with_cap.append(s)
    
    if not stocks_with_cap:
        return ok([], date=trade_date, total=0)
    
    # 如果需要计算技术指标，获取历史数据
    need_indicators = bool(macd_conditions or rsi_conditions or kdj_conditions or ma_conditions or vol_conditions
                           or macd_dif_filter or macd_dea_filter or macd_hist_filter)

    if need_indicators:
        # 向量化批量计算：groupby + Cython 加速的 rolling/ewm 一次算完全市场，
        # 替代原逐股建 DataFrame + 逐股调指标函数的 Python 循环（4000+ 只 × ~8ms ≈ 33s）
        with get_conn() as conn:
            hist_df = pd.read_sql_query("""
                SELECT code, trade_date, high, low, close, volume
                FROM daily_price
                WHERE trade_date >= date(?, '-60 days')
                ORDER BY code, trade_date
            """, conn, params=(trade_date,))

        # 与原逻辑一致：历史不足 20 根K线的股票不算指标
        if not hist_df.empty:
            cnt = hist_df.groupby("code", sort=False)["close"].transform("size")
            hist_df = hist_df[cnt >= 20].reset_index(drop=True)

        if not hist_df.empty:
            close = hist_df["close"]

            # MACD（EMA 递推 = ewm，逐组向量化）
            need_macd = bool(macd_conditions or macd_dif_filter or macd_dea_filter or macd_hist_filter)
            if need_macd:
                ema_fast = hist_df.groupby("code", sort=False)["close"].ewm(
                    span=macd_fast, adjust=False).mean().droplevel(0)
                ema_slow = hist_df.groupby("code", sort=False)["close"].ewm(
                    span=macd_slow, adjust=False).mean().droplevel(0)
                hist_df["macd_dif"] = ema_fast - ema_slow
                hist_df["macd_dea"] = hist_df.groupby("code", sort=False)["macd_dif"].ewm(
                    span=macd_signal, adjust=False).mean().droplevel(0)
                hist_df["macd_hist"] = (hist_df["macd_dif"] - hist_df["macd_dea"]) * 2

            # RSI14（Wilder 平滑 = ewm(com=n-1)）
            if rsi_conditions:
                delta = hist_df.groupby("code", sort=False)["close"].diff()
                hist_df["_gain"] = delta.where(delta > 0, 0.0)
                hist_df["_loss"] = -delta.where(delta < 0, 0.0)
                avg_gain = hist_df.groupby("code", sort=False)["_gain"].ewm(
                    com=13, adjust=False).mean().droplevel(0)
                avg_loss = hist_df.groupby("code", sort=False)["_loss"].ewm(
                    com=13, adjust=False).mean().droplevel(0)
                rs = avg_gain / avg_loss.replace(0, np.nan)
                hist_df["rsi14"] = 100 - (100 / (1 + rs))

            # KDJ（K/D 递推 k=2/3·k_prev+1/3·rsv 等价于 ewm(alpha=1/3)）
            if kdj_conditions:
                low_min = hist_df.groupby("code", sort=False)["low"].rolling(9).min().droplevel(0)
                high_max = hist_df.groupby("code", sort=False)["high"].rolling(9).max().droplevel(0)
                hist_df["_rsv"] = (close - low_min) / (high_max - low_min + 1e-10) * 100
                hist_df["kdj_k"] = hist_df.groupby("code", sort=False)["_rsv"].ewm(
                    alpha=1.0 / 3, adjust=False).mean().droplevel(0)
                hist_df["kdj_d"] = hist_df.groupby("code", sort=False)["kdj_k"].ewm(
                    alpha=1.0 / 3, adjust=False).mean().droplevel(0)
                hist_df["kdj_j"] = 3 * hist_df["kdj_k"] - 2 * hist_df["kdj_d"]

            # 均线偏离度
            if ma_conditions:
                for p in (5, 10, 20):
                    ma_p = hist_df.groupby("code", sort=False)["close"].rolling(p).mean().droplevel(0)
                    hist_df[f"ma{p}_dev"] = (close / ma_p - 1) * 100

            # 量比
            if vol_conditions:
                vol_ma5 = hist_df.groupby("code", sort=False)["volume"].rolling(5).mean().droplevel(0)
                hist_df["vol_ratio"] = hist_df["volume"] / vol_ma5.replace(0, np.nan)

            # 末行评估：只取每只股票最新一行的指标值挂到候选列表
            ind_cols = [c for c in (
                "macd_dif", "macd_dea", "macd_hist", "rsi14",
                "kdj_k", "kdj_d", "kdj_j",
                "ma5_dev", "ma10_dev", "ma20_dev", "vol_ratio",
            ) if c in hist_df.columns]
            last_rows = hist_df.groupby("code", sort=False).tail(1).set_index("code")
            ind_map = last_rows[ind_cols].to_dict("index") if ind_cols else {}

            for s in stocks_with_cap:
                vals = ind_map.get(s["code"])
                if not vals:
                    continue
                for key, v in vals.items():
                    s[key] = float(v) if pd.notna(v) else None
    
    # 应用筛选条件
    results = []
    for s in stocks_with_cap:
        # MACD 筛选
        need_macd = macd_conditions or macd_dif_filter or macd_dea_filter or macd_hist_filter
        if need_macd:
            dif = s.get("macd_dif")
            dea = s.get("macd_dea")
            hist = s.get("macd_hist")
            if dif is None or dea is None:
                continue
            
            macd_ok = True
            for cond in macd_conditions:
                if cond == "golden" and not (dif > dea):
                    macd_ok = False
                elif cond == "death" and not (dif < dea):
                    macd_ok = False
                elif cond == "hist_positive" and not (hist is not None and hist > 0):
                    macd_ok = False
                elif cond == "hist_negative" and not (hist is not None and hist < 0):
                    macd_ok = False
            
            # DIF 值筛选
            if macd_dif_filter and macd_ok:
                op = macd_dif_filter.get("op", "")
                val = macd_dif_filter.get("value", 0)
                if op == "gt" and not (dif > val):
                    macd_ok = False
                elif op == "gte" and not (dif >= val):
                    macd_ok = False
                elif op == "lt" and not (dif < val):
                    macd_ok = False
                elif op == "lte" and not (dif <= val):
                    macd_ok = False
                elif op == "eq" and not (abs(dif - val) < 0.01):
                    macd_ok = False
            # DEA 值筛选
            if macd_dea_filter and macd_ok:
                op = macd_dea_filter.get("op", "")
                val = macd_dea_filter.get("value", 0)
                if op == "gt" and not (dea > val):
                    macd_ok = False
                elif op == "gte" and not (dea >= val):
                    macd_ok = False
                elif op == "lt" and not (dea < val):
                    macd_ok = False
                elif op == "lte" and not (dea <= val):
                    macd_ok = False
                elif op == "eq" and not (abs(dea - val) < 0.01):
                    macd_ok = False
            # 柱状图数值筛选
            if macd_hist_filter and macd_ok and hist is not None:
                op = macd_hist_filter.get("op", "")
                val = macd_hist_filter.get("value", 0)
                if op == "gt" and not (hist > val):
                    macd_ok = False
                elif op == "gte" and not (hist >= val):
                    macd_ok = False
                elif op == "lt" and not (hist < val):
                    macd_ok = False
                elif op == "lte" and not (hist <= val):
                    macd_ok = False
                elif op == "eq" and not (abs(hist - val) < 0.01):
                    macd_ok = False

            if not macd_ok:
                continue
            s["macd_state"] = "golden" if dif > dea else "death"
        
        # RSI 筛选
        if rsi_conditions:
            rsi = s.get("rsi14")
            if rsi is None:
                continue
            
            rsi_ok = True
            for cond in rsi_conditions:
                if cond == "oversold" and not (rsi < 30):
                    rsi_ok = False
                elif cond == "overbought" and not (rsi > 70):
                    rsi_ok = False
                elif cond == "mid" and not (30 <= rsi <= 70):
                    rsi_ok = False
            if not rsi_ok:
                continue
            s["rsi_state"] = "oversold" if rsi < 30 else ("overbought" if rsi > 70 else "mid")
        
        # KDJ 筛选
        if kdj_conditions:
            k = s.get("kdj_k")
            d = s.get("kdj_d")
            j = s.get("kdj_j")
            if k is None or d is None:
                continue
            
            kdj_ok = True
            for cond in kdj_conditions:
                if cond == "golden" and not (k > d):
                    kdj_ok = False
                elif cond == "death" and not (k < d):
                    kdj_ok = False
                elif cond == "oversold" and not (j is not None and j < 0):
                    kdj_ok = False
                elif cond == "overbought" and not (j is not None and j > 100):
                    kdj_ok = False
            if not kdj_ok:
                continue
            s["kdj_state"] = "golden" if k > d else "death"
        
        # 均线筛选
        if ma_conditions:
            ma5 = s.get("ma5_dev")
            ma10 = s.get("ma10_dev")
            ma20 = s.get("ma20_dev")
            
            ma_ok = True
            for cond in ma_conditions:
                if cond == "bull":
                    if not (ma5 is not None and ma10 is not None and ma20 is not None and 
                            ma5 > ma10 and ma10 > ma20 and ma5 > 0):
                        ma_ok = False
                elif cond == "bear":
                    if not (ma5 is not None and ma10 is not None and ma20 is not None and 
                            ma5 < ma10 and ma10 < ma20 and ma5 < 0):
                        ma_ok = False
                elif cond == "above20":
                    if not (ma20 is not None and ma20 > 0):
                        ma_ok = False
                elif cond == "below20":
                    if not (ma20 is not None and ma20 < 0):
                        ma_ok = False
            if not ma_ok:
                continue
            
            if ma5 is not None and ma10 is not None and ma20 is not None:
                if ma5 > ma10 and ma10 > ma20:
                    s["ma_state"] = "bull"
                elif ma5 < ma10 and ma10 < ma20:
                    s["ma_state"] = "bear"
                else:
                    s["ma_state"] = "mixed"
        
        # 量能筛选
        if vol_conditions:
            vol = s.get("vol_ratio")
            if vol is None:
                continue
            
            vol_ok = True
            for cond in vol_conditions:
                if cond == "surge" and not (vol > 2):
                    vol_ok = False
                elif cond == "shrink" and not (vol < 0.5):
                    vol_ok = False
            if not vol_ok:
                continue
        
        # 换手率筛选（区间）
        if turnover_filter:
            to = s.get("turnover")
            if to is None:
                continue
            tmin = turnover_filter.get("min")
            tmax = turnover_filter.get("max")
            if tmin is not None and to < tmin:
                continue
            if tmax is not None and to > tmax:
                continue
        
        # 涨跌幅筛选
        if change_conditions:
            pc = s.get("pct_change")
            if pc is None:
                continue
            chg_ok = True
            for cond in change_conditions:
                if cond == "up" and not (pc > 0):
                    chg_ok = False
                elif cond == "down" and not (pc < 0):
                    chg_ok = False
                elif cond == "limit_up" and not (pc >= 9.8):
                    chg_ok = False
                elif cond == "limit_down" and not (pc <= -9.8):
                    chg_ok = False
            if not chg_ok:
                continue
        
        results.append(s)
    
    # 按市值排序
    results.sort(key=lambda x: x.get("market_cap", 0), reverse=True)
    
    return ok(results, date=trade_date, total=len(results))
