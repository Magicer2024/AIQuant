"""
tools/night_check.py —— L05 双源夜检：持仓 + 信号池每日差异清单
================================================================

用途（脑暴纪要 T1/L05）：
  - 范围只查「当前持仓 + 信号池」标的，不查全市场。
  - 主源（被测方）＝库内 daily_price（东财盘后同步，前复权口径）。
  - 副源＝腾讯 core/tx_kline（与东财完全异构的免费源，前复权）。
  - 复权口径单校＝东财 qfq/none 两口径推出隐含因子，与库内存价逐日对照——
    不依赖"多数票"，两源同用一个错因子也能暴露 DB 与权威口径的分叉。
  - 输出一页 markdown 差异清单 reports/night_check_YYYYMMDD.md。

只读脚本：不写任何业务表，历史数字零变更（T5 红线）。

用法：
    python tools/night_check.py                          # 检查最近交易日
    python tools/night_check.py --date 2026-10-16        # 指定日期
    python tools/night_check.py --lookback 30 --pool-days 5
    python tools/night_check.py --codes 600519,000001    # 显式标的覆盖
"""
from __future__ import annotations

import argparse
import os
import sys
import time as _time
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

REPORT_DIR = os.path.join(_ROOT, "reports")

# 默认阈值（相对差）
TOL_CLOSE = 0.005     # 收盘价：DB vs 腾讯
TOL_FACTOR = 0.002    # 复权因子：DB 隐含因子 vs 东财当前口径


# ─────────────────────────────────────────────
# 差异行
# ─────────────────────────────────────────────
@dataclass
class Diff:
    level: str        # red / yellow / info
    code: str
    name: str
    date: str
    item: str         # close/factor/db_gap/solo_row/tx_gap/anchor/fetch_fail/no_db
    a_val: str
    b_val: str
    rel: str
    note: str = ""


# ─────────────────────────────────────────────
# 标的范围（持仓 + 信号池）
# ─────────────────────────────────────────────
def collect_universe(conn, pool_days: int = 1) -> dict[str, str]:
    """返回 {code: name}（插入序＝核查优先级）。
    持仓＝personal_position(status='holding') 最优先，其次 personal_watchlist，
    再次信号池＝最近 pool_days 个 scan_date 的 stock_signal（按 fusion_score 降序）
    ∪ stock_deep_signal（buy 优先于 add）。"""
    codes: dict[str, str] = {}

    def _add(row):
        try:
            c = str(row["code"]).zfill(6)
        except (KeyError, IndexError, TypeError):
            return
        if not c or c == "000000":
            return
        name = ""
        try:
            if "name" in row.keys() and row["name"]:
                name = str(row["name"])
        except AttributeError:
            pass
        if name or c not in codes:
            codes[c] = name or codes.get(c, "")

    queries = [
        "SELECT code, name FROM personal_position WHERE status='holding'",
        "SELECT code FROM personal_watchlist",
        """SELECT s.code, s.name FROM stock_signal s
           WHERE s.scan_date IN (SELECT scan_date FROM (
               SELECT DISTINCT scan_date FROM stock_signal ORDER BY scan_date DESC LIMIT ?))
           GROUP BY s.code ORDER BY MAX(COALESCE(s.fusion_score, -1)) DESC""",
        """SELECT d.code, d.name FROM stock_deep_signal d
           WHERE d.scan_date IN (SELECT scan_date FROM (
               SELECT DISTINCT scan_date FROM stock_deep_signal ORDER BY scan_date DESC LIMIT ?))
           GROUP BY d.code
           ORDER BY MIN(CASE d.level WHEN 'buy' THEN 0 ELSE 1 END), MAX(COALESCE(d.score, -1)) DESC""",
    ]
    for sql in queries:
        try:
            params = (pool_days,) if sql.count("?") else ()
            for r in conn.execute(sql, params).fetchall():
                _add(r)
        except Exception:
            continue   # 表缺失/列变化不阻断，尽力而为

    # 名称回填 stock_info
    if codes:
        missing = [c for c, n in codes.items() if not n]
        if missing:
            marks = ",".join("?" * len(missing))
            try:
                for r in conn.execute(
                        f"SELECT code, name FROM stock_info WHERE code IN ({marks})", missing).fetchall():
                    codes[str(r["code"])] = str(r["name"])
            except Exception:
                pass
    return codes


# ─────────────────────────────────────────────
# 数据获取层（可注入替换，供测试离线跑）
# ─────────────────────────────────────────────
def load_db_frame(conn, code: str, start: str, end: str) -> pd.DataFrame:
    """库内主源：index=trade_date('YYYY-MM-DD'), columns=[close]"""
    df = pd.read_sql_query(
        "SELECT trade_date, close FROM daily_price "
        "WHERE code=? AND trade_date>=? AND trade_date<=? ORDER BY trade_date",
        conn, params=(code, start, end))
    if df.empty:
        return pd.DataFrame(columns=["close"])
    return df.set_index("trade_date")[["close"]]


def default_tx_fetcher(code: str, start: str, end: str) -> pd.DataFrame:
    """腾讯副源（前复权）：index=trade_date, columns=[close]；失败抛异常"""
    from core.tx_kline import fetch_kline_range
    df = fetch_kline_range(code, start, end)
    if df is None:
        raise RuntimeError(f"tx 接口失败 {code}")
    if df.empty:
        return pd.DataFrame(columns=["close"])
    out = df.copy()
    out["trade_date"] = out["trade_date"].astype(str).str[:10]
    return out.set_index("trade_date")[["close"]]


def default_em_fetcher(code: str, adjust: str, start: str, end: str) -> pd.DataFrame:
    """东财口径参照（qfq/none）：index=trade_date, columns=[close]；失败抛异常"""
    from core.em_kline import fetch_kline
    df = fetch_kline(code, adjust=adjust, klt="d")
    if df is None or df.empty:
        return pd.DataFrame(columns=["close"])
    out = df.copy()
    out.index = pd.to_datetime(out.index).strftime("%Y-%m-%d")
    out = out[(out.index >= start) & (out.index <= end)]
    return out[["close"]]


# ─────────────────────────────────────────────
# 比对层（纯函数，无网络无 DB）
# ─────────────────────────────────────────────
def _rel(a: float, b: float) -> float:
    if a == 0:
        return float("inf") if b != 0 else 0.0
    return abs(a - b) / abs(a)


def compare_stock(code: str, name: str, db: pd.DataFrame, tx: pd.DataFrame,
                  emq: pd.DataFrame | None, emn: pd.DataFrame | None,
                  *, tol_close: float = TOL_CLOSE, tol_factor: float = TOL_FACTOR,
                  fetch_notes: list[str] | None = None) -> list[Diff]:
    """单标的四类差异 + 口径单校。db/tx 必有；emq/emn 可为 None（口径检查降级）。"""
    rows: list[Diff] = []

    if db.empty:
        rows.append(Diff("red", code, name, "-", "no_db", "-", "-", "-",
                         "库内 daily_price 窗口内无任何记录"))
        return rows

    # 1) 收盘价：DB vs 腾讯（公共日期）
    common = db.index.intersection(tx.index)
    for d in common:
        c_db = float(db.at[d, "close"])
        c_tx = float(tx.at[d, "close"])
        if c_db <= 0 or c_tx <= 0:
            continue
        r = _rel(c_db, c_tx)
        if r > tol_close:
            tie = ""
            if emq is not None and d in emq.index:
                tie = "（DB 贴近东财当前值，腾讯异常日）" if _rel(float(emq.at[d, "close"]), c_db) < r else ""
            rows.append(Diff("red", code, name, str(d), "close",
                             f"{c_db:.3f}", f"{c_tx:.3f}", f"{r:.2%}",
                             f"收盘价相对差超阈值 {tol_close:.2%}{tie}"))

    # 2) 复权口径单校：DB 隐含因子 vs 东财当前因子（逐日、同分母不复权价）
    if emq is not None and emn is not None and not emq.empty and not emn.empty:
        f_em = (emq["close"] / emn["close"]).dropna()
        f_em = f_em[emn["close"] > 0]
        f_db = (db["close"] / emn["close"]).dropna()
        f_db = f_db[emn["close"] > 0]
        cf = f_db.index.intersection(f_em.index)
        worst_d, worst_r, worst_pair = "", 0.0, (0.0, 0.0)
        for d in cf:
            r = _rel(float(f_db.at[d]), float(f_em.at[d]))
            if r > worst_r:
                worst_d, worst_r, worst_pair = str(d), r, (float(f_db.at[d]), float(f_em.at[d]))
        if worst_r > tol_factor:
            rows.append(Diff("red", code, name, worst_d, "factor",
                             f"{worst_pair[0]:.4f}", f"{worst_pair[1]:.4f}", f"{worst_r:.2%}",
                             f"DB 隐含复权因子与东财当前口径偏差 > {tol_factor:.2%}"
                             "（陈旧因子/除权后未重刷；窗口内其余日期同口径正常，只列最大偏移日）"))

        # 3) 锚点自检：前复权最新日应≈不复权价（factor_em≈1）
        if len(f_em):
            anchor_d = f_em.index.max()
            anchor_f = float(f_em.at[anchor_d])
            if anchor_f > 0 and abs(anchor_f - 1.0) > tol_close:
                rows.append(Diff("yellow", code, name, str(anchor_d), "anchor",
                                 f"{anchor_f:.4f}", "≈1.0", f"{abs(anchor_f - 1):.2%}",
                                 "东财 qfq 锚点日因子≠1：当日有新除权，库内前复权序列待整体重刷"))

    # 4) 缺失/疑似停牌（外源日期并集 vs DB 日期）
    ext: set[str] = set(tx.index)
    if emq is not None:
        ext |= set(emq.index)
    db_set = set(db.index)
    for d in sorted(ext - db_set):
        rows.append(Diff("red", code, name, str(d), "db_gap", "-", "有数据", "-",
                         "外部源有该交易日行情、库内无行 → 主源漏数"))
    for d in sorted(db_set - ext):
        rows.append(Diff("yellow", code, name, str(d), "solo_row", "有数据", "均无数据", "-",
                         "库内单边有行、两外部源当日均无 → 疑似停牌/脏记录，人工确认"))
    if emq is not None:
        for d in sorted(set(emq.index) - set(tx.index)):
            rows.append(Diff("yellow", code, name, str(d), "tx_gap", "东财有", "腾讯无", "-",
                             "副源缺失该日（只记录不判主源错）"))

    # 5) 拉取失败降级说明
    for note in (fetch_notes or []):
        rows.append(Diff("info", code, name, "-", "fetch_fail", "-", "-", "-", note))
    return rows


# ─────────────────────────────────────────────
# 编排层
# ─────────────────────────────────────────────
def resolve_check_date(yyyymmdd_dash: str | None) -> tuple[str, bool]:
    """返回 (check_date 'YYYY-MM-DD', 是否交易日)。None→从今日向前找最近交易日（最多回退 15 天）。"""
    from core.trade_calendar import is_trading_day
    if yyyymmdd_dash:
        d = yyyymmdd_dash.replace("-", "")
        d = f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else yyyymmdd_dash
        return d, bool(is_trading_day(d))
    today = datetime.now().strftime("%Y-%m-%d")
    if is_trading_day(today):
        return today, True
    cursor = datetime.now()
    for _ in range(15):
        cursor -= timedelta(days=1)
        d = cursor.strftime("%Y-%m-%d")
        if is_trading_day(d):
            return d, True
    return today, False


def run_night_check(check_date: str | None = None, *, lookback_days: int = 30,
                    pool_days: int = 1, max_pool: int = 150,
                    codes: list[str] | None = None,
                    tol_close: float = TOL_CLOSE, tol_factor: float = TOL_FACTOR,
                    out_dir: str = REPORT_DIR, fetcher_tx=None, fetcher_em=None,
                    conn=None, sleep_s: float = 0.2, now: str | None = None) -> dict:
    """执行一夜双源检查并写报告。返回 {check_date, is_trading_day, universe, checked,
    truncated, red, yellow, info, summary, report_path, rows}。
    conn/fetcher 可注入（测试离线用）。max_pool 截断＝免费源限流下的显式降级，
    核查优先级见 collect_universe（持仓 → 自选 → 信号高分 → 深析 buy）。"""
    from core.db import get_conn

    cd, is_td = resolve_check_date(check_date)
    start = (pd.Timestamp(cd) - timedelta(days=int(lookback_days))).strftime("%Y-%m-%d")
    fetcher_tx = fetcher_tx or default_tx_fetcher
    fetcher_em = fetcher_em or default_em_fetcher

    own_conn = conn is None
    if own_conn:
        ctx = get_conn(readonly=True)
        conn = ctx.__enter__()
    try:
        universe = ({"code": "" for code in codes} if codes else
                    collect_universe(conn, pool_days=pool_days))
        if codes:
            names = {}
            if universe:
                marks = ",".join("?" * len(codes))
                try:
                    for r in conn.execute(
                            f"SELECT code, name FROM stock_info WHERE code IN ({marks})",
                            list(codes)).fetchall():
                        names[str(r["code"])] = str(r["name"])
                except Exception:
                    pass
            universe = {c: names.get(c, "") for c in codes}

        total_universe = len(universe)
        ordered = list(universe.items())
        if max_pool and max_pool > 0:
            ordered = ordered[:max_pool]
        truncated = total_universe - len(ordered)
        universe = dict(ordered)

        all_rows: list[Diff] = []
        checked: list[str] = []
        for code in sorted(universe):
            name = universe[code]
            db = load_db_frame(conn, code, start, cd)
            notes: list[str] = []
            try:
                tx = fetcher_tx(code, start, cd)
            except Exception as e:
                notes.append(f"腾讯副源拉取失败，该标的仅口径核查: {e}")
                tx = pd.DataFrame(columns=["close"])
            try:
                emq = fetcher_em(code, "qfq", start, cd)
                emn = fetcher_em(code, "none", start, cd)
                if emq.empty or emn.empty:
                    notes.append("东财口径参照（qfq/none）为空，口径单校降级跳过")
                    emq = emn = None
            except Exception as e:
                notes.append(f"东财口径参照拉取失败，口径单校跳过: {e}")
                emq = emn = None
            all_rows.extend(compare_stock(code, name, db, tx, emq, emn,
                                          tol_close=tol_close, tol_factor=tol_factor,
                                          fetch_notes=notes))
            checked.append(code)
            if sleep_s:
                _time.sleep(sleep_s)
    finally:
        if own_conn:
            ctx.__exit__(None, None, None)

    reds = [r for r in all_rows if r.level == "red"]
    yellows = [r for r in all_rows if r.level == "yellow"]
    infos = [r for r in all_rows if r.level == "info"]
    clean = [c for c in checked if c not in {r.code for r in all_rows}]

    md = render_markdown(cd, start, universe, all_rows, clean,
                         tol_close=tol_close, tol_factor=tol_factor,
                         is_trading_day=is_td, generated_at=now,
                         truncated=truncated, total_universe=total_universe)
    os.makedirs(out_dir, exist_ok=True)
    report_path = os.path.join(out_dir, f"night_check_{cd.replace('-', '')}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(md)

    summary = (f"{len(reds)} 红 · {len(yellows)} 黄 · {len(infos)} 不可核查 · "
               f"{len(clean)}/{len(universe)} 无差异"
               + (f" · 截断 {truncated} 只未查" if truncated else ""))
    return {"check_date": cd, "is_trading_day": is_td, "universe": total_universe,
            "checked": len(universe), "truncated": truncated,
            "red": len(reds), "yellow": len(yellows), "info": len(infos),
            "rows": all_rows, "summary": summary, "report_path": report_path}


# ─────────────────────────────────────────────
# 渲染
# ─────────────────────────────────────────────
_LEVEL_TITLE = {"red": "🔴 需要处理（主源差异）", "yellow": "🟡 提醒（待人工确认）",
                "info": "⚪ 不可核查（拉取降级）"}


def _table(rows: list[Diff]) -> str:
    lines = ["| 代码 | 名称 | 日期 | 项目 | 主源/DB | 对照 | 相对差 | 说明 |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r.code} | {r.name or '-'} | {r.date} | {r.item} "
                     f"| {r.a_val} | {r.b_val} | {r.rel} | {r.note} |")
    return "\n".join(lines)


def render_markdown(check_date: str, start: str, universe: dict[str, str],
                    rows: list[Diff], clean_codes: list[str], *,
                    tol_close: float, tol_factor: float,
                    is_trading_day: bool = True, generated_at: str | None = None,
                    truncated: int = 0, total_universe: int | None = None) -> str:
    """一页差异清单 markdown。"""
    ts = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    reds = [r for r in rows if r.level == "red"]
    yellows = [r for r in rows if r.level == "yellow"]
    infos = [r for r in rows if r.level == "info"]

    head = [f"# 双源夜检差异清单 — {check_date}", "",
            f"- 生成时间：{ts}",
            f"- 范围：{len(universe)} 只标的（持仓 + 信号池），窗口 {start} ~ {check_date}",
            "- 主源＝库内 daily_price（东财前复权）；副源＝腾讯 tx_kline（前复权）；"
            "口径单校＝东财 qfq/none 隐含因子",
            "- 阈值：close {0:.2%} · factor {1:.2%}".format(tol_close, tol_factor)]
    if truncated:
        head.append(f"- ⚠ 免费源限流截断：池共 {total_universe} 只，本次核查高分/持仓优先的 "
                    f"{len(universe)} 只，尾部 {truncated} 只未查")
    if not is_trading_day:
        head.append("- ⚠ 检查日为非交易日：仅存量核对，不含当日增量")
    head.append("")
    head.append(f"**结论：{len(reds)} 红 · {len(yellows)} 黄 · {len(infos)} 不可核查 · "
                f"{len(clean_codes)}/{len(universe)} 无差异**")
    head.append("")

    for level, data in (("red", reds), ("yellow", yellows), ("info", infos)):
        title = _LEVEL_TITLE[level]
        if data:
            head += [f"## {title}", "", _table(data), ""]
        else:
            head += [f"## {title}", "", "无。", ""]

    if clean_codes:
        head.append("## ✅ 无差异标的")
        head.append("")
        head.append("、".join(f"{c}" for c in clean_codes))
        head.append("")
    head.append("---")
    head.append("口径说明：factor 行对比的是「库内价/不复权价」与「东财前复权/不复权」两组隐含因子的最大偏离日；"
                "两源共用同一错误约定时 close 检不出，以本项兜底。solo_row 不自动定性停牌（免费源无停牌标志），请人工确认。")
    return "\n".join(head) + "\n"


# ─────────────────────────────────────────────
# 调度器入口 & CLI
# ─────────────────────────────────────────────
def run_and_report(**kwargs) -> dict:
    """scheduler 用包装：非交易日跳过并返回 summary。"""
    res = run_night_check(**kwargs)
    print(f"[night_check] {res['check_date']} "
          f"{'交易' if res['is_trading_day'] else '非交易日'} · {res['summary']}")
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="L05 双源夜检：持仓+信号池每日差异清单（只读）")
    ap.add_argument("--date", help="检查日 YYYY-MM-DD 或 YYYYMMDD，默认最近交易日")
    ap.add_argument("--lookback", type=int, default=30, help="比对窗口（自然日），默认 30")
    ap.add_argument("--pool-days", type=int, default=1, help="信号池取最近 N 个 scan_date，默认 1")
    ap.add_argument("--max-pool", type=int, default=150,
                    help="本次核查上限（限流降级，0=不限），默认 150")
    ap.add_argument("--codes", help="逗号分隔 6 位代码，显式覆盖标的范围")
    ap.add_argument("--tol-close", type=float, default=TOL_CLOSE)
    ap.add_argument("--tol-factor", type=float, default=TOL_FACTOR)
    ap.add_argument("--out", default=REPORT_DIR, help="报告输出目录，默认 reports/")
    args = ap.parse_args(argv)

    res = run_night_check(
        check_date=args.date, lookback_days=args.lookback, pool_days=args.pool_days,
        max_pool=args.max_pool,
        codes=[c.strip().zfill(6) for c in args.codes.split(",")] if args.codes else None,
        tol_close=args.tol_close, tol_factor=args.tol_factor, out_dir=args.out)
    print(f"[night_check] {res['check_date']} "
          f"{'交易日' if res['is_trading_day'] else '非交易日'} · "
          f"{res['universe']} 只标的 · {res['summary']}")
    print(f"[night_check] 报告：{res['report_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
