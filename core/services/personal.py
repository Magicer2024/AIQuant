"""
core/services/personal.py —— 个人持仓 / 自选股 / 风险告警编排（T7 从 routes/investor.py 下沉）
==========================================================================================

纯用户数据 CRUD 与组装（personal_position / personal_trade / personal_watchlist）：
  - 持仓列表（最新价 + 浮动盈亏 + ATR 出场诊断 + 止损预警 + 集中度）
  - 新增 / 减仓平仓 / 删除 / 历史流水
  - 自选列表（目标价距离）/ 加入（触发异步补拉）/ 删除
  - 风险告警（止损止盈、集中度、融合分下降、自选目标价）

请求校验失败抛 PersonalRequestError（携带 HTTP 状态），由薄路由转 fail。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from config.strategy_params import get_param
from core.db import get_conn
from utils.serialization import sanitize_numeric


class PersonalRequestError(Exception):
    """请求参数校验失败：msg 直接回前端，status 为 HTTP 状态码。"""

    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.msg = msg
        self.status = status


# ─────────────────────────────────────────────
# 持仓诊断与流水
# ─────────────────────────────────────────────
def diagnose_position(conn, code: str, cost_price, opened_at, horizon: str = "short") -> Optional[dict]:
    """对单条持仓运行 exit_advisor 出场诊断，返回 {status, reason, detail} 或 None。

    entry_price 取持仓成本价，entry_date 取建仓日；异常时降级为 None，不影响持仓主列表。
    三周期统一移动止盈（2026-08 落地）：short 用 short_take_profit 启动线+
    short_trailing_pct 回撤（2026-08-20 方案A：+8% 启动/3% 回撤/持10），
    mid/long 各自启动线+回撤（持仓上限 60 / 不限仓期），
    参数均来自 TUNABLE_PARAMS（DB 可覆盖）。
    """
    if not code or not cost_price or cost_price <= 0 or not opened_at:
        return None
    try:
        from strategy.exit_advisor import (evaluate_exit, get_max_hold,
                                           atr_dynamic_stop_pct, LIVE_MID_TRAILING_PCT)
        import pandas as pd
        rows = conn.execute(
            """
            SELECT trade_date, close, high, low
            FROM daily_price WHERE code = ?
            ORDER BY trade_date ASC
            """,
            (code,),
        ).fetchall()
        if not rows:
            return None
        df = pd.DataFrame([dict(r) for r in rows]).set_index("trade_date")
        kwargs = {}
        hz = horizon or "short"
        stop_mode, atr_pct = "fixed", None
        if hz == "short":
            # 短线：止损 = ATR 自适应，**每天用最新交易日的 ATR14 重算**（2026-09-14 落地）。
            # 与推荐线同口径：pct = clamp(k×ATR14%, floor, cap)，锚定成本价；
            # ATR 不可用（开关关闭/数据不足）时回退固定 short_stop_loss，绝不落成 0。
            # 移动止盈参数与推荐出场同口径（TUNABLE_PARAMS，DB 可覆盖）
            atr_pct = atr_dynamic_stop_pct(df)
            if atr_pct is not None:
                stop_mode = "atr"
            kwargs = dict(
                stop_loss_pct=(atr_pct if atr_pct is not None
                               else get_param("short_stop_loss")),
                # 动态止损 → 硬止损只按当前收盘判定（否则波动率下行时会假阳性）
                stop_is_dynamic=(atr_pct is not None),
                partial_tp=get_param("short_take_profit"),
                trailing_pct=get_param("short_trailing_pct"),
                max_hold_days=int(get_param("short_max_hold_days")),
            )
        else:
            # 中/长线：各自移动止盈启动线/回撤（与推荐出场同口径）+ 各自周期上限。
            # 止损不传 → evaluate_exit 默认 DEFAULT_STOP_LOSS=-6%，与 short_stop_loss 同值
            # （无独立 mid/long 止损参数；持仓诊断按成本价比例止损，与信号自带 ATR 止损价脱钩）
            kwargs = dict(
                partial_tp=get_param("mid_partial_tp") if hz == "mid" else get_param("long_partial_tp"),
                # ⚠ 中线此处用 LIVE_MID_TRAILING_PCT(0.10) 而**非** mid_trailing_pct(0.05)：
                # 本链路走 evaluate_exit（快照版，移动止盈无启动线），语义与推荐出场跟踪的
                # evaluate_exit_by_prices（需先浮盈达标才启动）不同，详见 exit_advisor 常量注释。
                trailing_pct=(LIVE_MID_TRAILING_PCT if hz == "mid"
                              else get_param("long_trailing_pct")),
                max_hold_days=get_max_hold(hz),
            )
        res = evaluate_exit(
            entry_price=float(cost_price),
            entry_date=str(opened_at)[:10],
            df=df,
            **kwargs,
        )
        # 标注止损来源，供前端区分「ATR 动态」与「固定比例」——
        # 否则用户看到止损价变化会以为是 bug（实际是波动率变了）。
        if isinstance(res, dict) and isinstance(res.get("detail"), dict):
            res["detail"]["stop_mode"] = stop_mode
            res["detail"]["stop_loss_pct"] = (
                atr_pct if atr_pct is not None
                else (kwargs.get("stop_loss_pct")))
        return res
    except Exception:
        return None


def _record_trade(conn, pos, action: str, shares: int, price: float, traded_at: str) -> dict:
    """写入一条卖出流水（减仓/平仓），返回 {pnl, pnl_pct}（元 / %）。

    收益率按持仓成本价 cost_price 计算：pnl_pct = (price - cost_price) / cost_price。
    """
    cost_price = float(pos["cost_price"] or 0)
    pnl = (price - cost_price) * shares if cost_price else 0.0
    pnl_pct = ((price - cost_price) / cost_price * 100) if cost_price else 0.0
    conn.execute(
        """
        INSERT INTO personal_trade
          (position_id, code, name, action, shares, price, cost_price, pnl, pnl_pct, traded_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (pos["id"], pos["code"], pos["name"], action, shares, price, cost_price,
         round(pnl, 2), round(pnl_pct, 2), traded_at, traded_at),
    )
    return {"pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2)}


# ─────────────────────────────────────────────
# 持仓 CRUD
# ─────────────────────────────────────────────
def list_positions_payload() -> dict:
    """列出当前持仓（含最新价 + 浮动盈亏 + 出场诊断）。"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT p.*,
                   lp.close AS latest_close,
                   lp.pct_change AS latest_pct
            FROM personal_position p
            LEFT JOIN latest_price lp ON lp.code = p.code
            WHERE p.status = 'holding'
            ORDER BY p.opened_at DESC
            """
        ).fetchall()

        items = []
        total_cost = 0.0
        total_value = 0.0
        for r in rows:
            d = dict(r)
            cost = (d.get("cost_price") or 0) * (d.get("shares") or 0)
            value = (d.get("latest_close") or 0) * (d.get("shares") or 0)
            pnl = value - cost if d.get("latest_close") else 0
            pnl_pct = (pnl / cost * 100) if cost > 0 else 0

            # 出场诊断（清仓/减仓/持有）——复用推荐票出场纪律（按持仓周期选参数）
            advice = diagnose_position(conn, d.get("code"),
                                       d.get("cost_price"), d.get("opened_at"),
                                       d.get("horizon") or "short")

            # 是否触发止损/止盈预警。止损价优先级：**用户手填 → ATR 动态**（每日重算）。
            # ATR 动态止损不落库（每天都变），从诊断 detail 取 —— 与诊断结论同源，
            # 否则会出现"预警说没破、诊断说已止损"的自相矛盾。
            warning = None
            sl = d.get("stop_loss")
            tp = d.get("take_profit")
            stop_eff, stop_src = sl, ("manual" if sl else None)
            if not stop_eff and advice:
                _det = advice.get("detail") or {}
                if _det.get("stop_price"):
                    stop_eff = _det["stop_price"]
                    stop_src = _det.get("stop_mode") or "atr"
            if d.get("latest_close") and stop_eff and d["latest_close"] <= stop_eff:
                _src_txt = "手填" if stop_src == "manual" else "ATR动态"
                warning = f"已触及止损价 {stop_eff:.2f}（{_src_txt}），请考虑止损"
            elif d.get("latest_close") and tp and d["latest_close"] >= tp:
                warning = "已触及止盈价，可考虑止盈"

            total_cost += cost
            total_value += value
            items.append({
                **{k: d.get(k) for k in (
                    "id", "code", "name", "shares", "cost_price",
                    "stop_loss", "take_profit", "note", "opened_at", "horizon"
                )},
                "latest_close": d.get("latest_close"),
                "latest_pct": d.get("latest_pct"),
                "cost": round(cost, 2),
                "value": round(value, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
                "warning": warning,
                # 生效止损价与来源（manual=用户手填 / atr=每日动态 / None=未取到）
                "stop_eff": round(float(stop_eff), 2) if stop_eff else None,
                "stop_source": stop_src,
                "advice": advice,
            })

        # 持仓集中度
        concentration = None
        if total_value > 0:
            max_single = max((it["value"] for it in items), default=0)
            concentration = round(max_single / total_value * 100, 1)

        return {
            "positions": sanitize_numeric(items),
            "summary": {
                "total_cost": round(total_cost, 2),
                "total_value": round(total_value, 2),
                "total_pnl": round(total_value - total_cost, 2),
                "total_pnl_pct": round((total_value - total_cost) / total_cost * 100, 2)
                                  if total_cost > 0 else 0,
                "position_count": len(items),
                "max_concentration_pct": concentration,
            },
        }


def add_position(body: dict) -> dict:
    """新增持仓 {code, shares, cost_price, stop_loss?, take_profit?, note?, horizon?}。"""
    code = (body.get("code") or "").strip()
    if not code or not body.get("shares") or not body.get("cost_price"):
        raise PersonalRequestError("缺少 code/shares/cost_price")

    try:
        shares = int(body["shares"])
        cost_price = float(body["cost_price"])
    except (ValueError, TypeError):
        raise PersonalRequestError("shares/cost_price 必须是数字")

    stop_loss = body.get("stop_loss")
    take_profit = body.get("take_profit")
    # 持仓周期（决定出场诊断参数：短线=移动止盈 TUNABLE_PARAMS，中/长线=默认纪律）
    horizon = (body.get("horizon") or "short").strip().lower()
    if horizon not in ("short", "mid", "long"):
        raise PersonalRequestError("horizon 仅支持 short / mid / long")

    with get_conn() as conn:
        # 自动补全股票名
        info = conn.execute(
            "SELECT name FROM stock_info WHERE code = ?", (code,)
        ).fetchone()
        name = info["name"] if info else body.get("name", code)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        conn.execute(
            """
            INSERT OR REPLACE INTO personal_position
              (code, name, shares, cost_price, stop_loss, take_profit,
               note, opened_at, status, created_at, updated_at, horizon)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'holding', ?, ?, ?)
            """,
            (code, name, shares, cost_price,
             float(stop_loss) if stop_loss else None,
             float(take_profit) if take_profit else None,
             body.get("note"), now, now, now, horizon),
        )
        new_id = conn.execute(
            "SELECT id FROM personal_position WHERE code=? AND opened_at=?",
            (code, now),
        ).fetchone()
        return {"id": new_id["id"] if new_id else None, "code": code, "name": name}


def reduce_position(pid: int, body: dict) -> dict:
    """减仓 / 平仓卖出：{shares, price}。

    shares < 剩余股数 → 减仓（部分卖出）；shares >= 剩余股数 → 平仓。
    每次卖出写一条 personal_trade 流水，用于历史持仓收益率统计。
    """
    try:
        shares = int(body.get("shares") or 0)
        price = float(body.get("price") or 0)
    except (ValueError, TypeError):
        raise PersonalRequestError("shares/price 必须是数字")
    if shares <= 0 or price <= 0:
        raise PersonalRequestError("请填写卖出的股数与价格")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        pos = conn.execute(
            "SELECT * FROM personal_position WHERE id = ?", (pid,)
        ).fetchone()
        if not pos or pos["status"] != "holding":
            raise PersonalRequestError("持仓不存在或已平仓", 404)

        remaining = int(pos["shares"] or 0)
        sell_shares = min(shares, remaining)
        if sell_shares <= 0:
            raise PersonalRequestError("持仓股数为 0，无法卖出")

        action = "close" if sell_shares >= remaining else "reduce"
        trade = _record_trade(conn, pos, action, sell_shares, price, now)

        new_shares = remaining - sell_shares
        if new_shares <= 0:
            conn.execute(
                "UPDATE personal_position SET shares=0, status='closed', closed_at=?, updated_at=? WHERE id=?",
                (now, now, pid),
            )
        else:
            conn.execute(
                "UPDATE personal_position SET shares=?, updated_at=? WHERE id=?",
                (new_shares, now, pid),
            )

        return {
            "action": action,
            "sold_shares": sell_shares,
            "price": price,
            "pnl": trade["pnl"],
            "pnl_pct": trade["pnl_pct"],
            "remaining_shares": max(new_shares, 0),
        }


def delete_position(pid: int) -> dict:
    """直接删除持仓记录（误录入时使用），并级联清理其卖出流水。

    与「平仓」不同：平仓会保留持仓并按卖出价结算收益；
    删除则是物理移除该条记录（含历史），不进入历史收益率统计。
    """
    with get_conn() as conn:
        pos = conn.execute(
            "SELECT * FROM personal_position WHERE id = ?", (pid,)
        ).fetchone()
        if not pos:
            raise PersonalRequestError("持仓不存在", 404)
        conn.execute("DELETE FROM personal_trade WHERE position_id = ?", (pid,))
        conn.execute("DELETE FROM personal_position WHERE id = ?", (pid,))
        return {"deleted": pid}


def positions_history_payload() -> dict:
    """历史持仓收益率统计：卖出流水（减仓/平仓）逐笔 + 汇总。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM personal_trade ORDER BY traded_at DESC, id DESC"
        ).fetchall()
        items = [dict(r) for r in rows]

        total_pnl = sum(t["pnl"] or 0 for t in items)
        wins = [t for t in items if (t["pnl"] or 0) > 0]
        losses = [t for t in items if (t["pnl"] or 0) < 0]
        win_rate = round(len(wins) / len(items) * 100, 1) if items else 0
        avg_pnl_pct = round(sum(t["pnl_pct"] or 0 for t in items) / len(items), 2) if items else 0
        total_sold = sum((t["shares"] or 0) * (t["price"] or 0) for t in items)

        return {
            "trades": sanitize_numeric(items),
            "summary": {
                "trade_count": len(items),
                "win_count": len(wins),
                "loss_count": len(losses),
                "total_pnl": round(total_pnl, 2),
                "win_rate": win_rate,
                "avg_pnl_pct": avg_pnl_pct,
                "best_pnl_pct": round(max((t["pnl_pct"] or 0 for t in items), default=0), 2),
                "worst_pnl_pct": round(min((t["pnl_pct"] or 0 for t in items), default=0), 2),
                "total_sold_amount": round(total_sold, 2),
            },
        }


# ─────────────────────────────────────────────
# 自选股
# ─────────────────────────────────────────────
def list_watchlist_payload():
    """自选股列表（附带最新价 + 当日打分 + 目标价）。"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT w.id, w.code, w.note, w.created_at,
                   w.target_price, w.alert_dir,
                   i.name, i.industry,
                   lp.close  AS latest_close,
                   lp.pct_change AS latest_pct,
                   lp.fusion_score AS latest_score
            FROM personal_watchlist w
            LEFT JOIN stock_info i   ON i.code = w.code
            LEFT JOIN latest_price lp ON lp.code = w.code
            ORDER BY w.created_at DESC
            """
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            tp = d.get("target_price")
            latest = d.get("latest_close")
            # 距目标价百分比（正=还需上涨，负=已超过）
            if tp and tp > 0 and latest:
                d["target_distance_pct"] = round((tp - latest) / latest * 100, 2)
            else:
                d["target_distance_pct"] = None
            items.append(d)
        return sanitize_numeric(items)


def add_watchlist(body: dict) -> dict:
    """加入自选：写表 + 异步触发该股历史数据补拉（不阻塞响应）。"""
    code = (body.get("code") or "").strip()
    if not code:
        raise PersonalRequestError("缺少 code")
    if not (code.isdigit() and len(code) == 6):
        raise PersonalRequestError("code 格式非法（需 6 位数字）")

    # 可选目标价（价格预警）：target_price + alert_dir(above/below)
    target_price = body.get("target_price")
    try:
        target_price = float(target_price) if target_price not in (None, "") else None
    except (TypeError, ValueError):
        target_price = None
    alert_dir = (body.get("alert_dir") or "above").strip().lower()
    if alert_dir not in ("above", "below"):
        alert_dir = "above"

    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO personal_watchlist(code, note, created_at) VALUES (?,?,?)",
            (code, body.get("note"), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        # 无论新增还是已存在，均允许更新目标价
        if target_price is not None:
            conn.execute(
                "UPDATE personal_watchlist SET target_price=?, alert_dir=? WHERE code=?",
                (target_price, alert_dir, code),
            )

    task_id = None
    sync_status = "already_has_data"
    try:
        from core.repository.price_repo import has_price_data
        from core.task_queue import submit_task, is_any_running
        # 已经在跑其它任务时，仍允许加自选（不阻塞）
        if is_any_running() and not has_price_data(code):
            sync_status = "queued"
        elif not has_price_data(code):
            from core.sync import sync_single_stock_to_watchlist
            task_id = submit_task(lambda: sync_single_stock_to_watchlist(code, verbose=False))
            sync_status = "syncing"
        # else: sync_status = "already_has_data" —— 不用再拉
    except Exception as e:
        # 异步触发失败不阻塞 INSERT 已成功的响应
        sync_status = f"sync_dispatch_failed: {e}"

    return {"code": code, "task_id": task_id, "sync_status": sync_status,
            "message": "已加入自选，历史数据补拉中" if sync_status == "syncing" else "已加入自选"}


def remove_watchlist(code: str) -> dict:
    """移出自选：仅删 personal_watchlist，保留 daily_price 历史（回测/复盘仍需）。"""
    if not (code.isdigit() and len(code) == 6):
        raise PersonalRequestError("code 格式非法")
    with get_conn() as conn:
        conn.execute("DELETE FROM personal_watchlist WHERE code = ?", (code,))
        return {"removed": code, "message": "已移出自选，历史行情数据保留"}


# ─────────────────────────────────────────────
# 风险告警
# ─────────────────────────────────────────────
def _score_drop_alerts(conn):
    """检查持仓票的打分变化，生成打分下降告警。"""
    alerts = []
    # 获取所有持仓票
    positions = conn.execute(
        "SELECT code, name FROM personal_position WHERE status='holding'"
    ).fetchall()

    for pos in positions:
        code = pos["code"]
        name = pos["name"] or code
        # 最近两次打分
        scores = conn.execute(
            """
            SELECT trade_date, fusion_score AS score FROM daily_price
            WHERE code = ?
            ORDER BY trade_date DESC LIMIT 2
            """,
            (code,),
        ).fetchall()

        # 注：融合分 fusion_score 为 0~50 量纲，阈值较原 0~100 打分折半
        if len(scores) >= 2:
            latest_score = scores[0]["score"] or 0
            prev_score = scores[1]["score"] or 0
            drop = prev_score - latest_score
            if drop >= 5:
                alerts.append({
                    "level": "warning",
                    "code": code,
                    "name": name,
                    "msg": f"融合分下降 {drop:.0f} 分（{prev_score:.0f}→{latest_score:.0f}），注意风险",
                })
            elif drop >= 3:
                alerts.append({
                    "level": "info",
                    "code": code,
                    "name": name,
                    "msg": f"融合分小幅回落 {drop:.0f} 分（{prev_score:.0f}→{latest_score:.0f}）",
                })
        elif len(scores) == 1:
            # 只有一次评分，检查是否低于阈值（0~50 量纲）
            s = scores[0]["score"] or 0
            if s < 15:
                alerts.append({
                    "level": "warning",
                    "code": code,
                    "name": name,
                    "msg": f"当前打分仅 {s:.0f} 分，低于安全线",
                })

    return alerts


def risk_alerts_payload() -> dict:
    """当前风险告警（持仓止损止盈 + 集中度 + 打分下降 + 自选目标价）。"""
    with get_conn() as conn:
        positions = conn.execute(
            """
            SELECT p.*, lp.close AS latest_close
            FROM personal_position p
            LEFT JOIN latest_price lp ON lp.code = p.code
            WHERE p.status='holding'
            """
        ).fetchall()

        alerts = []
        for r in positions:
            d = dict(r)
            latest = d.get("latest_close")
            if not latest:
                continue
            # 1) 触及止损
            if d.get("stop_loss") and latest <= d["stop_loss"]:
                alerts.append({
                    "level": "danger",
                    "code": d["code"], "name": d.get("name"),
                    "msg": f"已触及止损价 {d['stop_loss']:.2f}，当前 {latest:.2f}",
                })
            # 2) 接近止损（差 ≤3%）
            elif d.get("stop_loss") and latest <= d["stop_loss"] * 1.03:
                alerts.append({
                    "level": "warning",
                    "code": d["code"], "name": d.get("name"),
                    "msg": f"接近止损价（差 {((latest/d['stop_loss']-1)*100):.1f}%）",
                })
            # 3) 触及止盈
            if d.get("take_profit") and latest >= d["take_profit"]:
                alerts.append({
                    "level": "info",
                    "code": d["code"], "name": d.get("name"),
                    "msg": f"已触及止盈价 {d['take_profit']:.2f}，可考虑止盈",
                })

        # 持仓集中度告警
        rows = conn.execute(
            """
            SELECT p.code, p.shares, lp.close,
                   (p.cost_price * p.shares) AS cost,
                   (lp.close * p.shares)    AS value
            FROM personal_position p
            LEFT JOIN latest_price lp ON lp.code = p.code
            WHERE p.status='holding'
            """
        ).fetchall()
        total = sum((r["value"] or 0) for r in rows)
        if total > 0:
            for r in rows:
                v = r["value"] or 0
                ratio = v / total * 100
                if ratio >= 20:
                    alerts.append({
                        "level": "warning",
                        "code": r["code"], "name": r["code"],
                        "msg": f"单股仓位占比 {ratio:.1f}%，过于集中（建议 <20%）",
                    })

        # 持仓票打分下降告警
        alerts.extend(_score_drop_alerts(conn))

        # 自选股价格预警：目标价触达/接近
        watch_rows = conn.execute(
            """
            SELECT w.code, w.target_price, w.alert_dir,
                   i.name, lp.close AS latest_close
            FROM personal_watchlist w
            LEFT JOIN stock_info i ON i.code = w.code
            LEFT JOIN latest_price lp ON lp.code = w.code
            WHERE w.target_price IS NOT NULL AND w.target_price > 0
            """
        ).fetchall()
        for r in watch_rows:
            d = dict(r)
            latest = d.get("latest_close")
            tp = d.get("target_price")
            if not latest or not tp:
                continue
            direction = (d.get("alert_dir") or "above").lower()
            name = d.get("name") or d["code"]
            gap_pct = (latest - tp) / tp * 100
            if direction == "above":
                if latest >= tp:
                    alerts.append({
                        "level": "info", "code": d["code"], "name": name,
                        "msg": f"自选已达目标价 {tp:.2f}（现价 {latest:.2f}）",
                    })
                elif gap_pct >= -2:
                    alerts.append({
                        "level": "warning", "code": d["code"], "name": name,
                        "msg": f"自选接近目标价 {tp:.2f}（差 {abs(gap_pct):.1f}%）",
                    })
            else:  # below
                if latest <= tp:
                    alerts.append({
                        "level": "info", "code": d["code"], "name": name,
                        "msg": f"自选已跌至目标价 {tp:.2f}（现价 {latest:.2f}）",
                    })
                elif gap_pct <= 2:
                    alerts.append({
                        "level": "warning", "code": d["code"], "name": name,
                        "msg": f"自选接近目标价 {tp:.2f}（差 {abs(gap_pct):.1f}%）",
                    })

        return {"alerts": alerts, "count": len(alerts)}
