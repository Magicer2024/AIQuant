"""回归：筹码影子列 chip_conc + 前向记录旁路表（core/sync.py + core/outcome_tracker.py）。

覆盖
----
A. 数据库迁移：stock_signal.chip_conc 列 + recommend_outcome_shadow 表
B. 线上排序键**逐字节不变**（short_chip_sort_prioritize 默认 0）
C. 排序开关打开时 chip_conc 键的**位置**正确（策略 CASE 之后、扩展度之前）
D. evaluate_outcomes 的表名白名单（防止 SQL 注入/写错表）
E. _build_signal_records：short 组写值、非 short 组记 NULL
F. **warmup 逻辑真的生效**：低换手股的记录值必须等于 1300+ 行口径，
   而不是增量路径 480 行口径（这是本次实现最关键的不变量）
G. 前向记录旁路表：写入后与线上基线组群体对齐

用法
----
    python tests/_verify_chip_shadow.py          # 只读校验（A~F）
    python tests/_verify_chip_shadow.py --write  # 额外在临时副本跑验证 G；不会修改源库
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if __name__ == "__main__":
    # 所有迁移和 --write 验证只在一致性副本执行，源库始终只读。
    import sqlite3
    import tempfile
    from pathlib import Path
    source = Path(os.getenv("AIQUANT_DB_PATH", str(Path(ROOT) / "core" / "quant.db")))
    work = Path(ROOT) / ".pytest_cache" / "isolated"
    work.mkdir(parents=True, exist_ok=True)
    target = Path(tempfile.mkdtemp(prefix="chip-", dir=work)) / "verify.db"
    if source.exists():
        with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as src:
            with sqlite3.connect(target) as dst:
                src.backup(dst)
    os.environ["AIQUANT_DB_PATH"] = str(target)
    os.environ["AIQUANT_TEST_ROOT"] = str(work)
    os.environ["AIQUANT_TESTING"] = "1"
    print(f"验证仅操作隔离副本：{target}")

# core/sync.py 顶层 `from qlib_engine.data_bridge import ...`，而 qlib_engine/__init__
# 顶层 `import qlib` —— 本测试只用 _build_signal_records（纯计算，不碰 qlib 数据层），
# 故 qlib 缺失时打最小桩，让回归能在任何环境跑；装了 qlib 则不打桩、走真实导入。
try:
    import qlib  # noqa: F401
except ImportError:
    import types
    _q = types.ModuleType("qlib")
    _qc = types.ModuleType("qlib.constant")
    _qc.REG_CN = "cn"
    _q.constant = _qc
    _q.init = lambda *a, **k: None
    sys.modules.setdefault("qlib", _q)
    sys.modules.setdefault("qlib.constant", _qc)

from core.db import get_conn, get_daily_price, init_db        # noqa: E402
from core import sync as s                                    # noqa: E402
from core import outcome_tracker as ot                        # noqa: E402
from config.strategy_params import get_param                  # noqa: E402

# 与 core/sync.py 的调用方同口径（SIG_THRESHOLD/STOP_LOSS_SC/TAKE_PROFIT_SC
# 都是那两个函数里的局部量，故按 _verify_atr_stop.py 的做法就地解析）
SIG_THRESHOLD, _ = s._resolve_sig_threshold()
STOP_LOSS_SC = float(get_param("short_stop_loss"))
TAKE_PROFIT_SC = float(get_param("short_take_profit"))

PASS, FAIL = [], []


def ck(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""))


# ───────────────────────── A. 迁移 ─────────────────────────

def t_migration():
    print("\n=== A. 数据库迁移 ===")
    init_db()
    with get_conn() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(stock_signal)").fetchall()]
        ck("stock_signal.chip_conc 列已建", "chip_conc" in cols,
           f"共 {len(cols)} 列")
        t = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='recommend_outcome_shadow'").fetchone()
        ck("recommend_outcome_shadow 表已建", t is not None)
        if t:
            scols = [r[1] for r in conn.execute(
                "PRAGMA table_info(recommend_outcome_shadow)").fetchall()]
            need = {"code", "scan_date", "horizon", "strategy", "entry_price",
                    "stop_loss", "take_profit", "fusion_score", "t1_return",
                    "t5_return", "exit_reason", "chip_conc", "in_baseline"}
            ck("影子表列名与 recommend_outcome 对齐（+chip_conc/in_baseline）",
               need.issubset(set(scols)), f"缺 {sorted(need - set(scols))}" if not need.issubset(set(scols)) else "")
        # 线上表不得被改动结构
        ocols = [r[1] for r in conn.execute(
            "PRAGMA table_info(recommend_outcome)").fetchall()]
        ck("线上 recommend_outcome 结构未被污染",
           "chip_conc" not in ocols and "in_baseline" not in ocols)


# ───────────────────────── B/C. 排序键 ─────────────────────────

OLD_ORDER = ("CASE WHEN s.strategy = '隔日动量' THEN 0 "
             "WHEN s.strategy = '缩量回踩' THEN 1 ELSE 2 END, "
             "COALESCE(s.pct_above_ma20, 0) ASC, "
             "COALESCE(s.fusion_score, 0) DESC")


def t_order():
    print("\n=== B/C. 排序键 ===")
    off = ot._short_order_keys("s", False, "ASC")
    ck("关闭时排序键与改造前**逐字节相同**", off == OLD_ORDER,
       "差异：" + repr(off) if off != OLD_ORDER else "")
    ck("short_chip_sort_prioritize 默认 = 0", int(get_param("short_chip_sort_prioritize")) == 0)
    ck("short_order_clause() 默认走关闭分支", ot.short_order_clause() == off)

    on = ot._short_order_keys("s", True, "ASC")
    i_case = on.index("ELSE 2 END")
    i_chip = on.index("chip_conc")
    i_ext = on.index("pct_above_ma20")
    ck("开启时 chip_conc 在策略 CASE 之后", i_chip > i_case)
    ck("开启时 chip_conc 在扩展度之前（关键，放后面完全无效）", i_chip < i_ext)
    ck("开启时 chip_conc 为 ASC + COALESCE 兜底（NULL 排最后）",
       "COALESCE(s.chip_conc, 9.9) ASC" in on)
    ck("开启时其余键位与关闭时一致",
       on.replace("COALESCE(s.chip_conc, 9.9) ASC, ", "") == off)
    # DESC 方向下同样成立
    on_d = ot._short_order_keys("s", True, "DESC")
    ck("降序方向下位置不变",
       on_d.index("chip_conc") < on_d.index("pct_above_ma20") < on_d.index("DESC"))
    # 影子组用的正是 chip_first=True 这同一份构造 ⇒ 不存在两处口径漂移
    ck("影子组与开关打开时共用同一构造函数（无口径漂移风险）",
       ot._short_order_keys("s", True, "ASC") == on)


# ───────────────────────── D. 表名白名单 ─────────────────────────

def t_table_guard():
    print("\n=== D. evaluate_outcomes 表名白名单 ===")
    for bad in ("stock_signal", "recommend_outcome; DROP TABLE x"):
        try:
            ot.evaluate_outcomes(bad)
            ck(f"非法表名被拒: {bad!r}", False, "未抛异常")
        except ValueError:
            ck(f"非法表名被拒: {bad[:30]!r}", True)


# ───────────────────────── E/F. 写列 + warmup ─────────────────────────

def t_write_and_warmup():
    print("\n=== E/F. 影子列写值 + warmup 生效 ===")
    WARM = 1500          # 必须与 core/sync.py::_CHIP_WARMUP_ROWS 一致

    # 从**真实信号池**挑标的（否则高换手票在 latest 上可能根本没有信号记录，
    # 导致 _build_signal_records 返回空、测不到东西）
    with get_conn() as conn:
        sig = pd.read_sql_query("""
            SELECT s.scan_date, s.code, i.name, i.circ_shares, i.total_shares
            FROM stock_signal s JOIN stock_info i ON i.code = s.code
            WHERE COALESCE(s.horizon,'short')='short'
              AND s.scan_date >= (SELECT date(MAX(trade_date), '-20 days')
                                  FROM daily_price)
            GROUP BY s.scan_date, s.code
        """, conn)
        if sig.empty:
            ck("有可测标的", False, "近 20 天无 short 信号记录")
            return
        _latest = conn.execute("SELECT MAX(trade_date) FROM daily_price").fetchone()[0]
        # 用信号日往前 20 个交易日的均量算换手率，用于分层
        pool = []
        for _, r in sig.iterrows():
            v = conn.execute(
                "SELECT AVG(volume) FROM daily_price WHERE code=? "
                "AND trade_date <= ? ORDER BY trade_date DESC LIMIT 20",
                (r["code"], r["scan_date"])).fetchone()[0]
            # 上面 LIMIT 在 ORDER BY 之后不生效于子集，改用窗口式子查询等价写法
            v = conn.execute("""
                SELECT AVG(volume) FROM (
                    SELECT volume FROM daily_price WHERE code=? AND trade_date <= ?
                    ORDER BY trade_date DESC LIMIT 20)
            """, (r["code"], r["scan_date"])).fetchone()[0]
            if v and r["circ_shares"]:
                pool.append((r["scan_date"], r["code"], r["name"],
                             r["circ_shares"], r["total_shares"],
                             float(v) / float(r["circ_shares"])))
    if not pool:
        ck("有可测标的（含股本）", False, "池内无可用股本")
        return
    print(f"  信号池 {len(pool)} 条（{len(set(p[1] for p in pool))} 只），"
          f"换手率 {min(p[5] for p in pool)*100:.3f}% ~ "
          f"{max(p[5] for p in pool)*100:.2f}%")

    low_set = sorted(pool, key=lambda x: x[5])[:3]
    high_set = sorted(pool, key=lambda x: -x[5])[:3]

    from strategy.chip import compute_chip_factors
    checked = {"低换手": 0, "高换手": 0}
    for tag, picks in (("低换手", low_set), ("高换手", high_set)):
        for scan_date, code, name, cs, ts, to in picks:
            # 忠实复刻增量路径的读取窗口（scan_date 前 700 自然日、截断到 scan_date）
            start = (pd.Timestamp(scan_date) - pd.Timedelta(days=700)).strftime("%Y-%m-%d")
            df = get_daily_price(code, start_date=start, end_date=scan_date)
            if df is None or len(df) < 60:
                continue
            recs = s._build_signal_records(
                df, code, name, ts, SIG_THRESHOLD,
                STOP_LOSS_SC, TAKE_PROFIT_SC, scan_date=scan_date,
                reuse_scores=True, circ_shares=cs)
            shorts = [x for x in recs if x.get("horizon") == "short"]
            others = [x for x in recs if x.get("horizon") != "short"]
            if not shorts:
                continue
            checked[tag] += 1

            vals = [x.get("chip_conc") for x in shorts]
            ck(f"[{tag}] {code} {scan_date} 换手{to*100:.2f}% chip_conc 有效",
               all(v is not None and np.isfinite(v) and 0.0 <= v <= 1.0 for v in vals),
               f"{vals[:2]}")
            ck(f"[{tag}] {code} 非 short 记录 chip_conc = NULL",
               all(x.get("chip_conc") is None for x in others),
               f"n_non_short={len(others)}")

            # 参考口径：同一日期、全历史 tail(WARM)。
            # ⚠ _chip_conc_at 返回值做了 round(v, 4)，故参考值也要同样取整再比
            # （否则会误判成"窗口不可复现"，实际只是显示精度差）。
            full = get_daily_price(code, end_date=scan_date)
            ref = float(compute_chip_factors(full.tail(WARM), cs)["conc"].get(
                pd.Timestamp(scan_date), np.nan))
            got = float(vals[0])
            ck(f"[{tag}] {code} 记录值 == 固定 {WARM} 行口径（可复现）",
               abs(got - round(ref, 4)) < 1e-9,
               f"取 {got:.4f} / 参考 {ref:.6f}→{round(ref,4):.4f}")

            # 窗口无关性（tail(WARM) 的真正目的）：同一 scan_date，用增量路径的
            # ~480 行 df 与全历史 df 跑，chip_conc 必须**逐位相同**
            # —— 全量重算路径传的是全历史，若无固定窗口两路径会写出不同的值。
            recs_full = s._build_signal_records(
                full, code, name, ts, SIG_THRESHOLD,
                STOP_LOSS_SC, TAKE_PROFIT_SC, scan_date=scan_date,
                reuse_scores=True, circ_shares=cs)
            vf = [x.get("chip_conc") for x in recs_full
                  if x.get("horizon") == "short"]
            ck(f"[{tag}] {code} 增量(480行df) 与 全量(全历史df) 写值一致",
               bool(vf) and vf[0] == got, f"{got} vs {vf[:1]}")

            if tag == "低换手":
                alt = float(compute_chip_factors(full.tail(480), cs)["conc"].get(
                    pd.Timestamp(scan_date), np.nan))
                if np.isfinite(alt) and abs(ref - alt) > 0.005:
                    ck(f"[低换手] {code} 480 行口径确实偏离（warmup 必要）", True,
                       f"480 行 {alt:.4f} vs {WARM} 行 {ref:.4f}，差 {alt-ref:+.4f}")
                else:
                    print(f"      · {code} 480 行与 {WARM} 行差仅 "
                          f"{alt-ref if np.isfinite(alt) else float('nan'):+.5f}"
                          f"（换手 {to*100:.2f}% 不够低，属正常）")

    ck("低换手样本已覆盖", checked["低换手"] > 0, f"{checked['低换手']} 只")
    ck("高换手样本已覆盖", checked["高换手"] > 0, f"{checked['高换手']} 只")


# ───────────────────────── G. 旁路表对齐 ─────────────────────────

def t_shadow_table():
    print("\n=== G. 前向记录旁路表（集成：临时写入 + 完整回滚）===")
    if "--write" not in sys.argv:
        print("  [SKIP] 未传 --write")
        return

    from strategy.chip import compute_chip_factors
    WARM = 1500

    with get_conn() as conn:
        n_null = conn.execute(
            "SELECT COUNT(*) FROM stock_signal WHERE chip_conc IS NOT NULL").fetchone()[0]

    # ── G0 实验前残渣自愈：预埋一行 chip_conc IS NULL 的旁路行（模拟"实验前 /
    #    调试期写入的基线副本"），跑一次 insert_new_outcomes 后**必须被清掉**。
    #    否则它因 scan_date < chip_ready 永不被区间重建覆盖，会**永久污染**
    #    对照样本与重叠度统计（实测库中曾残留 4 行 2026-09-08~09-11）。──
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO recommend_outcome_shadow "
            "(code, scan_date, horizon, strategy, entry_price, chip_conc) "
            "VALUES ('REGRESS_PROBE', '2020-01-02', 'short', '短线融合', 1.0, NULL)")
        conn.commit()

    # ── G1 守卫：chip_conc 全 NULL 时不得写入影子行（与 G0 共用同一次调用）──
    ot.insert_new_outcomes()
    with get_conn() as conn:
        junk = conn.execute(
            "SELECT COUNT(*) FROM recommend_outcome_shadow "
            "WHERE code = 'REGRESS_PROBE'").fetchone()[0]
        n_sh = conn.execute(
            "SELECT COUNT(*) FROM recommend_outcome_shadow").fetchone()[0]
    ck("实验前残渣（chip_conc IS NULL）被自动清理", junk == 0, f"残留 {junk} 行")
    if n_null == 0:
        ck("chip_conc 尚无数据 ⇒ 影子组为空（守卫生效，不灌基线副本）",
           n_sh == 0, f"影子表 {n_sh} 行")
    else:
        ck("已存在 chip_conc 数据 ⇒ 影子组应有行", n_sh > 0, f"{n_sh} 行")

    # ── 选一个基线组有名额的日期（否则两组都空，测不出对齐性）──
    with get_conn() as conn:
        r = conn.execute("""
            SELECT scan_date FROM recommend_outcome
            WHERE COALESCE(horizon,'short')='short' AND scan_date >= '2026-07-20'
            GROUP BY scan_date ORDER BY COUNT(*) DESC, scan_date DESC LIMIT 1
        """).fetchone()
        if not r:
            ck("找到基线组有记录的日期", False, "recommend_outcome 无 short 记录")
            return
        d = r[0]
        ba0 = [x[0] for x in conn.execute(
            "SELECT code FROM recommend_outcome WHERE scan_date=? "
            "AND COALESCE(horizon,'short')='short'", (d,)).fetchall()]
        codes = [x[0] for x in conn.execute(
            "SELECT code FROM stock_signal WHERE scan_date=? "
            "AND COALESCE(horizon,'short')='short'", (d,)).fetchall()]
    print(f"  测试日 {d}：基线 top{len(ba0)} = {ba0}，池内 short 信号 {len(codes)} 条")

    def _sh_rows():
        with get_conn() as conn:
            return [dict(x) for x in conn.execute(
                "SELECT code, chip_conc, in_baseline FROM recommend_outcome_shadow "
                "WHERE scan_date=? ORDER BY chip_conc, code", (d,)).fetchall()]

    # 旁路表 id 快照：回滚必须恢复到这个集合，而不是只删测试日。
    # ⚠ insert_new_outcomes 的重建窗口 = [max(start_date, chip_ready), ∞)，测试把
    #   chip_ready 人为提前到 d ⇒ 窗口内**其他日期**（conc 全 NULL、退化成基线副本）
    #   的行也会被写回，只删 d 会留下残留（曾实测残留 4 行 2026-09-08~09-11）。
    with get_conn() as conn:
        before_ids = {r[0] for r in conn.execute(
            "SELECT id FROM recommend_outcome_shadow").fetchall()}

    def _set(vals: dict):
        with get_conn() as conn:
            conn.executemany(
                "UPDATE stock_signal SET chip_conc=? WHERE scan_date=? AND code=? "
                "AND COALESCE(horizon,'short')='short'",
                [(v, d, c) for c, v in vals.items()])
            conn.commit()

    try:
        # ── G2 对齐性精确证明：把该日全部 short 记录写成**同一个 conc**，
        #    此时筹码键退化为并列 ⇒ 影子组必须与基线组**完全同集**。
        #    若两侧 WHERE 有任何差异，集合必然不同 —— 这是唯一能精确抓住
        #    "影子组漏套过滤条件"这类 bug 的测试（曾实际发生：误用 for 循环残留的
        #    long 组空 WHERE，导致 2026-09-15 基线 0 条 vs 影子 3 条）。
        _set({c: 0.5 for c in codes})
        ot.insert_new_outcomes()
        sh_u = _sh_rows()
        with get_conn() as conn:
            ba_u = sorted(x[0] for x in conn.execute(
                "SELECT code FROM recommend_outcome WHERE scan_date=? "
                "AND COALESCE(horizon,'short')='short'", (d,)).fetchall())
        ck("均匀 conc ⇒ 影子组与基线组**完全同集**（WHERE 对齐性精确证明）",
           sorted(x["code"] for x in sh_u) == ba_u,
           f"影子 {sorted(x['code'] for x in sh_u)} vs 基线 {ba_u}")

        # ── G3 真实 conc：验证排序确实换票 ──
        real = {}
        for c in codes:
            with get_conn() as conn:
                sh = conn.execute(
                    "SELECT circ_shares, total_shares FROM stock_info WHERE code=?",
                    (c,)).fetchone()
            cs = (sh["circ_shares"] or sh["total_shares"]) if sh else None
            full = get_daily_price(c, end_date=d)
            if full is None or len(full) < 30 or not cs:
                continue
            v = float(compute_chip_factors(full.tail(WARM), cs)["conc"].get(
                pd.Timestamp(d), np.nan))
            if np.isfinite(v):
                real[c] = round(v, 4)
        _set(real)
        ot.insert_new_outcomes()
        sh = _sh_rows()
        with get_conn() as conn:
            ba = sorted(x[0] for x in conn.execute(
                "SELECT code FROM recommend_outcome WHERE scan_date=? "
                "AND COALESCE(horizon,'short')='short'", (d,)).fetchall())

        ck("真实 conc 写出", len(real) > 0, f"{len(real)} 条")
        ck("影子组与基线组条数一致（同 top_n 口径）", len(sh) == len(ba),
           f"影子 {len(sh)} vs 基线 {len(ba)}")
        ck("影子组 chip_conc 全部有值", all(x["chip_conc"] is not None for x in sh))
        ck("影子组按 chip_conc 升序",
           all(sh[i]["chip_conc"] <= sh[i + 1]["chip_conc"]
               for i in range(len(sh) - 1)))
        ov = sum(x["in_baseline"] for x in sh) / len(sh) * 100 if sh else 0
        print(f"  重叠度 {ov:.0f}%（{sum(x['in_baseline'] for x in sh)}/{len(sh)}）")
        print(f"  影子组 {[(x['code'], x['chip_conc']) for x in sh]}")
        print(f"  基线组 {ba}")
        s_conc = [x["chip_conc"] for x in sh]
        b_conc = [real[c] for c in ba if c in real]
        ck("影子组 conc 均值 ≤ 基线组（排序键方向正确）",
           bool(s_conc) and bool(b_conc) and
           np.mean(s_conc) <= np.mean(b_conc) + 1e-9,
           f"影子 {np.mean(s_conc):.4f} vs 基线 {np.mean(b_conc):.4f}")
        # 对齐性的第二重：两者的"候选池"必须相同 ⇒ 影子组 ⊆ 该日 short 记录
        ck("影子组 ⊆ 该日 short 记录（无越界选票）",
           all(x["code"] in set(codes) for x in sh))
    finally:
        # ── 完整回滚：chip_conc 复位 NULL + 把旁路表恢复到测试前的 id 集合 ──
        with get_conn() as conn:
            conn.execute(
                "UPDATE stock_signal SET chip_conc = NULL WHERE scan_date = ?", (d,))
            conn.execute(
                "DELETE FROM recommend_outcome_shadow WHERE scan_date = ?", (d,))
            extra = [r[0] for r in conn.execute(
                "SELECT id FROM recommend_outcome_shadow").fetchall()
                if r[0] not in before_ids]
            if extra:
                conn.executemany(
                    "DELETE FROM recommend_outcome_shadow WHERE id = ?",
                    [(i,) for i in extra])
            conn.commit()
            left = conn.execute(
                "SELECT COUNT(*) FROM stock_signal WHERE chip_conc IS NOT NULL").fetchone()[0]
            left_sh = conn.execute(
                "SELECT COUNT(*) FROM recommend_outcome_shadow").fetchone()[0]
        ck("已回滚：chip_conc 无残留", left == n_null, f"残留 {left}（原 {n_null}）")
        ck("已回滚：旁路表恢复到测试前状态", left_sh == len(before_ids),
           f"剩 {left_sh} 行（原 {len(before_ids)} 行）")
        if extra:
            print(f"  （另清理测试期间写回的 {len(extra)} 行其他日期记录）")


def main():
    print("=" * 74)
    print("回归：筹码影子列 + 前向记录旁路表")
    print("=" * 74)
    t_migration()
    t_order()
    t_table_guard()
    t_write_and_warmup()
    t_shadow_table()
    print("\n" + "=" * 74)
    print(f"结果：PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
    else:
        print("  全部通过")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
