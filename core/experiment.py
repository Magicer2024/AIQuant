"""experiment.py —— 筹码影子与参数实验（方案 C3）。

不变式：
  - 基线与影子使用同一 signal_run、同一候选资格与执行配置，唯一允许差异是「登记的排序参数」。
  - 实验编号及完整实验配置（sort_variant）冻结在批次 policy 中；基线为当天真正发布的
    published 批次，不是以后重算出来的名单。
  - 首批从新版上线后开始前向积累；旧 recommend_outcome_shadow 表仅作 legacy_snapshot，
    本模块不读取它、不追认其前向实验身份。
  - 若整个候选池均无筹码数据，记录跳过原因，不生成假对照组。
  - 报告仅描述对照，不因短期均值变好自动晋升正式策略。
"""
import json

from config.personal_config import is_main_board
from core.db import get_conn
from core import recommendation_service as svc
from core.repository import outcome_repo

# 筹码影子实验编号（冻结在批次 experiment_id + policy.sort_variant）。
CHIP_SHADOW_EXPERIMENT_ID = "chip_shadow_v1"
CHIP_SHADOW_VARIANT = {"chip_first": True}


def _list_spec(list_key):
    if list_key not in svc.LIST_SPECS:
        raise ValueError(f"未支持的榜单键：{list_key}")
    return svc.LIST_SPECS[list_key]


def _chip_coverage(run_id, list_key):
    """统计该运行中、该榜单候选池的筹码数据覆盖率。

    返回 (with_chip, total)：total 为符合榜单周期/策略白名单/主板的候选信号数，
    with_chip 为其中 chip_conc 非空的个数。用于「整池无筹码则跳过」的守卫。
    """
    _cohort, horizon, allowed_keys = _list_spec(list_key)
    with get_conn(readonly=True) as conn:
        events = conn.execute(
            "SELECT code, horizon, strategy_key, payload_json FROM signal_event WHERE run_id=?",
            (run_id,)).fetchall()
    total = with_chip = 0
    for e in events:
        if e["horizon"] != horizon or e["strategy_key"] not in allowed_keys:
            continue
        if not is_main_board(e["code"]):
            continue
        total += 1
        payload = json.loads(e["payload_json"])
        if payload.get("chip_conc") is not None:
            with_chip += 1
    return with_chip, total


def select_chip_shadow(run_id, list_key="short", experiment_id=CHIP_SHADOW_EXPERIMENT_ID):
    """从同一运行选出筹码影子对照批次（comparison 状态，不冒充正式发布）。

    与基线唯一差异是登记的排序变体 chip_first=True；资格/名额/执行配置完全一致。
    受运行冻结的 short_chip_shadow_enabled 开关控制（0=停记）；整池无筹码数据时跳过
    并记录原因，不生成假对照组。
    """
    run, _events = svc._load_run(run_id)
    params = json.loads(run["params_json"])
    enabled = int((params.get("effective") or {}).get("short_chip_shadow_enabled", 1) or 0)
    if not enabled:
        return {"skipped": "shadow_disabled", "experiment_id": experiment_id, "list_key": list_key}
    with_chip, total = _chip_coverage(run_id, list_key)
    if total == 0:
        return {"skipped": "no_candidates", "with_chip": 0, "total": 0,
                "experiment_id": experiment_id, "list_key": list_key}
    if with_chip == 0:
        return {"skipped": "no_chip_data", "with_chip": 0, "total": total,
                "experiment_id": experiment_id, "list_key": list_key}
    batch_id = svc.select_and_publish(
        run_id, list_key, experiment_id=experiment_id,
        sort_variant=CHIP_SHADOW_VARIANT, comparison=True)
    return {"batch_id": batch_id, "with_chip": with_chip, "total": total,
            "experiment_id": experiment_id, "list_key": list_key}


def _batches_by_experiment(list_key, experiment_id, since=None, scan_date=None):
    """取实验（comparison）批次，按 scan_date 过滤。"""
    sql = ("SELECT id,run_id,scan_date,list_key,cohort,experiment_id,status,policy_json "
           "FROM recommendation_batch WHERE list_key=? AND experiment_id=? "
           "AND status IN ('comparison','published')")
    params = [list_key, experiment_id]
    if scan_date:
        sql += " AND scan_date=?"
        params.append(scan_date)
    if since:
        sql += " AND scan_date>=?"
        params.append(since)
    sql += " ORDER BY scan_date"
    with get_conn(readonly=True) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _batch_codes(batch_id):
    with get_conn(readonly=True) as conn:
        rows = conn.execute("SELECT code FROM recommendation_item WHERE batch_id=? ORDER BY rank",
                            (batch_id,)).fetchall()
    return [r["code"] for r in rows]


def chip_shadow_report(*, since=None, scan_date=None, list_key="short",
                       experiment_id=CHIP_SHADOW_EXPERIMENT_ID):
    """对照报告：重叠率、有效交易数、未成交率、收益分布、数据缺失。

    基线 = 当天真正发布的 published 批次（experiment_id=''）；实验 = comparison 批次。
    两者来自同一 run 与同一资格，唯一差异是排序参数，故重叠率直接反映排序换票程度。
    """
    exp_batches = _batches_by_experiment(list_key, experiment_id, since=since, scan_date=scan_date)
    per_date = []
    overlaps = []
    for eb in exp_batches:
        base = svc.get_published_batch(eb["scan_date"], list_key, cohort="production", experiment_id="")
        exp_codes = _batch_codes(eb["id"])
        base_codes = _batch_codes(base["id"]) if base else []
        inter = len(set(exp_codes) & set(base_codes))
        overlap = (inter / len(exp_codes)) if exp_codes else None
        if overlap is not None:
            overlaps.append(overlap)
        per_date.append({
            "scan_date": eb["scan_date"], "experiment_batch": eb["id"],
            "baseline_batch": base["id"] if base else None,
            "exp_codes": exp_codes, "base_codes": base_codes,
            "overlap": overlap, "overlap_n": inter,
        })

    # 交易成绩：基线（production）与实验（shadow）分别统计，不混算。
    base_trades = outcome_repo.list_trades(cohort="production", list_key=list_key, experiment_id="")
    exp_trades = outcome_repo.list_trades(cohort="shadow", list_key=list_key, experiment_id=experiment_id)
    base_stats = outcome_repo.settlement_stats(base_trades)
    exp_stats = outcome_repo.settlement_stats(exp_trades)

    def _unfilled_rate(stats):
        denom = stats["settled"] + stats["holding"] + stats["unfilled"] + stats["expired"]
        return (stats["expired"] + stats["unfilled"]) / denom if denom else None

    return {
        "experiment_id": experiment_id, "list_key": list_key,
        "dates": per_date, "n_dates": len(per_date),
        "mean_overlap": (sum(overlaps) / len(overlaps)) if overlaps else None,
        "full_overlap_dates": sum(1 for o in overlaps if o == 1.0),
        "baseline": {**base_stats, "unfilled_rate": _unfilled_rate(base_stats)},
        "experiment": {**exp_stats, "unfilled_rate": _unfilled_rate(exp_stats)},
        "note": "对照仅描述，不因短期均值变好自动晋升正式策略；旧影子表不追认为前向实验。",
    }
