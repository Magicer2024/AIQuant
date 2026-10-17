"""
core/discipline_calibration.py —— 割肉点校准引擎（JH4 spec §5）
================================================================

诚实边界（写死在行为里，不在口号里）：
  - 首轮样本几乎必然 <8 → 落 pooled/uncalibrated，校准是季度累积物不是即时交付物。
  - 输出只写新增表 risk_param_calibration + 报告文件；
    绝不回写 config/personal_config.py / strategy_params / 推荐链路（spec §0.2 红线）。
  - pnl_pct 入库为百分数（如 -5.0），公式内 /100 转幅度（R7 单位红线）。
  - account 用 POSITION_PLAN_ACCOUNT（1 万参考），不是 RESEARCH_CAPITAL（R7）。
"""
from __future__ import annotations

import json
import os
import statistics
from datetime import datetime

from core.discipline import make_hash

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_DIR = os.path.join(ROOT, "reports")


def _quantile(values: list, q: float):
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[idx]


def _regime_bucket(day_pct_avg: float | None) -> str:
    """出场日全市场平均涨跌幅 → 5 档 regime 代理（compute_market_regime 只给当下，
    历史逐日 regime 无留存；用同日宽度做近似并在报告注明，不假装精确）。"""
    if day_pct_avg is None:
        return "unknown"
    if day_pct_avg >= 0.8:
        return "hot"
    if day_pct_avg >= 0.3:
        return "warm"
    if day_pct_avg > -0.3:
        return "neutral"
    if day_pct_avg > -0.8:
        return "cool"
    return "cold"


def _samples(conn) -> list[dict]:
    """§5.1 全仓割肉样本（减仓不进主样本，防同一持仓重复计权）。"""
    trades = conn.execute(
        """SELECT t.*, p.opened_at, p.cost_price, p.horizon
           FROM personal_trade t LEFT JOIN personal_position p ON p.id = t.position_id
           WHERE t.action IN ('卖出','平仓') AND t.pnl_pct < 0""").fetchall()
    out = []
    for t in trades:
        opened = str(t["opened_at"])[:10] if t["opened_at"] else None
        exit_day = str(t["traded_at"])[:10]
        if not opened:
            continue
        seg = conn.execute(
            "SELECT trade_date, high, low, close FROM daily_price "
            "WHERE code=? AND trade_date>=? AND trade_date<=? ORDER BY trade_date",
            (t["code"], opened, exit_day)).fetchall()
        hold_days = len(seg)
        if hold_days < 2:
            continue
        highs = [float(r["high"]) for r in seg if r["high"]]
        peak = max(highs) if highs else None
        if not peak:
            continue
        # 出场日 ATR%（原始 ATR/close，不复用 atr_dynamic_stop_pct 的夹逼值——spec §5.3）
        atr_pct = _atr_pct_at(conn, t["code"], exit_day)
        if atr_pct is None:
            continue
        exit_price = float(t["price"])
        if exit_price > peak * (1 - 0.5 * atr_pct):
            continue   # 未从高点回吐过半，为止盈误记/噪音换仓，不算割肉
        day_pct = conn.execute(
            "SELECT AVG(pct_change) FROM daily_price WHERE trade_date=?",
            (exit_day,)).fetchone()[0]
        out.append({
            "trade_id": t["id"], "code": t["code"], "exit_day": exit_day,
            "cut_mag": abs(float(t["pnl_pct"])) / 100.0,
            "giveback": exit_price / peak - 1,
            "hold_days": hold_days,
            "loss_yuan": abs(float(t["pnl"] or 0)),
            "exit_regime": _regime_bucket(float(day_pct) if day_pct is not None else None),
            "atr_pct_at_exit": atr_pct,
        })
    return out


def _atr_pct_at(conn, code: str, day: str):
    rows = conn.execute(
        "SELECT trade_date, high, low, close FROM daily_price "
        "WHERE code=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 20",
        (code, day)).fetchall()
    if len(rows) < 15:
        return None
    import pandas as pd
    from strategy.indicators import calc_atr
    df = pd.DataFrame([dict(r) for r in rows]).iloc[::-1]
    atr = calc_atr(df["high"].astype(float), df["low"].astype(float),
                   df["close"].astype(float), 14)
    close = float(df["close"].iloc[-1])
    v = atr.iloc[-1]
    return float(v) / close if v and close > 0 else None


def _calc_stop(mags: list, atrs: list):
    """§5.3 公式。返回 (stop_loss_pct, extras, low_sample:bool)。"""
    m_p50 = statistics.median(mags)
    m_p75 = _quantile(mags, 0.75)
    m_p25 = _quantile(mags, 0.25)
    a_p50 = statistics.median(atrs) if atrs else None
    extras = {"m_p25": m_p25, "m_p50": m_p50, "m_p75": m_p75, "a_p50": a_p50}
    if a_p50 is None:
        return None, extras, False
    raw = 0.80 * m_p50
    floor = max(1.50 * a_p50, 0.03)
    cap = min(m_p50, 0.12)
    low_sample = False
    if floor > cap:
        m_star = min(cap, 0.06)
        low_sample = True
    else:
        m_star = max(floor, min(raw, cap))
    return -round(m_star, 4), extras, low_sample


def _calc_position_pct(m_star: float, loss_yuan_median: float):
    """§5.4 由可承受损失倒推单仓上限。"""
    from config.personal_config import POSITION_PLAN_ACCOUNT, MAX_SINGLE_POSITION_PCT
    if not m_star:
        return None
    pct = loss_yuan_median / (POSITION_PLAN_ACCOUNT * m_star)
    return round(max(0.05, min(pct, min(MAX_SINGLE_POSITION_PCT, 0.20))), 4)


def run_calibration(conn, as_of: str | None = None, out_dir: str = REPORT_DIR) -> dict:
    """重算并写新行（旧行不覆盖）。返回 {status, calibration_id, report_path, n,...}"""
    samples = _samples(conn)
    as_of = as_of or datetime.now().strftime("%Y-%m-%d")
    n = len(samples)
    ids = [s["trade_id"] for s in samples]

    status, stratum, stop_pct, pos_pct, extras, med_loss = "uncalibrated", "none", None, None, {}, None
    if n >= 3:
        med_loss = statistics.median([s["loss_yuan"] for s in samples]) if samples else 0.0
        # 分层判定：取最近一次割肉所在层，层内 n>=8 → calibrated；否则 pooled 全样本
        by_layer: dict[str, list] = {}
        for s in samples:
            by_layer.setdefault(s["exit_regime"], []).append(s)
        latest_layer = samples[-1]["exit_regime"]
        use = by_layer[latest_layer] if n >= 8 and len(by_layer[latest_layer]) >= 8 else samples
        stratum = latest_layer if (n >= 8 and use is by_layer[latest_layer]) else "pooled"
        status = "calibrated" if stratum != "pooled" else "pooled"
        stop_pct, extras, low_sample = _calc_stop([s["cut_mag"] for s in use],
                                                  [s["atr_pct_at_exit"] for s in use])
        if low_sample:
            status = "low_sample"
        if stop_pct is not None:
            pos_pct = _calc_position_pct(abs(stop_pct), med_loss)

    payload = {"as_of": as_of, "stratum": stratum, "n": n, "status": status,
               "stop": stop_pct, "pos": pos_pct,
               "m_p25": extras.get("m_p25"), "m_p50": extras.get("m_p50"),
               "m_p75": extras.get("m_p75"), "a_p50": extras.get("a_p50"),
               "med_loss": med_loss}
    params_hash = make_hash(None, payload)
    with conn as c:
        c.execute(
            """INSERT INTO risk_param_calibration
               (as_of, regime_stratum, n_samples, m_p25, m_p50, m_p75, a_p50,
                stop_loss_pct, max_single_position_pct, median_cut_loss_yuan,
                sample_ids_json, status, params_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (as_of, stratum, n, extras.get("m_p25"), extras.get("m_p50"),
             extras.get("m_p75"), extras.get("a_p50"), stop_pct, pos_pct, med_loss,
             json.dumps(ids), status, params_hash))
        cal_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]

    report = _render_report(as_of, status, stratum, n, samples, extras, stop_pct, pos_pct,
                            med_loss)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"discipline_calib_{as_of.replace('-', '')}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)
    return {"calibration_id": cal_id, "status": status, "n": n,
            "stop_loss_pct": stop_pct, "max_single_position_pct": pos_pct,
            "report_path": path}


_NOT_PROVE = (
    "- 样本只覆盖「真的割过肉」的行为，不覆盖你忍住的回撤——止损宽度按已崩溃点校准，"
    "不代表你能扛住下一次。\n"
    "- regime 分层用出场日全市场宽度代理（历史 regime 无逐日留存），分档粗糙，"
    "层边界样本可能错分。\n"
    "- 减仓割肉不入主样本（防重复计权），对「分批投降」型行为无感知。\n"
    "- 校准是季度累积物：pooled/uncalibrated 状态下本结论只能沿用默认，不能当风控参数用。")


def _render_report(as_of, status, stratum, n, samples, extras, stop_pct, pos_pct,
                   med_loss) -> str:
    lines = [f"# 割肉点校准报告 — {as_of}", "",
             f"- 结论状态：**{status}**（分层={stratum}，全仓割肉样本 n={n}）",
             f"- 建议止损宽度：{stop_pct if stop_pct is not None else '不给数（样本不足，沿用默认）'}",
             f"- 建议单仓上限：{pos_pct if pos_pct is not None else '同上'}",
             f"- 历史割肉损失中位：{f'{med_loss:.0f} 元' if med_loss is not None else '无样本'}", ""]
    if samples:
        lines += ["## 样本逐笔清单", "",
                  "| trade_id | code | 出场日 | cut_mag | giveback | hold_days | regime | atr_pct |",
                  "|---|---|---|---|---|---|---|---|"]
        for s in samples:
            lines.append(f"| {s['trade_id']} | {s['code']} | {s['exit_day']} "
                         f"| {s['cut_mag']:.4f} | {s['giveback']:.4f} | {s['hold_days']} "
                         f"| {s['exit_regime']} | {s['atr_pct_at_exit']:.4f} |")
        lines.append("")
        lines.append("## 公式代入（spec §5.3/§5.4）")
        lines.append("")
        lines.append(f"- m_p25={extras.get('m_p25')} · m_p50={extras.get('m_p50')} "
                     f"· m_p75={extras.get('m_p75')} · a_p50={extras.get('a_p50')}")
        lines.append("- raw=0.80×m_p50 · floor=max(1.50×a_p50, 0.03) · cap=min(m_p50, 0.12) "
                     "· m_star=clip(raw, floor, cap)（floor>cap → min(cap,0.06)+low_sample）")
        lines.append("")
    lines.append("## 本结论不能证明什么")
    lines.append("")
    lines.append(_NOT_PROVE)
    return "\n".join(lines) + "\n"
