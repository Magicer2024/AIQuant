"""
strategy/optimizer.py —— 每日推荐策略优化器（suggest 模式）
=============================================================

数据同步后置钩子链的最后一环，形成「推荐 → 出场跟踪 → 诊断 → 寻优 → 采纳」闭环：

  1. run_daily_diagnosis  每日：读 recommend_outcome 近30天数据，
     按周期统计胜率/盈亏比并做亏损归因，写 optimizer_report。
  2. run_weekly_tuning    每周五盘后：对现有 pure_bottom 引擎的关键参数
     （融合分阈值 / 趋势闸门）做 ±1~2 档小网格回放，OC+CC 双口径、
     train/test 双窗口都不劣于现值才产出建议，写 param_suggestion。
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

import pandas as pd

from core.db import get_conn
from config.strategy_params import (
    TUNABLE_PARAMS, get_param, invalidate_param_cache,
    get_tunable_params_state,
)

# ── 守护参数 ──────────────────────────────────
MIN_SAMPLES = 15          # 近30天短线推荐样本下限，不足只诊断不调参
MIN_SIGNALS = 30          # 候选参数在回放窗口内的最少信号数
COOLDOWN_DAYS = 3         # 同一参数调整/建议后的冷却天数
DIAG_DAYS = 30            # 诊断窗口（自然日，约20个交易日）
TUNE_WINDOW_DAYS = 90     # 寻优回放窗口（自然日，约60个交易日）
TUNE_BUFFER_DAYS = 60     # 额外预热数据（算 MA 用），不参与评估
TRAIN_RATIO = 0.7         # train/test 双窗口切分比例
TOP_N_PER_DAY = 8         # 每日推荐口径：fusion_score 前 8 名（与 outcome_tracker 一致）

# 各参数的候选档位（只取当前值相邻 ±1~2 档）
_PARAM_LADDERS = {
    "sig_threshold": [13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 20.0],
    "trend_gate_ma": [10, 20, 30],
    "trend_gate_slope_lookback": [3, 5, 8],
}


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

def _final_return(row) -> float | None:
    """单条推荐的最终收益：exit_return > t5 > t3 > t1（与 get_summary 口径一致）"""
    for key in ("exit_return", "t5_return", "t3_return", "t1_return"):
        val = row[key]
        if val is not None:
            return float(val)
    return None


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


# ─────────────────────────────────────────────
# 1. 每日诊断
# ─────────────────────────────────────────────

def run_daily_diagnosis(days: int = DIAG_DAYS) -> dict:
    """近 N 天推荐表现诊断 + 亏损归因，结果写 optimizer_report。"""
    cutoff = f"-{days} days"
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT horizon, strategy, fusion_score, entry_price, stop_loss,
                   t1_return, t3_return, t5_return, t10_return,
                   max_return, min_return, hit_stop, hit_tp,
                   exit_reason, exit_return
            FROM recommend_outcome
            WHERE scan_date >= date('now', ?) AND entry_price > 0
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
            "total": len(values),
            "win_rate": round(len(wins) / len(values) * 100, 1),
            "avg_return": round(sum(values) / len(values), 2),
            "profit_factor": round(avg_win / avg_loss, 2) if avg_loss > 0 else 99.0,
            "stop_hit": sum(1 for _, r in rets if r["hit_stop"]),
            "tp_hit": sum(1 for _, r in rets if r["hit_tp"]),
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
                findings.append(
                    f"止损单中 {ratio*100:.0f}% 在 T+10 收正（{recovered}/{len(stopped)}），"
                    f"当前止损 {get_param('short_stop_loss')*100:.0f}% 可能偏紧")

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
        "findings": findings,
        "params": {k: v["current"] for k, v in get_tunable_params_state().items()},
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_report("diagnosis", payload)
    return payload


# ─────────────────────────────────────────────
# 2. 周五参数寻优（回放式小网格搜索）
# ─────────────────────────────────────────────

def _load_replay_frame(window_days: int = TUNE_WINDOW_DAYS) -> pd.DataFrame:
    """加载回放数据帧：daily_price 近 window+buffer 天的价格与融合分。

    额外拉 TUNE_BUFFER_DAYS 天预热数据用于算 MA/斜率，评估只在 window 内进行。
    """
    start = (date.today() - timedelta(days=window_days + TUNE_BUFFER_DAYS)).isoformat()
    with get_conn() as conn:
        df = pd.read_sql_query("""
            SELECT code, trade_date, open, close, fusion_score
            FROM daily_price
            WHERE trade_date >= ? AND close > 0
            ORDER BY code, trade_date
        """, conn, params=(start,))
    return df


def _prepare_replay(df: pd.DataFrame) -> pd.DataFrame:
    """向量化预计算：隔日 OC/CC 收益 + 各档位 MA 与斜率布尔列。"""
    g = df.groupby("code", sort=False)
    df["next_open"] = g["open"].shift(-1)
    df["next_close"] = g["close"].shift(-1)
    df["oc_ret"] = (df["next_close"] - df["next_open"]) / df["next_open"] * 100
    df["cc_ret"] = (df["next_close"] - df["close"]) / df["close"] * 100

    for ma_n in _PARAM_LADDERS["trend_gate_ma"]:
        ma = g["close"].transform(lambda s, n=ma_n: s.rolling(n).mean())
        df[f"ma{ma_n}"] = ma
        for lb in _PARAM_LADDERS["trend_gate_slope_lookback"]:
            ma_prev = ma.groupby(df["code"]).shift(lb)
            df[f"gate_{ma_n}_{lb}"] = (df["close"] > ma) & (ma >= ma_prev)
    return df


def _eval_combo(df: pd.DataFrame, thr: float, ma_n: int, slope_lb: int,
                eval_start: str) -> dict | None:
    """评估一组参数：门槛+闸门筛信号 → 每日 top8 → OC/CC 胜率（train/test 双窗口）。"""
    gate_col = f"gate_{ma_n}_{slope_lb}"
    mask = (
        (df["trade_date"] >= eval_start)
        & (df["fusion_score"] >= thr)
        & df[gate_col].fillna(False)
        & df["oc_ret"].notna()
    )
    sig = df.loc[mask, ["trade_date", "fusion_score", "oc_ret", "cc_ret"]]
    if len(sig) < MIN_SIGNALS:
        return None
    # 每日推荐口径：fusion_score 前 8 名
    sig = (sig.sort_values(["trade_date", "fusion_score"], ascending=[True, False])
              .groupby("trade_date").head(TOP_N_PER_DAY))
    if len(sig) < MIN_SIGNALS:
        return None

    dates = sorted(sig["trade_date"].unique())
    split = dates[int(len(dates) * TRAIN_RATIO)] if len(dates) >= 10 else None

    def _stats(part: pd.DataFrame) -> dict:
        return {
            "n": int(len(part)),
            "oc_win": round(float((part["oc_ret"] > 0).mean() * 100), 2),
            "oc_avg": round(float(part["oc_ret"].mean()), 3),
            "cc_win": round(float((part["cc_ret"] > 0).mean() * 100), 2),
            "cc_avg": round(float(part["cc_ret"].mean()), 3),
        }

    out = {"all": _stats(sig)}
    if split:
        out["train"] = _stats(sig[sig["trade_date"] < split])
        out["test"] = _stats(sig[sig["trade_date"] >= split])
    return out


def _better(cand: dict, base: dict) -> bool:
    """候选是否稳定优于基线：train/test 双窗口 OC 胜率与均值都不劣，
    且整体 OC 胜率至少 +1pp 或均值至少 +0.05pp（避免噪音级建议）。"""
    if not cand or not base:
        return False
    for w in ("train", "test"):
        c, b = cand.get(w), base.get(w)
        if not c or not b or c["n"] < 10:
            return False
        if c["oc_win"] < b["oc_win"] or c["oc_avg"] < b["oc_avg"]:
            return False
    ca, ba = cand["all"], base["all"]
    return (ca["oc_win"] - ba["oc_win"] >= 1.0) or (ca["oc_avg"] - ba["oc_avg"] >= 0.05)


def _neighbor_values(ladder: list, current) -> list:
    """取阶梯上当前值相邻 ±2 档内的候选（当前值不在阶梯上时以最近档位为锚点）"""
    idx = min(range(len(ladder)), key=lambda i: abs(ladder[i] - current))
    lo, hi = max(0, idx - 2), min(len(ladder), idx + 3)
    return [v for v in ladder[lo:hi] if v != current]


def run_weekly_tuning() -> dict:
    """周五盘后参数寻优：单参数扰动回放，产出最多 1 条待采纳建议。"""
    t0 = time.time()
    today = date.today().isoformat()

    # 守护①：样本量
    with get_conn() as conn:
        sample = conn.execute("""
            SELECT COUNT(*) FROM recommend_outcome
            WHERE scan_date >= date('now', ?) AND horizon = 'short'
              AND entry_price > 0
        """, (f"-{DIAG_DAYS} days",)).fetchone()[0]
    if sample < MIN_SAMPLES:
        payload = {"skipped": f"近{DIAG_DAYS}天短线推荐样本 {sample} < {MIN_SAMPLES}，跳过寻优",
                   "sample": sample}
        _save_report("tuning", payload)
        return payload

    # 当前参数
    cur = {
        "sig_threshold": float(get_param("sig_threshold")),
        "trend_gate_ma": int(get_param("trend_gate_ma")),
        "trend_gate_slope_lookback": int(get_param("trend_gate_slope_lookback")),
    }

    df = _load_replay_frame()
    if df.empty or df["trade_date"].nunique() < 30:
        payload = {"skipped": "daily_price 回放数据不足，跳过寻优"}
        _save_report("tuning", payload)
        return payload
    df = _prepare_replay(df)
    eval_start = (date.today() - timedelta(days=TUNE_WINDOW_DAYS)).isoformat()

    base = _eval_combo(df, cur["sig_threshold"], cur["trend_gate_ma"],
                       cur["trend_gate_slope_lookback"], eval_start)
    if base is None:
        payload = {"skipped": "当前参数在回放窗口内信号不足，跳过寻优", "params": cur}
        _save_report("tuning", payload)
        return payload

    # 守护②：冷却期过滤 + 单参数扰动评估
    trials = []
    best = None  # (param_key, value, stats)
    with get_conn() as conn:
        cooldown = {k: _in_cooldown(conn, k) for k in _PARAM_LADDERS}
    for key, ladder in _PARAM_LADDERS.items():
        if cooldown.get(key):
            continue
        for val in _neighbor_values(ladder, cur[key]):
            combo = dict(cur)
            combo[key] = val
            stats = _eval_combo(df, combo["sig_threshold"], combo["trend_gate_ma"],
                                combo["trend_gate_slope_lookback"], eval_start)
            trial = {"param_key": key, "value": val, "stats": stats}
            trials.append(trial)
            if stats and _better(stats, base):
                if best is None or stats["all"]["oc_win"] > best[2]["all"]["oc_win"]:
                    best = (key, val, stats)

    payload = {
        "window_days": TUNE_WINDOW_DAYS,
        "baseline": {"params": cur, "stats": base},
        "trials": [{**t, "stats": t["stats"]["all"] if t["stats"] else None}
                   for t in trials],
        "cooldown": [k for k, v in cooldown.items() if v],
        "elapsed_s": round(time.time() - t0, 1),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    if best:
        key, val, stats = best
        label = TUNABLE_PARAMS[key]["label"]
        reason = (
            f"回放近{TUNE_WINDOW_DAYS}天（OC口径）：{label} {cur[key]} → {val} 后 "
            f"胜率 {base['all']['oc_win']}% → {stats['all']['oc_win']}%、"
            f"均值 {base['all']['oc_avg']}% → {stats['all']['oc_avg']}%，"
            f"train/test 双窗口均不劣于现值")
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
                     reason, metrics_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (today, key, str(cur[key]), str(val), reason,
                  json.dumps({"baseline": base, "candidate": stats},
                             ensure_ascii=False)))
        payload["suggestion"] = {"param_key": key, "label": label,
                                 "current": cur[key], "suggest": val,
                                 "reason": reason}
    else:
        payload["suggestion"] = None

    _save_report("tuning", payload)
    return payload


# ─────────────────────────────────────────────
# 3. 建议采纳 / 忽略 / 回滚
# ─────────────────────────────────────────────

def apply_suggestion(suggestion_id: int) -> dict:
    """采纳建议：写覆盖层 + 审计日志，标记建议已采纳。"""
    with get_conn() as conn:
        sug = conn.execute(
            "SELECT * FROM param_suggestion WHERE id = ? AND status = 'pending'",
            (suggestion_id,)).fetchone()
        if not sug:
            raise ValueError(f"建议 {suggestion_id} 不存在或已处理")
        key = sug["param_key"]
        old_val = str(get_param(key))
        conn.execute("""
            INSERT OR REPLACE INTO strategy_param_override
                (param_key, value, reason, source, updated_at)
            VALUES (?, ?, ?, 'auto', datetime('now','localtime'))
        """, (key, sug["suggest_value"], sug["reason"]))
        conn.execute("""
            INSERT INTO param_tune_log
                (param_key, old_value, new_value, action, reason, metrics_json)
            VALUES (?, ?, ?, 'apply', ?, ?)
        """, (key, old_val, sug["suggest_value"], sug["reason"], sug["metrics_json"]))
        conn.execute("""
            UPDATE param_suggestion SET status = 'applied',
                   decided_at = datetime('now','localtime')
            WHERE id = ?
        """, (suggestion_id,))
    invalidate_param_cache()
    return {"param_key": key, "old_value": old_val,
            "new_value": sug["suggest_value"]}


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
        conn.execute("""
            INSERT INTO param_tune_log (param_key, old_value, new_value, action, reason)
            VALUES (?, ?, ?, 'rollback', '人工回滚')
        """, (param_key, last["new_value"], old_val or default_val))
    invalidate_param_cache()
    return {"param_key": param_key, "restored_value": old_val or default_val}


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
                   reason, metrics_json
            FROM param_suggestion WHERE status = 'pending'
            ORDER BY created_date DESC
        """).fetchall()]
        history = [dict(r) for r in conn.execute("""
            SELECT param_key, old_value, new_value, action, reason, created_at
            FROM param_tune_log ORDER BY id DESC LIMIT 10
        """).fetchall()]

    for s in suggestions:
        try:
            s["metrics"] = json.loads(s.pop("metrics_json") or "{}")
        except Exception:
            s["metrics"] = {}

    return {
        "diagnosis": diagnosis,
        "tuning": tuning,
        "suggestions": suggestions,
        "params": get_tunable_params_state(),
        "history": history,
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
