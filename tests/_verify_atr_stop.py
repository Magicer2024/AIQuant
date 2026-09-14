"""临时校验：_build_signal_records 产出的短线止损价 == ATR 自适应公式（只读，不写库）。
跑完可删（logs/ 下临时脚本）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

import core.sync as s
from core.db import get_conn, get_market_cap_map, get_all_stocks
from config.strategy_params import get_param
from strategy.indicators import calc_atr

K = float(get_param("short_atr_stop_k"))
FLOOR = float(get_param("short_atr_stop_floor"))
CAP = float(get_param("short_atr_stop_cap"))


def expected_stop(df, ts, price):
    atr = calc_atr(df["high"].astype(float), df["low"].astype(float),
                   df["close"].astype(float), 14)
    a = float(atr.get(ts, float("nan")))
    if not a or a != a or a <= 0:
        return None
    w = min(max(a / price * K, FLOOR), CAP)
    return round(price * (1 - w), 2)


def main():
    with get_conn() as conn:
        latest = conn.execute("SELECT MAX(trade_date) mx FROM daily_price").fetchone()["mx"]
        codes = [r["code"] for r in conn.execute(
            "SELECT DISTINCT code FROM daily_price WHERE trade_date=? LIMIT 40",
            (latest,)).fetchall()]

    sig_th, _ = s._resolve_sig_threshold()
    stop = float(get_param("short_stop_loss"))
    take = float(get_param("short_take_profit"))

    try:
        df0 = get_all_stocks()
        name_map = dict(zip(df0["code"], df0["name"])) if not df0.empty else {}
    except Exception:
        name_map = {}
    mcap = get_market_cap_map()

    n_chk = n_bad = n_atr_missing = n_capped_hi = n_capped_lo = 0
    widths, atr_pcts = [], []
    samples = []

    for code in codes:
        df = s.get_daily_price(code)
        if df is None or len(df) < 30:
            continue
        ts_sh = (mcap.get(code) or {}).get("total_shares")
        recs = s._build_signal_records(
            df, code, name_map.get(code, code), ts_sh, sig_th, stop, take,
            lhb_row=None, scan_date=None)
        shorts = [r for r in recs if r["horizon"] == "short"]
        if not shorts:
            continue
        # 抽查最多 4 条/股
        for r in shorts[:: max(1, len(shorts) // 4)][:4]:
            ts = pd.Timestamp(r["scan_date"])
            price = float(r["price"])
            exp = expected_stop(df, ts, price)
            n_chk += 1
            if exp is None:
                n_atr_missing += 1
                # ATR 缺失应回退固定比例
                if abs(float(r["stop_loss"]) - round(price * (1 + stop), 2)) > 0.011:
                    n_bad += 1
                    print(f"  [BAD-回退] {code} {r['scan_date']} stop={r['stop_loss']} "
                          f"期望回退={round(price*(1+stop),2)}")
                continue
            if abs(float(r["stop_loss"]) - exp) > 0.011:
                n_bad += 1
                print(f"  [BAD] {code} {r['scan_date']} price={price} stop={r['stop_loss']} 期望={exp}")
            w = 1 - float(r["stop_loss"]) / price
            widths.append(w)
            atr = float(calc_atr(df["high"].astype(float), df["low"].astype(float),
                                 df["close"].astype(float), 14).get(ts))
            atr_pcts.append(atr / price)
            if abs(w - CAP) < 1e-4:
                n_capped_hi += 1
            if abs(w - FLOOR) < 1e-4:
                n_capped_lo += 1
        if len(samples) < 6:
            r = shorts[0]
            samples.append((code, r["scan_date"], r["price"], r["stop_loss"],
                            round(1 - float(r["stop_loss"]) / float(r["price"]), 4)))

    print(f"\n=== ATR 自适应止损校验（k={K} floor={FLOOR} cap={CAP}）===")
    print(f"抽查 {n_chk} 条；不一致 {n_bad} 条；ATR 缺失回退 {n_atr_missing} 条")
    if widths:
        w = np.array(widths)
        a = np.array(atr_pcts)
        print(f"止损宽度: 中位 {np.median(w)*100:.2f}%  均值 {w.mean()*100:.2f}%  "
              f"P05 {np.percentile(w,5)*100:.2f}%  P95 {np.percentile(w,95)*100:.2f}%  "
              f"max {w.max()*100:.2f}%")
        print(f"ATR14%:  中位 {np.median(a)*100:.2f}%  均值 {a.mean()*100:.2f}%")
        print(f"触顶(={CAP:.0%}) {n_capped_hi} 条 / 触底(={FLOOR:.0%}) {n_capped_lo} 条")
        print(f"固定 -5% 对照：中位宽度 5.00%（ATR 口径中位 {np.median(w)*100:.2f}%）")
    print("\n样例（code, scan_date, price, stop_loss, 宽度）:")
    for x in samples:
        print("  ", x)

    # 关闭开关 → 应完全回退固定 -5%
    print("\n=== 关闭开关回退校验（short_atr_stop_enabled=0）===")
    import config.strategy_params as sp
    orig = sp.TUNABLE_PARAMS["short_atr_stop_enabled"]["default"]
    sp.TUNABLE_PARAMS["short_atr_stop_enabled"]["default"] = 0
    sp.invalidate_param_cache()
    code = codes[0]
    df = s.get_daily_price(code)
    r2 = [r for r in s._build_signal_records(
        df, code, name_map.get(code, code), (mcap.get(code) or {}).get("total_shares"),
        sig_th, stop, take, lhb_row=None, scan_date=None) if r["horizon"] == "short"]
    ok_fb = all(abs(float(r["stop_loss"]) - round(float(r["price"]) * (1 + stop), 2)) <= 0.011
                for r in r2)
    print(f"{code}: {len(r2)} 条短线记录，全部等于 price×(1{stop:+.2f}) → {ok_fb}")
    sp.TUNABLE_PARAMS["short_atr_stop_enabled"]["default"] = orig
    sp.invalidate_param_cache()

    print("\n结论:", "PASS" if (n_bad == 0 and ok_fb and n_chk > 0) else "FAIL")
    return 0 if (n_bad == 0 and ok_fb and n_chk > 0) else 1


if __name__ == "__main__":
    sys.exit(main())
