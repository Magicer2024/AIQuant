"""个股深度止损宽度上限（deep_atr_stop_cap=8%）落地回归。

覆盖 5 件事（任一失败即 exit=1）：
  ① 夹逼数学：绑定/不绑定/None/关闭/退化 cap 五种输入；
  ② 公式与回测臂等价：stop_on == max(stop_off, round(entry×(1−cap), 2))；
  ③ **只动止损**：同一只票 enabled=1 与 enabled=0 两跑，take_profit 必须逐行相同；
  ④ 真实样本确实被夹逼（该票原始宽度 > 8%）且**只会收紧、绝不放宽**；
  ⑤ `_signal_plan_at`（K 线买点/持仓统计）与 `_current_signal`（落库/跟踪）两条出口口径一致。

⚠ 为什么用「开关前后两跑对比」而不是硬编码期望值：深析信号依赖一整套 rolling 指标，
手工推算期望止损极易算错；用关闭态当"改动前基线"是唯一无歧义的等价性判据。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy.stock_deep as sd                       # noqa: E402
from core.db import get_conn                           # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


REAL = sd._get_param
CAP = float(REAL("deep_atr_stop_cap"))
ENABLED = int(REAL("deep_atr_stop_enabled"))
print(f"\n=== 生效参数: deep_atr_stop_enabled={ENABLED} · deep_atr_stop_cap={CAP} ===")


def _patch(enabled, cap):
    def fake(key, *a, **k):
        if key == "deep_atr_stop_enabled":
            return enabled
        if key == "deep_atr_stop_cap":
            return cap
        return REAL(key, *a, **k)
    sd._get_param = fake


def _restore():
    sd._get_param = REAL


# ── ① 夹逼数学 ──────────────────────────────────────────────
print("\n=== ① 夹逼数学（cap=8%）===")
_restore()
check("宽度 12% > 8% → 收到 entry×0.92", sd._deep_stop_cap(100.0, 88.0) == 92.0,
      f"got {sd._deep_stop_cap(100.0, 88.0)}")
check("宽度 7% < 8% → 原样不动（只收紧不放宽）", sd._deep_stop_cap(100.0, 93.0) == 93.0)
check("宽度恰 8% → 不动", sd._deep_stop_cap(100.0, 92.0) == 92.0)
check("stop=None → None", sd._deep_stop_cap(100.0, None) is None)
check("entry<=0 → 原样返回", sd._deep_stop_cap(0.0, 88.0) == 88.0)
_patch(0, CAP)
check("开关关闭 → 原样不动", sd._deep_stop_cap(100.0, 88.0) == 88.0)
_patch(1, 0)
check("cap=0 → 原样不动", sd._deep_stop_cap(100.0, 88.0) == 88.0)
_patch(1, 1.5)
check("cap>=1（退化）→ 原样不动", sd._deep_stop_cap(100.0, 88.0) == 88.0)
_patch(1, CAP)


# ── 挑一只真实高波动样本（原始宽度 > 10%）────────────────────
# 逐个候选试跑：库里少数票会因停牌/次新股导致行情断档（analyze 返回 insufficient），
# 这里跳过它们，保证测试不因样本选择而假失败。
print("\n=== 选取真实样本（原始宽度 > 10% 的最近 buy/add）===")
CANDS = []
with get_conn() as _c:
    for r in _c.execute(
            "SELECT scan_date, code, name, level, entry_price, stop_loss, atr_pct "
            "FROM stock_deep_signal "
            "WHERE level IN ('buy','add') AND entry_price > 0 AND stop_loss > 0 "
            "  AND stop_loss < entry_price "
            "  AND (entry_price - stop_loss) * 1.0 / entry_price > 0.10 "
            "ORDER BY scan_date DESC, code LIMIT 60").fetchall():
        d = dict(r)
        d["base_width"] = (d["entry_price"] - d["stop_loss"]) / d["entry_price"]
        CANDS.append(d)
if not CANDS:
    print("  [FAIL] 库里找不到原始宽度 > 10% 的样本（无法验证）")
    sys.exit(1)


def _analyze(code, day, enabled):
    """在同一次 analyze_stock 调用内固定开关状态，跑完立刻还原。"""
    _patch(enabled, CAP)
    try:
        with get_conn() as conn:
            return sd.analyze_stock(conn, code, as_of=day, recent_days=45)
    finally:
        _restore()


on = off = TARGET = None
for cand in CANDS:
    try:
        a_on = _analyze(cand["code"], cand["scan_date"], ENABLED)
        if a_on.get("insufficient") or not (a_on.get("signal") or {}).get("action_plan", {}).get("stop_loss"):
            continue
        a_off = _analyze(cand["code"], cand["scan_date"], 0)
    except Exception as e:                              # pragma: no cover
        print(f"  跳过 {cand['code']}: {type(e).__name__}: {e}")
        continue
    on, off, TARGET = a_on, a_off, cand
    break
if TARGET is None:
    print("  [FAIL] 60 个候选全都跑不出可用信号（行情断档）")
    sys.exit(1)
print(f"  {TARGET['scan_date']} {TARGET['code']} {TARGET['name']} "
      f"level={TARGET['level']} entry={TARGET['entry_price']} "
      f"stop={TARGET['stop_loss']} 原始宽度={TARGET['base_width']*100:.2f}% "
      f"ATR%={TARGET['atr_pct']}")

D, CODE = TARGET["scan_date"], TARGET["code"]

sig_on, sig_off = on["signal"], off["signal"]
ap_on, ap_off = sig_on["action_plan"], sig_off["action_plan"]
entry = float(ap_on["entry_price"])
stop_on, stop_off = float(ap_on["stop_loss"]), float(ap_off["stop_loss"])
w_on = (entry - stop_on) / entry
w_off = (entry - stop_off) / entry
floor = round(entry * (1.0 - CAP), 2)

# ── ④ 真实样本确实被夹逼，且只收紧不放宽 ──
print("\n=== ④ 真实信号（_current_signal / 落库口径）===")
print(f"  关闭态(改动前) 止损={stop_off}  宽度={w_off*100:.2f}%")
print(f"  开启态(改动后) 止损={stop_on}   宽度={w_on*100:.2f}%  上限地板={floor}")
check("样本原始宽度确实 > cap（否则本条测试无意义）", w_off > CAP,
      f"{w_off*100:.2f}% vs {CAP*100:.0f}%")
# ⚠ 止损价 round 到分位后，宽度可能比名义 cap 略高一点点（如 entry=4.99 → 4.59 → 8.016%）。
#   回测量化口径用的是同一个 round(entry×(1−cap), 2)，故这属**等价实现**，断言需带舍入容差。
_tol_on = CAP + 0.005 / entry + 1e-9
check("开启后宽度 ≤ cap（含分位舍入容差）", w_on <= _tol_on,
      f"{w_on*100:.3f}% ≤ {(CAP+0.005/entry)*100:.3f}%")
check("只会收紧：stop_on ≥ stop_off", stop_on >= stop_off - 1e-9)
# ── ② 公式与回测臂等价 ──
check("公式等价 stop_on == max(stop_off, round(entry×(1−cap), 2))",
      abs(stop_on - max(stop_off, floor)) < 1e-9,
      f"{stop_on} vs {max(stop_off, floor)}")
# ── ③ 只动止损，止盈不变 ──
check("止盈不受影响（回测 A0 口径一致）",
      float(ap_on["take_profit"]) == float(ap_off["take_profit"]),
      f"{ap_on['take_profit']} vs {ap_off['take_profit']}")
check("风报比按夹逼后真实止损重算（≠ 关闭态）",
      sig_on.get("risk_pct") is not None
      and abs(float(sig_on["risk_pct"]) - w_on * 100) < 0.02,
      f"risk_pct={sig_on.get('risk_pct')} vs 宽度 {w_on*100:.2f}%")

# ── ⑤ 两条出口口径一致（_signal_plan_at vs _current_signal）──
print("\n=== ⑤ K 线/持仓统计出口（_signal_plan_at）===")
adv_on = {a["date"]: a for a in (on.get("recent_advice") or []) if a.get("stop_loss")}
adv_off = {a["date"]: a for a in (off.get("recent_advice") or []) if a.get("stop_loss")}
check("两态 advice 行数一致", len(adv_on) == len(adv_off),
      f"{len(adv_on)} vs {len(adv_off)}")
bad, n_ok, n_cap = [], 0, 0
for d, a_on in adv_on.items():
    a_off = adv_off.get(d)
    if not a_off:
        bad.append((d, "缺对照行"))
        continue
    e = float(a_on["entry_price"])
    s_on, s_off = float(a_on["stop_loss"]), float(a_off["stop_loss"])
    if abs(s_on - max(s_off, round(e * (1.0 - CAP), 2))) > 1e-9:
        bad.append((d, f"{s_on} != {max(s_off, round(e*(1.0-CAP), 2))}"))
    elif abs(float(a_on["take_profit"]) - float(a_off["take_profit"])) > 1e-9:
        bad.append((d, "止盈被改动"))
    else:
        n_ok += 1
    if (e - s_on) / e <= CAP + 1e-9 and (e - s_off) / e > CAP + 1e-9:
        n_cap += 1
check("advice 每行 stop == max(原值, 上限地板) 且止盈不变", not bad,
      f"{len(bad)} 行异常" + (f" 例: {bad[:2]}" if bad else ""))
check("advice 里确有行被夹逼（覆盖到该出口）", n_cap > 0, f"被夹逼 {n_cap} 行")
_viol = [(a["date"], a["entry_price"],
          (float(a["entry_price"]) - float(a["stop_loss"])) / float(a["entry_price"]))
         for a in adv_on.values()
         if (float(a["entry_price"]) - float(a["stop_loss"])) / float(a["entry_price"])
         > CAP + 0.005 / float(a["entry_price"]) + 1e-9]
_worst = max(((float(a["entry_price"]) - float(a["stop_loss"])) / float(a["entry_price"]),
              float(a["entry_price"]), a["date"]) for a in adv_on.values())
print(f"  最宽的一行: {_worst[2]} entry={_worst[1]} 宽度={_worst[0]*100:.3f}% "
      f"（含舍入外溢上限 {(CAP+0.005/_worst[1])*100:.3f}%）")
check("夹逼后所有 advice 行宽度 ≤ cap（含分位舍入容差）", not _viol,
      f"{len(_viol)} 行超差" + (f" 例: {_viol[:2]}" if _viol else ""))

# ── 收尾 ────────────────────────────────────────────────────
_restore()
print("\n" + "=" * 70)
if FAILED:
    print(f"❌ {len(FAILED)} 项失败: {FAILED}")
    sys.exit(1)
print("✅ 全部通过：deep_atr_stop_cap 只收紧止损、止盈与其他字段不变。")
