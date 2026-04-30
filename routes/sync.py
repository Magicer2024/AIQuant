"""
routes/sync.py —— 数据同步相关接口
"""
import time
import json as _json
import traceback
from flask import jsonify

from routes import sync_bp
from core.db import get_all_stocks, get_daily_price, get_conn
from core.sync import sync_strategy_score
from strategy.strategies import (
    strategy_volume_breakout,
    strategy_ma_convergence,
    strategy_price_volume_divergence,
    strategy_bottom_fishing,
    strategy_whale_accumulation,
    fuse_signals,
    DEFAULT_WEIGHTS,
)


@sync_bp.route("/recalc_all_scores", methods=["GET"])
def recalc_all_scores():
    """
    对数据库中所有股票的历史评分进行全量补算，
    同时将每日融合分高于阈值的日期写入 stock_signal 表。
    """
    STOP_LOSS_SC = -0.06
    TAKE_PROFIT_SC = 0.20
    START_CAPITAL_SC = 10000
    POSITION_PER_SC = 0.5
    SIG_THRESHOLD = 15.0

    stocks_df = get_all_stocks()
    if stocks_df.empty:
        return jsonify({"error": "股票列表为空"})

    stocks = list(zip(stocks_df["code"], stocks_df["name"]))
    success = 0
    failed = 0
    total_days = 0
    sig_records = []
    start = time.time()

    for i, (code, name) in enumerate(stocks):
        if (i + 1) % 200 == 0:
            print(f"  补算进度: {i+1}/{len(stocks)}  成功:{success}  失败:{failed}")

        ok = sync_strategy_score(code, verbose=False)
        if ok:
            success += 1
            df = get_daily_price(code)
            if df is not None:
                total_days += len(df)
        else:
            failed += 1
            continue

        try:
            df = get_daily_price(code)
            if df is None or len(df) < 30:
                continue
            s1 = strategy_volume_breakout(df)
            s2 = strategy_ma_convergence(df)
            s3 = strategy_price_volume_divergence(df)
            s4 = strategy_bottom_fishing(df)
            s5 = strategy_whale_accumulation(df)
            fused = fuse_signals([s1, s2, s3, s4, s5], weights=DEFAULT_WEIGHTS)
            for _, row in fused.iterrows():
                fs = float(row.get("FUSION_SCORE", 0) or 0)
                if fs < SIG_THRESHOLD:
                    continue
                trade_date = str(row.name.date()) if hasattr(row.name, "date") else str(row.name)[:10]
                price = round(float(row["close"]), 2)
                buy_money = int(START_CAPITAL_SC * POSITION_PER_SC)
                buy_volume = int(buy_money // (price * 100) * 100)
                stop_loss = round(price * (1 + STOP_LOSS_SC), 2)
                take_profit = round(price * (1 + TAKE_PROFIT_SC), 2)
                trigger_list = []
                if float(row.get("VOL_SCORE", 0) or 0) >= 2.0:
                    trigger_list.append("放量突破")
                if float(row.get("MA_SCORE", 0) or 0) >= 2.0:
                    trigger_list.append("均线粘合")
                if float(row.get("DIVERGE_SCORE", 0) or 0) >= 2.0:
                    trigger_list.append("量价背离")
                if float(row.get("BOTTOM_SCORE", 0) or 0) >= 2.0:
                    trigger_list.append("抄底")
                if float(row.get("WHALE_SCORE", 0) or 0) >= 2.0:
                    trigger_list.append("主力建仓")
                sig_records.append({
                    "scan_date": trade_date,
                    "trade_date": trade_date,
                    "code": code,
                    "name": name or code,
                    "price": price,
                    "fusion_score": round(fs, 2),
                    "vol_score": round(float(row.get("VOL_SCORE", 0) or 0), 1),
                    "ma_score": round(float(row.get("MA_SCORE", 0) or 0), 1),
                    "diverge_score": round(float(row.get("DIVERGE_SCORE", 0) or 0), 1),
                    "bottom_score": round(float(row.get("BOTTOM_SCORE", 0) or 0), 1),
                    "whale_score": round(float(row.get("WHALE_SCORE", 0) or 0), 1),
                    "trigger_list": _json.dumps(trigger_list, ensure_ascii=False),
                    "buy_price": price,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "buy_volume": buy_volume,
                    "buy_money": buy_money,
                    "sent_wechat": 0,
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
        except Exception as e:
            print(f"  [{code}] stock_signal 写入失败: {e}")

    if sig_records:
        with get_conn() as conn:
            conn.executemany("""
                INSERT OR REPLACE INTO stock_signal
                  (scan_date, trade_date, code, name, price, fusion_score,
                   vol_score, ma_score, diverge_score, bottom_score, whale_score,
                   trigger_list, buy_price, stop_loss, take_profit,
                   buy_volume, buy_money, sent_wechat, created_at)
                VALUES
                  (:scan_date, :trade_date, :code, :name, :price, :fusion_score,
                   :vol_score, :ma_score, :diverge_score, :bottom_score, :whale_score,
                   :trigger_list, :buy_price, :stop_loss, :take_profit,
                   :buy_volume, :buy_money, :sent_wechat, :created_at)
            """, sig_records)
        print(f"  stock_signal 写入完成: {len(sig_records)} 条记录")

    elapsed = time.time() - start
    print(f"历史评分补算完成: {success} 只成功 / {failed} 只失败，{len(sig_records)} 条推荐写入 stock_signal，耗时 {elapsed:.0f}秒")
    return jsonify({
        "success": success,
        "failed": failed,
        "days": total_days,
        "signals": len(sig_records),
        "elapsed_s": round(elapsed, 1),
    })
