"""recommendation_service.py —— 从冻结信号选择、冻结并发布推荐名单。

服务边界（与方案 C1 一致）：
    select_recommendations(run_id, list_key, policy) -> 草稿批次 ID
    publish_batch(batch_id)                          -> 已发布批次
    get_published_batch(date, list_key, cohort)      -> 冻结名单

核心不变式：
  - 只读该运行已冻结的 signal_event 与运行内冻结参数，不读当前行情、不用今日价格
    或市场状态重新筛掉过去已发布的赢家。
  - 观察线/影子线使用不同 list_key 与 cohort，绝不占用正式名额。
  - 同股同周期只保留一个主信号（沿用现有业务优先级），其余命中作为支持信息保留。
  - 排序并列时按 (code, strategy_key) 稳定打破，消除数据库返回顺序的不确定性。
  - 名额来自该日期冻结的市场状态；空榜也发布批次以便追溯。
"""
import json

from config.personal_config import is_main_board
from core.db import get_conn
from core.repository import recommendation_repo as repo

# 榜单键 → (cohort, horizon, 允许参与的稳定策略键集合)。
# production 严格排除观察线（强势突破/反转首日/缩量回踩），与今日推荐/复盘入库同口径。
LIST_SPECS = {
    "short": ("production", "short", {"next_day_momentum", "short_composite", "oversold_rebound"}),
    "mid": ("production", "mid", {"mid_composite"}),
    "long": ("production", "long", {"long_trend"}),
    "deep": ("production", "deep", {"stock_deep"}),
    "observe_first_reversal": ("observation", "short", {"first_reversal"}),
    "observe_surge": ("observation", "short", {"surge_breakout"}),
    "observe_pullback": ("observation", "short", {"pullback_dip"}),
}

# 主信号优先级：同股同周期多命中时选主信号，其余转支持信息。
# 沿用现有业务优先级（隔日动量优先占短线名额）。
_PRIMARY_PRIORITY = {
    "next_day_momentum": 0, "pullback_dip": 1, "short_composite": 2,
    "oversold_rebound": 2, "surge_breakout": 3, "first_reversal": 3,
    "mid_composite": 0, "long_trend": 0, "stock_deep": 0,
}

# 观察线缺省名额（正式线名额由 RECO_REGIME_CAP + short_top_n/limit 决定）。
_OBSERVE_DEFAULT_CAP = 8


def _load_run(run_id):
    with get_conn(readonly=True) as conn:
        run = conn.execute("SELECT * FROM signal_run WHERE id=?", (run_id,)).fetchone()
        if not run:
            raise ValueError("运行不存在")
        events = conn.execute(
            "SELECT * FROM signal_event WHERE run_id=?", (run_id,)).fetchall()
    return dict(run), [dict(e) for e in events]


def _is_tradable_name(name):
    name = str(name or "")
    return "ST" not in name and "退" not in name


def _short_sort_key(sig, chip_first=False):
    """复现 _short_order_keys：策略名额档 → [筹码集中度] → 扩展度升序 → 融合分降序。

    chip_first=False（基线，short_chip_sort_prioritize=0）：不含筹码键，
    与线上 _short_order_keys 关闭分支逐位对齐（tier → ext↑ → fusion↓ → code）。
    chip_first=True（筹码变体）：chip_conc 升序插在策略档之后、扩展度之前
    （缺失 COALESCE 到 9.9 排最后），与影子组共用同一构造，无口径漂移。
    """
    strategy_key = sig["strategy_key"]
    tier = 0 if strategy_key == "next_day_momentum" else (1 if strategy_key == "pullback_dip" else 2)
    ext = sig["payload"].get("pct_above_ma20")
    ext = 0.0 if ext is None else float(ext)
    fusion = sig["payload"].get("fusion_score") or sig.get("raw_score") or 0.0
    if chip_first:
        chip = sig["payload"].get("chip_conc")
        chip = 9.9 if chip is None else float(chip)
        return (tier, chip, ext, -float(fusion), sig["code"], strategy_key)
    return (tier, ext, -float(fusion), sig["code"], strategy_key)


def _long_sort_key(sig):
    rank = sig["payload"].get("long_rank_key")
    rank = 9.9 if rank is None else float(rank)
    fusion = sig["payload"].get("fusion_score") or 0.0
    return (rank, -float(fusion), sig["code"], sig["strategy_key"])


def _mid_sort_key(sig):
    fusion = sig["payload"].get("fusion_score") or sig.get("raw_score") or 0.0
    return (-float(fusion), sig["code"], sig["strategy_key"])


def _deep_sort_key(sig):
    # 沿用 Q12：buy 优先 → frac20 低吸档 → 多维质量分降序 → code。
    p = sig["payload"]
    level = 0 if str(p.get("level") or p.get("signal_level") or "buy") == "buy" else 1
    frac = p.get("frac20")
    low_bucket = 0 if (frac is not None and float(frac) < 0.33) else 1
    quality = p.get("score") or 0.0
    return (level, low_bucket, -float(quality), sig["code"], sig["strategy_key"])


def _regime_cap(list_key, horizon, market_state, effective, strategy_config):
    """该日期市场状态对应的名额上限；正式线读 RECO_REGIME_CAP，观察线用固定上限。"""
    cohort = LIST_SPECS[list_key][0]
    if cohort != "production":
        return _OBSERVE_DEFAULT_CAP
    caps = (strategy_config.get("RECO_REGIME_CAP") or {}).get(horizon, {})
    if market_state in caps:
        return int(caps[market_state])
    if horizon == "short":
        return max(1, int(effective.get("short_top_n", 3)))
    return 4  # mid/long 默认每日前 4


def _eligible(sig, list_key, horizon, allowed_keys, effective, exclusions):
    """资格过滤：主板、非 ST/退、榜单策略白名单、周期匹配、短线置信门控。"""
    if sig["horizon"] != horizon:
        return "horizon_mismatch"
    if sig["strategy_key"] not in allowed_keys:
        return "not_in_list"
    if not is_main_board(sig["code"]):
        return "board_restricted"
    if not _is_tradable_name(sig["payload"].get("name")):
        return "st_or_delisting"
    if list_key == "short":
        fusion = sig["payload"].get("fusion_score")
        gate = float(effective.get("short_conf_gate", 0.0))
        if fusion is None or float(fusion) < gate:
            return "below_conf_gate"
    if list_key == "long" and effective.get("long_lowvol_sort_enabled"):
        mask = int(sig["payload"].get("long_mask") or 0)
        if (mask & 7) != 7:
            return "long_pool_filter"
    if sig["code"] in exclusions:
        return "explicitly_excluded"
    return None


def select_recommendations(run_id, list_key, *, experiment_id="", exclusions=None,
                           sort_variant=None, source="recommendation_service"):
    """从运行冻结信号选出草稿批次；重复相同输入幂等返回同一草稿。

    sort_variant：登记的排序变体（如 {"chip_first": True}），仅改变排序键，候选资格、
    名额上限与执行配置与基线完全一致（方案 C3：唯一允许差异是登记的排序参数）。
    为 None 时按运行冻结的 short_chip_sort_prioritize 决定基线排序。
    """
    if list_key not in LIST_SPECS:
        raise ValueError(f"未支持的榜单键：{list_key}")
    cohort, horizon, allowed_keys = LIST_SPECS[list_key]
    exclusions = set(exclusions or [])
    run, events = _load_run(run_id)
    if run["status"] == "failed":
        raise ValueError("失败运行不可生成推荐名单")
    parameters = json.loads(run["params_json"])
    effective = parameters.get("effective", {})
    strategy_config = parameters.get("strategy_config", {})
    quality = json.loads(run["quality_json"])
    market_state = quality.get("market_state") or "unknown"

    parsed = []
    for e in events:
        e = dict(e)
        e["payload"] = json.loads(e["payload_json"])
        parsed.append(e)

    selected, excluded = [], []
    for sig in parsed:
        reason = _eligible(sig, list_key, horizon, allowed_keys, effective, exclusions)
        if reason:
            excluded.append({"code": sig["code"], "reason": reason,
                             "strategy_key": sig["strategy_key"]})
            continue
        selected.append(sig)

    # 同股同周期去重：保留主信号，其余作为支持信息（不重复占名额）。
    by_code = {}
    for sig in selected:
        by_code.setdefault(sig["code"], []).append(sig)
    primaries = []
    for code, group in by_code.items():
        group.sort(key=lambda s: (_PRIMARY_PRIORITY.get(s["strategy_key"], 9),
                                  s["strategy_key"]))
        primary = group[0]
        primary = dict(primary)
        primary["support"] = [{"signal_id": s["id"], "strategy_key": s["strategy_key"],
                               "strategy": s["strategy"]} for s in group[1:]]
        primaries.append(primary)

    # 排序：short 的筹码优先由 sort_variant（实验）或冻结开关（基线）决定；
    # 其余周期排序键固定。并列一律以 (code, strategy_key) 稳定打破。
    if horizon == "short":
        if sort_variant is not None:
            chip_first = bool(sort_variant.get("chip_first"))
        else:
            chip_first = bool(int(effective.get("short_chip_sort_prioritize", 0) or 0))
        primaries.sort(key=lambda s: _short_sort_key(s, chip_first=chip_first))
    else:
        chip_first = False
        sort_key = {"long": _long_sort_key, "mid": _mid_sort_key, "deep": _deep_sort_key}[horizon]
        primaries.sort(key=sort_key)

    cap = _regime_cap(list_key, horizon, market_state, effective, strategy_config)
    chosen = primaries[:cap]

    policy = {
        "list_key": list_key, "cohort": cohort, "horizon": horizon,
        "allowed_strategy_keys": sorted(allowed_keys), "market_state": market_state,
        "cap": cap, "short_conf_gate": effective.get("short_conf_gate"),
        "short_top_n": effective.get("short_top_n"),
        "long_lowvol_sort_enabled": bool(effective.get("long_lowvol_sort_enabled")),
        "sort_variant": sort_variant, "chip_first": chip_first,
        "params_hash": run["params_hash"], "input_hash": run["input_hash"],
        "code_version": json.loads(run["code_version_json"]),
    }
    batch_id = repo.create_draft_batch(
        run_id=run_id, scan_date=run["scan_date"], list_key=list_key, cohort=cohort,
        policy=policy, market_state=market_state, source=source,
        experiment_id=experiment_id, exclusions=excluded)

    items = []
    for rank, sig in enumerate(chosen, start=1):
        items.append({
            "signal_id": sig["id"], "code": sig["code"], "horizon": sig["horizon"],
            "rank": rank,
            "reason": {"strategy": sig["strategy"], "strategy_key": sig["strategy_key"],
                       "signal_kind": sig["signal_kind"],
                       "fusion_score": sig["payload"].get("fusion_score")},
            # 冻结交易计划：参考价、买点、止损止盈原样保留，不因发布而改基准。
            "plan": {"signal_reference_price": sig["signal_reference_price"],
                     "reference_kind": sig["reference_kind"],
                     "entry_target": sig["entry_target"],
                     "stop_loss": sig["stop_loss"], "take_profit": sig["take_profit"]},
            "support": sig.get("support", []),
        })
    # 仅在草稿尚无条目且本次确有入选时写入，保证重复调用幂等：
    # 空榜批次条目恒为空，若不加 items 判空会对已发布空榜重复触发写入而被拒。
    existing = repo.get_batch(batch_id)
    if items and existing and existing["status"] == "draft" and not existing["items"]:
        repo.add_items(batch_id, items)
    return batch_id


def publish_batch(batch_id, *, comparison=False):
    """发布草稿批次；comparison=True 用于旁路影子对照，不冒充已向用户发布。"""
    return repo.publish_batch(batch_id, status="comparison" if comparison else "published")


def republish_batch(batch_id):
    """显式重新发布：替代当日同榜旧发布批次（旧批次模拟单已成交时拒绝）。"""
    return repo.republish_batch(batch_id)


def get_published_batch(scan_date, list_key, cohort="production", experiment_id=""):
    """只读回取当日冻结名单；不存在返回 None，绝不重新选股。"""
    return repo.get_published_batch(scan_date, list_key, cohort, experiment_id)


def select_and_publish(run_id, list_key, *, experiment_id="", exclusions=None,
                       sort_variant=None, comparison=False, source="recommendation_service"):
    """日常自动流程便捷入口：选择 + 发布（幂等）。"""
    batch_id = select_recommendations(run_id, list_key, experiment_id=experiment_id,
                                      exclusions=exclusions, sort_variant=sort_variant,
                                      source=source)
    publish_batch(batch_id, comparison=comparison)
    return batch_id


# 各信号来源运行完成后应发布的榜单键。deep 榜来自 stock_deep_signal 运行，
# stock_signal 运行不含个股深度信号，故不在此发布 deep（避免生成无意义空榜）。
_RUN_LIST_KEYS = {
    "stock_signal": ("short", "mid", "long",
                     "observe_first_reversal", "observe_surge", "observe_pullback"),
    "stock_deep_signal": ("deep",),
}


def _run_source(run):
    """从运行冻结的输入清单还原信号来源（signal_run 无独立 source 列）。"""
    try:
        return json.loads(run["input_manifest_json"]).get("source", "stock_signal")
    except (KeyError, TypeError, ValueError):
        return "stock_signal"


def publish_run_lists(run_id, *, comparison=False, source="recommendation_service"):
    """运行完成后按来源发布对应榜单：逐榜幂等、best-effort。

    - 仅在运行 status=='complete' 时发布；partial/failed 不发布，避免用不完整
      横截面产出误导性名单（对齐方案「必要数据缺失时不发布受影响榜单」）。
    - comparison=True 用于 shadow 旁路对照，落 comparison 状态，不冒充已向用户发布。
    - 单个榜单失败不影响其余榜单，错误记入返回值供上层汇总为 partial_success。
    返回 {list_key: batch_id} 或 {list_key: {"error": ...}}。
    """
    run, _ = _load_run(run_id)
    if run["status"] != "complete":
        return {}
    results = {}
    for list_key in _RUN_LIST_KEYS.get(_run_source(run), ()):
        try:
            results[list_key] = select_and_publish(
                run_id, list_key, comparison=comparison, source=source)
        except Exception as exc:  # 单榜失败隔离，不阻断其余榜单
            results[list_key] = {"error": str(exc)}
    return results
