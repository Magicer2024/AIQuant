"""
strategy/optimizer.py —— 每日推荐策略优化器（suggest 模式）
=============================================================

数据同步后置钩子链的最后一环，形成「推荐 → 出场跟踪 → 诊断 → 寻优 → 采纳」闭环：

  1. run_daily_diagnosis  每日：读 recommend_outcome 近30天数据，
     按周期统计胜率/盈亏比并做亏损归因，写 optimizer_report。
  2. run_weekly_tuning    每周五盘后：对作用于「正式主榜」的关键参数做 ±1~2 档
     单参数扰动；调参依据 = 冻结配置管道（信号产生 → 推荐选择 → 模拟执行）下、
     相同执行配置的有效交易收益，train 段选优 / test 段确认均不劣于现值才产出建议，
     写 param_suggestion（不再用「每日融合分 Top8 + 次日开收 OC/CC」代理）。
     不具备完整执行回放条件（当前 legacy/shadow/v2 三模式均无正式主榜已结算模拟单
     累积）时，仅输出诊断、不产出可采纳建议。
  3. apply_suggestion / rollback_param  人工采纳后写 strategy_param_override
     覆盖层 + param_tune_log 审计，支持一键回滚。

设计约束（与用户既定原则一致）：
  - 只在现有引擎内调参，永不建议切换 SHORT_ENGINE；
  - suggest 模式：建议不自动生效，前端人工确认后才写覆盖层；
  - 单次寻优最多产出 1 条建议、只动一个参数一档；
  - 同一参数 3 个交易日冷却期内不重复建议；
  - 近30天短线推荐样本 < MIN_SAMPLES 时只诊断不调参。
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta

from core.db import get_conn
from config.strategy_params import (
    TUNABLE_PARAMS, get_param, invalidate_param_cache,
    get_tunable_params_state, compute_params_baseline_hash,
)

# ── 守护参数 ──────────────────────────────────
MIN_SAMPLES = 15          # 近30天短线正式样本下限，不足只诊断不调参
COOLDOWN_DAYS = 3         # 同一参数调整/建议后的冷却天数
DIAG_DAYS = 30            # 诊断窗口（自然日，约20个交易日）
TUNE_WINDOW_DAYS = 90     # 寻优回放窗口（自然日，约60个交易日）
TRAIN_RATIO = 0.7         # train/test 双窗口切分比例

# 各参数的候选档位（只取当前值相邻 ±1~2 档）
_PARAM_LADDERS = {
    "sig_threshold": [13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 20.0],
    "trend_gate_ma": [10, 20, 30],
    "trend_gate_slope_lookback": [3, 5, 8],
}

# 参数 → 正式信号线映射（方案 E3 bullet 3）。只有作用于「正式主榜（production cohort）
# 且启用」的信号线的可调参数，才可能产出该线建议；仅作用于观察线/停用策略、或未登记
# 映射的参数，_param_formal_line 返回 None ⇒ 上层产出「不适用于正式推荐」而非建议。
# list_key 与 core.recommendation_service.LIST_SPECS 对齐（short/mid/long/deep 为正式主榜）。
_PARAM_SIGNAL_LINE = {
    "sig_threshold": "short",
    "high_vol_sig_threshold": "short",
    "trend_gate_ma": "short",
    "trend_gate_slope_lookback": "short",
    "short_stop_loss": "short",
    "short_atr_stop_enabled": "short",
    "short_atr_stop_k": "short",
    "short_atr_stop_floor": "short",
    "short_atr_stop_cap": "short",
    "short_take_profit": "short",
    "deep_atr_stop_enabled": "deep",
    "deep_atr_stop_cap": "deep",
}

# 反事实回放产生的推荐批次归入此隔离实验编号（comparison 状态）：绝不占用正式名额、
# 不进入正式样本门槛、不与真实发布单混算（方案 C3 实验编号冻结同口径）。
OPTIMIZER_TUNING_EXPERIMENT_ID = "optimizer_tuning_v1"


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

from core.outcome_tracker import LEGACY_PRODUCTION_FILTER, settled_return, settlement_stats


def _final_return(row) -> float | None:
    """结算口径只取有效已出场收益。"""
    return settled_return(row)


def _group_by(rows, keyfn):
    """按 keyfn 分组，返回 (key, [rows]) 迭代（诊断按周期/策略分组用）。"""
    out: dict = {}
    for r in rows:
        out.setdefault(keyfn(r), []).append(r)
    return out.items()


def _save_report(report_type: str, payload: dict):
    with get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO optimizer_report (report_date, report_type, payload_json)
            VALUES (?, ?, ?)
        """, (date.today().isoformat(), report_type,
              json.dumps(payload, ensure_ascii=False)))


def _in_cooldown(conn, param_key: str) -> bool:
    """参数近 COOLDOWN_DAYS 天内被调整过，或已有待采纳建议 → 冷却"""
    cutoff = f"-{COOLDOWN_DAYS} days"
    tuned = conn.execute("""
        SELECT 1 FROM param_tune_log
        WHERE param_key = ? AND created_at >= datetime('now', ?) LIMIT 1
    """, (param_key, cutoff)).fetchone()
    if tuned:
        return True
    pending = conn.execute("""
        SELECT 1 FROM param_suggestion
        WHERE param_key = ? AND status = 'pending' LIMIT 1
    """, (param_key,)).fetchone()
    return pending is not None


def current_param_version(conn) -> int:
    """当前参数版本号：每次采纳/回滚生成一个新版本（单调递增，0=代码默认）。

    版本 = param_tune_log 中改参数动作（apply/rollback）的累计条数。诊断按此分组、
    采纳时递增；旧推荐与旧模拟单按其生成时的参数版本冻结、不回写（方案 E3 bullet 7）。
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM param_tune_log WHERE action IN ('apply','rollback')"
    ).fetchone()
    return int(row["n"]) if row and row["n"] is not None else 0


# ─────────────────────────────────────────────
# 1. 每日诊断
# ─────────────────────────────────────────────

def _param_version_now() -> int:
    """读取当前参数版本（独立短连接，供诊断/寻优 payload 打标）。"""
    with get_conn() as conn:
        return current_param_version(conn)


def _strategy_breakdown(items) -> dict:
    """同一周期内按策略分组的结算胜率/均值/样本数（方案 E3 bullet 1）。"""
    out: dict = {}
    for strat, sub in _group_by(items, lambda r: r["strategy"] or "未标注"):
        vals = [v for v in (_final_return(r) for r in sub) if v is not None]
        if not vals:
            continue
        out[strat] = {
            "n": len(vals),
            "win_rate": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 2),
            "avg": round(sum(vals) / len(vals), 3),
        }
    return out


def _observation_summary(days: int) -> dict:
    """观察线单独诊断（方案 E3 bullet 1）：不进入正式样本门槛，仅描述性展示。

    正式诊断只统计 production 主榜（LEGACY_PRODUCTION_FILTER）；此处单独汇总被该过滤
    排除的观察策略（反转首日/强势突破/缩量回踩），明确标注 not_formal=True，避免观察线
    样本被误当作正式调参依据（与寻优的正式样本门槛彻底分离）。
    """
    cutoff = f"-{days} days"
    with get_conn() as conn:
        rows = conn.execute(f"""
            SELECT strategy, exit_reason, exit_date, exit_return
            FROM recommend_outcome
            WHERE scan_date >= date('now', ?) AND entry_price > 0
              AND NOT ({LEGACY_PRODUCTION_FILTER})
        """, (cutoff,)).fetchall()
    by_strat: dict = {}
    for r in rows:
        v = settled_return(r)
        if v is None:
            continue
        by_strat.setdefault(r["strategy"] or "未标注", []).append(v)
    breakdown = {
        s: {"n": len(vals),
            "win_rate": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 2),
            "avg": round(sum(vals) / len(vals), 3)}
        for s, vals in by_strat.items()
    }
    return {"not_formal": True, "by_strategy": breakdown,
            "note": "观察线单独诊断，不进入正式样本门槛，不作生产调参依据"}


def run_daily_diagnosis(days: int = DIAG_DAYS) -> dict:
    """近 N 天推荐表现诊断 + 亏损归因，结果写 optimizer_report。"""
    cutoff = f"-{days} days"
    with get_conn() as conn:
        rows = conn.execute(f"""
            SELECT horizon, strategy, fusion_score, entry_price, stop_loss,
                   t1_return, t3_return, t5_return, t10_return,
                   max_return, min_return, hit_stop, hit_tp,
                   exit_reason, exit_date, exit_return
            FROM recommend_outcome
            WHERE scan_date >= date('now', ?) AND entry_price > 0
              AND {LEGACY_PRODUCTION_FILTER}
        """, (cutoff,)).fetchall()

    by_horizon: dict = {}
    for r in rows:
        h = r["horizon"] or "short"
        by_horizon.setdefault(h, []).append(r)

    summary = {}
    findings = []
    for h, items in by_horizon.items():
        rets = [(_final_return(r), r) for r in items]
        rets = [(v, r) for v, r in rets if v is not None]
        if not rets:
            continue
        values = [v for v, _ in rets]
        wins = [v for v in values if v > 0]
        losses = [v for v in values if v <= 0]
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 1.0
        summary[h] = {
            "total": len(values), "signal_count": len(items),
            **settlement_stats(items),
            "stop_hit": sum(1 for _, r in rets if r["hit_stop"]),
            "tp_hit": sum(1 for _, r in rets if r["hit_tp"]),
            # 按策略再分组（方案 E3 bullet 1：正式诊断按周期/策略/版本分组）
            "by_strategy": _strategy_breakdown(items),
        }

        # ── 亏损归因（只对短线做，样本最多且是优化主对象）──
        if h != "short" or len(values) < 10:
            continue

        # 归因①：止损过紧——止损被打后 T10 仍收正的占比
        stopped = [r for _, r in rets if r["hit_stop"]]
        if stopped:
            recovered = sum(1 for r in stopped
                            if r["t10_return"] is not None and r["t10_return"] > 0)
            ratio = recovered / len(stopped)
            if ratio >= 0.4:
                if bool(int(get_param("short_atr_stop_enabled") or 0)):
                    _stop_desc = (
                        f"当前 ATR 自适应止损（{get_param('short_atr_stop_k')}×ATR14，"
                        f"夹逼 {get_param('short_atr_stop_floor'):.0%}~"
                        f"{get_param('short_atr_stop_cap'):.0%}）")
                else:
                    _stop_desc = f"当前固定止损 {get_param('short_stop_loss')*100:.0f}%"
                findings.append(
                    f"止损单中 {ratio*100:.0f}% 在 T+10 收正（{recovered}/{len(stopped)}），"
                    f"{_stop_desc} 可能偏紧")

        # 归因②：低分单拖累——fusion_score 下半区 vs 上半区胜率
        scored = sorted([(float(r["fusion_score"] or 0), v) for v, r in rets],
                        key=lambda x: x[0])
        half = len(scored) // 2
        if half >= 5:
            low_wr = sum(1 for _, v in scored[:half] if v > 0) / half * 100
            high_wr = sum(1 for _, v in scored[half:] if v > 0) / (len(scored) - half) * 100
            if high_wr - low_wr >= 10:
                findings.append(
                    f"低分单拖累明显：融合分下半区胜率 {low_wr:.0f}% vs 上半区 {high_wr:.0f}%，"
                    f"提高融合分阈值（当前 {get_param('sig_threshold')}）或有增益")

        # 归因③：接飞刀残留——亏损单最大浮亏中位数
        if losses:
            loss_min = sorted(float(r["min_return"] or 0) for v, r in rets if v <= 0)
            median_dd = loss_min[len(loss_min) // 2]
            if median_dd <= -8:
                findings.append(
                    f"亏损单最大浮亏中位数 {median_dd:.1f}%，趋势闸门"
                    f"（MA{get_param('trend_gate_ma')}/回看{get_param('trend_gate_slope_lookback')}日）"
                    f"可能仍放行弱势股")

    short_stat = summary.get("short", {})
    if short_stat and not findings:
        findings.append(
            f"近{days}天短线胜率 {short_stat['win_rate']}%、盈亏比 {short_stat['profit_factor']}，"
            "未发现显著缺陷")

    payload = {
        "window_days": days,
        "summary": summary,
        "observation": _observation_summary(days),
        "findings": findings,
        "params": {k: v["current"] for k, v in get_tunable_params_state().items()},
        # 版本追溯（方案 E3 bullet 1）：本诊断窗口对应的参数版本与基线哈希
        "param_version": _param_version_now(),
        "baseline_hash": compute_params_baseline_hash(),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_report("diagnosis", payload)
    return payload


# ─────────────────────────────────────────────
# 2. 周五参数寻优（冻结管道对照，方案 E3）
# ─────────────────────────────────────────────
#
# 调参依据（bullet 2）：冻结配置的「信号产生 → 推荐选择 → 实际模拟执行」流程下、
#   相同执行配置的有效交易收益；**不再**用「每日融合分 Top8 + 次日开收(OC/CC)收益」
#   这一代理指标作为生产调参依据。
# 降级（bullet 8）：样本不足、不具备完整执行回放条件或成本数据不足时，仅输出诊断、
#   不产出可采纳建议。当前 legacy/shadow/v2 三种模式均无正式主榜已结算模拟单累积
#   （见 _frozen_pipeline_available 说明），故寻优恒降级为「仅诊断」。
# 保留（bullet 4）：单参数扰动、样本门槛、冷却、人工采纳；不在结构重构中另调阈值。


def _production_lines() -> set:
    """LIST_SPECS 中 cohort=='production' 的正式主榜键集合。"""
    from core.recommendation_service import LIST_SPECS
    return {k for k, spec in LIST_SPECS.items() if spec[0] == "production"}


def _param_formal_line(param_key: str):
    """参数面向的正式信号线（bullet 3）。

    返回正式主榜 list_key；若参数仅作用于观察线/停用策略、或未登记映射，返回 None
    ⇒ 上层据此产出「不适用于正式推荐」而非建议。
    """
    line = _PARAM_SIGNAL_LINE.get(param_key)
    return line if (line and line in _production_lines()) else None


def _frozen_pipeline_available(conn, eval_start: str):
    """是否具备完整执行回放条件（bullet 2/8）。返回 (available, reason)。

    只有正式主榜（production cohort）已发布批次累积了足量、带执行配置的已结算模拟单，
    才能对候选参数做「同执行配置的有效交易收益」对照。当前三种模式均不满足：
      - legacy（默认）：日同步不产 signal_run / 发布批次；
      - shadow：只产 comparison 旁路批次（cohort=shadow），按 bullet 1 不入正式门槛；
      - v2：require_signal_model_ready 拒绝（未完成闭环 + 5 日旁路验收）。
    """
    from config.settings import SIGNAL_MODEL_MODE
    if SIGNAL_MODEL_MODE not in ("shadow", "v2"):
        return False, "当前 legacy 模式无冻结运行/发布批次"
    row = conn.execute("""
        SELECT COUNT(*) AS n
        FROM simulated_trade t
        JOIN recommendation_item i ON i.id = t.first_recommendation_id
        JOIN recommendation_batch b ON b.id = i.batch_id
        WHERE b.cohort='production' AND b.status='published'
          AND b.scan_date >= ? AND t.status='closed' AND t.gross_return IS NOT NULL
    """, (eval_start,)).fetchone()
    n = int(row["n"]) if row and row["n"] is not None else 0
    if n < MIN_SAMPLES:
        return False, f"正式主榜已结算模拟单 {n} < {MIN_SAMPLES}（样本/成本数据不足）"
    return True, ""


def _split_train_test(trades, boundary_date):
    """按日期分界切分训练/验证段，剔除持仓跨分界的样本（bullet 5）。

    trades: [{entry_date, exit_date, ret}]；boundary_date: 训练段 < 界 <= 验证段。
    跨分界（entry_date < 界 <= exit_date）的持仓既不完全属于训练也不完全属于验证，
    剔除以免同一笔在两段落被反复用来选优。以出场日归段（收益在出场时点实现）；
    无出场日的按入场日归段。返回 (train, test, dropped_cross)。
    """
    train, test, dropped = [], [], 0
    for t in trades:
        ed, xd = t.get("entry_date"), t.get("exit_date")
        if ed and xd and ed < boundary_date <= xd:
            dropped += 1
            continue
        anchor = xd or ed
        if anchor is None:
            continue
        (train if anchor < boundary_date else test).append(t)
    return train, test, dropped


def _settled_metric(trades) -> dict:
    """一组已结算有效交易收益的胜率/均值/样本数（bullet 6：相同执行配置口径）。"""
    rets = [float(t["ret"]) for t in trades if t.get("ret") is not None]
    n = len(rets)
    if n == 0:
        return {"n": 0, "win": None, "avg": None}
    return {"n": n,
            "win": round(sum(1 for r in rets if r > 0) / n * 100, 2),
            "avg": round(sum(rets) / n, 4)}


def _score_segments(trades):
    """对一组已结算交易按 TRAIN_RATIO 切分，返回 {all,train,test,boundary,dropped_cross}。

    训练段选优、验证段仅确认（bullet 5）。样本过少（<10）或日期跨度不足时返回 None，
    由上层记为「不通过：样本不足」。
    """
    if len(trades) < 10:
        return None
    dates = sorted({(t.get("exit_date") or t.get("entry_date")) for t in trades
                    if (t.get("exit_date") or t.get("entry_date"))})
    if len(dates) < 10:
        return None
    boundary = dates[int(len(dates) * TRAIN_RATIO)]
    train, test, dropped = _split_train_test(trades, boundary)
    return {"all": _settled_metric(trades), "train": _settled_metric(train),
            "test": _settled_metric(test), "boundary": boundary,
            "dropped_cross": dropped}


def _better_settled(cand, base) -> bool:
    """候选是否稳定优于基线（沿用现有改善幅度门槛，指标换成有效交易收益）。

    先训练段选优、再验证段确认（bullet 5）：train/test 双段胜率与均值都不劣于基线，
    且整体胜率至少 +1pp 或均值至少 +0.05pp（避免噪音级建议）。
    """
    if not cand or not base:
        return False
    for seg in ("train", "test"):
        c, b = cand.get(seg), base.get(seg)
        if not c or not b or c["n"] < 10 or b["n"] < 10:
            return False
        if c["win"] is None or b["win"] is None:
            return False
        if c["win"] < b["win"] or c["avg"] < b["avg"]:
            return False
    ca, ba = cand.get("all"), base.get("all")
    if not ca or not ba or ca["win"] is None or ba["win"] is None:
        return False
    return (ca["win"] - ba["win"] >= 1.0) or (ca["avg"] - ba["avg"] >= 0.05)


def _neighbor_values(ladder: list, current) -> list:
    """取阶梯上当前值相邻 ±2 档内的候选（当前值不在阶梯上时以最近档位为锚点）"""
    idx = min(range(len(ladder)), key=lambda i: abs(ladder[i] - current))
    lo, hi = max(0, idx - 2), min(len(ladder), idx + 3)
    return [v for v in ladder[lo:hi] if v != current]


def run_weekly_tuning() -> dict:
    """周五盘后参数寻优（方案 E3）：单参数扰动，产出最多 1 条待采纳建议。

    调参依据 = 冻结配置管道下、相同执行配置的有效交易收益（bullet 2/6）；**不再**用
    「每日融合分 Top8 + 次日开收(OC/CC)收益」代理。不具备完整执行回放条件时仅输出
    诊断、不产出可采纳建议（bullet 8）。保留单参数扰动/样本门槛/冷却/人工采纳（bullet 4）。
    """
    t0 = time.time()
    today = date.today().isoformat()
    baseline_hash = compute_params_baseline_hash()
    eval_start = (date.today() - timedelta(days=TUNE_WINDOW_DAYS)).isoformat()

    # 守护①：正式样本量（近 DIAG_DAYS 天正式短线已结算推荐）+ 版本 + 回放条件
    with get_conn() as conn:
        sample_rows = conn.execute(f"""
            SELECT exit_reason, exit_date, exit_return FROM recommend_outcome
            WHERE scan_date >= date('now', ?) AND horizon = 'short'
              AND entry_price > 0 AND {LEGACY_PRODUCTION_FILTER}
        """, (f"-{DIAG_DAYS} days",)).fetchall()
        sample = sum(settled_return(r) is not None for r in sample_rows)
        param_version = current_param_version(conn)
        available, why = _frozen_pipeline_available(conn, eval_start)

    base_meta = {"baseline_hash": baseline_hash, "param_version": param_version,
                 "sample": sample}
    if sample < MIN_SAMPLES:
        payload = {**base_meta, "diagnosis_only": True,
                   "skipped": f"近{DIAG_DAYS}天正式短线样本 {sample} < {MIN_SAMPLES}，仅诊断不调参"}
        _save_report("tuning", payload)
        return payload

    # 当前参数
    cur = {
        "sig_threshold": float(get_param("sig_threshold")),
        "trend_gate_ma": int(get_param("trend_gate_ma")),
        "trend_gate_slope_lookback": int(get_param("trend_gate_slope_lookback")),
    }

    # 守护②：完整执行回放条件（bullet 2/8）。当前 legacy/shadow/v2 均不满足 ⇒ 仅诊断，
    #   绝不退回 OC/CC 代理产出可采纳建议。
    if not available:
        payload = {**base_meta, "diagnosis_only": True, "params": cur,
                   "skipped": f"不具备完整执行回放条件：{why}；仅输出诊断，不产出可采纳建议"}
        _save_report("tuning", payload)
        return payload

    return _tune_via_frozen_pipeline(cur, base_meta, eval_start, today, t0)


def _baseline_settled_trades(conn, eval_start: str, list_key: str) -> list:
    """正式主榜已发布批次、已结算模拟单的有效交易收益（bullet 2 基线口径）。

    只取 production cohort、published 正式批次（experiment_id=''）、closed 且
    gross_return 非空的模拟单；net_return 缺失时回退 gross_return（成本模型见方案 E2，
    缺成本数据时仅毛收益，是否可采纳由 _frozen_pipeline_available 与样本门槛把关）。
    """
    rows = conn.execute("""
        SELECT t.exec_entry_date AS entry_date, t.exec_exit_date AS exit_date,
               COALESCE(t.net_return, t.gross_return) AS ret
        FROM simulated_trade t
        JOIN recommendation_item i ON i.id = t.first_recommendation_id
        JOIN recommendation_batch b ON b.id = i.batch_id
        WHERE b.cohort='production' AND b.status='published' AND b.list_key=?
          AND b.experiment_id='' AND b.scan_date >= ?
          AND t.status='closed' AND t.gross_return IS NOT NULL
        ORDER BY t.exec_exit_date
    """, (list_key, eval_start)).fetchall()
    return [{"entry_date": r["entry_date"], "exit_date": r["exit_date"], "ret": r["ret"]}
            for r in rows]


def _counterfactual_settled_trades(param_key, value, eval_start, as_of, list_key) -> list:
    """反事实回放（bullet 2）：在 param_key=value 覆盖下重跑「信号产生→推荐选择→
    模拟执行」，返回该正式线已结算有效交易收益 [{entry_date, exit_date, ret}]。

    - freeze_effective_parameters 取当前有效参数并覆盖单个候选值，replay_signal_dates
      以 run_type='replay' 逐交易日冻结重放；
    - 每个完成运行 select_and_publish 进 **本候选专属隔离实验编号** 的 comparison 批次
      （绝不占用正式名额、不入正式门槛、不与真实发布单或其它候选混算）；
    - advance_simulated_trades 按运行冻结执行配置推进成交与出场；
    - 读取该实验编号下的已结算模拟单收益。
    ⚠ 仅在 _frozen_pipeline_available() 为真（v2 已上线并累积正式发布批次）时调用；
      当前不可达，将在 v2 闭环验收随旁路一并验证。反事实运行/批次以实验编号隔离、只增
      不删（signal_run 受触发器保护不可删），属审计留痕。
    """
    from core.signal_runtime import replay_signal_dates
    from core import recommendation_service as svc
    from core.simulated_trading import advance_simulated_trades
    from config.strategy_params import freeze_effective_parameters

    exp_id = f"{OPTIMIZER_TUNING_EXPERIMENT_ID}:{param_key}={value}"
    with get_conn(readonly=True) as conn:
        parameters = freeze_effective_parameters(conn)
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM daily_price "
            "WHERE trade_date>=? AND trade_date<=? ORDER BY trade_date",
            (eval_start, as_of))]
    parameters["effective"][param_key] = value
    for r in replay_signal_dates(dates, source="stock_signal", parameters=parameters):
        run_id = r.get("run_id")
        if run_id and r.get("status") == "complete":
            try:
                svc.select_and_publish(run_id, list_key, experiment_id=exp_id,
                                       comparison=True)
            except Exception:
                pass  # 单榜/单日失败不阻断其余回放
    advance_simulated_trades(as_of=as_of)
    with get_conn(readonly=True) as conn:
        rows = conn.execute("""
            SELECT exec_entry_date AS entry_date, exec_exit_date AS exit_date,
                   COALESCE(net_return, gross_return) AS ret
            FROM simulated_trade
            WHERE experiment_id=? AND list_key=? AND status='closed'
              AND gross_return IS NOT NULL
            ORDER BY exec_exit_date
        """, (exp_id, list_key)).fetchall()
    return [{"entry_date": r["entry_date"], "exit_date": r["exit_date"], "ret": r["ret"]}
            for r in rows]


def _tune_via_frozen_pipeline(cur, base_meta, eval_start, today, t0) -> dict:
    """具备完整执行回放条件时的正式寻优（bullet 2/3/5/6）。

    仅对「作用于正式主榜」的可调参数做单参数扰动；候选在训练段选优、验证段确认，剔除
    持仓跨分界样本；指标为相同执行配置的有效交易收益；产出最多 1 条建议，并冻结基线
    参数哈希/版本、目标正式线、样本数供采纳前校验与追溯。
    """
    as_of = date.today().isoformat()
    lines = {_param_formal_line(k) for k in _PARAM_LADDERS}
    lines.discard(None)
    if not lines:
        # bullet 3：可调参数仅作用于停用/观察策略 ⇒ 不适用于正式推荐
        payload = {**base_meta, "diagnosis_only": True, "params": cur,
                   "skipped": "当前可调参数仅作用于停用或观察策略，不适用于正式推荐"}
        _save_report("tuning", payload)
        return payload
    list_key = sorted(lines)[0]

    with get_conn(readonly=True) as conn:
        base_trades = _baseline_settled_trades(conn, eval_start, list_key)
    base = _score_segments(base_trades)
    if base is None:
        payload = {**base_meta, "diagnosis_only": True, "params": cur, "list_key": list_key,
                   "skipped": f"正式线 {list_key} 已结算有效交易样本不足（<10），仅诊断不调参"}
        _save_report("tuning", payload)
        return payload

    with get_conn() as conn:
        cooldown = {k: _in_cooldown(conn, k) for k in _PARAM_LADDERS}

    trials, best = [], None
    for key, ladder in _PARAM_LADDERS.items():
        if _param_formal_line(key) != list_key:
            trials.append({"param_key": key, "value": None,
                           "rejected": "不适用于正式推荐（仅作用于观察/停用线）"})
            continue
        if cooldown.get(key):
            trials.append({"param_key": key, "value": None, "rejected": "冷却期内"})
            continue
        for val in _neighbor_values(ladder, cur[key]):
            cand = _score_segments(
                _counterfactual_settled_trades(key, val, eval_start, as_of, list_key))
            if cand is None:
                trials.append({"param_key": key, "value": val,
                               "rejected": "候选样本不足/日期跨度不够"})
                continue
            passed = _better_settled(cand, base)
            trials.append({"param_key": key, "value": val, "stats": cand["all"],
                           "train": cand["train"], "test": cand["test"],
                           "dropped_cross": cand["dropped_cross"],
                           "rejected": None if passed else "未稳定优于基线"})
            if passed and (best is None or cand["all"]["win"] > best[2]["all"]["win"]):
                best = (key, val, cand)

    payload = {
        **base_meta, "window_days": TUNE_WINDOW_DAYS, "list_key": list_key,
        "baseline": {"params": cur, "stats": base},
        "trials": trials,
        "cooldown": [k for k, v in cooldown.items() if v],
        "elapsed_s": round(time.time() - t0, 1),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    if best:
        key, val, stats = best
        label = TUNABLE_PARAMS[key]["label"]
        reason = (
            f"冻结管道回放近{TUNE_WINDOW_DAYS}天（{list_key} 正式线·相同执行配置有效交易收益）："
            f"{label} {cur[key]} → {val} 后 胜率 {base['all']['win']}% → {stats['all']['win']}%、"
            f"均值 {base['all']['avg']}% → {stats['all']['avg']}%，train 选优/test 确认均不劣于现值")
        with get_conn() as conn:
            # 同参数旧的 pending 建议标记过期，保持每参数最多一条待办
            conn.execute("""
                UPDATE param_suggestion SET status = 'expired',
                       decided_at = datetime('now','localtime')
                WHERE param_key = ? AND status = 'pending'
            """, (key,))
            conn.execute("""
                INSERT INTO param_suggestion
                    (created_date, param_key, current_value, suggest_value,
                     reason, metrics_json, baseline_hash, baseline_version, list_key, sample_n)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (today, key, str(cur[key]), str(val), reason,
                  json.dumps({"baseline": base, "candidate": stats}, ensure_ascii=False),
                  base_meta["baseline_hash"], base_meta["param_version"], list_key,
                  stats["all"]["n"]))
        payload["suggestion"] = {"param_key": key, "label": label, "list_key": list_key,
                                 "current": cur[key], "suggest": val, "reason": reason}
    else:
        payload["suggestion"] = None

    _save_report("tuning", payload)
    return payload


# ─────────────────────────────────────────────
# 3. 建议采纳 / 忽略 / 回滚
# ─────────────────────────────────────────────

def apply_suggestion(suggestion_id: int) -> dict:
    """采纳建议：校验基线未变 → 写覆盖层 + 审计日志（生成新参数版本），标记已采纳。

    方案 E3 bullet 7：采纳前校验建议生成时冻结的基线参数哈希——若当前有效参数已变化
    （期间采纳/回滚过其它参数），说明该建议是在过期基线上得出的，标记 expired 并拒绝，
    要求重新寻优。一次采纳生成一个新参数版本；旧推荐与旧模拟单按其生成时的版本冻结、
    不回写（本函数只写 strategy_param_override 覆盖层 + param_tune_log 审计 + 建议状态，
    绝不触碰 recommend_outcome / simulated_trade / recommendation_* 等历史记录）。
    """
    baseline_now = compute_params_baseline_hash()
    # 第一段事务：读取建议 + 基线校验；过期则标记 expired（随 with 正常退出提交）
    with get_conn() as conn:
        sug = conn.execute(
            "SELECT * FROM param_suggestion WHERE id = ? AND status = 'pending'",
            (suggestion_id,)).fetchone()
        if not sug:
            raise ValueError(f"建议 {suggestion_id} 不存在或已处理")
        # 基线校验：建议生成时冻结的基线哈希与当前不一致 ⇒ 建议过期（旧记录不回写）
        stored_hash = sug["baseline_hash"] if "baseline_hash" in sug.keys() else None
        stale = bool(stored_hash) and stored_hash != baseline_now
        if stale:
            conn.execute("""
                UPDATE param_suggestion SET status = 'expired',
                       decided_at = datetime('now','localtime')
                WHERE id = ?
            """, (suggestion_id,))
    # 过期标记已提交，此处在事务外抛错（避免 get_conn 异常回滚丢失 expired 状态）
    if stale:
        raise ValueError("基线参数已变化，建议过期，请重新寻优后再采纳")

    key = sug["param_key"]
    old_val = str(get_param(key))
    # 第二段事务：写覆盖层 + 审计（生成新参数版本）+ 标记已采纳
    with get_conn() as conn:
        new_version = current_param_version(conn) + 1
        conn.execute("""
            INSERT OR REPLACE INTO strategy_param_override
                (param_key, value, reason, source, updated_at)
            VALUES (?, ?, ?, 'auto', datetime('now','localtime'))
        """, (key, sug["suggest_value"], sug["reason"]))
        conn.execute("""
            INSERT INTO param_tune_log
                (param_key, old_value, new_value, action, reason, metrics_json, new_version)
            VALUES (?, ?, ?, 'apply', ?, ?, ?)
        """, (key, old_val, sug["suggest_value"], sug["reason"], sug["metrics_json"],
              new_version))
        conn.execute("""
            UPDATE param_suggestion SET status = 'applied',
                   decided_at = datetime('now','localtime')
            WHERE id = ?
        """, (suggestion_id,))
    invalidate_param_cache()
    return {"param_key": key, "old_value": old_val,
            "new_value": sug["suggest_value"], "param_version": new_version}


def dismiss_suggestion(suggestion_id: int) -> dict:
    """忽略建议（不生效，仅标记）。"""
    with get_conn() as conn:
        cur = conn.execute("""
            UPDATE param_suggestion SET status = 'dismissed',
                   decided_at = datetime('now','localtime')
            WHERE id = ? AND status = 'pending'
        """, (suggestion_id,))
        if cur.rowcount == 0:
            raise ValueError(f"建议 {suggestion_id} 不存在或已处理")
    return {"id": suggestion_id, "status": "dismissed"}


def rollback_param(param_key: str) -> dict:
    """回滚参数到最近一次 apply 之前的值。"""
    with get_conn() as conn:
        last = conn.execute("""
            SELECT * FROM param_tune_log
            WHERE param_key = ? AND action = 'apply'
            ORDER BY id DESC LIMIT 1
        """, (param_key,)).fetchone()
        if not last:
            raise ValueError(f"参数 {param_key} 没有可回滚的调整记录")
        old_val = last["old_value"]
        spec = TUNABLE_PARAMS.get(param_key, {})
        default_val = str(spec.get("default", ""))
        if old_val is None or old_val == default_val:
            # 调整前就是代码默认值 → 直接删除覆盖行，回归默认
            conn.execute("DELETE FROM strategy_param_override WHERE param_key = ?",
                         (param_key,))
        else:
            conn.execute("""
                INSERT OR REPLACE INTO strategy_param_override
                    (param_key, value, reason, source, updated_at)
                VALUES (?, ?, '回滚恢复', 'manual', datetime('now','localtime'))
            """, (param_key, old_val))
        new_version = current_param_version(conn) + 1
        conn.execute("""
            INSERT INTO param_tune_log
                (param_key, old_value, new_value, action, reason, new_version)
            VALUES (?, ?, ?, 'rollback', '人工回滚', ?)
        """, (param_key, last["new_value"], old_val or default_val, new_version))
    invalidate_param_cache()
    return {"param_key": param_key, "restored_value": old_val or default_val,
            "param_version": new_version}


# ─────────────────────────────────────────────
# 4. 状态查询（供 API）
# ─────────────────────────────────────────────

def get_latest_state() -> dict:
    """最新诊断/寻优报告 + 待采纳建议 + 参数状态 + 最近调参历史。"""
    with get_conn() as conn:
        def _latest(rtype):
            row = conn.execute("""
                SELECT report_date, payload_json FROM optimizer_report
                WHERE report_type = ? ORDER BY report_date DESC LIMIT 1
            """, (rtype,)).fetchone()
            if not row:
                return None
            payload = json.loads(row["payload_json"])
            payload["report_date"] = row["report_date"]
            return payload

        diagnosis = _latest("diagnosis")
        tuning = _latest("tuning")
        suggestions = [dict(r) for r in conn.execute("""
            SELECT id, created_date, param_key, current_value, suggest_value,
                   reason, metrics_json, baseline_hash, baseline_version,
                   list_key, sample_n
            FROM param_suggestion WHERE status = 'pending'
            ORDER BY created_date DESC
        """).fetchall()]
        history = [dict(r) for r in conn.execute("""
            SELECT param_key, old_value, new_value, action, reason, created_at,
                   new_version
            FROM param_tune_log ORDER BY id DESC LIMIT 10
        """).fetchall()]
        param_version = current_param_version(conn)

    baseline_now = compute_params_baseline_hash()
    for s in suggestions:
        try:
            s["metrics"] = json.loads(s.pop("metrics_json") or "{}")
        except Exception:
            s["metrics"] = {}
        # 采纳前基线校验预标记（bullet 7）：基线已变 ⇒ 建议过期，前端提示重新寻优
        stored = s.get("baseline_hash")
        s["stale"] = bool(stored) and stored != baseline_now

    return {
        "diagnosis": diagnosis,
        "tuning": tuning,
        "suggestions": suggestions,
        "params": get_tunable_params_state(),
        "history": history,
        "param_version": param_version,
        "baseline_hash": baseline_now,
    }


# ─────────────────────────────────────────────
# 5. 同步后置钩子入口
# ─────────────────────────────────────────────

def run_post_sync() -> dict:
    """recalc_all_scores 末尾调用：每日诊断，周五追加寻优。best-effort。"""
    result = {"diagnosis": None, "tuning": None}
    diag = run_daily_diagnosis()
    result["diagnosis"] = {"findings": diag.get("findings", []),
                           "short": diag.get("summary", {}).get("short")}
    if date.today().weekday() == 4:  # 周五盘后寻优
        tune = run_weekly_tuning()
        result["tuning"] = {"suggestion": tune.get("suggestion"),
                            "skipped": tune.get("skipped")}
    return result


if __name__ == "__main__":
    print(json.dumps(run_post_sync(), ensure_ascii=False, indent=2))
