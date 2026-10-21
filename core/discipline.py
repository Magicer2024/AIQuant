"""
core/discipline.py —— JH4 纪律装置包内核（T2）+ L09' 封存盲核（T4）
====================================================================

spec：shared/specs/JH4_spec.md（管理A 三条裁定已批准，2026-10-17）

核心不变式（改动前请先读）：
  1. EXIT_EXEMPT 是 frozenset、无 config 键、无热配置入口——延迟出场是纪要判定
     全清单唯一「直接伤金面」项，可配置就能被失控的自己关掉（自设自拆）。
     测试红线 R1：monkeypatch 任何 config 不改变豁免结果。
  2. classify_intent 只看 direction + 数量/价格变动符号计算，禁止读 reason 文本
     （R2 防文本伪装：reason='止损' 但符号为 ENTRY 的意向仍走延迟）。
  3. 本工具不接券商、不做物理拦截（D-5 维持未关闭）。不可逆件 =
     append-only 表 + 哈希链 + 外部副本导出：拦不住手，拦得住事后改写记忆。
  4. 历史数字零变更红线（R9）：本模块只读写新增表，不碰
     config/personal_config.py、strategy_params、backtest/、daily_price。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from difflib import SequenceMatcher

# ─────────────────────────────────────────────
# 出场全豁免清单（§3.1，写死；无对应 config 键）
# ─────────────────────────────────────────────
EXIT_EXEMPT = frozenset({
    "stop_loss_hit",          # 破止损（固定或 ATR 动态）
    "trailing_stop_hit",      # 自高点回撤触发
    "giveback_anchor_dead",   # 理由卡 giveback_from_peak 锚死亡
    "max_hold_days_expired",  # 到期出场
    "partial_take_profit",    # 浮盈减半（降风险）
    "suspend_or_delist",      # 停牌/退市风险公告
    "account_drawdown_guard", # 账户回撤≤-10%：仅允许降到该水位以下的减仓
    "single_day_gap_down",    # 单日跌幅≤-7%（主板）/≤-13%（创业/科创）
})

ENTRY = "ENTRY"
EXIT_RISK_REDUCING = "EXIT_RISK_REDUCING"

# 24h 档：进场偏离延迟秒数（裁定 2）。soft_hold 仅提醒语义，不做拦截。
RELEASE_DELAY_SECONDS = 24 * 3600
SOFT_HOLD_SECONDS = 600

# 漂移守卫：放行时最新 close > t0 参考价 × (1+DRIFT_UP_PCT) → voided_drift。
# 仅此一处可配（spec §4.2 指定 config/discipline_params.py）。
try:
    from config.discipline_params import DRIFT_UP_PCT  # type: ignore
except Exception:
    DRIFT_UP_PCT = 0.03

# 数字失效锚指标白名单（§2.2）。每项都必须有可取数路径（_INDICATOR_SOURCES），
# 取不到数据时锚记 pending_data，不误判死。
ANCHOR_KINDS = frozenset({
    "price_below", "drawdown_from_entry", "giveback_from_peak",
    "deadline", "indicator_below", "unlock_event",
})
INDICATOR_WHITELIST = frozenset({"fusion_score", "vol_ratio", "chip_conc",
                                 "close_vs_ma20", "j", "rsi14"})


def _assert_whitelist():
    # 启动断言：白名单与已实现取数路径的交集非空（spec §2.2）
    assert INDICATOR_WHITELIST & frozenset(_INDICATOR_SOURCES), \
        "指标白名单与取数路径交集为空"


# ─────────────────────────────────────────────
# 意图分类（R1/R2 的判定源头：纯符号计算）
# ─────────────────────────────────────────────
def classify_intent(*, direction: str, qty_delta: float = 0.0,
                    stop_old: float | None = None,
                    stop_new: float | None = None,
                    current_price: float | None = None) -> str:
    """direction ∈ {'buy','sell','stop_change'}。禁止传 reason 文本参与判定。

    多头口径：收紧止损 = 止损价上移（更靠近现价）→ 降风险；
    放宽/撤销（下移或置空）→ 增风险（归 ENTRY，走偏离单）。
    """
    d = str(direction)
    if d == "sell":
        return EXIT_RISK_REDUCING if qty_delta < 0 else ENTRY  # 负数量卖出=降风险
    if d == "buy":
        return ENTRY if qty_delta > 0 else EXIT_RISK_REDUCING
    if d == "stop_change":
        if stop_new is None:                       # 撤销止损
            return ENTRY
        if stop_old is None:                       # 新设止损
            return EXIT_RISK_REDUCING
        # 收紧=向现价方向移动。多头止损在现价下方：上移为收紧。
        if current_price is not None and stop_new >= current_price:
            return ENTRY                           # 止损抬到现价上方=异常放宽形态
        return EXIT_RISK_REDUCING if stop_new > stop_old else ENTRY
    raise ValueError(f"未知 direction: {d!r}")


def delay_seconds_for(action_class: str, exempt_kind: str | None = None,
                      tier: str = "24h") -> float:
    """豁免与出场类意图延迟恒为 0；ENTRY 偏离按档返回延迟秒数。

    exempt_kind 只要属于 EXIT_EXEMPT 即 0——该集合是 frozenset 常量，
    不查任何配置（R1 红线的实现落点）。
    """
    if exempt_kind is not None:
        if exempt_kind in EXIT_EXEMPT:
            return 0.0
        raise ValueError(f"非法 exempt_kind: {exempt_kind!r}（不在豁免清单）")
    if action_class == EXIT_RISK_REDUCING:
        return 0.0
    return SOFT_HOLD_SECONDS if tier == "soft_hold" else RELEASE_DELAY_SECONDS


# ─────────────────────────────────────────────
# 放行/逾期时间计算（§4.2/§4.3）
# ─────────────────────────────────────────────
def _is_trading_day(dt_str: str) -> bool:
    try:
        from core.trade_calendar import is_trading_day
        return bool(is_trading_day(dt_str))
    except Exception:
        return datetime.strptime(dt_str, "%Y-%m-%d").weekday() < 5


def _next_trading_day(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)
    for _ in range(15):
        s = d.strftime("%Y-%m-%d")
        if _is_trading_day(s):
            return s
        d += timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def compute_release_expire(submitted_at: datetime) -> tuple[str, str]:
    """release_at = max(下一交易日 09:30, t0+24h)；
    expire_at = release_at 所在（或顺延到的）交易日 15:00。"""
    t0 = submitted_at
    next_open = datetime.strptime(_next_trading_day(t0.strftime("%Y-%m-%d")),
                                  "%Y-%m-%d").replace(hour=9, minute=30)
    release = max(next_open, t0 + timedelta(seconds=RELEASE_DELAY_SECONDS))
    # 若落在非交易日（t0+24h 恰在周末），顺延下一交易日 09:30
    if not _is_trading_day(release.strftime("%Y-%m-%d")):
        release = datetime.strptime(_next_trading_day(release.strftime("%Y-%m-%d")),
                                    "%Y-%m-%d").replace(hour=9, minute=30)
    expire_day = release.strftime("%Y-%m-%d")
    expire = datetime.strptime(expire_day, "%Y-%m-%d").replace(hour=15)
    return release.strftime("%Y-%m-%d %H:%M:%S"), expire.strftime("%Y-%m-%d %H:%M:%S")


def trading_days_after(start_date: str, n: int) -> str:
    """start_date 起第 n 个交易日（含顺延），n≥1。"""
    d = start_date
    for _ in range(max(1, int(n))):
        d = _next_trading_day(d)
    return d


# ─────────────────────────────────────────────
# 哈希链（§2.4 / §6.3：篡改可检出的最佳可得手段）
# ─────────────────────────────────────────────
_HASH_TABLES = ("trade_plan", "deviation_ticket", "case_file", "sealed_decision")
# 全局排除：链覆盖「证据件不可变核心」，状态推进与回填列一律不入链
_HASH_EXCLUDE = {"content_hash", "prev_hash", "updated_at", "status"}
_EXTRA_EXCLUDE = {
    "deviation_ticket": {"drift_pct", "void_reason", "executed_trade_id"},
    "case_file": {"exec_ret_5", "exec_ret_10", "exec_ret_20",
                  "counterfactual_ret_5", "counterfactual_ret_10", "counterfactual_ret_20",
                  "worst_adverse_ret"},
    "trade_plan": set(),
    "sealed_decision": set(),
}


def canonical_json(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _excluded_cols(table: str) -> set:
    return _HASH_EXCLUDE | _EXTRA_EXCLUDE.get(table, set())


def _payload_for_row(table: str, row) -> dict:
    return {k: row[k] for k in row.keys()
            if k != "id" and k not in _excluded_cols(table)}


def chain_prev_hash(conn, table: str) -> str | None:
    if table not in _HASH_TABLES:
        raise ValueError(f"非哈希链表: {table}")
    r = conn.execute(f"SELECT content_hash FROM {table} ORDER BY id DESC LIMIT 1").fetchone()
    return r[0] if r else None


def make_hash(prev_hash: str | None, payload: dict) -> str:
    base = canonical_json(payload) + (prev_hash or "")
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


_COLS_CACHE: dict[str, list] = {}


def insert_hashed(conn, table: str, cols: dict) -> tuple[int, str]:
    """链上表统一写入：哈希覆盖「不可变核心列」（行全列排除链外可变列，缺列补 None），
    INSERT 保留调用方显式给的列（含 status 等链外必填列）。
    verify_chain 的行重建与本函数 payload 完全一致。"""
    if table not in _HASH_TABLES:
        raise ValueError(f"非哈希链表: {table}")
    with conn as c:
        if table not in _COLS_CACHE:
            _COLS_CACHE[table] = [
                r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()
                if r[1] != "id" and r[1] not in _excluded_cols(table)]
        rowcols = _COLS_CACHE[table]
        payload = {k: cols.get(k) for k in rowcols}
        prev = chain_prev_hash(c, table)
        h = make_hash(prev, payload)
        cols2 = dict(cols)
        cols2["content_hash"] = h
        cols2["prev_hash"] = prev
        keys = list(cols2)
        cur = c.execute(
            f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
            [cols2[k] for k in keys])
        return cur.lastrowid, h


def verify_chain(conn, table: str) -> list[dict]:
    """R11：逐行重算哈希，返回断裂点列表（绕过 trigger 直改 DB 文件可被检出）。"""
    with conn as c:
        rows = c.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
    breaks, prev = [], None
    for r in rows:
        recomputed = make_hash(prev, _payload_for_row(table, r))
        if (r["content_hash"] or "") != recomputed or (r["prev_hash"] or "") != (prev or ""):
            breaks.append({"table": table, "id": r["id"],
                           "expected_hash": recomputed,
                           "stored_hash": r["content_hash"]})
            break          # 报告首个断裂点即止（链断后后续全是噪音）
        prev = r["content_hash"]
    return breaks


def export_seal_chain(conn, out_dir: str, month: str | None = None,
                      copy_to: str | None = None) -> str:
    """每日追加写 reports/seal_chain_YYYYMM.jsonl；copy_to 目录存在则再拷一份外部副本。
    诚实边界：本地整体替换库文件仍可伪造，本导出只保证「篡改可检出」。"""
    import os
    month = month or datetime.now().strftime("%Y-%m")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"seal_chain_{month}.jsonl")
    with conn as c:
        with open(path, "a", encoding="utf-8") as f:
            for table in _HASH_TABLES:
                for r in c.execute(
                        f"SELECT id,content_hash,prev_hash FROM {table} ORDER BY id").fetchall():
                    f.write(canonical_json({"t": table, "id": r["id"],
                                            "h": r["content_hash"], "p": r["prev_hash"]}) + "\n")
    if copy_to and os.path.isdir(copy_to):
        import shutil
        shutil.copyfile(path, os.path.join(copy_to, os.path.basename(path)))
    return path


# ─────────────────────────────────────────────
# 理由卡校验（§2.1/§2.2/§2.3）
# ─────────────────────────────────────────────
def thesis_duplicate(conn, code: str, thesis: str, before_id: int | None = None) -> dict | None:
    """近 180 天同 code 历史卡相似度 ≥0.9 → 返回冲突行（需 delta_note），否则 None。"""
    cutoff = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")
    q = ("SELECT id, thesis FROM trade_plan WHERE code=? AND created_at>=? "
         "AND (? IS NULL OR id<?) ORDER BY id")
    rows = conn.execute(q, (code, cutoff, before_id, before_id)).fetchall()
    for r in rows:
        if SequenceMatcher(None, r["thesis"], thesis).ratio() >= 0.9:
            return {"conflict_plan_id": r["id"]}
    return None


def validate_anchors(anchors: list) -> list[str]:
    """硬校验，返回错误码列表（空=通过）。threshold_num 数字锚是 DDL 级硬门，
    这里校验 kind/条数/指标可取数。"""
    errs: list[str] = []
    if not anchors:
        errs.append("ANCHOR_NO_NUMBER")
        return errs
    if len(anchors) > 4:
        errs.append("ANCHOR_TOO_MANY")
    for a in anchors:
        kind = a.get("anchor_kind")
        if kind not in ANCHOR_KINDS:
            errs.append("ANCHOR_KIND_UNKNOWN")
            continue
        if a.get("threshold_num") is None:
            errs.append("ANCHOR_NO_NUMBER")
        if kind == "indicator_below":
            ind = a.get("indicator")
            if ind not in INDICATOR_WHITELIST or ind not in _INDICATOR_SOURCES:
                errs.append("INDICATOR_NOT_AVAILABLE")
    return errs


# ─────────────────────────────────────────────
# 指标取数（锚评估用；取不到 → None → pending_data）
# ─────────────────────────────────────────────
def _df_price(conn, code: str) -> "pd.DataFrame | None":
    import pandas as pd
    df = pd.read_sql_query(
        "SELECT trade_date, open, high, low, close, volume, fusion_score "
        "FROM daily_price WHERE code=? ORDER BY trade_date", conn, params=(code,))
    if df.empty:
        return None
    return df.set_index("trade_date")


def _indicator_value(conn, code: str, indicator: str):
    df = _df_price(conn, code)
    if df is None:
        return None
    src = _INDICATOR_SOURCES.get(indicator)
    if src is None:
        return None
    return src(conn, code, df)


def _ind_fusion(conn, code, df):
    v = df["fusion_score"].dropna()
    return float(v.iloc[-1]) if len(v) else None


def _ind_vol_ratio(conn, code, df):
    import pandas as pd
    if len(df) < 21:
        return None
    ma = pd.to_numeric(df["volume"], errors="coerce").rolling(20).mean()
    last = float(ma.iloc[-1])
    return float(df["volume"].iloc[-1]) / last if last > 0 else None


def _ind_close_vs_ma20(conn, code, df):
    import pandas as pd
    if len(df) < 20:
        return None
    ma20 = pd.to_numeric(df["close"], errors="coerce").rolling(20).mean().iloc[-1]
    return float(df["close"].iloc[-1]) / float(ma20) - 1 if ma20 and ma20 > 0 else None


def _ind_j(conn, code, df):
    from strategy.indicators import calc_kdj
    if len(df) < 10:
        return None
    kdj = calc_kdj(df["high"].astype(float), df["low"].astype(float), df["close"].astype(float))
    j = kdj["J"].dropna() if "J" in kdj else None
    return float(j.iloc[-1]) if j is not None and len(j) else None


def _ind_rsi14(conn, code, df):
    from strategy.indicators import calc_rsi
    if len(df) < 16:
        return None
    rsi = calc_rsi(df["close"].astype(float))
    col = "RSI_14" if "RSI_14" in rsi else ("rsi14" if "rsi14" in rsi else None)
    if col is None:
        return None
    v = rsi[col].dropna()
    return float(v.iloc[-1]) if len(v) else None


def _ind_chip_conc(conn, code, df):
    r = conn.execute("SELECT chip_conc FROM stock_signal WHERE code=? "
                     "AND chip_conc IS NOT NULL ORDER BY scan_date DESC LIMIT 1",
                     (code,)).fetchone()
    return float(r[0]) if r else None


_INDICATOR_SOURCES = {
    "fusion_score": _ind_fusion,
    "vol_ratio": _ind_vol_ratio,
    "close_vs_ma20": _ind_close_vs_ma20,
    "j": _ind_j,
    "rsi14": _ind_rsi14,
    "chip_conc": _ind_chip_conc,
}
_assert_whitelist()


# ─────────────────────────────────────────────
# 锚评估与 plan 状态推进（§2.4 / §2.5，夜检后每日 sweep 调用）
# ─────────────────────────────────────────────
def evaluate_anchor(conn, anchor, plan, pos) -> str:
    """返回评估后 state：alive/dead/pending_data。只读 daily_price，不写库。

    pos：该 code 的 alive 持仓行（cost_price/opened_at）或 None；
    无持仓时 drawdown/giveback/deadline 用 entry_trigger.max_buy_price 为参照。
    """
    kind = anchor["anchor_kind"]
    thr = float(anchor["threshold_num"])
    df = _df_price(conn, plan["code"])
    if df is None or df.empty:
        return "pending_data"
    close = float(df["close"].iloc[-1])
    import json as _json
    expr = {}
    try:
        expr = _json.loads(anchor["check_expr_json"] or "{}")
    except Exception:
        pass

    cost = None
    entry_from = None
    if pos is not None:
        cost = float(pos["cost_price"] or 0) or None
        entry_from = str(pos["opened_at"])[:10] if pos["opened_at"] else None
    if cost is None:
        try:
            cost = float(_json.loads(plan["entry_trigger_json"]).get("max_buy_price")) or None
        except Exception:
            cost = None

    if kind == "price_below":
        return "dead" if close < thr else "alive"
    if kind == "drawdown_from_entry":
        if not cost:
            return "pending_data"
        return "dead" if (close / cost - 1) <= thr else "alive"
    if kind == "giveback_from_peak":
        if not cost:
            return "pending_data"
        seg = df.loc[entry_from:] if entry_from else df
        high = seg["high"].astype(float)
        peak = float(high.max()) if len(high) else close
        if peak <= 0:
            return "pending_data"
        return "dead" if (close / peak - 1) <= thr else "alive"
    if kind == "deadline":
        due = expr.get("date") or str(int(thr))
        if len(due) == 8 and due.isdigit():
            due = f"{due[:4]}-{due[4:6]}-{due[6:]}"
        last_day = str(df.index[-1])[:10]
        if last_day < due:
            return "alive"
        if not cost:
            return "pending_data"
        return "dead" if (close / cost - 1) < float(plan["expected_return_pct"]) else "alive"
    if kind == "indicator_below":
        v = _indicator_value(conn, plan["code"], anchor["indicator"] or "")
        if v is None:
            return "pending_data"
        return "dead" if v < thr else "alive"
    if kind == "unlock_event":
        # 免费源无结构化解禁事件表：不猜测，保持 pending_data（人工确认）。
        return "pending_data"
    return "pending_data"


def check_all_anchors(conn) -> dict:
    """每日核对 sweep：更新锚状态；全锚死亡判定在 check 内聚合；
    锚死→plan invalidated；锚死且仍持仓→violations（首次不重复）。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    died = checked = pending = violations = 0
    with conn as c:
        plans = c.execute("SELECT * FROM trade_plan WHERE status='alive'").fetchall()
        for plan in plans:
            anchors = c.execute(
                "SELECT * FROM plan_anchor WHERE plan_id=? AND state!='dead'",
                (plan["id"],)).fetchall()
            if not anchors:
                continue
            pos = c.execute("SELECT * FROM personal_position WHERE code=? AND status='holding'",
                            (plan["code"],)).fetchone()
            for a in anchors:
                st = evaluate_anchor(c, a, plan, pos)
                checked += 1
                if st == "dead":
                    c.execute("UPDATE plan_anchor SET state='dead', died_at=?, last_checked_at=? "
                              "WHERE id=?", (now, now, a["id"]))
                    died += 1
                else:
                    if st == "pending_data":
                        pending += 1
                    c.execute("UPDATE plan_anchor SET state=?, last_checked_at=? WHERE id=?",
                              (st, now, a["id"]))
            dead_now = c.execute("SELECT COUNT(*) FROM plan_anchor WHERE plan_id=? AND state='dead'",
                                 (plan["id"],)).fetchone()[0]
            if dead_now:
                c.execute("UPDATE trade_plan SET status='invalidated', updated_at=? WHERE id=?",
                          (now, plan["id"]))
                if pos is not None:
                    exists = c.execute(
                        "SELECT 1 FROM discipline_violation WHERE plan_id=? "
                        "AND kind='held_after_invalidated'", (plan["id"],)).fetchone()
                    if not exists:
                        c.execute("INSERT INTO discipline_violation (plan_id,kind,detected_at,note) "
                                  "VALUES (?,'held_after_invalidated',?,?)",
                                  (plan["id"], now, f"锚已死仍持仓（code={plan['code']}）"))
                        violations += 1
    return {"checked": checked, "died": died, "pending_data": pending, "violations": violations}


# ─────────────────────────────────────────────
# 偏离判定 D1–D8（§4.1）
# ─────────────────────────────────────────────
def _account_scale():
    from config.personal_config import POSITION_PLAN_ACCOUNT
    return float(POSITION_PLAN_ACCOUNT)


def account_drawdown_pct(conn) -> float | None:
    """最新账户快照收益率（小数）。无快照 → None（不命中 D8/H7）。"""
    r = conn.execute("SELECT total_pnl, total_cost FROM account_snapshots "
                     "ORDER BY snapshot_date DESC LIMIT 1").fetchone()
    if not r or not r["total_cost"]:
        return None
    return float(r["total_pnl"]) / float(r["total_cost"])


def alive_plan(conn, code: str):
    return conn.execute("SELECT * FROM trade_plan WHERE code=? AND status='alive' "
                        "ORDER BY id DESC LIMIT 1", (code,)).fetchone()


def evaluate_deviation(conn, *, code: str, direction: str, qty: float, price: float,
                       stop_old: float | None = None, stop_new: float | None = None) -> dict:
    """进场意向 → {action_class, is_deviation, rules, plan_id, required_seal,
    seal_triggers}。出场/豁免恒 is_deviation=False。

    口径注：D6 计数只统计偏离意向（计划内意向不落库，spec 表域外不擅自加表）。
    """
    action_class = classify_intent(direction=direction, qty_delta=qty,
                                   stop_old=stop_old, stop_new=stop_new,
                                   current_price=price if direction != "stop_change" else None)
    if action_class == EXIT_RISK_REDUCING:
        return {"action_class": action_class, "is_deviation": False, "rules": [],
                "plan_id": None, "required_seal": False, "seal_triggers": []}

    rules: list[str] = []
    account = _account_scale()
    plan = alive_plan(conn, code)
    amount = abs(float(qty)) * float(price) if direction != "stop_change" else 0.0
    today = datetime.now().strftime("%Y-%m-%d")

    if plan is None:
        rules.append("D1")
    else:
        try:
            max_buy = float(json.loads(plan["entry_trigger_json"]).get("max_buy_price", 0))
        except Exception:
            max_buy = 0.0
        if plan["position_pct"] and amount / account > float(plan["position_pct"]) * 1.10:
            rules.append("D2")
        if direction != "stop_change" and max_buy > 0 and float(price) > max_buy * 1.02:
            rules.append("D3")
        # D4：plan 的数字锚若含 indicator_below 且当日未达标 → 必要条件未满足
        a4 = conn.execute("SELECT * FROM plan_anchor WHERE plan_id=? AND state='alive' "
                          "AND anchor_kind='indicator_below'", (plan["id"],)).fetchall()
        for a in a4:
            v = _indicator_value(conn, code, a["indicator"] or "")
            if v is not None and v < float(a["threshold_num"]):
                rules.append("D4")
                break
        if direction == "stop_change":
            rules.append("D4")   # 计划内不含止损变更，止损变更恒偏离
    pos = conn.execute("SELECT * FROM personal_position WHERE code=? AND status='holding'",
                       (code,)).fetchone()
    last_plan = conn.execute("SELECT status FROM trade_plan WHERE code=? ORDER BY id DESC LIMIT 1",
                             (code,)).fetchone()
    if pos is not None and last_plan is not None and last_plan["status"] == "invalidated":
        rules.append("D5")
    same_day = conn.execute(
        "SELECT qty, price_ref FROM deviation_ticket WHERE submitted_at>=? AND status!='withdrawn'",
        (today,)).fetchall()
    if len(same_day) + 1 > 2:
        rules.append("D6")
    day_amount = sum(float(r["qty"]) * float(r["price_ref"]) for r in same_day) + amount
    if day_amount > account * 0.40:
        rules.append("D6")
    if direction == "stop_change":
        rules.append("D7")
    dd = account_drawdown_pct(conn)
    if dd is not None and dd <= -0.10:
        rules.append("D8")
    rules = sorted(set(rules))

    seal = evaluate_high_risk(conn, code=code, amount=amount, direction=direction,
                              stop_old=stop_old, stop_new=stop_new)
    return {"action_class": action_class, "is_deviation": bool(rules),
            "rules": rules, "plan_id": plan["id"] if plan else None,
            "required_seal": bool(seal), "seal_triggers": seal}


# ─────────────────────────────────────────────
# 高风险触发 H1–H7（§6.1）
# ─────────────────────────────────────────────
def evaluate_high_risk(conn, *, code: str, amount: float, direction: str = "buy",
                       stop_old: float | None = None, stop_new: float | None = None,
                       ticket_id: int | None = None) -> list[str]:
    """封存触发集（命中任一即应封存；封存≠延迟，两表独立）。"""
    t: list[str] = []
    account = _account_scale()
    pct = amount / account if account else 0.0
    cal = conn.execute("SELECT max_single_position_pct, status FROM risk_param_calibration "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    if cal is not None and cal["status"] in ("calibrated", "pooled", "low_sample") \
            and cal["max_single_position_pct"]:
        threshold = float(cal["max_single_position_pct"]) * 1.2
    else:
        threshold = 0.15   # spec H1：无校准时默认阈值
    if direction == "buy" and pct > threshold:
        t.append("H1")
    prior = conn.execute("SELECT 1 FROM personal_trade WHERE code=? AND action IN ('买入','加仓') "
                         "LIMIT 1", (code,)).fetchone()
    pos = conn.execute("SELECT 1 FROM personal_position WHERE code=? AND status='holding' LIMIT 1",
                       (code,)).fetchone()
    if direction == "buy" and prior and pos:
        t.append("H2")
    today = datetime.now().strftime("%Y-%m-%d")
    day = conn.execute("SELECT qty, price_ref FROM deviation_ticket WHERE submitted_at>=? "
                       "AND status!='withdrawn'", (today,)).fetchall()
    day_amt = sum(float(r["qty"]) * float(r["price_ref"]) for r in day) + amount
    if direction == "buy" and day_amt > account * 0.30:
        t.append("H3")
    in_pool = conn.execute("SELECT 1 FROM stock_signal WHERE scan_date="
                           "(SELECT MAX(scan_date) FROM stock_signal) AND code=? LIMIT 1",
                           (code,)).fetchone()
    if in_pool is None:
        t.append("H4")
    if direction == "stop_change" and classify_intent(direction="stop_change",
                                                      stop_old=stop_old, stop_new=stop_new) == ENTRY:
        t.append("H5")
    if ticket_id is not None:
        r = conn.execute("SELECT status FROM deviation_ticket WHERE id=?", (ticket_id,)).fetchone()
        if r and r["status"] == "released":
            t.append("H6")
    dd = account_drawdown_pct(conn)
    if dd is not None and dd <= -0.10:
        t.append("H7")
    return t


# ─────────────────────────────────────────────
# 反事实/执行盈亏回填（§4.4，夜检后每日 sweep 调用）
# ─────────────────────────────────────────────
def _return_window(conn, code: str, ref_price: float, from_date: str, n_days: int):
    """ref_price 起第 n 交易日收盘收益（小数）；不足 n 日 → None。"""
    rows = conn.execute(
        "SELECT close, low FROM daily_price WHERE code=? AND trade_date>? ORDER BY trade_date LIMIT ?",
        (code, from_date, n_days)).fetchall()
    if len(rows) < n_days or not ref_price:
        return None, None
    close = float(rows[-1]["close"])
    lows = [float(r["low"]) for r in rows if r["low"]]
    adverse = (min(lows) / ref_price - 1) if lows else None
    return close / ref_price - 1, adverse


def backfill_case_files(conn) -> dict:
    """对终态判例回填 5/10/20 日 exec/counterfactual 与最大逆行（NULL→值一次写入）。

    最大逆行按 20 日观察窗在数据齐备时一次落地：case_file 触发器只允许 NULL→值写一次，
    若先用 5/10 日短窗写入会永久锁死更差的 20 日逆行（且第二次重写会被触发器拒绝）。
    """
    filled = scanned = 0
    with conn as c:
        cases = c.execute(
            "SELECT cf.*, t.submitted_at, t.status FROM case_file cf "
            "JOIN deviation_ticket t ON t.id=cf.ticket_id "
            "WHERE (t.status='executed' AND (cf.exec_ret_5 IS NULL OR cf.exec_ret_10 IS NULL "
            "  OR cf.exec_ret_20 IS NULL OR cf.worst_adverse_ret IS NULL)) "
            "OR (t.status<>'executed' AND (cf.counterfactual_ret_5 IS NULL "
            "  OR cf.counterfactual_ret_10 IS NULL OR cf.counterfactual_ret_20 IS NULL "
            "  OR cf.worst_adverse_ret IS NULL))").fetchall()
        for cf in cases:
            scanned += 1
            executed = cf["status"] == "executed"
            for n in (5, 10, 20):
                target = f"exec_ret_{n}" if executed else f"counterfactual_ret_{n}"
                if cf[target] is not None:
                    continue
                ret, _ = _return_window(c, cf["code"], cf["ref_price"] or 0,
                                        str(cf["submitted_at"])[:10], n)
                if ret is None:
                    continue
                c.execute(f"UPDATE case_file SET {target}=? WHERE id=?", (round(ret, 6), cf["id"]))
                filled += 1
            if cf["worst_adverse_ret"] is None:
                _, adverse = _return_window(c, cf["code"], cf["ref_price"] or 0,
                                            str(cf["submitted_at"])[:10], 20)
                if adverse is not None:
                    c.execute("UPDATE case_file SET worst_adverse_ret=? WHERE id=?",
                              (round(adverse, 6), cf["id"]))
                    filled += 1
    return {"scanned": scanned, "filled_cells": filled}


# ─────────────────────────────────────────────
# L09' 到期盲核计算（§6.4：顺序约束——blind 只给结果，reveal 才对照）
# ─────────────────────────────────────────────
def compute_result_metrics(conn, sealed) -> dict:
    """到期观察窗（sealed_at→due_on）的真实表现指标。绝不含封存字段。"""
    start = str(sealed["sealed_at"])[:10]
    end = str(sealed["due_on"])[:10]
    rows = conn.execute(
        "SELECT trade_date, close, high, low FROM daily_price "
        "WHERE code=? AND trade_date>? AND trade_date<=? ORDER BY trade_date",
        (sealed["code"], start, end)).fetchall()
    ref = float(sealed["ref_price"])
    if not rows or not ref:
        return {"window_days": 0, "interval_return": None, "max_drawdown": None,
                "worst_case_reached": None}
    closes = [float(r["close"]) for r in rows]
    lows = [float(r["low"]) for r in rows]
    dd = 0.0
    peak = ref
    for lo, cl in zip(lows, closes):
        dd = min(dd, cl / peak - 1)
        peak = max(peak, cl)
    worst = float(sealed["worst_case_pct"])
    return {"window_days": len(rows),
            "interval_return": round(closes[-1] / ref - 1, 6),
            "max_drawdown": round(dd, 6),
            "worst_case_reached": bool(min(lows) / ref - 1 <= worst)}


def reveal_compare(conn, sealed, blind) -> dict:
    """对照计算（写入前）：gap/兑现/最坏情形/解释分歧。explanation_diverged 取
    1-相似度（越大越分歧）。"""
    recall = str(blind["recall_verdict"])
    hit = 1 if recall == "yes" else (0 if recall == "no" else None)
    sim = SequenceMatcher(None, str(blind["recall_reason"]),
                          str(sealed["thesis_snapshot"])).ratio()
    metrics = json.loads(blind["result_metrics_json"] or "{}")
    mdd = metrics.get("max_drawdown")
    worst_hit = None
    if mdd is not None:
        worst_hit = 1 if mdd <= float(sealed["worst_case_pct"]) else 0
    return {"gap_expected": round(float(blind["recall_expected_return_pct"])
                                  - float(sealed["expected_return_pct"]), 6),
            "confidence_hit": hit, "worst_case_hit": worst_hit,
            "explanation_diverged": round(1.0 - sim, 4)}


# ─────────────────────────────────────────────
# 状态 sweep（scheduler 每 5 分钟；到期→终态+归档，幂等）
# ─────────────────────────────────────────────
def archive_case(conn, ticket, now: str) -> int | None:
    """终态工单归档判例（一 ticket 一行，重复调用跳过）。"""
    with conn as c:
        exists = c.execute("SELECT 1 FROM case_file WHERE ticket_id=?", (ticket["id"],)).fetchone()
        if exists:
            return None
        cid, _h = insert_hashed(c, "case_file", {
            "ticket_id": ticket["id"], "code": ticket["code"],
            "outcome": ticket["status"], "intent_reason": ticket["intent_reason"],
            "ref_price": ticket["price_ref"], "ref_amount": ticket["amount_ref"],
            "qty": ticket["qty"], "regime_at_submit": None, "archived_at": now})
        return cid


def sweep_ticket_states(conn, now: datetime | None = None) -> dict:
    """pending 逾期→expired；released 逾期→abandoned；released 且漂移→voided_drift。
    全部终态归档判例。10 分钟 soft_hold 档不进成功率统计（此处语义：只做提醒展示，
    状态机照常推进，聚合端按 tier 过滤）。"""
    now = now or datetime.now()
    now_s = now.strftime("%Y-%m-%d %H:%M:%S")
    expired = abandoned = drifted = archived = 0
    with conn as c:
        for t in c.execute("SELECT * FROM deviation_ticket WHERE status='pending'").fetchall():
            if now_s > t["expire_at"]:
                c.execute("UPDATE deviation_ticket SET status='expired' WHERE id=?", (t["id"],))
                t = dict(t) | {"status": "expired"}
                archive_case(c, t, now_s)
                expired += 1
                archived += 1
        for t in c.execute("SELECT * FROM deviation_ticket WHERE status='released'").fetchall():
            if now_s > t["expire_at"]:
                c.execute("UPDATE deviation_ticket SET status='abandoned' WHERE id=?", (t["id"],))
                t = dict(t) | {"status": "abandoned"}
                archive_case(c, t, now_s)
                abandoned += 1
                archived += 1
            else:
                last = c.execute("SELECT close FROM daily_price WHERE code=? "
                                 "ORDER BY trade_date DESC LIMIT 1", (t["code"],)).fetchone()
                if last and t["price_ref"] and \
                        float(last["close"]) > float(t["price_ref"]) * (1 + DRIFT_UP_PCT):
                    c.execute("UPDATE deviation_ticket SET status='voided_drift', void_reason=? "
                              "WHERE id=?",
                              (f"放行时收盘 {float(last['close']):.3f} > 参考价×(1+{DRIFT_UP_PCT})",
                               t["id"]))
                    t = dict(t) | {"status": "voided_drift"}
                    archive_case(c, t, now_s)
                    drifted += 1
                    archived += 1
        for s in ("withdrawn",):
            for t in c.execute("SELECT * FROM deviation_ticket WHERE status=?", (s,)).fetchall():
                if not c.execute("SELECT 1 FROM case_file WHERE ticket_id=?",
                                 (t["id"],)).fetchone():
                    archive_case(c, t, now_s)
                    archived += 1
    return {"expired": expired, "abandoned": abandoned, "voided_drift": drifted,
            "archived": archived}


def sweep_sealed(conn) -> int:
    """sealed 且 due_on 已过 → due（提醒只给 id/code，不给内容）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    with conn as c:
        rows = c.execute("SELECT id FROM sealed_decision WHERE status='sealed' AND due_on<=?",
                        (today,)).fetchall()
        for r in rows:
            c.execute("UPDATE sealed_decision SET status='due' WHERE id=?", (r["id"],))
        return len(rows)


def insert_deviation_ticket(conn, *, plan_id, code, action, qty, price_ref, amount_ref,
                            rules: list, tier: str, intent_reason: str,
                            submitted_at: datetime | None = None) -> dict:
    """建偏离单：时间计算 + 哈希链 + retry_of（同 code 7 天内最近一张过期单）。"""
    t0 = submitted_at or datetime.now()
    submitted_s = t0.strftime("%Y-%m-%d %H:%M:%S")
    release_s, expire_s = compute_release_expire(t0)
    with conn as c:
        retry = c.execute(
            "SELECT id FROM deviation_ticket WHERE code=? AND status='expired' "
            "AND submitted_at >= datetime('now','localtime','-7 days') "
            "ORDER BY id DESC LIMIT 1", (code,)).fetchone()
        tid, h = insert_hashed(c, "deviation_ticket", {
            "plan_id": plan_id, "code": code, "action": action, "qty": int(qty),
            "price_ref": price_ref, "amount_ref": amount_ref,
            "deviation_rules_json": json.dumps(sorted(rules)), "tier": tier,
            "intent_reason": intent_reason[:300], "submitted_at": submitted_s,
            "release_at": release_s, "expire_at": expire_s, "status": "pending",
            "retry_of": retry["id"] if retry else None, "created_at": submitted_s})
        return {"ticket_id": tid, "release_at": release_s, "expire_at": expire_s,
                "content_hash": h, "retry_of": retry["id"] if retry else None}
