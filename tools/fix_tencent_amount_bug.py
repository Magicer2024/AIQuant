"""
tools/fix_tencent_amount_bug.py —— 修复腾讯兜底路径写坏的成交量/成交额
====================================================================
背景（2026-10-21 排查）：
  core/sync.py 的腾讯兜底（akshare stock_zh_a_hist_tx）曾把接口的 amount 列
  当成「成交额(万元)」×10000，再用 amount/close 反推 volume。实测该列是
  「成交量(手)」：002009 在 2026-06-26 真实成交 175,897 手，库里却写成
  amount=1,758,970,000、volume=92,140,911.47（约为真实量的 5.2 倍）。

受影响行签名（可与正常行区分）：
    turnover = 0
    volume 非整数（amount/close 反推的痕迹，小数部分 > 0.01）
    amount  = volume × close（同一条生成链路的产物）
全库扫描：2025 年后命中 5,193 行，集中在 2026-05-25 / 06-15 / 06-16 /
06-24 / 06-25 / 06-26 六个腾讯兜底日；2025 年前 0 行。

修法：
  按 (code, trade_date) 用 baostock「不复权」重取真实的
  volume / amount / pct_change / turnover 覆盖这四列；OHLC 不动
  （避免把当日价格切到最新前复权基准、破坏与相邻日的衔接）。

两种校验（防止错股/错日）：
  1) 严格：不复权收盘与库内收盘相差 ≤1% → 直接取 baostock 量额。
  2) 缩放：库内 OHLC 若只是与不复权差「一个复权系数 q」（除权除息后的正常
     现象），则 open/high/low/close 全部按 q 折算后逐项比对 ≤1% → 视为同一股
     同日，量额（与复权基准无关）照取。实测首批 216 行走的是这条路。
  3) 腾讯兜底：baostock 无数据的（如 2026-05 上市新股 302132），用腾讯
     stock_zh_a_hist_tx(qfq) 的 amount(手)×100 作 volume，成交额按
     volume×(高+低+收)/3 估算，涨跌幅用前收计算；turnover 无法获得保持 0。

用法：
  python tools/fix_tencent_amount_bug.py                 # dry-run：只统计，不写库
  python tools/fix_tencent_amount_bug.py --apply         # 实际修复
  python tools/fix_tencent_amount_bug.py --since 2026-01-01 --apply

注意：
  - 修完需重算受影响交易日的策略分（volume/amount 参与 vol_score / amt20）。
  - Qlib 侧当前 QLIB_ENABLED=False，无需处理；若开启需重建受影响股票的 bin。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import baostock as bs
from core.db import get_conn

SIG = """turnover=0 AND close>0 AND volume>0
  AND ABS(volume-ROUND(volume))>0.01
  AND ABS(amount-volume*close)<=MAX(1, amount*1e-9)"""

TOL = 0.01  # OHLC 缩放/严格比对容差


def _bs_code(code: str) -> str:
    """6 位代码 → baostock 代码（与 core/sync.py 保持一致）。"""
    if code.startswith(("sh.", "sz.", "bj.")):
        return code
    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return f"sh.{code}"
    if code.startswith(("430", "830", "870", "920")):
        return f"bj.{code}"
    if code.startswith(("000", "001", "002", "003", "300", "301")):
        return f"sz.{code}"
    return f"sh.{code}"


def _tx_code(code: str) -> str:
    """6 位代码 → 腾讯格式（sh600000 / sz000001）。"""
    code = code.replace('sh.', '').replace('sz.', '').strip()
    if code.startswith(('600', '601', '603', '605', '688', '689')):
        return f"sh{code}"
    return f"sz{code}"


def _scaled_match(stored: dict, o: float, h: float, l: float, c: float, tol: float = TOL) -> bool:
    """库内 OHLC 与外部不复权 OHLC 是否只差一个复权系数 q。

    同一只股票在除权除息后，外部不复权序列 × q 应逐项等于库内序列。
    任一项折算后偏差 > tol 即判定不是同一标的/日期，拒绝写入。
    """
    if not stored.get("close") or c <= 0:
        return False
    q = c / stored["close"]
    if q <= 0:
        return False
    for sv, ev in ((stored.get("open"), o), (stored.get("high"), h),
                   (stored.get("low"), l), (stored.get("close"), c)):
        if not sv or ev <= 0:
            return False
        if abs((ev / q) / sv - 1) > tol:
            return False
    return True


def find_affected(conn, since: str):
    rows = conn.execute(
        f"SELECT code, trade_date, open, high, low, close, volume, amount "
        f"FROM daily_price WHERE trade_date>=? AND {SIG} ORDER BY code, trade_date",
        (since,)).fetchall()
    by_code: dict[str, list] = {}
    by_date: dict[str, int] = {}
    for r in rows:
        by_code.setdefault(r["code"], []).append(dict(r))
        by_date[r["trade_date"]] = by_date.get(r["trade_date"], 0) + 1
    return rows, by_code, by_date


def _apply_baostock(conn, by_code) -> tuple[int, int, int, list]:
    """baostock 不复权覆盖量额。返回 (strict, scaled, mismatch, 剩余行[(code,row)])。"""
    strict = scaled = mismatch = 0
    leftover: list[tuple[str, dict]] = []
    lg = bs.login()
    if lg.error_code != '0':
        print(f"[ERROR] baostock 登录失败: {lg.error_msg}")
        sys.exit(1)
    try:
        for i, (code, items) in enumerate(sorted(by_code.items()), 1):
            dates = [r["trade_date"] for r in items]
            rs = bs.query_history_k_data_plus(
                _bs_code(code), "date,open,high,low,close,volume,amount,turn,pctChg",
                start_date=min(dates), end_date=max(dates),
                frequency="d", adjustflag="3")
            got = {}
            while rs.error_code == '0' and rs.next():
                r = rs.get_row_data()
                got[r[0]] = r
            for r in items:
                b = got.get(r["trade_date"])
                if not b:
                    leftover.append((code, r))
                    continue
                try:
                    b_o = float(b[1]); b_h = float(b[2]); b_l = float(b[3]); b_c = float(b[4])
                    vol = float(b[5]); amt = float(b[6]); turn = float(b[7]); pct = float(b[8])
                except (ValueError, TypeError):
                    leftover.append((code, r))
                    continue
                is_strict = bool(r["close"]) and abs(b_c / r["close"] - 1) <= TOL
                if not is_strict and not _scaled_match(r, b_o, b_h, b_l, b_c):
                    mismatch += 1
                    leftover.append((code, r))
                    print(f"  [SKIP] {code} {r['trade_date']} OHLC 无法对齐: "
                          f"库={r['open']}/{r['high']}/{r['low']}/{r['close']} "
                          f"bs={b_o}/{b_h}/{b_l}/{b_c}")
                    continue
                conn.execute(
                    "UPDATE daily_price SET volume=?, amount=?, pct_change=?, turnover=? "
                    "WHERE code=? AND trade_date=?",
                    (vol, amt, pct, turn, code, r["trade_date"]))
                if is_strict:
                    strict += 1
                else:
                    scaled += 1
            if i % 200 == 0:
                conn.commit()
                print(f"  ... {i}/{len(by_code)} 只，已修 {strict + scaled} 行")
    finally:
        bs.logout()
    return strict, scaled, mismatch, leftover


def _apply_tencent(conn, leftover) -> tuple[int, int]:
    """baostock 无数据的行用腾讯 qfq 兜底：量=手×100，额=量×均价(高/低/收)。"""
    import akshare as ak
    import pandas as pd
    from datetime import datetime as _dt

    by_code: dict[str, list] = {}
    for code, r in leftover:
        by_code.setdefault(code, []).append(r)

    fixed = skipped = 0
    for code, items in sorted(by_code.items()):
        dates = [r["trade_date"] for r in items]
        start = (_dt.strptime(min(dates), "%Y-%m-%d") - pd.Timedelta(days=10)).strftime("%Y%m%d")
        end = max(dates).replace("-", "")
        try:
            df = ak.stock_zh_a_hist_tx(symbol=_tx_code(code), start_date=start,
                                       end_date=end, adjust="qfq")
        except Exception as e:
            print(f"  [TX-ERR] {code}: {e}")
            skipped += len(items)
            continue
        if df is None or df.empty:
            skipped += len(items)
            continue
        df = df.rename(columns={"amount": "volume_lot"})
        for col in ("open", "close", "high", "low", "volume_lot"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        # 前收用于 pct（多取的 10 个自然日暖机）
        df["pct_change"] = (df["close"].pct_change() * 100).fillna(0)
        got = {str(d)[:10]: row for d, row in zip(df["date"], df.to_dict("records"))}
        for r in items:
            b = got.get(r["trade_date"])
            if not b or not _scaled_match(r, b["open"], b["high"], b["low"], b["close"]):
                skipped += 1
                print(f"  [TX-SKIP] {code} {r['trade_date']} OHLC 无法对齐")
                continue
            vol = float(b["volume_lot"]) * 100.0
            amt = round(vol * (float(b["high"]) + float(b["low"]) + float(b["close"])) / 3.0, 2)
            conn.execute(
                "UPDATE daily_price SET volume=?, amount=?, pct_change=?, turnover=0 "
                "WHERE code=? AND trade_date=?",
                (vol, amt, float(b["pct_change"]), code, r["trade_date"]))
            fixed += 1
        conn.commit()
    return fixed, skipped


def main():
    ap = argparse.ArgumentParser(description="修复腾讯兜底路径写坏的成交量/成交额")
    ap.add_argument("--apply", action="store_true", help="实际写库（默认 dry-run）")
    ap.add_argument("--since", default="2025-01-01", help="只处理该日期之后（默认 2025-01-01）")
    args = ap.parse_args()

    with get_conn() as conn:
        rows, by_code, by_date = find_affected(conn, args.since)

    print(f"受影响行: {len(rows)}  涉及股票: {len(by_code)}  涉及交易日: {len(by_date)}")
    for d in sorted(by_date):
        print(f"  {d}  {by_date[d]:>5} 行")
    print("\n样本（code / date / 库里 volume / 库里 amount / close）:")
    for r in rows[:5]:
        print(f"  {r['code']} {r['trade_date']}  vol={r['volume']:.2f}  "
              f"amt={r['amount']:.0f}  close={r['close']}")

    if not args.apply:
        print("\n[dry-run] 未写库。加 --apply 才会用 baostock/腾讯真实量额覆盖。")
        return

    # 写库前备份旧值（便于回滚）
    import json
    from datetime import datetime as _dt
    reports_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
    os.makedirs(reports_dir, exist_ok=True)
    backup_path = os.path.join(reports_dir, f"fix_tencent_bug_backup_{_dt.now():%Y%m%d_%H%M%S}.jsonl")
    with open(backup_path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({"code": r["code"], "trade_date": r["trade_date"],
                                "volume": r["volume"], "amount": r["amount"],
                                "close": r["close"]}, ensure_ascii=False) + "\n")
    print(f"旧值备份: {backup_path}")

    with get_conn() as conn:
        strict, scaled, mismatch, leftover = _apply_baostock(conn, by_code)
        tx_fixed = tx_skipped = 0
        if leftover:
            print(f"\nbaostock 未覆盖 {len(leftover)} 行，转腾讯兜底 ...")
            tx_fixed, tx_skipped = _apply_tencent(conn, leftover)

    print(f"\n[apply] 已修复 {strict} 行(严格) + {scaled} 行(缩放校验) + "
          f"{tx_fixed} 行(腾讯兜底)；baostock/腾讯均无法覆盖 {tx_skipped} 行；"
          f"OHLC 对不齐跳过 {mismatch} 行。")
    with get_conn() as conn:
        _left, _by_code2, by_date2 = find_affected(conn, args.since)
    left = sum(by_date2.values())
    print(f"复查：仍命中签名 {left} 行" + ("（应为 0）" if left == 0 else "，请按日期排查"))
    print("提示：volume/amount 参与 vol_score / amt20，需重算受影响交易日的策略分。")


if __name__ == "__main__":
    main()
