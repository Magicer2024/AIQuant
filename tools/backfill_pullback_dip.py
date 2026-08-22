# -*- coding: utf-8 -*-
"""tools/backfill_pullback_dip.py —— 历史缩量回踩信号回填（只写 stock_signal）

背景：缩量回踩信号（strategy='缩量回踩'）线上只写最新交易日，stock_signal
历史无样本 → 复盘/回测看不到该信号线。本工具用 strategy/pullback_dip.py 的
pullback_dip_series 向量化扫全主板历史（与生产扫描共用同一实现，口径零漂移），
字段构造与 core/sync.py 缩量回踩分支完全一致：
  - V3 = 回踩 MA10 企稳 + 量缩至 5日均量(含当日) 60% 内 + 趋势闸门 + 前20日涨幅≥10%
  - 过质量过滤（ST/流动性/市值，与生产 passes_quality 同函数）
  - fusion = 30 + 量缩bonus(≤10) + 贴线bonus(≤10)，cap 50
  - buy_price=当日收盘；stop_loss=×0.95；take_profit=×1.08（PULLBACK_DIP）
  - 冲突保护：同 (scan_date, code) 已有「隔日动量」信号时跳过——生产追加顺序
    动量在回踩之后 INSERT OR REPLACE 胜出，回填若直接 REPLACE 会把动量信号
    覆盖成回踩（字段全变），故显式跳过保持一致

用法：python tools/backfill_pullback_dip.py [--since 2024-01-01] [--dry-run]
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
from config.strategy_params import PULLBACK_DIP, QUALITY_FILTER
from strategy.pullback_dip import pullback_dip_series
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
    since = "2024-01-01"
    dry_run = False
    if "--since" in args:
        since = args[args.index("--since") + 1]
    if "--dry-run" in args:
        dry_run = True

    if not PULLBACK_DIP.get("enabled"):
        print("[回填] PULLBACK_DIP.enabled=False，跳过（一键下线状态）")
        return

    p = PULLBACK_DIP
    shrink_max = float(p.get("vol_shrink_max", 0.6))
    band = float(p.get("ma_touch_band", 0.01))
    stop_pct = float(p.get("stop_loss_pct", -0.05))
    take_pct = float(p.get("take_profit_pct", 0.08))
    rally_min = float(p.get("rally_min", 0.10))

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # 主板 only：与今日推荐/复盘入库同口径（personal_config 排除前缀）
    from config.personal_config import MAIN_BOARD_ONLY, EXCLUDED_BOARD_PREFIXES
    codes = [r["code"] for r in conn.execute(
        "SELECT code FROM stock_info ORDER BY code").fetchall()]
    if MAIN_BOARD_ONLY:
        codes = [c for c in codes
                 if not str(c).startswith(tuple(EXCLUDED_BOARD_PREFIXES))]
    print(f"[回填] 主板股票 {len(codes)} 只（since={since}）", flush=True)

    # 冲突保护：已有隔日动量信号的 (date, code) 集合
    mom_keys = {(r["scan_date"], r["code"]) for r in conn.execute(
        "SELECT DISTINCT scan_date, code FROM stock_signal "
        "WHERE strategy = '隔日动量' AND scan_date >= ?", [since]).fetchall()}
    print(f"[回填] 隔日动量冲突键 {len(mom_keys)} 个（将跳过）", flush=True)

    info = {r["code"]: dict(r) for r in conn.execute(
        "SELECT code, name, total_shares FROM stock_info").fetchall()}

    records = []
    skipped_q = skipped_conflict = 0
    BATCH = 400
    for bi in range(0, len(codes), BATCH):
        ch = codes[bi:bi + BATCH]
        ph = ",".join("?" * len(ch))
        rows = conn.execute(
            f"SELECT code, trade_date, low, close, volume, amount FROM daily_price "
            f"WHERE code IN ({ph}) AND trade_date >= date(?, '-60 day') "
            f"ORDER BY code, trade_date", [*ch, since]).fetchall()
        by_code: dict = {}
        for r in rows:
            by_code.setdefault(r["code"], []).append(
                (r["trade_date"], r["low"], r["close"], r["volume"], r["amount"]))
        for code, seq in by_code.items():
            if len(seq) < 30:
                continue
            df = pd.DataFrame(seq, columns=["trade_date", "low", "close", "volume", "amount"])
            df = df.astype({"low": float, "close": float, "volume": float, "amount": float})
            df.index = pd.to_datetime(df["trade_date"])
            df = df[~df.index.duplicated(keep="last")]
            ii = info.get(code) or {}
            name = ii.get("name") or code
            ts = ii.get("total_shares")
            if _is_st_name(name):
                continue
            s = pullback_dip_series(df, p)
            hits = df.index[s["PB_HIT"].to_numpy() & (df.index >= pd.Timestamp(since))]
            if len(hits) == 0:
                continue
            q = quality_series(name, df, ts, QUALITY_FILTER)
            ma10 = df["close"].rolling(10).mean()
            ma20 = df["close"].rolling(20).mean()
            shrink_s = s["PB_SHRINK"]
            for dt in hits:
                d = str(dt.date())
                if (d, code) in mom_keys:
                    skipped_conflict += 1
                    continue
                if not bool(q.loc[dt]):
                    skipped_q += 1
                    continue
                close = float(df.loc[dt, "close"])
                if close <= 0:
                    continue
                shrink = float(shrink_s.loc[dt])
                m10 = float(ma10.loc[dt])
                m20 = float(ma20.loc[dt])
                pct_above = round(close / m20 - 1.0, 4) if m20 > 0 else 0.0
                # fusion 与 scan_pullback_dip 同公式
                shrink_bonus = min(max(shrink_max - shrink, 0.0) / shrink_max, 1.0) * 10.0
                touch_dev = max(close / m10 - 1.0, 0.0) if m10 > 0 else 1.0
                touch_bonus = min(max(1.0 - touch_dev / band, 0.0), 1.0) * 10.0
                fusion = round(min(30.0 + shrink_bonus + touch_bonus, 50.0), 2)
                triggers = [
                    f"缩量回踩 MA10 企稳（量缩至 5日均量 {shrink * 100:.0f}%）",
                    f"MA20 向上且站上（趋势确认）",
                    f"前 20 日涨幅 ≥{rally_min * 100:.0f}%（题材强度）",
                ]
                records.append({
                    "scan_date": d,
                    "trade_date": d,
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
                    "strategy": "缩量回踩",
                    "pct_above_ma20": pct_above,
                })
        print(f"[回填] 进度 {min(bi + BATCH, len(codes))}/{len(codes)} 只，"
              f"累计命中 {len(records)} 条", flush=True)

    print(f"[回填] 通过过滤 {len(records)} 条 / 质量跳过 {skipped_q} "
          f"/ 动量冲突跳过 {skipped_conflict}")
    if dry_run:
        print("[回填] dry-run：未写库")
        conn.close()
        return
    with get_conn() as c:
        c.executemany(_SIGNAL_INSERT_SQL, records)
    conn.close()
    print(f"[回填] 完成：写入/覆盖 {len(records)} 条缩量回踩信号到 stock_signal")


if __name__ == "__main__":
    main()
