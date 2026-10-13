"""
tools/bench_t8_engine.py —— T8 回测引擎护栏 + 性能基准（固定库，关网只读）
=======================================================================

用法（库路径经 AIQUANT_DB_PATH 注入，必须在 import 项目前设置）：

  # 基线 / 复测：跑视觉回测 3 组条件场景 + 指定 rule_id 一键回测，结果落 JSON
  python tools/bench_t8_engine.py run --out reports/t8_baseline.json
  python tools/bench_t8_engine.py run --out reports/t8_after.json --visual-repeat 3

  # 指纹对比（收益/胜率/交易数/逐笔交易必须 diff=0）+ 耗时对照
  python tools/bench_t8_engine.py compare reports/t8_baseline.json reports/t8_after.json

  # 同机 A/B（重构后）：新「按需指标」 vs monkeypatch 强制「全套指标」，
  # 顺序刻意对旧实现有利（新冷跑→旧热跑），指纹必须一致
  python tools/bench_t8_engine.py ab
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

# ── 固定库注入：必须在 import 项目模块前完成 ──
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _ROOT)
_DB = os.path.abspath(os.path.join(_ROOT, "data_cache", "t7_golden.db"))
os.environ.setdefault("AIQUANT_DB_PATH", _DB)
os.environ["AIQUANT_SCHEDULER_ENABLED"] = "0"
os.environ["AIQUANT_AUTO_SYNC_ENABLED"] = "0"
os.environ["AIQUANT_QLIB_ENABLED"] = "0"
os.environ.pop("AIQUANT_TESTING", None)

import backtest.engine as engine_mod
from backtest.engine import BacktestParams, VisualBacktestEngine, _add_indicators
from backtest.rule_engine import run_rule_backtest
from core.db import get_conn

# 视觉回测场景：覆盖「零重指标 / 仅均线 / 全套重指标」三类
WINDOW = ("2025-01-01", "2025-12-31")
VISUAL_SCENARIOS = {
    # 轻量：只用内联 rolling/pct_change，按理应不触发 MA/MACD/RSI/KDJ 任何重算
    "light": [
        {"indicator": "pct_change", "params": {"min": -5, "max": 5}},
        {"indicator": "volume_ratio", "operator": "gt", "params": {"min": 1.2}},
    ],
    # 仅均线：ma_cross + close_above_ma（自定义周期，验证按需取列）
    "ma_only": [
        {"indicator": "ma_cross", "operator": "golden_cross",
         "params": {"short_period": 5, "long_period": 20}},
        {"indicator": "close_above_ma", "operator": "above",
         "params": {"period": 30}},
    ],
    # 全套：MA/MACD/KDJ/RSI/BIAS/因子分 全覆盖（优化后应与旧版同速且同结果）
    "heavy": [
        {"indicator": "ma_cross", "operator": "golden_cross",
         "params": {"short_period": 5, "long_period": 20}},
        {"indicator": "macd_cross", "operator": "golden_cross"},
        {"indicator": "kdj_cross", "operator": "oversold", "params": {"oversold": 20}},
        {"indicator": "rsi", "operator": "oversold", "params": {"period": 14, "oversold": 30}},
        {"indicator": "bias", "operator": "lt", "params": {"period": 6, "value": -3}},
        {"indicator": "fusion_score", "operator": "gt", "params": {"min": 60}},
    ],
}
DEFAULT_RULE_IDS = [602, 606]


def _fingerprint(result: dict) -> dict:
    """提取与「结果一致性」相关的指纹（耗时/进度等物理字段不入）。"""
    keys = ("final_assets", "total_return", "annual_return", "max_drawdown",
            "sharpe_ratio", "win_rate", "profit_factor", "total_trades",
            "win_trades", "loss_trades", "stock_pool_size", "trade_days")
    fp = {k: result.get(k) for k in keys if k in result}
    # 权益曲线逐日净值求和（捕捉任何一日的估值漂移）
    eq = result.get("equity_curve") or []
    fp["equity_sum"] = round(sum(float(p["total"]) for p in eq), 2)
    fp["equity_len"] = len(eq)
    # 逐笔交易：代码/买卖日/股数/单笔盈亏/出场原因
    fp["trades"] = [
        (t["code"], t["buy_date"], t["sell_date"], t["shares"],
         t["pnl"], t.get("exit_reason"))
        for t in (result.get("trades") or [])
    ]
    return fp


def _make_params(start: str, end: str) -> BacktestParams:
    """与线上默认同口径的回测参数（止损止盈走引擎默认 None）。"""
    return BacktestParams(start_date=start, end_date=end)


def _run_visual_once(name: str, conditions: list) -> dict:
    """跑一次视觉回测，返回指纹 + 各阶段墙钟（秒）。"""
    stage_first: dict[str, float] = {}

    def _cb(info):
        stage = info.get("stage")
        if stage and stage not in stage_first:
            stage_first[stage] = time.perf_counter()

    eng = VisualBacktestEngine(_make_params(*WINDOW), progress_cb=_cb)
    t0 = time.perf_counter()
    result = eng.run(conditions)
    total = time.perf_counter() - t0

    def _seg(a, b):
        return round(stage_first[b] - stage_first[a], 3) if a in stage_first and b in stage_first else None

    return {
        "fingerprint": _fingerprint(result),
        "timing": {
            "total": round(total, 3),
            "load": _seg("init", "indicators"),
            "indicators": _seg("indicators", "signal"),
            "signal": _seg("signal", "running"),
            "portfolio": round(total - (stage_first["running"] - t0), 3)
            if "running" in stage_first else None,
        },
    }


def _install_universe_limit(n: int) -> None:
    """截断规则回测的扫描宇宙到前 n 只（版本间快速 diff=0 用，不影响回测口径）。

    run_rule_backtest 在函数体内 `from strategy.scorer import _load_stock_data_streaming`，
    patch 源模块属性即可在调用时生效。
    """
    import strategy.scorer as scorer
    if n <= 0:
        return
    _orig = scorer._load_stock_data_streaming

    def _limited(trade_date, batch_size=100):
        for i, item in enumerate(_orig(trade_date, batch_size)):
            if i >= n:
                break
            yield item

    scorer._load_stock_data_streaming = _limited


def _run_rule_once(rule_id: int) -> dict:
    """跑一条 strategy_rules 规则的一键回测。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM strategy_rules WHERE id = ?", (rule_id,)
        ).fetchone()
    if not row:
        raise SystemExit(f"规则不存在: {rule_id}")
    rule = dict(row)
    last = [0.0]

    def _rule_cb(info):
        now = time.perf_counter()
        if now - last[0] > 2:  # 节流：最多 ~2s 一条进度
            print(f"   ...{info.get('message')} ({info.get('progress')}%)", flush=True)
            last[0] = now

    t0 = time.perf_counter()
    result = run_rule_backtest(rule, _make_params(*WINDOW), progress_cb=_rule_cb)
    out = {"timing": {"total": round(time.perf_counter() - t0, 3)}}
    if result.get("success"):
        out["fingerprint"] = _fingerprint(result)
    else:
        out["fingerprint"] = {"error": result.get("error"),
                              "total_trades": result.get("total_trades", 0)}
    out["rule_name"] = rule.get("rule_name")
    return out


def cmd_run(args) -> None:
    payload = {"window": list(WINDOW), "db": os.environ["AIQUANT_DB_PATH"],
               "visual": {}, "rules": {}}
    for i in range(args.visual_repeat):
        for name, conds in VISUAL_SCENARIOS.items():
            print(f"[visual] round {i+1}/{args.visual_repeat} scenario={name}", flush=True)
            payload["visual"].setdefault(name, []).append(_run_visual_once(name, conds))
    payload["rule_universe_limit"] = args.universe_limit
    _install_universe_limit(args.universe_limit)
    for rid in args.rule_ids:
        print(f"[rule] rule_id={rid}", flush=True)
        payload["rules"][str(rid)] = _run_rule_once(rid)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, default=str)
    print(f"已写入 {args.out}")


def _fp_equal(a, b, path="") -> list:
    """递归比较指纹，返回差异路径列表（空列表=完全一致）。"""
    diffs = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                diffs.append(f"{path}.{k} 缺失")
            else:
                diffs += _fp_equal(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            diffs.append(f"{path} 长度 {len(a)} != {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            diffs += _fp_equal(x, y, f"{path}[{i}]")
    elif a != b:
        diffs.append(f"{path}: {a!r} != {b!r}")
    return diffs


def cmd_compare(args) -> None:
    a = json.load(open(args.a, encoding="utf-8"))
    b = json.load(open(args.b, encoding="utf-8"))
    bad = 0
    # 视觉场景：基线最后一轮 vs 复测最后一轮（结果应逐轮一致，取末轮最稳）
    for name in VISUAL_SCENARIOS:
        fa = a["visual"][name][-1]["fingerprint"]
        fb = b["visual"][name][-1]["fingerprint"]
        diffs = _fp_equal(fa, fb, name)
        ta = sum(r["timing"]["total"] for r in a["visual"][name]) / len(a["visual"][name])
        tb = sum(r["timing"]["total"] for r in b["visual"][name]) / len(b["visual"][name])
        ia = _avg_stage(a, name, "indicators"); ib = _avg_stage(b, name, "indicators")
        status = "✅" if not diffs else "❌"
        if diffs:
            bad += 1
        print(f"{status} {name}: 指纹 {'一致' if not diffs else diffs[:3]}")
        print(f"   total {ta:.2f}s → {tb:.2f}s（{_pct(ta, tb)}）；"
              f"indicators {ia}s → {ib}s")
    for rid in a.get("rules", {}):
        fa = a["rules"][rid].get("fingerprint"); fb = b["rules"][rid].get("fingerprint")
        diffs = _fp_equal(fa, fb, f"rule {rid}")
        ta = a["rules"][rid]["timing"]["total"]; tb = b["rules"][rid]["timing"]["total"]
        status = "✅" if not diffs else "❌"
        if diffs:
            bad += 1
        print(f"{status} rule {rid}: 指纹 {'一致' if not diffs else diffs[:3]}；"
              f"{ta:.2f}s → {tb:.2f}s")
    print("✅ T8 FINGERPRINT DIFF = 0" if bad == 0 else f"❌ {bad} 处不一致")
    sys.exit(1 if bad else 0)


def _avg_stage(doc, name, stage):
    vals = [r["timing"].get(stage) for r in doc["visual"][name]
            if r["timing"].get(stage) is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def _pct(old, new):
    if not old:
        return "n/a"
    d = (new - old) / old * 100
    return f"{d:+.1f}%"


def cmd_ab(args) -> dict:
    """同进程 A/B：新按需实现 vs 强制全套（旧行为），交替两轮。"""
    def _force_full(df, need=None):
        # 旧 _add_indicators 单参语义 = 全套指标（need 被忽略）
        return _add_indicators(df)

    rounds = []
    for i in range(2):
        # 顺序刻意对旧实现有利：新（冷）→ 旧（热）
        new_res, old_res = {}, {}
        for name, conds in VISUAL_SCENARIOS.items():
            new_res[name] = _run_visual_once(name, conds)
        engine_mod._add_indicators = _force_full
        try:
            for name, conds in VISUAL_SCENARIOS.items():
                old_res[name] = _run_visual_once(name, conds)
        finally:
            engine_mod._add_indicators = _add_indicators
        rounds.append((new_res, old_res))

    ok = True
    for name in VISUAL_SCENARIOS:
        fa = rounds[-1][0][name]["fingerprint"]
        fb = rounds[-1][1][name]["fingerprint"]
        diffs = _fp_equal(fa, fb, name)
        tn = rounds[-1][0][name]["timing"]["total"]
        to = rounds[-1][1][name]["timing"]["total"]
        in_ = rounds[-1][0][name]["timing"].get("indicators")
        io_ = rounds[-1][1][name]["timing"].get("indicators")
        print(f"{'✅' if not diffs else '❌'} {name}: total 新{tn:.2f}s/旧{to:.2f}s "
              f"({_pct(to, tn)})；indicators 新{in_}s/旧{io_}s；"
              f"指纹{'一致' if not diffs else diffs[:3]}")
        ok &= not diffs
    print("✅ A/B 指纹一致（新=按需，旧=强制全套）" if ok else "❌ A/B 指纹漂移")
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("--out", required=True)
    p_run.add_argument("--visual-repeat", type=int, default=3)
    p_run.add_argument("--rule-ids", type=int, nargs="*", default=DEFAULT_RULE_IDS)
    p_run.add_argument("--universe-limit", type=int, default=0,
                       help="规则回测只扫前 N 只股票（>0 时；快速版本对照用）")
    p_run.set_defaults(func=cmd_run)
    p_cmp = sub.add_parser("compare")
    p_cmp.add_argument("a"); p_cmp.add_argument("b")
    p_cmp.set_defaults(func=cmd_compare)
    p_ab = sub.add_parser("ab")
    p_ab.set_defaults(func=cmd_ab)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
