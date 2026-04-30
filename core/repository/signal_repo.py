"""
signal_repo.py —— signal_records / stock_signal 表的数据访问层
"""
import math
import json
from datetime import datetime, date


def _get_conn():
    from core.db import get_conn
    return get_conn()


def save_signals(signals: list[dict], sent_wechat: bool = False):
    """
    保存策略筛选结果到 signal_records 表
    """
    if not signals:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = date.today().strftime("%Y-%m-%d")
    with _get_conn() as conn:
        conn.executemany("""
            INSERT INTO signal_records
              (scan_time, trade_date, code, name, price, score,
               stop_loss, take_profit, buy_volume, buy_money, sent_wechat)
            VALUES
              (:scan_time, :trade_date, :code, :name, :price, :score,
               :stop_loss, :take_profit, :buy_volume, :buy_money, :sent_wechat)
        """, [{
            "scan_time":   now,
            "trade_date":  today,
            "code":        s.get("code", ""),
            "name":        s.get("name", ""),
            "price":       s.get("price", 0),
            "score":       s.get("score", 0),
            "stop_loss":   s.get("stop_loss", 0),
            "take_profit": s.get("take_profit", 0),
            "buy_volume":  s.get("buy_volume", 0),
            "buy_money":   s.get("buy_money", 0),
            "sent_wechat": 1 if sent_wechat else 0,
        } for s in signals])
    print(f"[DB] 保存 {len(signals)} 条信号记录，推送微信={'是' if sent_wechat else '否'}")


def save_scan_signals(scan_results: list[dict], sent_wechat: bool = False):
    """
    保存每日策略扫描推荐结果到 stock_signal 表。
    """
    if not scan_results:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = date.today().strftime("%Y-%m-%d")
    records = []
    for s in scan_results:
        def _si(v):
            if v is None: return 0.0
            if isinstance(v, float) and math.isnan(v): return 0.0
            return round(float(v), 2)
        trigger = s.get("trigger_list", [])
        if isinstance(trigger, list):
            trigger_json = json.dumps(trigger, ensure_ascii=False)
        else:
            trigger_json = str(trigger or "[]")
        records.append({
            "scan_date":    today,
            "trade_date":   s.get("trade_date") or today,
            "code":         s.get("code", ""),
            "name":         s.get("name", ""),
            "price":        _si(s.get("price")),
            "fusion_score": _si(s.get("score")),
            "vol_score":    _si(s.get("s1_vol_break")),
            "ma_score":     _si(s.get("s2_ma_conv")),
            "diverge_score": _si(s.get("s3_pv_div")),
            "bottom_score":  _si(s.get("s4_bottom")),
            "whale_score":   _si(s.get("s5_whale")),
            "trigger_list":  trigger_json,
            "buy_price":     _si(s.get("price")),
            "stop_loss":     _si(s.get("stop_loss")),
            "take_profit":   _si(s.get("take_profit")),
            "buy_volume":    int(s.get("buy_volume", 0)),
            "buy_money":     _si(s.get("buy_money")),
            "sent_wechat":   1 if sent_wechat else 0,
            "created_at":    now,
        })
    with _get_conn() as conn:
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
        """, records)
    print(f"[DB] 保存 {len(scan_results)} 条扫描推荐到 stock_signal")


def get_signals(trade_date: str = None, limit: int = 100) -> list[dict]:
    """查询筛选记录"""
    sql = "SELECT * FROM signal_records"
    params = []
    if trade_date:
        sql += " WHERE trade_date=?"
        params.append(trade_date)
    sql += " ORDER BY scan_time DESC LIMIT ?"
    params.append(limit)
    with _get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_today_signals() -> list[dict]:
    """获取今日筛选记录"""
    return get_signals(trade_date=date.today().strftime("%Y-%m-%d"))
