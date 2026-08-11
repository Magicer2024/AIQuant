# -*- coding: utf-8 -*-
"""tools/backfill_lhb_momentum.py —— 历史龙虎榜动量信号回填（只写 stock_signal）

背景：动量信号（strategy='隔日动量'）线上只写最新交易日，stock_signal 历史
无动量样本 → 复盘/回测/历史 ?date= 查看看不到动量（此前回测实际是纯抄底）。
本工具按 NEXT_DAY_MOMENTUM 条件从 stock_lhb_detail 全期（2024-07 起）生成
动量信号回填 stock_signal，口径与 core/sync.py::_build_signal_records 的
动量分支完全一致：
  - 净买占比 >= min_net_buy_ratio(10) 且当日非涨停（20cm 阈值 19.8 / 其余 9.8）
  - 过质量过滤（ST/流动性 amt20>=8000万/市值 30亿~3000亿，缺市值项跳过）
  - fusion_score = 30 + min((ratio-10)/20,1)*20（净买占比映射 0~50）
  - buy_price=当日收盘；stop_loss=×0.96；take_profit=×1.065（NEXT_DAY_MOMENTUM）
  - INSERT OR REPLACE：同 (scan_date, code, short) 动量信号覆盖抄底（线上同语义）

用法：python tools/backfill_lhb_momentum.py [--since 2024-07-01] [--dry-run]
"""
import os
import sys
import sqlite3
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd

from core.db import get_conn
from config.strategy_params import NEXT_DAY_MOMENTUM, QUALITY_FILTER
from strategy.rec_filters import quality_series, _is_st_name

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "core", "quant.db")
_SIGNAL_INSERT_SQL = """
    INSERT OR REPLACE INTO stock_signal
      (scan_date, trade_date, code, name, price, fusion_score,
       vol_score, ma_score, diverge_score, bottom_score, whale_score,
       trigger_list, buy_price, stop_loss, take_profit,
       buy_volume, buy_money, sent_wechat, created_at,
       horizon, strategy, pct_above_ma20)
    VALUES
      (:scan_date, :trade_date, :code, :name, :price, :fusion_score,
       :vol_score, :ma_score, :diverge_score, :bottom_score, :whale_score,
       :trigger_list, :buy_price, :stop_loss, :take_profit,
       :buy_volume, :buy_money, :sent_wechat, :created_at,
       :horizon, :strategy, :pct_above_ma20)
"""


def main():
    args = sys.argv[1:]
    since = None
    dry_run = False
    if "--since" in args:
        since = args[args.index("--since") + 1]
    if "--dry-run" in args:
        dry_run = True

    p = NEXT_DAY_MOMENTUM
    min_ratio = float(p.get("min_net_buy_ratio", 10.0))
    stop_pct = float(p.get("stop_loss_pct", -0.04))
    take_pct = float(p.get("take_profit_pct", 0.065))

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # 1) 动量候选：净买>=阈值 且 非涨停（全市场，含创业板）
    where = "WHERE net_buy_ratio >= ? AND pct_change < 9.8"
    args_sql: list = [min_ratio]
    if since:
        where += " AND trade_date >= ?"
        args_sql.append(since)
    cand_rows = conn.execute(
        f"SELECT trade_date, code, net_buy_ratio, pct_change, reason "
        f"FROM stock_lhb_detail {where} ORDER BY trade_date", args_sql).fetchall()
    # 非涨停阈值按板块（20cm 19.8）
    cand = [dict(r) for r in cand_rows
            if r["pct_change"] is None or float(r["pct_change"]) < (
                19.8 if str(r["code"]).startswith(("300", "301", "688", "689")) else 9.8)]
    print(f"[回填] 动量候选 {len(cand)} 条（净买≥{min_ratio} 非涨停，since={since or '全部'}）")

    # 2) 按 code 加载日线 + 质量
    codes = sorted({r["code"] for r in cand})
    info = {r["code"]: dict(r) for r in conn.execute(
        "SELECT code, name, total_shares FROM stock_info").fetchall()}
    px = {}
    for i in range(0, len(codes), 400):
        ch = codes[i:i + 400]
        ph = ",".join("?" * len(ch))
        rows = conn.execute(
            f"SELECT code, trade_date, close, amount FROM daily_price "
            f"WHERE code IN ({ph}) ORDER BY code, trade_date", ch).fetchall()
        for r in rows:
            px.setdefault(r["code"], []).append((r["trade_date"], r["close"], r["amount"]))
    print(f"[回填] 涉及股票 {len(px)} 只")

    records = []
    skipped = 0
    by_code = {}
    for r in cand:
        by_code.setdefault(r["code"], []).append(r)
    for code, lst in by_code.items():
        seq = px.get(code)
        if not seq or len(seq) < 25:
            skipped += len(lst)
            continue
        df = pd.DataFrame(seq, columns=["trade_date", "close", "amount"])
        df = df.astype({"close": float, "amount": float})
        df.index = pd.to_datetime(df["trade_date"])
        ii = info.get(code) or {}
        name = ii.get("name") or code
        ts = ii.get("total_shares")
        if _is_st_name(name):
            skipped += len(lst)
            continue
        q = quality_series(name, df, ts, QUALITY_FILTER)
        ma20 = df["close"].rolling(20).mean()
        for r in lst:
            ts_dt = pd.Timestamp(r["trade_date"])
            if ts_dt not in df.index:
                skipped += 1
                continue
            if not bool(q.loc[ts_dt]):
                skipped += 1
                continue
            close = float(df.loc[ts_dt, "close"])
            m = float(ma20.loc[ts_dt]) if ts_dt in ma20.index else 0.0
            if close <= 0:
                skipped += 1
                continue
            pct_above = round(close / m - 1.0, 4) if m and m > 0 else 0.0
            ratio = float(r["net_buy_ratio"])
            fusion = round(30.0 + min(max(ratio - min_ratio, 0.0) / 20.0, 1.0) * 20.0, 2)
            reason = str(r.get("reason") or "").strip()
            triggers = [
                f"龙虎榜净买占比 {ratio:.1f}%（≥{int(min_ratio)}）",
                "次日开盘买入（龙虎榜盘后公布）",
            ]
            if reason:
                triggers.append(reason[:24])
            records.append({
                "scan_date": r["trade_date"],
                "trade_date": r["trade_date"],
                "code": code,
                "name": name,
                "price": round(close, 2),
                "fusion_score": fusion,
                "vol_score": 0.0, "ma_score": 0.0, "diverge_score": 0.0,
                "bottom_score": 0.0, "whale_score": 0.0,
                "trigger_list": json.dumps(triggers, ensure_ascii=False),
                "buy_price": round(close, 2),
                "stop_loss": round(close * (1 + stop_pct), 2),
                "take_profit": round(close * (1 + take_pct), 2),
                "buy_volume": 0, "buy_money": 0, "sent_wechat": 0,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "horizon": "short",
                "strategy": "隔日动量",
                "pct_above_ma20": pct_above,
            })
    print(f"[回填] 通过过滤 {len(records)} 条 / 跳过 {skipped} 条")
    if dry_run:
        print("[回填] dry-run：未写库")
        conn.close()
        return
    with get_conn() as c:
        c.executemany(_SIGNAL_INSERT_SQL, records)
    conn.close()
    print(f"[回填] 完成：写入/覆盖 {len(records)} 条动量信号到 stock_signal")


if __name__ == "__main__":
    main()
