"""strategy/deep_tracker.py —— 个股深度推荐跟踪（买点建仓 → 卖点出场 → 逐笔统计）

定位
────
「个股深度 · 买卖建议」面板每日盘后由全市场深析扫描（strategy/stock_deep.run_full_market_scan）
产出候选并落库 stock_deep_signal。本模块把这些候选转成**可跟踪的模拟单**，逐日按真实可执行的
买点/卖点推进，并统计每一笔的持仓时间与盈亏，供面板下方的「跟踪列表」展示。

三个动作（每日盘后按顺序跑一次，见 scheduler/runner.py）
──────────────────────────────────────────────────────
1. sync_from_scan  —— 把当日扫描候选写成跟踪单（status=watching，未建仓）
2. update_open_tracks —— 推进所有未了结的跟踪单：
     watching：在窗口内等回踩买点 → 成交转 holding；超期未触发 → expired
     holding ：按止损 / 移动止盈 / 持仓上限出场 → closed，并结算收益与持仓天数
3. get_stats —— 汇总胜率、平均持仓交易日、平均收益、盈亏比、出场原因分布

执行口径（刻意与既有模块对齐，避免同一笔推荐在不同面板出现两套结果）
────────────────────────────────────────────────────────────────
· 买点：信号日 T 的收盘价即参考买点 sig_entry（= stock_deep 的 action_plan.entry_price）。
        T+1 起 entry_window_days 个交易日内，只要某日 **最低价触及买点** 才算成交，
        成交价 = min(sig_entry, 当日开盘)（开盘已低于买点则按开盘价，否则按买点成交）。
        → 不追高：涨上去不追，只在回踩时买。窗口内一直没回踩则判 expired，不计入统计。
· 卖点：与 core/outcome_tracker._evaluate_short（方案A）同口径 ——
        exec_entry = 实际建仓价
        止损价   = 推荐自带 stop_loss，缺失回退 exec_entry × (1 + short_stop_loss)
        启动线   = exec_entry × (1 + short_take_profit)，回退推荐自带 take_profit
        移动止盈 = 持仓最高价 × (1 - short_trailing_pct)，只上移不下移
        T+1 规则：建仓当日只累计持仓，不判出场（A股当日买不能卖）
        收盘价 ≤ 止损 → stop_loss；已启动且收盘 ≤ 移动止盈线 → trailing_stop
        持满 max_hold_days 个交易日 → max_hold_days（按当日收盘了结）
· 收益：return_pct 基于实际建仓价与实际出场价；max_return/min_return 用持仓期盘中最高低点。
· 持仓时间：hold_tdays = 建仓日到出场日之间的**交易日数**（不含建仓日）；hold_days 为自然日。

⚠ 与 recommend_outcome（出场跟踪面板）的区别：那边跟踪的是**每日推荐**（fusion 打分），
  本表跟踪的是**个股深度深析信号**，两者信号源不同、互不覆盖；出场口径保持一致便于横向对比。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Optional

from config.strategy_params import DEEP_TRACK, get_param

_TABLE = "deep_track"

# 状态机：watching(等回踩) → holding(持仓) → closed(已出场)
#         watching 超期 → expired(未建仓，不计入收益统计)
STATUS_WATCHING = "watching"
STATUS_HOLDING = "holding"
STATUS_CLOSED = "closed"
STATUS_EXPIRED = "expired"

# 出场原因 → 中文（前端直接展示）
EXIT_LABEL = {
    "stop_loss": "止损",
    "trailing_stop": "移动止盈",
    "max_hold_days": "到期了结",
    "manual": "手动平仓",
    "delisted": "数据缺失",
}


# ─────────────────────────────────────────────
# 建表
# ─────────────────────────────────────────────
def ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(f"""
    CREATE TABLE IF NOT EXISTS {_TABLE} (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_date     TEXT NOT NULL,   -- 信号日（= stock_deep_signal.scan_date）
        code          TEXT NOT NULL,
        name          TEXT,
        industry      TEXT,
        level         TEXT,            -- buy / add
        label         TEXT,
        sig_entry     REAL,            -- 参考买点（信号日收盘）
        sig_stop      REAL,            -- 推荐止损
        sig_tp        REAL,            -- 推荐止盈
        status        TEXT NOT NULL DEFAULT '{STATUS_WATCHING}',
        entry_date    TEXT,            -- 实际建仓日
        entry_price   REAL,            -- 实际建仓价
        exit_date     TEXT,
        exit_price    REAL,
        exit_reason   TEXT,
        hold_days     INTEGER,         -- 持仓自然日
        hold_tdays    INTEGER,         -- 持仓交易日（不含建仓日）
        return_pct    REAL,            -- 收益率 %（基于实际建仓价）
        max_return    REAL,            -- 持仓期最大浮盈 %
        min_return    REAL,            -- 持仓期最大浮亏 %
        last_price    REAL,            -- 持仓中的最新收盘（浮盈展示）
        last_date     TEXT,
        reasons       TEXT,            -- JSON 数组，推荐理由
        created_at    TEXT,
        updated_at    TEXT,
        UNIQUE (scan_date, code)
    );
    CREATE INDEX IF NOT EXISTS idx_dt_status ON {_TABLE}(status);
    CREATE INDEX IF NOT EXISTS idx_dt_code   ON {_TABLE}(code);
    CREATE INDEX IF NOT EXISTS idx_dt_scan   ON {_TABLE}(scan_date);
    """)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _reasons_to_json(v) -> str:
    """推荐理由 → JSON 字符串。

    stock_deep_signal.reasons 存的已经是 JSON 字符串，这里若再 dumps 一次会变成双重编码
    （前端拿到的是字符串而不是数组）。故先判断：已是 JSON 数组的原样透传。
    """
    if v is None:
        return "[]"
    if isinstance(v, (list, tuple)):
        return json.dumps(list(v), ensure_ascii=False)
    s = str(v).strip()
    if s.startswith("["):
        return s
    return json.dumps([s], ensure_ascii=False)


def _reasons_load(v) -> list:
    """解析 reasons 字段，兼容历史双重编码的数据。"""
    if isinstance(v, (list, tuple)):
        return list(v)
    if not v:
        return []
    try:
        out = json.loads(v)
    except Exception:
        return []
    # 双重编码（旧数据）：解出来还是 JSON 字符串，再解一次
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except Exception:
            return []
    return out if isinstance(out, list) else []


# ─────────────────────────────────────────────
# 参数（可被 DEEP_TRACK 覆盖；留空则跟随短线全局参数）
# ─────────────────────────────────────────────
def _p(key: str, fallback_key: str, default):
    v = DEEP_TRACK.get(key)
    if v is not None:
        return v
    try:
        v = get_param(fallback_key)
    except Exception:
        v = None
    return default if v is None else v


def _entry_window() -> int:
    return int(_p("entry_window_days", "entry_window_days", 5) or 5)


def _max_hold() -> int:
    return int(_p("max_hold_days", "short_max_hold_days", 10) or 10)


def _stop_pct() -> float:
    """负数，如 -0.06"""
    v = _p("stop_loss_pct", "short_stop_loss", -0.06)
    v = float(v)
    return v if v < 0 else -abs(v)


def _tp_pct() -> float:
    """移动止盈启动线比例，如 0.08"""
    return float(_p("take_profit_pct", "short_take_profit", 0.08) or 0.08)


def _trailing_pct() -> float:
    """移动止盈回撤比例，如 0.03"""
    return float(_p("trailing_pct", "short_trailing_pct", 0.03) or 0.03)


def _rank_order_by(conn) -> str:
    """候选排名键：统一委托给 stock_deep.candidate_order_by（该表的所有者）。

    延迟 import 是为了不让 deep_tracker 承担 pandas 的导入开销 —— 只有真正同步候选
    （此刻必然要连库跑扫描）时才加载 stock_deep。
    排序键本身的口径与评估依据见 stock_deep.candidate_order_by 的文档。
    """
    from strategy.stock_deep import candidate_order_by
    return candidate_order_by(conn)


def _daily_top_n() -> Optional[int]:
    """每日最多跟踪几只候选（None=不限）。

    为什么要限：全市场深析扫描每天能筛出 1000+ 只 buy/add 候选，全部建跟踪单既不可读
    （用户看不过来），也不符实盘（资金不可能同时买 1000 只）。这里按「信号级别 + 不追高」
    排序取头部，等价于"每天只盯最值得买的几只"。
    """
    v = DEEP_TRACK.get("daily_top_n", 10)
    return None if v in (None, 0, "") else int(v)


# ─────────────────────────────────────────────
# 1. 从每日扫描结果同步候选
# ─────────────────────────────────────────────
def sync_from_scan(conn: sqlite3.Connection, scan_date: Optional[str] = None) -> dict:
    """把最近一次（或指定）全市场深析扫描的候选写成跟踪单。

    已存在同 (scan_date, code) 的记录跳过，重跑安全。
    返回 { scan_date, added, skipped, candidates }。
    """
    ensure_table(conn)
    if not scan_date:
        row = conn.execute(
            "SELECT MAX(scan_date) AS d FROM stock_deep_signal").fetchone()
        scan_date = row["d"] if row else None
    if not scan_date:
        return {"scan_date": None, "added": 0, "skipped": 0, "candidates": 0}

    # 排序取头部：见 _rank_order_by 的评估依据（级别 → 强信号档 → 贴 MA20 → 代码稳定序）
    # 板块过滤与面板 1 的 get_market_signal_latest 同一条件 —— 两边必须一致，
    # 否则「明日买入候选」看到的和「推荐跟踪」建单的是两批票。
    top_n = _daily_top_n()
    from strategy.stock_deep import candidate_board_filter
    sql = f"""SELECT * FROM stock_deep_signal WHERE scan_date = ? {candidate_board_filter()}
              ORDER BY {_rank_order_by(conn)}"""
    if top_n:
        sql += f" LIMIT {int(top_n)}"
    rows = conn.execute(sql, (scan_date,)).fetchall()
    now = _now()
    added = skipped = 0
    for r in rows:
        d = dict(r)
        # 没有可执行买点的候选不跟踪（无止损位就无法定义卖点）
        if d.get("entry_price") is None:
            skipped += 1
            continue
        cur = conn.execute(
            f"SELECT id FROM {_TABLE} WHERE scan_date = ? AND code = ?",
            (scan_date, d["code"]),
        ).fetchone()
        if cur:
            skipped += 1
            continue
        conn.execute(
            f"""INSERT INTO {_TABLE}
                (scan_date, code, name, industry, level, label,
                 sig_entry, sig_stop, sig_tp, status, reasons, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (scan_date, d["code"], d.get("name"), d.get("industry"),
             d.get("level"), d.get("label"),
             d.get("entry_price"), d.get("stop_loss"), d.get("take_profit"),
             STATUS_WATCHING,
             _reasons_to_json(d.get("reasons")),
             now, now),
        )
        added += 1
    conn.commit()
    return {"scan_date": scan_date, "added": added, "skipped": skipped,
            "candidates": len(rows)}


# ─────────────────────────────────────────────
# 2. 推进未了结的跟踪单
# ─────────────────────────────────────────────
def _prices_after(conn, code: str, start_date: str, limit: int = 60):
    """取 start_date 之后（不含）的交易日行情，升序。"""
    return conn.execute(
        """SELECT trade_date, open, high, low, close
           FROM daily_price
           WHERE code = ? AND trade_date > ?
           ORDER BY trade_date ASC LIMIT ?""",
        (code, start_date, limit),
    ).fetchall()


def _prices_from(conn, code: str, start_date: str, limit: int = 60):
    """取 start_date 起（含）的交易日行情，升序。

    出场推进必须用"含建仓日"的序列：建仓当日是 T+1 买入日（当日不能卖），
    序列 j=1 就是建仓日，跳过它才等价于"A 股 T+1 当日不判出场"。
    若用"建仓日之后"的序列，j=1 会变成建仓次日，T+1 规则会误跳过一个交易日。
    """
    return conn.execute(
        """SELECT trade_date, open, high, low, close
           FROM daily_price
           WHERE code = ? AND trade_date >= ?
           ORDER BY trade_date ASC LIMIT ?""",
        (code, start_date, limit),
    ).fetchall()


def _last_market_date(conn) -> Optional[str]:
    """全市场最新交易日（用于区分"还没到下一个交易日"与"真的停牌/退市"）。"""
    row = conn.execute("SELECT MAX(trade_date) AS d FROM daily_price").fetchone()
    return row["d"] if row else None


def _count_tdays(conn, code: str, d0: str, d1: str) -> int:
    """d0（不含）到 d1（含）之间的交易日数。"""
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM daily_price WHERE code = ? AND trade_date > ? AND trade_date <= ?",
        (code, d0, d1),
    ).fetchone()
    return int(row["c"]) if row else 0


def _try_fill(conn, t: dict) -> Optional[str]:
    """watching → holding：窗口内回踩买点即成交；超期判 expired。

    返回新状态（holding / expired），无变化返回 None。
    """
    code, scan_date = t["code"], t["scan_date"]
    sig_entry = t["sig_entry"]
    if not sig_entry or sig_entry <= 0:
        return None
    prices = _prices_after(conn, code, scan_date, limit=_entry_window() + 2)
    if not prices:
        return None
    now = _now()
    for idx, p in enumerate(prices):
        if idx >= _entry_window():
            break
        low = p["low"]
        open_ = p["open"]
        if low is None:
            continue
        # 触及买点：开盘已低于买点按开盘价成交，否则按买点成交（回踩成交）
        if low <= sig_entry:
            entry_px = round(min(sig_entry, open_ if open_ else sig_entry), 2)
            conn.execute(
                f"""UPDATE {_TABLE} SET status=?, entry_date=?, entry_price=?,
                    last_price=?, last_date=?, updated_at=? WHERE id=?""",
                (STATUS_HOLDING, p["trade_date"], entry_px,
                 p["close"], p["trade_date"], now, t["id"]),
            )
            return STATUS_HOLDING
    # 走完窗口仍未回踩 → 失效
    if len(prices) >= _entry_window():
        conn.execute(
            f"UPDATE {_TABLE} SET status=?, updated_at=? WHERE id=?",
            (STATUS_EXPIRED, now, t["id"]),
        )
        return STATUS_EXPIRED
    return None


def _run_exit(conn, t: dict) -> Optional[dict]:
    """holding → closed：按止损 / 移动止盈 / 持仓上限出场并结算。

    返回出场结果 dict，未出场返回 None（已更新浮盈）。
    """
    code = t["code"]
    entry_date = t["entry_date"]
    exec_entry = t["entry_price"]
    if not entry_date or not exec_entry or exec_entry <= 0:
        return None

    prices = _prices_from(conn, code, entry_date, limit=_max_hold() + 5)
    if len(prices) < 2:
        # 除建仓日外没有任何后续行情。两种可能：
        #   ① 建仓日就是全市场最新交易日（T+1 还没到）→ 保持 holding，等下一交易日推进
        #   ② 停牌/退市，全市场已往前走了它却没数据 → 判数据缺失，按建仓价平掉不计盈亏
        lmd = _last_market_date(conn)
        if lmd and lmd <= entry_date:
            return None
        conn.execute(
            f"""UPDATE {_TABLE} SET status=?, exit_date=?, exit_price=?, exit_reason=?,
                hold_days=0, hold_tdays=0, return_pct=0, updated_at=? WHERE id=?""",
            (STATUS_CLOSED, entry_date, exec_entry, "delisted", _now(), t["id"]),
        )
        return {"id": t["id"], "exit_reason": "delisted"}

    # 止损价：推荐自带优先，缺失回退参数比例
    stop = t["sig_stop"]
    if not stop or stop <= 0:
        stop = exec_entry * (1 + _stop_pct())
    # 启动线：参数比例优先（基于实际建仓价），回退推荐自带止盈
    tp_ratio = _tp_pct()
    launch = exec_entry * (1 + tp_ratio) if 0 < tp_ratio < 1.0 else None
    if not launch and t["sig_tp"] and t["sig_tp"] > 0:
        launch = float(t["sig_tp"])
    trail = _trailing_pct()
    trail_ok = trail is not None and 0.01 <= trail < 1.0
    max_hold = _max_hold()

    highest = None
    tline = None
    launched = False
    now = _now()

    for j, p in enumerate(prices[:max_hold], start=1):
        close = p["close"]
        if not close or close <= 0:
            break
        high = p["high"] if p["high"] else close
        low = p["low"] if p["low"] else close
        if highest is None or high > highest:
            highest = high
        if not launched and launch and highest >= launch:
            launched = True
        if launched and trail_ok:
            line = highest * (1 - trail)
            if tline is None or line > tline:
                tline = line

        # 持仓期浮盈/浮亏（每笔都要记，无论最终如何出场）
        mr = (high - exec_entry) / exec_entry * 100
        nr = (low - exec_entry) / exec_entry * 100
        t["_max_ret"] = mr if t.get("_max_ret") is None else max(t["_max_ret"], mr)
        t["_min_ret"] = nr if t.get("_min_ret") is None else min(t["_min_ret"], nr)

        # T+1：建仓当日不判出场
        if j == 1:
            continue

        exit_reason = None
        if close <= stop:
            exit_reason = "stop_loss"
        elif launched and tline is not None and close <= tline:
            exit_reason = "trailing_stop"
        elif j >= max_hold:
            exit_reason = "max_hold_days"

        if exit_reason:
            return _close(conn, t, p["trade_date"], close, exit_reason)

    # 未触发任何出场条件：更新浮盈，继续持有
    last = prices[min(len(prices), max_hold) - 1]
    mr = t.get("_max_ret")
    nr = t.get("_min_ret")
    conn.execute(
        f"""UPDATE {_TABLE} SET last_price=?, last_date=?, max_return=?, min_return=?, updated_at=?
            WHERE id=?""",
        (last["close"], last["trade_date"],
         round(mr, 2) if mr is not None else None,
         round(nr, 2) if nr is not None else None,
         now, t["id"]),
    )
    return None


def _close(conn, t: dict, exit_date: str, exit_price: float, reason: str) -> dict:
    """结算一笔：写出场价/原因/持仓天数/收益。"""
    exec_entry = float(t["entry_price"])
    ret = round((exit_price - exec_entry) / exec_entry * 100, 2)
    # 持仓自然日
    try:
        d0 = datetime.strptime(str(t["entry_date"])[:10], "%Y-%m-%d")
        d1 = datetime.strptime(str(exit_date)[:10], "%Y-%m-%d")
        hold_days = max(0, (d1 - d0).days)
    except Exception:
        hold_days = None
    hold_tdays = _count_tdays(conn, t["code"], str(t["entry_date"])[:10], str(exit_date)[:10])
    mr = t.get("_max_ret")
    nr = t.get("_min_ret")
    conn.execute(
        f"""UPDATE {_TABLE}
            SET status=?, exit_date=?, exit_price=?, exit_reason=?,
                hold_days=?, hold_tdays=?, return_pct=?, max_return=?, min_return=?,
                last_price=?, last_date=?, updated_at=?
            WHERE id=?""",
        (STATUS_CLOSED, exit_date, round(exit_price, 2), reason,
         hold_days, hold_tdays, ret,
         round(mr, 2) if mr is not None else None,
         round(nr, 2) if nr is not None else None,
         round(exit_price, 2), exit_date, _now(), t["id"]),
    )
    return {"id": t["id"], "code": t["code"], "name": t["name"],
            "exit_reason": reason, "exit_date": exit_date,
            "return_pct": ret, "hold_tdays": hold_tdays}


def update_open_tracks(conn: sqlite3.Connection, limit: int = 2000) -> dict:
    """推进所有 watching / holding 的跟踪单。返回 { filled, expired, closed, holding, watching }。"""
    ensure_table(conn)
    rows = conn.execute(
        f"""SELECT * FROM {_TABLE}
            WHERE status IN (?, ?)
            ORDER BY scan_date ASC, code ASC LIMIT ?""",
        (STATUS_WATCHING, STATUS_HOLDING, limit),
    ).fetchall()

    filled = expired = closed = 0
    errors: list = []
    for r in rows:
        t = dict(r)
        try:
            if t["status"] == STATUS_WATCHING:
                st = _try_fill(conn, t)
                if st == STATUS_HOLDING:
                    filled += 1
                    t["status"] = STATUS_HOLDING
                    row = conn.execute(
                        f"SELECT entry_date, entry_price FROM {_TABLE} WHERE id=?", (t["id"],)).fetchone()
                    if row:
                        t["entry_date"] = row["entry_date"]
                        t["entry_price"] = row["entry_price"]
                    _run_exit(conn, t)  # 建仓当日只累计，通常返回 None
                elif st == STATUS_EXPIRED:
                    expired += 1
            else:
                res = _run_exit(conn, t)
                if res:
                    closed += 1
        except Exception as e:
            # 单笔失败不能中断整体推进，但必须计数并留样本，否则出错时前端只会看到
            # “建仓 0 笔 / 出场 0 笔”，无法区分是没到条件还是代码抛异常了。
            if len(errors) < 5:
                errors.append(f"{t.get('code')}#{t.get('id')}: {e}")
            continue
    conn.commit()

    cnt = conn.execute(
        f"""SELECT
              SUM(CASE WHEN status='holding'  THEN 1 ELSE 0 END) AS holding,
              SUM(CASE WHEN status='watching' THEN 1 ELSE 0 END) AS watching
            FROM {_TABLE}""").fetchone()
    out = {
        "filled": filled, "expired": expired, "closed": closed,
        "holding": int(cnt["holding"] or 0), "watching": int(cnt["watching"] or 0),
        "errors": len(errors),
    }
    if errors:
        out["error_samples"] = errors
    return out


# ─────────────────────────────────────────────
# 3. 查询与统计
# ─────────────────────────────────────────────
def _row_to_item(d: dict) -> dict:
    d["reasons"] = _reasons_load(d.get("reasons"))
    d["exit_reason_label"] = EXIT_LABEL.get(d.get("exit_reason"), d.get("exit_reason") or "")
    d["status_label"] = {
        STATUS_WATCHING: "待回踩",
        STATUS_HOLDING: "持仓中",
        STATUS_CLOSED: "已出场",
        STATUS_EXPIRED: "已失效",
    }.get(d.get("status"), d.get("status"))
    # 持仓中的浮动盈亏
    if d.get("status") == STATUS_HOLDING and d.get("entry_price") and d.get("last_price"):
        d["float_return"] = round(
            (d["last_price"] - d["entry_price"]) / d["entry_price"] * 100, 2)
    else:
        d["float_return"] = None
    # 盈亏比（计划）：(止盈-入场)/(入场-止损)
    ep, sl, tp = d.get("sig_entry"), d.get("sig_stop"), d.get("sig_tp")
    if ep and sl and tp and ep > sl:
        d["plan_rr"] = round((tp - ep) / (ep - sl), 2)
    else:
        d["plan_rr"] = None
    return d


def get_tracks(conn: sqlite3.Connection, status: Optional[str] = None,
               limit: int = 100, offset: int = 0) -> list:
    """读取跟踪列表。

    status 支持逗号分隔的多个状态（如 "holding,closed"），空=全部。
    排序固定按 **推荐日（scan_date）从近到远** —— 用户要的是"最近推荐的先看"，
    不再按状态分组（旧版 holding 优先会让新推荐的票被压到后面）。
    """
    ensure_table(conn)
    where, args = "", []
    if status:
        keys = [s.strip() for s in str(status).split(",") if s.strip()]
        if keys:
            where = "WHERE status IN ({})".format(",".join("?" * len(keys)))
            args.extend(keys)
    rows = conn.execute(
        f"""SELECT * FROM {_TABLE} {where}
            ORDER BY scan_date DESC, code ASC
            LIMIT ? OFFSET ?""",
        (*args, limit, offset),
    ).fetchall()
    return [_row_to_item(dict(r)) for r in rows]


def get_stats(conn: sqlite3.Connection, days: Optional[int] = None) -> dict:
    """汇总统计：仅 status=closed 的笔计入胜率/收益（未建仓的 expired 不计）。

    days 限制按 scan_date 回溯的天数（None=全部）。
    """
    ensure_table(conn)
    where, args = "WHERE status = ?", [STATUS_CLOSED]
    if days:
        where += " AND scan_date >= date('now', ?)"
        args.append(f"-{int(days)} days")

    rows = conn.execute(
        f"""SELECT return_pct, hold_tdays, hold_days, exit_reason, max_return, min_return
            FROM {_TABLE} {where}""", (*args,)
    ).fetchall()
    closed = [dict(r) for r in rows]

    n = len(closed)
    rets = [r["return_pct"] for r in closed if r["return_pct"] is not None]
    wins = [x for x in rets if x > 0]
    losses = [x for x in rets if x <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    tdays = [r["hold_tdays"] for r in closed if r["hold_tdays"] is not None]
    ndays = [r["hold_days"] for r in closed if r["hold_days"] is not None]

    by_reason: dict = {}
    for r in closed:
        k = r.get("exit_reason") or "unknown"
        by_reason[k] = by_reason.get(k, 0) + 1

    cnt = conn.execute(
        f"""SELECT
              SUM(CASE WHEN status='watching' THEN 1 ELSE 0 END) AS watching,
              SUM(CASE WHEN status='holding'  THEN 1 ELSE 0 END) AS holding,
              SUM(CASE WHEN status='closed'   THEN 1 ELSE 0 END) AS closed,
              SUM(CASE WHEN status='expired'  THEN 1 ELSE 0 END) AS expired
            FROM {_TABLE}""").fetchone()

    return {
        "total_closed": n,
        "watching": int(cnt["watching"] or 0),
        "holding": int(cnt["holding"] or 0),
        "closed": int(cnt["closed"] or 0),
        "expired": int(cnt["expired"] or 0),
        "win_rate": round(len(wins) * 100 / len(rets), 2) if rets else None,
        "avg_return": round(sum(rets) / len(rets), 2) if rets else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (
            None if not gross_win else 999.0),
        "cum_return": round(sum(rets), 2) if rets else None,
        "best": round(max(rets), 2) if rets else None,
        "worst": round(min(rets), 2) if rets else None,
        "avg_hold_tdays": round(sum(tdays) / len(tdays), 1) if tdays else None,
        "avg_hold_days": round(sum(ndays) / len(ndays), 1) if ndays else None,
        "max_hold_tdays": max(tdays) if tdays else None,
        "min_hold_tdays": min(tdays) if tdays else None,
        "by_reason": {EXIT_LABEL.get(k, k): v for k, v in sorted(
            by_reason.items(), key=lambda kv: -kv[1])},
        "params": {
            "entry_window_days": _entry_window(),
            "max_hold_days": _max_hold(),
            "stop_loss_pct": round(_stop_pct(), 4),
            "take_profit_pct": round(_tp_pct(), 4),
            "trailing_pct": round(_trailing_pct(), 4),
        },
    }


# ─────────────────────────────────────────────
# 4. 手动操作
# ─────────────────────────────────────────────
def close_track(conn: sqlite3.Connection, tid: int,
                price: Optional[float] = None, reason: str = "manual") -> dict:
    """手动平仓：未指定价格则用最新收盘。仅 holding 可平（watching 用 delete 或直接平）。"""
    t = conn.execute(f"SELECT * FROM {_TABLE} WHERE id = ?", (tid,)).fetchone()
    if not t:
        raise ValueError("跟踪单不存在")
    t = dict(t)
    if t["status"] == STATUS_CLOSED:
        raise ValueError("该笔已出场")

    if t["status"] == STATUS_WATCHING:
        # 未建仓：按最新收盘价记账建仓+平仓（用户想强制了结）
        last = conn.execute(
            "SELECT trade_date, close FROM daily_price WHERE code=? ORDER BY trade_date DESC LIMIT 1",
            (t["code"],)).fetchone()
        if not last:
            raise ValueError("无行情数据，无法平仓")
        conn.execute(
            f"UPDATE {_TABLE} SET entry_date=?, entry_price=?, status=? WHERE id=?",
            (last["trade_date"], last["close"], STATUS_HOLDING, tid))
        t["entry_date"] = last["trade_date"]
        t["entry_price"] = last["close"]
        t["status"] = STATUS_HOLDING

    if price is None:
        last = conn.execute(
            "SELECT trade_date, close FROM daily_price WHERE code=? ORDER BY trade_date DESC LIMIT 1",
            (t["code"],)).fetchone()
        if not last:
            raise ValueError("无行情数据，无法平仓")
        price, exit_date = last["close"], last["trade_date"]
    else:
        exit_date = conn.execute(
            "SELECT MAX(trade_date) AS d FROM daily_price WHERE code=?", (t["code"],)).fetchone()["d"]
        exit_date = exit_date or datetime.now().strftime("%Y-%m-%d")

    res = _close(conn, t, exit_date, float(price), reason)
    conn.commit()
    return res


def delete_track(conn: sqlite3.Connection, tid: int) -> bool:
    cur = conn.execute(f"DELETE FROM {_TABLE} WHERE id = ?", (tid,))
    conn.commit()
    return cur.rowcount > 0


def refresh(conn: sqlite3.Connection, scan_date: Optional[str] = None) -> dict:
    """一次跑完整流程：同步候选 + 推进未了结。供调度与手动刷新共用。"""
    if not DEEP_TRACK.get("enabled", True):
        return {"enabled": False}
    s = sync_from_scan(conn, scan_date)
    u = update_open_tracks(conn)
    return {"sync": s, "update": u}


def backfill(conn: sqlite3.Connection, days: int = 30) -> dict:
    """历史回补：对最近 days 个 scan_date 依次补建跟踪单并推进到当前。

    用途：首次接入（或停用一段时间）后，让「跟踪列表」立刻有可统计的样本，而不是
    从今天起重新攒。已存在的 (scan_date, code) 会被跳过，重复执行安全。
    """
    ensure_table(conn)
    # 数据源里全部可用的扫描日数量：让用户知道"想补 N 天"的上限在哪，
    # 否则回补 90 天却只补到 6 天时，前端只报 added=0 会让人以为接口坏了。
    avail = conn.execute(
        "SELECT COUNT(DISTINCT scan_date) AS c FROM stock_deep_signal").fetchone()["c"]
    dates = [r["d"] for r in conn.execute(
        "SELECT DISTINCT scan_date AS d FROM stock_deep_signal "
        "ORDER BY scan_date DESC LIMIT ?", (int(days),)).fetchall()]
    dates.reverse()  # 从老到新推进，保证出场顺序正确
    out = {"days": len(dates), "avail_days": int(avail or 0), "requested_days": int(days),
           "added": 0, "skipped": 0, "candidates": 0,
           "filled": 0, "expired": 0, "closed": 0}
    for d in dates:
        try:
            s = sync_from_scan(conn, d)
            out["added"] += s["added"]
            out["skipped"] += s["skipped"]
            out["candidates"] += s["candidates"]
        except Exception:
            continue
    u = update_open_tracks(conn, limit=5000)
    out["filled"] = u["filled"]
    out["expired"] = u["expired"]
    out["closed"] = u["closed"]
    out["holding"] = u["holding"]
    out["watching"] = u["watching"]
    out["errors"] = u.get("errors", 0)
    if u.get("error_samples"):
        out["error_samples"] = u["error_samples"]
    return out
