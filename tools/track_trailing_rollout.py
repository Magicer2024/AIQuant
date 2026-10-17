# -*- coding: utf-8 -*-
"""任务4 跟踪：long_trailing_pct 0.15→0.20 上线后月度对账（只读，可重复执行）。

背景：2026-10-17 Yu Fang 批准长线移动止盈回撤 15%→20%（任务2 E2t_20 回放，
群共享 task2_replay/report_e2e3b_grid.md）。失败线（结案包 §4）：
实测相对改善 < 回放预测的 50% ⇒ 报管理A转 Yu Fang 回滚（参数改回 0.15 即完全回退）。

回放基线（2015-2026 八臂网格，扣0.3%，已了结口径）：
  base(0.15): 每笔净期望 +9.25pp、平均持有 147 日、top5% 贡献 45.1%
  trail20   : 每笔净期望 +15.46pp、平均持有 191 日、p10 -20.1 vs -19.8
  ΔEV 预测 = +6.21pp/笔；失败线 = 实测 ΔEV < +3.11pp（50%）

对账口径：recommend_outcome（1c 后窗口滚动+归档）取 horizon='long' 且
scan_date>=CHANGE_DATE 的已结算行，与变更前对照组（END 之前 9 个月窗口）比
exit_return 均值。⚠ 长线结算慢（中位持有 190 日），上线初期 n 小属预期，
n<30 时只报进度不下结论；t10/t60 诊断列先行观察。

用法（月度手动执行，或挂调度）：
    PYTHONIOENCODING=utf-8 python tools/track_trailing_rollout.py
退出码 0=正常/样本不足，2=触发失败线（需上报）。
"""
import sqlite3, sys, os
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "quant.db")
CHANGE_DATE = "2026-10-17"          # 参数生效日（含）
PRE_FROM = "2026-01-01"             # 变更前对照窗口起（变更前 9 个月）
REPLAY_BASE_EV = 9.25               # 回放 base 臂（pp/笔，含成本，毛口径偏差见报告）
REPLAY_TRAIL20_EV = 15.46
REPLAY_DELTA = REPLAY_TRAIL20_EV - REPLAY_BASE_EV   # +6.21
FAIL_LINE = REPLAY_DELTA * 0.5                       # +3.11
MIN_N = 30                           # 已结算样本下限，未达不下结论

def fetch(conn, since, until):
    rows = conn.execute("""
        SELECT exit_return, hold_days FROM recommend_outcome
        WHERE horizon='long' AND exit_return IS NOT NULL
          AND scan_date >= ? AND scan_date < ?""", (since, until)).fetchall()
    # 1c 归档表补历史（窗口滚动可能移出旧行，archive 为 append-only 全量）
    try:
        rows += conn.execute("""
            SELECT exit_return, hold_days FROM recommend_outcome_archive
            WHERE horizon='long' AND exit_return IS NOT NULL
              AND scan_date >= ? AND scan_date < ?""", (since, until)).fetchall()
        seen = set(); dedup = []
        for r in rows:
            if r[0] is None: continue
            key = (r[0], r[1])
            dedup.append(r)
        rows = dedup
    except sqlite3.OperationalError:
        pass  # 无归档表则仅用主表
    return [r[0] for r in rows if r[0] is not None]

def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    post = fetch(con, CHANGE_DATE, "2099-01-01")
    pre = fetch(con, PRE_FROM, CHANGE_DATE)
    print(f"变更前对照（{PRE_FROM}~{CHANGE_DATE}，long 已结算）: n={len(pre)}")
    print(f"变更后（{CHANGE_DATE}~，long 已结算）: n={len(post)}")
    if post:
        print(f"  均值={np.mean(post):.2f}pp 中位={np.median(post):.2f} p10={np.percentile(post,10):.1f}")
    if pre:
        print(f"  对照均值={np.mean(pre):.2f}pp（注意：对照组是 0.15 纪律在近期市况的表现，含年景成分）")
    # 代理指标（结算未完成部分）
    proxy = con.execute("""
        SELECT COUNT(*), AVG(t10_return) FROM recommend_outcome
        WHERE horizon='long' AND scan_date>=? AND t10_return IS NOT NULL""",
        (CHANGE_DATE,)).fetchone()
    print(f"变更后 t10 代理（未完成结算样本）: n={proxy[0]} avg={proxy[1] if proxy[1] is None else round(proxy[1],2)}")

    if len(post) < MIN_N:
        print(f"\n样本不足（{len(post)}/{MIN_N}）：长线中位持有≈190日，预计首批结算在上线后 6-9 个月。"
              f"本期只跟踪进度，不触发判定。")
        return 0
    delta = np.mean(post) - (np.mean(pre) if pre else REPLAY_BASE_EV)
    print(f"\n实测 ΔEV（post vs pre 对照）= {delta:+.2f}pp/笔；回放预测 +{REPLAY_DELTA:.2f}，失败线 +{FAIL_LINE:.2f}")
    if delta < FAIL_LINE:
        print(">>> 触发失败线：请报管理A转 Yu Fang 回滚（strategy_params long_trailing_pct 改回 0.15）。")
        return 2
    print(">>> 未触发失败线。继续月度跟踪（跟踪窗至 2027-04）。")
    return 0

if __name__ == "__main__":
    sys.exit(main())
