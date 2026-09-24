"""
tools/today_review.py —— 今日推荐复盘（信号日 T+0 质量体检）
=============================================================

用途：对「今日 / 指定日期」系统实际推送的全部推荐做一次复盘，产出 HTML 报告。

与「推荐结果闭环」（core/outcome_tracker，T+1 起评估收益）的区别：
  - outcome_tracker = 事后收益归因（需要未来行情）
  - 本工具         = 信号日当天体检（当天即可跑），回答三件事：
      1. 今天到底推了什么（线上 /today 口径，不是全量信号池）
      2. 这批推荐是怎么被选出来的（漏斗：每层闸门淘汰多少）
      3. 这批票今天处在什么位置（追高还是低位、盈亏比、在途往期推荐状态）
    并附上近 60 日同周期已结算样本的历史基准，作为期望值参照。

口径铁律：
  - 「推荐」= routes/investor.py::/today 的返回（三周期 + 各闸门 + regime 天花板），
    不自建筛选逻辑，避免与线上口径漂移。
  - 只读数据库，不写任何表。

用法：
    python tools/today_review.py                  # 复盘最新 signal 日
    python tools/today_review.py --date 2026-09-18
    python tools/today_review.py --days 60        # 历史基准窗口
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "core", "quant.db")
REPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "reports")

HZ_LABEL = {"short": "短线", "mid": "中线", "long": "长线"}


# ─────────────────────────────────────────────
# 数据层
# ─────────────────────────────────────────────

def _conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def latest_signal_date(conn, date_arg: str | None = None) -> str | None:
    if date_arg:
        return date_arg
    r = conn.execute("SELECT MAX(scan_date) d FROM stock_signal").fetchone()
    return r["d"] if r else None


INDEX_NAME = {
    "000001": "上证指数", "399001": "深证成指", "000300": "沪深300",
    "000905": "中证500", "000852": "中证1000", "399006": "创业板指",
}


def fetch_market(conn, d: str) -> dict:
    """大盘背景：指数 + 全市场涨跌分布 + 中位数/均值。"""
    idx = conn.execute("""
        SELECT code, close, pct_change
        FROM index_daily WHERE trade_date = ?
        ORDER BY code
    """, (d,)).fetchall()
    idx = [dict(r, name=INDEX_NAME.get(r["code"], r["code"])) for r in idx]
    # 5 日动量：index_daily 无现成列，按近 6 日收盘现算
    for r in idx:
        hist = conn.execute(
            "SELECT close FROM index_daily WHERE code = ? AND trade_date <= ? "
            "ORDER BY trade_date DESC LIMIT 6", (r["code"], d)).fetchall()
        r["trend_5d_pct"] = (round((hist[0]["close"] / hist[-1]["close"] - 1) * 100, 2)
                             if len(hist) >= 6 and hist[-1]["close"] else None)
    # 指数涨跌存的是百分数还是小数？index_daily 与 daily_price 同为百分数口径
    mkt = conn.execute("""
        SELECT COUNT(*) n, AVG(pct_change) avg_pct,
               SUM(CASE WHEN pct_change > 0 THEN 1 ELSE 0 END) up,
               SUM(CASE WHEN pct_change < 0 THEN 1 ELSE 0 END) dn
        FROM daily_price WHERE trade_date = ?
    """, (d,)).fetchone()
    vals = [r[0] for r in conn.execute(
        "SELECT pct_change FROM daily_price WHERE trade_date = ? "
        "AND pct_change IS NOT NULL ORDER BY pct_change", (d,)).fetchall()]
    med = vals[len(vals) // 2] if vals else None
    n = mkt["n"] or 0
    return {
        "indices": [dict(r) for r in idx],
        "n": n,
        "up": mkt["up"] or 0,
        "dn": mkt["dn"] or 0,
        "avg_pct": round(mkt["avg_pct"], 2) if mkt["avg_pct"] is not None else None,
        "median_pct": round(med, 2) if med is not None else None,
        "up_ratio": round((mkt["up"] or 0) / n * 100, 1) if n else None,
        # 涨停/跌停（A股主板 ±10%，用 9.8% 近似过滤异常价源）
        "limit_up": sum(1 for v in vals if v >= 9.8),
        "limit_dn": sum(1 for v in vals if v <= -9.8),
    }


def fetch_recommendations(date_arg: str | None = None) -> dict:
    """走线上 /today 口径取今日推荐（Flask test_client，避免 qlib 依赖）。"""
    from flask import Flask
    from routes.investor import investor_bp
    app = Flask(__name__)
    app.register_blueprint(investor_bp)
    client = app.test_client()
    q = f"/api/investor/today?limit=4" + (f"&date={date_arg}" if date_arg else "")
    data = client.get(q).get_json()
    return (data or {}).get("data") or {}


def fetch_stock_context(conn, code: str, d: str) -> dict:
    """单票行情上下文：当日 OHLC/量能/近 5·20 日位置/连涨天数。"""
    rows = conn.execute("""
        SELECT trade_date, open, high, low, close, volume, amount, pct_change
        FROM daily_price WHERE code = ? AND trade_date <= ?
        ORDER BY trade_date DESC LIMIT 25
    """, (code, d)).fetchall()
    if not rows:
        return {}
    today = rows[0]
    closes = [r["close"] for r in rows][::-1]      # 升序
    vols = [r["volume"] or 0 for r in rows][::-1]
    highs = [r["high"] or r["close"] for r in rows][::-1]

    def _ret(n):
        return round((closes[-1] / closes[-1 - n] - 1) * 100, 2) if len(closes) > n else None

    vol_ratio = None
    if len(vols) >= 6 and vols[-2:-7:-1] and sum(vols[-6:-1]) > 0:
        vol_ratio = round(vols[-1] / (sum(vols[-6:-1]) / 5), 2)
    high20 = max(highs[-20:]) if len(highs) >= 20 else max(highs)
    # 连涨天数（含当日）
    streak = 0
    for r in rows:
        if (r["pct_change"] or 0) > 0:
            streak += 1
        else:
            break
    return {
        "open": today["open"], "high": today["high"], "low": today["low"],
        "close": today["close"], "pct": today["pct_change"],
        "amount": today["amount"], "vol_ratio": vol_ratio,
        "ret5": _ret(5), "ret10": _ret(10), "ret20": _ret(20),
        "dd_from_high20": round((today["close"] / high20 - 1) * 100, 2) if high20 else None,
        "amplitude": round((today["high"] - today["low"]) / today["low"] * 100, 2)
                     if today["low"] else None,
        "up_streak": streak,
        "trade_date": today["trade_date"],
    }


def fetch_funnel(conn, d: str) -> list:
    """短线名额漏斗：候选池 → 各闸门 → 最终名额（复用线上 SQL 片段，口径同源）。"""
    from config.personal_config import MAIN_BOARD_ONLY, EXCLUDED_BOARD_PREFIXES
    from config.strategy_params import get_param
    from core.outcome_tracker import (short_t1_filter_sql, short_market_gate_sql,
                                      short_observe_bottom_sql)
    gate = float(get_param("short_conf_gate"))
    t1_cond, t1_params = short_t1_filter_sql(conn, d, d)
    mk_cond, mk_params = short_market_gate_sql(conn, d, d)
    ob_cond, ob_params = short_observe_bottom_sql()
    board_filter = ""
    if MAIN_BOARD_ONLY:
        board_filter = "".join(f" AND s.code NOT LIKE '{p}%'" for p in EXCLUDED_BOARD_PREFIXES)
    base = ("FROM stock_signal s WHERE s.scan_date = ? "
            "AND COALESCE(s.horizon,'short')='short' "
            "AND s.buy_price IS NOT NULL AND s.buy_price > 0 "
            "AND s.name NOT LIKE '%ST%' AND s.name NOT LIKE '%退%' "
            "AND COALESCE(s.strategy,'') != '强势突破' "
            "AND COALESCE(s.strategy,'') != '缩量回踩' " + board_filter)

    def _cnt(extra_sql="", params=()):
        return conn.execute(f"SELECT COUNT(*) n {base} {extra_sql}",
                            (d, *params)).fetchone()["n"]

    steps = []
    prev = _cnt()
    steps.append(("短线候选池（非 ST/主板/有名额资格）", prev, ""))
    cur = _cnt(" AND s.fusion_score >= ?", (gate,))
    steps.append((f"融合分门控 ≥ {gate:g}", cur, f"淘汰 {prev - cur}"))
    prev = cur
    cur = _cnt(f" AND s.fusion_score >= ? AND {t1_cond}", (gate, *t1_params))
    steps.append(("T1 辅助过滤（恐慌日闸门+MA5 偏离）", cur, f"淘汰 {prev - cur}"))
    prev = cur
    cur = _cnt(f" AND s.fusion_score >= ? AND {t1_cond} AND {mk_cond}",
               (gate, *t1_params, *mk_params))
    steps.append(("大盘走弱闸门（弱市日仅留隔日动量）", cur, f"淘汰 {prev - cur}"))
    prev = cur
    cur = _cnt(f" AND s.fusion_score >= ? AND {t1_cond} AND {mk_cond} AND {ob_cond}",
               (gate, *t1_params, *mk_params, *ob_params))
    steps.append(("观察线排除（抄底扩展度不达标）", cur, f"淘汰 {prev - cur}"))
    final = max(1, int(get_param("short_top_n")))
    steps.append((f"名额上限 short_top_n = {final}（= 入库复盘口径）", min(cur, final), ""))

    # 展示层再剔除：/today 会用信号灯剔除 avoid（盈亏比<1.5）与 sell（gap guard 追高）
    # ⇒ 入库 3 只不等于展示 3 只。这一层必须显式呈现，否则「漏斗说 3 只、页面 1 只」
    # 会被误读成 bug。
    from flask import Flask
    from routes.investor import investor_bp
    app = Flask(__name__)
    app.register_blueprint(investor_bp)
    shown = client_today_short_count(app, d)
    steps.append(("展示层剔除（avoid 盈亏比<1.5 / sell 追高守卫）→ 页面实际推送",
                  shown, f"淘汰 {min(cur, final) - shown}" if shown is not None else ""))
    return steps


def client_today_short_count(app, d: str) -> int | None:
    """/today 口径下 short 组实际推送条数（走 test_client，与线上同口径）。"""
    try:
        data = app.test_client().get(f"/api/investor/today?date={d}&limit=4").get_json()
        return len(((data or {}).get("data") or {}).get("groups", {}).get("short", []))
    except Exception:
        return None


def fetch_history_baseline(conn, days: int, end_date: str) -> dict:
    """近 N 日已结算推荐的历史基准（按周期），作为今日这批的期望值参照。"""
    from datetime import date as _date, timedelta
    start = (_date.fromisoformat(end_date) - timedelta(days=days)).isoformat()
    out = {}
    for hz in ("short", "mid", "long"):
        rows = conn.execute("""
            SELECT t1_return, t3_return, t5_return, exit_return, exit_reason, hit_stop
            FROM recommend_outcome
            WHERE scan_date >= ? AND scan_date <= ?
              AND COALESCE(horizon,'short') = ?
              AND entry_price > 0
              AND COALESCE(exit_reason,'') != 'no_fill'
        """, (start, end_date, hz)).fetchall()

        def _stat(key):
            vals = [r[key] for r in rows if r[key] is not None]
            if not vals:
                return {"n": 0, "win": 0, "win_rate": None, "avg": None}
            win = sum(1 for v in vals if v > 0)
            return {"n": len(vals), "win": win,
                    "win_rate": round(win / len(vals) * 100, 1),
                    "avg": round(sum(vals) / len(vals), 2)}

        settled = [r["exit_return"] for r in rows if r["exit_return"] is not None]
        # 出场原因拆分：区分「止损砍出来的亏」与「到期/移动止盈了结」——
        # 前者是入场时机问题，后者是持有期问题，两者的修复动作完全不同
        reasons: dict = {}
        for r in rows:
            if r["exit_return"] is None:
                continue
            k = norm_exit_reason(r["exit_reason"])
            reasons[k] = reasons.get(k, 0) + 1
        out[hz] = {
            "total": len(rows),
            "settled": len(settled),
            "reasons": reasons,
            "win_rate": (round(sum(1 for v in settled if v > 0) / len(settled) * 100, 1)
                         if settled else None),
            "avg": round(sum(settled) / len(settled), 2) if settled else None,
            "best": round(max(settled), 2) if settled else None,
            "worst": round(min(settled), 2) if settled else None,
            "stop_rate": (round(sum(1 for r in rows if r["hit_stop"]) / len(rows) * 100, 1)
                          if rows else None),
            "t1": _stat("t1_return"), "t3": _stat("t3_return"), "t5": _stat("t5_return"),
        }
    return out


def fetch_inflight(conn, end_date: str, within: int = 12) -> list:
    """在途推荐：近 within 个自然日内尚未结算（exit_return 为空）的条目 + 当前浮盈。"""
    from datetime import date as _date, timedelta
    start = (_date.fromisoformat(end_date) - timedelta(days=within)).isoformat()
    rows = conn.execute("""
        SELECT o.code, si.name, o.scan_date, o.horizon, o.strategy, o.entry_price,
               o.stop_loss, o.take_profit, o.fusion_score, o.exit_reason,
               lp.close AS last_close, lp.trade_date AS last_date
        FROM recommend_outcome o
        LEFT JOIN stock_info si ON si.code = o.code
        LEFT JOIN latest_price lp ON lp.code = o.code
        WHERE o.scan_date >= ? AND o.scan_date <= ?
          AND o.entry_price > 0
          AND o.exit_return IS NULL
          AND COALESCE(o.exit_reason,'') NOT IN ('no_fill')
        ORDER BY o.scan_date DESC, o.horizon
    """, (start, end_date)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d["last_close"] and d["entry_price"]:
            d["pnl"] = round((d["last_close"] / d["entry_price"] - 1) * 100, 2)
        else:
            d["pnl"] = None
        out.append(d)
    return out


def norm_exit_reason(raw) -> str:
    """出场原因归一化。

    ⚠ short 组写的是枚举（stop_loss / trailing_stop / max_hold_days），
    而 mid/long 走 strategy/exit_advisor，写的是中文长描述且内嵌具体价格
    （"收盘跌破止损价 149.16"、"移动止盈：自高点 43.00 回撤 10.2% ≥ 10%，清仓"）。
    两者不能直接 GROUP BY —— 后者每只票一个唯一串，统计出来全是 1。
    这里统一按语义前缀归并成 4 类。
    """
    s = str(raw or "未标注")
    if s in ("stop_loss", "trailing_stop", "max_hold_days", "take_profit", "no_fill"):
        return {"stop_loss": "止损", "trailing_stop": "移动止盈",
                "max_hold_days": "到期了结", "take_profit": "止盈",
                "no_fill": "未成交"}[s]
    if "止损" in s:
        return "止损"
    if "移动止盈" in s or "回撤" in s:
        return "移动止盈"
    if "止盈" in s:
        return "止盈"
    if "到期" in s or "持有" in s:
        return "到期了结"
    return "其他"


def fetch_streak(conn, code: str, hz: str, d: str, max_days: int = 20) -> int:
    """该票在该周期上「连续被推荐」的交易日数（含当日）。

    连续 = 中间没有跳过的交易日。长线/中线常出现同一批票天天上榜，
    本质是排序键并列导致的名单固化（见诊断 ③），必须量化出来。
    """
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT scan_date FROM recommend_outcome "
        "WHERE code = ? AND COALESCE(horizon,'short') = ? AND scan_date <= ? "
        "ORDER BY scan_date DESC LIMIT ?", (code, hz, d, max_days)).fetchall()]
    if not dates or dates[0] != d:
        return 0
    streak = 1
    for older, newer in zip(dates[1:], dates[:-1]):
        nxt = conn.execute(
            "SELECT trade_date FROM daily_price WHERE code = ? AND trade_date > ? "
            "ORDER BY trade_date ASC LIMIT 1", (code, older)).fetchone()
        if nxt and nxt[0] == newer:
            streak += 1
        else:
            break
    return streak


# ─────────────────────────────────────────────
# 渲染层
# ─────────────────────────────────────────────

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--bd:#e4e7eb;--tx:#1f2328;--tx2:#57606a;--tx3:#8b949e;
--up:#d92b2b;--down:#128a43;--acc:#2563eb;--acc-s:#eff4ff;--warn:#b45309;--warn-s:#fff7ed;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;font-size:14px;line-height:1.6}
.wrap{max-width:1180px;margin:0 auto;padding:28px 22px 60px}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:16px;margin:30px 0 12px;padding-left:9px;border-left:3px solid var(--acc)}
.sub{color:var(--tx2);font-size:13px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:16px 18px;margin-bottom:14px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:6px}
.kpi{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:12px 14px}
.kpi .k{font-size:12px;color:var(--tx2)}
.kpi .v{font-size:20px;font-weight:600;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:13px;background:var(--card)}
th,td{padding:8px 10px;border-bottom:1px solid var(--bd);text-align:right;white-space:nowrap}
th{background:#f1f3f5;color:var(--tx2);font-weight:600;text-align:right;position:sticky;top:0}
th:first-child,td:first-child{text-align:left}
tbody tr:hover{background:#fafbfc}
.up{color:var(--up);font-weight:600}.down{color:var(--down);font-weight:600}
.flat{color:var(--tx3)}
.tag{display:inline-block;padding:1px 7px;border-radius:4px;font-size:12px;border:1px solid var(--bd);color:var(--tx2)}
.tag.buy{background:#eef7f0;color:#128a43;border-color:#cdeadd}
.tag.wait{background:var(--warn-s);color:var(--warn);border-color:#f4d9b0}
.tag.avoid{background:#fdecec;color:var(--up);border-color:#f5c6c6}
.mut{color:var(--tx3)}
ol,ul{margin:8px 0;padding-left:22px}
li{margin:5px 0}
.diag{background:var(--warn-s);border:1px solid #f4d9b0;border-radius:8px;padding:10px 14px;margin:10px 0;font-size:13px}
.ok{background:#eef7f0;border-color:#cdeadd}
.foot{color:var(--tx3);font-size:12px;margin-top:28px;border-top:1px solid var(--bd);padding-top:12px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:900px){.grid2{grid-template-columns:1fr}}
.bar{height:7px;background:#eef0f2;border-radius:4px;overflow:hidden;min-width:70px}
.bar>i{display:block;height:100%;background:var(--acc)}
"""


def _fmt(v, suffix="", nd=2, sign=False):
    if v is None:
        return '<span class="mut">—</span>'
    if isinstance(v, (int, float)):
        s = f"{v:+.{nd}f}" if sign else f"{v:.{nd}f}"
        cls = "up" if v > 0 else ("down" if v < 0 else "flat")
        return f'<span class="{cls}">{s}{suffix}</span>'
    return str(v)


def render(ctx: dict) -> str:
    d = ctx["date"]
    mkt = ctx["market"]
    groups = ctx["groups"]
    m = ctx["ctx_map"]

    # ── 大盘 KPI ──
    idx_html = "".join(
        f'<div class="kpi"><div class="k">{r["name"]}</div>'
        f'<div class="v">{_fmt(r["pct_change"], "%", sign=True)}</div>'
        f'<div class="mut" style="font-size:12px">{r["close"]:.2f} · 5日 '
        f'{_fmt(r.get("trend_5d_pct"), "%", sign=True)}</div></div>'
        for r in mkt["indices"][:5])
    breadth = (f'<div class="kpi"><div class="k">市场宽度</div>'
               f'<div class="v"><span class="up">{mkt["up"]}</span> / '
               f'<span class="down">{mkt["dn"]}</span></div>'
               f'<div class="mut" style="font-size:12px">上涨占比 {mkt["up_ratio"]}%</div></div>')
    med = (f'<div class="kpi"><div class="k">个股中位数 / 均值</div>'
           f'<div class="v">{_fmt(mkt["median_pct"], "%", sign=True)}</div>'
           f'<div class="mut" style="font-size:12px">均值 {_fmt(mkt["avg_pct"], "%", sign=True)}'
           f' · 涨停 {mkt["limit_up"]} / 跌停 {mkt["limit_dn"]}</div></div>')

    # ── 推荐明细表 ──
    def _rows(items):
        out = []
        for it in items:
            c = m.get(it["code"], {})
            ap = it["action_plan"]
            sig = it["signal"]
            close = it.get("latest_close")
            entry = ap.get("entry_price")
            ext = it.get("pct_above_ma20")
            out.append(f"""<tr>
<td><b>{it['code']}</b> {it['name'] or ''}</td>
<td><span class="tag">{it.get('strategy') or '-'}</span></td>
<td>{it['fusion_score']:.1f}</td>
<td>{_fmt(ext * 100 if ext is not None else None, '%', nd=2, sign=True)}</td>
<td>{_fmt(c.get('pct'), '%', sign=True)}</td>
<td>{_fmt(c.get('ret5'), '%', sign=True)}</td>
<td>{entry if entry else '—'}</td>
<td>{close if close else '—'}</td>
<td>{_fmt(c.get('dd_from_high20'), '%', sign=True)}</td>
<td>{ap.get('stop_loss') or '—'}</td>
<td>{ap.get('take_profit') or '—'}</td>
<td>{_fmt(ap.get('risk_pct'), '%')}</td>
<td>{ap.get('risk_reward_ratio') or '—'}</td>
<td>{_fmt(c.get('vol_ratio'))}</td>
<td><span class="tag {sig['level']}">{sig['emoji']} {sig['label']}</span></td>
</tr>""")
        return "\n".join(out)

    # 注：T+0 时「现价 vs 信号价」恒为 0（信号价 = 当日收盘），无信息量，
    # 换成「距 20 日高点」—— 更能说明这批票是处在山顶还是半山腰。
    TH = ("<tr><th>代码 / 名称</th><th>策略线</th><th>融合分</th><th>扩展度</th>"
          "<th>当日涨跌</th><th>近5日</th><th>信号价</th><th>收盘</th><th>距20日高</th>"
          "<th>止损</th><th>止盈</th><th>止损宽度</th><th>盈亏比</th><th>量比</th>"
          "<th>信号灯</th></tr>")
    sec_tables = ""
    for hz in ("short", "mid", "long"):
        items = groups.get(hz, [])
        sec_tables += (f'<h2>{HZ_LABEL[hz]}推荐 · {len(items)} 只</h2>')
        if not items:
            sec_tables += '<div class="card mut">无（当日该周期未产出推荐）</div>'
            continue
        sec_tables += f'<div class="card" style="padding:0;overflow:auto"><table>{TH}<tbody>{_rows(items)}</tbody></table></div>'

    # ── 漏斗 ──
    funnel = ctx["funnel"]
    top = funnel[0][1] or 1
    f_html = "".join(
        f'<tr><td>{name}</td><td>{n}</td>'
        f'<td><div class="bar"><i style="width:{min(100, n / top * 100):.1f}%"></i></div></td>'
        f'<td class="mut">{note}</td></tr>'
        for name, n, note in funnel)

    # ── 历史基准 ──
    base = ctx["baseline"]
    b_html = ""
    reason_lines = []
    for hz in ("short", "mid", "long"):
        b = base[hz]
        # 出场原因已由 norm_exit_reason 归一化（short 是枚举、mid/long 是中文长串）
        rl = "、".join(f"{k} {v}" for k, v in (b.get("reasons") or {}).items())
        if rl:
            reason_lines.append(f"{HZ_LABEL[hz]}：{rl}")
        b_html += (f"<tr><td>{HZ_LABEL[hz]}</td><td>{b['total']}</td><td>{b['settled']}</td>"
                   f"<td>{b['win_rate'] if b['win_rate'] is not None else '—'}</td>"
                   f"<td>{_fmt(b['avg'], '%', sign=True)}</td>"
                   f"<td>{_fmt(b['best'], '%', sign=True)}</td>"
                   f"<td>{_fmt(b['worst'], '%', sign=True)}</td>"
                   f"<td>{b['stop_rate'] if b['stop_rate'] is not None else '—'}</td>"
                   f"<td>{b['t1']['win_rate'] if b['t1']['n'] else '—'} / "
                   f"{_fmt(b['t1']['avg'], '%', sign=True)}</td>"
                   f"<td>{b['t5']['win_rate'] if b['t5']['n'] else '—'} / "
                   f"{_fmt(b['t5']['avg'], '%', sign=True)}</td></tr>")

    # ── 在途 ──
    infl_all = ctx["inflight"]
    infl = infl_all[:40]
    infl_note = (f'<div class="mut" style="font-size:12px;margin-top:6px">'
                 f'共 {len(infl_all)} 条在途，此处显示最近 {len(infl)} 条。</div>'
                 if len(infl_all) > len(infl) else "")
    i_html = "".join(
        f"<tr><td>{r['scan_date']}</td><td>{HZ_LABEL.get(r['horizon'], r['horizon'])}</td>"
        f"<td><b>{r['code']}</b> {r['name'] or ''}</td><td>{r['strategy'] or '-'}</td>"
        f"<td>{r['entry_price']}</td><td>{r['last_close'] or '—'}</td>"
        f"<td>{_fmt(r['pnl'], '%', sign=True)}</td>"
        f"<td class='mut'>{r['last_date'] or ''}</td></tr>"
        for r in infl) or '<tr><td colspan="8" class="mut">无在途推荐</td></tr>'

    # ── 诊断 ──
    diags = "".join(f'<div class="diag {x.get("cls", "")}"><b>{x["t"]}</b><br>{x["b"]}</div>'
                    for x in ctx["diags"])

    total = sum(len(v) for v in groups.values())
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>今日推荐复盘 {d}</title><style>{CSS}</style></head><body><div class="wrap">
<h1>今日推荐复盘 · {d}</h1>
<div class="sub">信号日 T+0 体检（线上 <code>/api/investor/today</code> 口径）·
共 {total} 只推荐 · 大盘 regime = <b>{ctx['regime']}</b> ·
生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}</div>

<div class="card"><b>结论先行</b>
<ol style="margin-top:6px">{''.join(f'<li>{x["t"]}</li>' for x in ctx["diags"])}</ol></div>

<h2>一、大盘背景</h2>
<div class="kpis">{idx_html}{breadth}{med}</div>

<h2>二、今日推荐明细</h2>
{sec_tables}

<h2>三、短线名额漏斗（今日为什么只出 {len(groups.get('short', []))} 只）</h2>
<div class="card" style="padding:0;overflow:auto"><table>
<tr><th>环节</th><th>剩余</th><th>占比</th><th>淘汰</th></tr>
{f_html}</table></div>

<h2>四、关键诊断</h2>
{diags}

<h2>五、历史基准（近 {ctx['days']} 日已结算样本）</h2>
<div class="card" style="padding:0;overflow:auto"><table>
<tr><th>周期</th><th>样本</th><th>已结算</th><th>胜率%</th><th>平均收益</th>
<th>最好</th><th>最差</th><th>止损率%</th><th>T+1 胜率/均值</th><th>T+5 胜率/均值</th></tr>
{b_html}</table></div>
<div class="mut" style="font-size:12px;margin-top:6px">
出场原因分布：{'；'.join(reason_lines) or '—'}<br>
口径：<code>recommend_outcome</code>（与线上同口径录入）· 剔除 no_fill（回踩未成交）·
收益为单笔百分比，不可相加（铁律 1）· 平均收益 = 等权算术平均。<br>
⚠ 长线持有窗口 130 交易日，近 {ctx['days']} 日样本绝大多数尚未结算
（{base['long']['total']} 条仅 {base['long']['settled']} 条结算）⇒ 长线行<b>样本不足，
不作结论</b>；短线/中线已结算样本才可用于判断。</div>

<h2>六、在途推荐（近 {ctx['inflight_days']} 日未结算）</h2>
<div class="card" style="padding:0;overflow:auto"><table>
<tr><th>信号日</th><th>周期</th><th>代码 / 名称</th><th>策略线</th><th>信号价</th>
<th>最新价</th><th>浮动盈亏</th><th>行情日</th></tr>
{i_html}</table></div>{infl_note}

<div class="foot">
本报告只读生成，不写入数据库。T+1 起由 <code>core/outcome_tracker</code> 自动评估实际收益。
所有结论均为信号日当天体检，不构成投资建议。
</div>
</div></body></html>"""


# ─────────────────────────────────────────────
# 诊断生成
# ─────────────────────────────────────────────

def build_diags(groups, ctx_map, market, funnel, baseline,
                days: int = 60, streaks: dict | None = None) -> list:
    d = []
    streaks = streaks or {}
    med = market["median_pct"]
    # 1) 追高检测
    chase = []
    for hz, items in groups.items():
        for it in items:
            c = ctx_map.get(it["code"], {})
            pct = c.get("pct")
            if pct is not None and med is not None and pct > med + 2:
                chase.append((it["code"], it["name"], pct, c.get("ret5"),
                              c.get("dd_from_high20")))
    if chase:
        rows = "；".join(f"{c} {n} 当日 {p:+.2f}%（近5日 {r:+.1f}%"
                        f"{'，距20日高点 ' + format(h, '.1f') + '%' if h is not None else ''}）"
                        for c, n, p, r, h in chase)
        d.append({"t": f"追高嫌疑：{len(chase)} 只当日涨幅显著高于市场中位数 {med:+.2f}%",
                  "b": rows + "。<br>机制：融合分里的反弹项奖励「较 10 日低点反弹」，"
                              "信号往往在反弹之后最强（见 docs/short-reco-anti-chase-methods.md）。"
                              "普涨日该偏差会被放大 —— 今日上涨占比 "
                              f"{market['up_ratio']}%，选出来的票天然偏强。"})
    else:
        d.append({"t": "追高检测：通过",
                  "b": f"无个股当日涨幅超过市场中位数（{med:+.2f}%）+2pct。", "cls": "ok"})

    # 2) 短线供给
    short_n = len(groups.get("short", []))
    pool = funnel[0][1]
    if short_n <= 1:
        d.append({"t": f"短线供给收缩：{pool} 只候选 → 仅 {short_n} 只入选",
                  "b": "漏斗显示淘汰主要发生在哪一层（见第三节）。根因组合：抄底融合线被"
                       "「观察线」阈值（pct_above_ma20 ≥ 5.5%）拦下，只剩隔日动量这根独立"
                       " alpha 线能占名额。这是 2026-09-06 有意为之（抄底线实证弱），"
                       "副作用是短线名额高度依赖龙虎榜动量信号的有无。"})
    else:
        d.append({"t": f"短线供给正常：{pool} 只候选 → {short_n} 只", "cls": "ok",
                  "b": "名额未被闸门压到 1 只以下。"})

    # 3) 同分并列（排序失效）
    for hz in ("long", "mid"):
        items = groups.get(hz, [])
        if len(items) >= 2:
            scores = [round(it["fusion_score"], 1) for it in items]
            if len(set(scores)) == 1:
                d.append({"t": f"{HZ_LABEL[hz]}排序失效：入选 {len(items)} 只融合分全部并列 "
                               f"{scores[0]}",
                          "b": "并列意味着 ORDER BY 后续无有效区分键，实际取的是库内物理顺序"
                               "（先入库者先得），不是「选出的最优 N 只」。若当日该周期候选"
                               "普遍满分，需补次级排序键（如动量/质量/波动），否则推荐名单"
                               "本质是随机抽样。"})

    # 4) 盈亏比与止损宽度
    tight = [it for hz in groups for it in groups[hz]
             if (it["action_plan"].get("risk_reward_ratio") or 0) < 1.5]
    if tight:
        d.append({"t": f"盈亏比不足：{len(tight)} 只 < 1.5",
                  "b": "、".join(f"{it['code']} {it['name']} "
                                 f"({it['action_plan']['risk_reward_ratio']})"
                                 for it in tight)})

    # 5) 逆风警报（历史基准）：样本足够且平均为负 → 系统性逆风，不是选股问题
    for hz in ("short", "mid", "long"):
        b = baseline.get(hz, {})
        n = b.get("settled") or 0
        if n < 20 or b.get("avg") is None or b["avg"] >= 0:
            continue
        reasons = "、".join(f"{k} {v}" for k, v in (b.get("reasons") or {}).items())
        d.append({"t": f"{HZ_LABEL[hz]}处于逆风期：近 {days} 日已结算 n={n}，"
                       f"胜率 {b['win_rate']}%，平均 {b['avg']:+.2f}%",
                  "b": f"出场原因分布：{reasons or '—'}；止损率 {b['stop_rate']}%。"
                       "这不是今日这批票的问题，是周期级逆风 —— 阈值微调救不了逆风月，"
                       "能动的只有仓位（降 top_n / 减仓）。"})

    # 6) 持有期衰减：T+5 还行、出场却亏 —— 问题在出场纪律而非选股
    for hz in ("short", "mid", "long"):
        b = baseline.get(hz, {})
        t5, ex = b.get("t5", {}).get("avg"), b.get("avg")
        if t5 is None or ex is None or b.get("settled", 0) < 20:
            continue
        if ex < t5 - 1.0:
            d.append({"t": f"{HZ_LABEL[hz]}持有期衰减：T+5 均值 {t5:+.2f}% → "
                           f"出场收益 {ex:+.2f}%（差 {ex - t5:+.2f}pct）",
                      "b": "选股在 5 日内是正/平的，钱是在之后回吐的 ⇒ 优先修出场"
                           "（收紧移动止盈回撤比例 / 缩短 max_hold），而不是继续调选股阈值。"
                           "与既有结论一致：个股深度实测「收益来自出场纪律不是选股」。"})

    # 7) 名单固化：连续多日推荐同一批票
    repeat = [(it["code"], it["name"], streaks.get((it["code"], hz), 1))
              for hz in groups for it in groups[hz]
              if streaks.get((it["code"], hz), 1) >= 2]
    if repeat:
        rows = "、".join(f"{c} {n}（连续 {s} 日）" for c, n, s in repeat)
        d.append({"t": f"名单固化：{len(repeat)}/{sum(len(v) for v in groups.values())} "
                       "只是连续多日重复推荐",
                  "b": rows + "。若排序键并列（见「排序失效」一条），同一批票会天天复现，"
                              "「每日推荐」退化成「每周一只」。对中长线可接受（本就是持有），"
                              "对短线则意味着信号没有更新。"})

    # 统一重编号：各分支条件触发，硬编码序号会重复/跳号
    cn = "①②③④⑤⑥⑦⑧⑨"
    for i, x in enumerate(d):
        x["t"] = f"{cn[i] if i < len(cn) else str(i + 1)} {x['t']}"
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="复盘日期（默认最新 signal 日）")
    ap.add_argument("--days", type=int, default=60, help="历史基准窗口（自然日）")
    ap.add_argument("--inflight-days", type=int, default=7, help="在途推荐窗口（自然日）")
    ap.add_argument("--out", default=None, help="输出 HTML 路径")
    a = ap.parse_args()

    conn = _conn()
    d = latest_signal_date(conn, a.date)
    if not d:
        print("无 signal 数据")
        return
    recs = fetch_recommendations(d)
    groups = recs.get("groups") or {}
    codes = [it["code"] for hz in groups for it in groups[hz]]
    ctx_map = {c: fetch_stock_context(conn, c, d) for c in codes}
    ctx = {
        "date": d,
        "regime": recs.get("market_regime"),
        "groups": groups,
        "ctx_map": ctx_map,
        "market": fetch_market(conn, d),
        "funnel": fetch_funnel(conn, d),
        "baseline": fetch_history_baseline(conn, a.days, d),
        "inflight": fetch_inflight(conn, d, a.inflight_days),
        "days": a.days,
        "inflight_days": a.inflight_days,
    }
    streaks = {(c, hz): fetch_streak(conn, c, hz, d)
               for hz in groups for c in (it["code"] for it in groups[hz])}
    ctx["diags"] = build_diags(groups, ctx_map, ctx["market"], ctx["funnel"],
                               ctx["baseline"], a.days, streaks)

    os.makedirs(REPORT_DIR, exist_ok=True)
    out = a.out or os.path.join(REPORT_DIR, f"today_review_{d}.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(render(ctx))
    print(f"[today_review] {d} 推荐 {sum(len(v) for v in groups.values())} 只 → {out}")


if __name__ == "__main__":
    main()
