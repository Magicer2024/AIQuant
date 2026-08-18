"""
tools/fix_volume_unit.py —— 修复 daily_price 量价单位不统一（手/股 + 成交额）

背景：
  不同数据源混用导致 daily_price 部分行的量价单位错位。判据用无量纲比值：
      ratio = amount / volume / close
  正常行情 ratio ≈ 1（成交额 / 成交量 ≈ 均价 ≈ 收盘价）。

  两类异常与修法（均已用全库数据验证）：
    1. ratio > 20     → volume 存成「手」（1手=100股，偏小 100 倍）→ volume × 100
    2. ratio < 0.05   → amount 偏小 100 倍（volume 正常）           → amount × 100
       （注：ratio<0.05 理论上也可能是 volume 被重复 ×100，但实测全库仅
        2026-06-24~26 沪市这批是 amount 偏小，故统一按 amount×100 处理。）

用法：
  python tools/fix_volume_unit.py                 # dry-run：只统计，不写库
  python tools/fix_volume_unit.py --apply         # 实际修复
  python tools/fix_volume_unit.py --since 2024-01-01   # 只修某个日期之后

安全：
  - 只触碰 ratio 明确异常（>20 或 <0.05）的行，正常行情（0.5~2）不碰。
  - 默认 dry-run，必须显式 --apply 才写库。
  - 修复后可再跑一次 dry-run 校验：异常行数应趋近 0。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core.db import get_conn

# ratio 判据
RATIO_SMALL = 20.0    # volume 偏小（手），需 volume × 100
RATIO_LARGE = 0.05    # amount 偏小，需 amount × 100


def _where(since):
    cond = "volume > 0 AND amount > 0 AND close > 0"
    args = []
    if since:
        cond += " AND trade_date >= ?"
        args.append(since)
    return cond, args


def scan(conn, since):
    cond, args = _where(since)
    small = conn.execute(
        f"SELECT COUNT(*) FROM daily_price WHERE {cond} AND amount/volume/close > ?",
        args + [RATIO_SMALL],
    ).fetchone()[0]
    large = conn.execute(
        f"SELECT COUNT(*) FROM daily_price WHERE {cond} AND amount/volume/close < ?",
        args + [RATIO_LARGE],
    ).fetchone()[0]
    return small, large


def main():
    ap = argparse.ArgumentParser(description="修复 daily_price 量价单位（手→股 / 成交额×100）")
    ap.add_argument("--apply", action="store_true", help="实际写库（默认 dry-run）")
    ap.add_argument("--since", default=None, help="只处理该日期之后，如 2024-01-01")
    args = ap.parse_args()

    with get_conn() as conn:
        small, large = scan(conn, args.since)
        print(f"volume 偏小(手→股，需 volume×100): {small} 行")
        print(f"amount 偏小(需 amount×100):         {large} 行")

        if not args.apply:
            print("\n[dry-run] 未写库。加 --apply 才会实际修复。")
            return

        cond, pargs = _where(args.since)
        if small:
            conn.execute(
                f"UPDATE daily_price SET volume = volume * 100 "
                f"WHERE {cond} AND amount/volume/close > ?",
                pargs + [RATIO_SMALL],
            )
        if large:
            conn.execute(
                f"UPDATE daily_price SET amount = amount * 100 "
                f"WHERE {cond} AND amount/volume/close < ?",
                pargs + [RATIO_LARGE],
            )
        print(f"\n[apply] 已修复 {small} 行(volume×100) + {large} 行(amount×100)。")
        print("提示：修复后建议重跑本脚本（dry-run）校验异常已清零；")
        print("      若策略分依赖成交量/成交额，需重跑 python core/sync.py full 重算打分。")


if __name__ == "__main__":
    main()
