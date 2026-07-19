"""
routes/screen.py —— 指标筛选 API
"""
from flask import Blueprint, request, jsonify
from utils.api import ok, fail
from core.db import get_conn
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
            SELECT code, close, volume
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
        if max_market_cap > 0 and market_cap > max_market_cap:
            continue
        stocks_with_cap.append(s)
    
    if not stocks_with_cap:
        return ok([], date=trade_date, total=0)
    
    # 如果需要计算技术指标，获取历史数据
    need_indicators = bool(macd_conditions or rsi_conditions or kdj_conditions or ma_conditions or vol_conditions)
    
    if need_indicators:
        # 获取最近60天的历史数据用于计算指标
        from strategy.indicators import calc_macd, calc_rsi, calc_kdj, calc_ma
        
        with get_conn() as conn:
            history_rows = conn.execute("""
                SELECT code, trade_date, open, high, low, close, volume
                FROM daily_price
                WHERE trade_date >= date(?, '-60 days')
                ORDER BY code, trade_date
            """, (trade_date,)).fetchall()
        
        # 按股票分组
        stock_history = {}
        for r in history_rows:
            code = r["code"]
            if code not in stock_history:
                stock_history[code] = []
            stock_history[code].append(dict(r))
        
        # 计算每只股票的指标
        for s in stocks_with_cap:
            code = s["code"]
            hist = stock_history.get(code, [])
            
            if len(hist) < 20:
                continue
            
            df = pd.DataFrame(hist)
            close = df["close"]
            high = df["high"]
            low = df["low"]
            
            # MACD
            need_macd = macd_conditions or macd_dif_filter
            if need_macd:
                try:
                    macd = calc_macd(close, fast=macd_fast, slow=macd_slow, signal=macd_signal)
                    dif = macd["MACD_DIF"].iloc[-1]
                    dea = macd["MACD_DEA"].iloc[-1]
                    hist_val = macd["MACD_HIST"].iloc[-1]
                    s["macd_dif"] = float(dif) if pd.notna(dif) else None
                    s["macd_dea"] = float(dea) if pd.notna(dea) else None
                    s["macd_hist"] = float(hist_val) if pd.notna(hist_val) else None
                except:
                    pass
            
            # RSI
            if rsi_conditions:
                try:
                    rsi = calc_rsi(close)
                    val = rsi["RSI14"].iloc[-1]
                    s["rsi14"] = float(val) if pd.notna(val) else None
                except:
                    pass
            
            # KDJ
            if kdj_conditions:
                try:
                    kdj = calc_kdj(high, low, close)
                    k = kdj["KDJ_K"].iloc[-1]
                    d = kdj["KDJ_D"].iloc[-1]
                    j = kdj["KDJ_J"].iloc[-1]
                    s["kdj_k"] = float(k) if pd.notna(k) else None
                    s["kdj_d"] = float(d) if pd.notna(d) else None
                    s["kdj_j"] = float(j) if pd.notna(j) else None
                except:
                    pass
            
            # 均线
            if ma_conditions:
                try:
                    ma = calc_ma(close)
                    # 计算偏离度
                    s["ma5_dev"] = float((close.iloc[-1] / ma["MA5"].iloc[-1] - 1) * 100) if pd.notna(ma["MA5"].iloc[-1]) else None
                    s["ma10_dev"] = float((close.iloc[-1] / ma["MA10"].iloc[-1] - 1) * 100) if pd.notna(ma["MA10"].iloc[-1]) else None
                    s["ma20_dev"] = float((close.iloc[-1] / ma["MA20"].iloc[-1] - 1) * 100) if pd.notna(ma["MA20"].iloc[-1]) else None
                except:
                    pass
            
            # 量比
            if vol_conditions:
                try:
                    vol = df["volume"]
                    vol_ma5 = vol.rolling(5).mean().iloc[-1]
                    if pd.notna(vol_ma5) and vol_ma5 > 0:
                        s["vol_ratio"] = float(vol.iloc[-1] / vol_ma5)
                except:
                    pass
    
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
        
        results.append(s)
    
    # 按市值排序
    results.sort(key=lambda x: x.get("market_cap", 0), reverse=True)
    
    return ok(results, date=trade_date, total=len(results))
