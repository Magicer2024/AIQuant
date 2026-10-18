"""方案A：详情页时间线改读每日落库快照（stock_deep_daily_level / stock_deep_signal）。

覆盖验收口径（量化策略工程师 2026-10-18 裁定，测试A逐条核验项的后端部分）：
- 新表 schema：PK(scan_date,code)，无 entry/stop/tp 列，与 stock_deep_signal 严格分表；
- 扫描写入侧：全票落最终 level（含 ext>15%/ST），stock_deep_signal 只收候选、链路不变；
- 时间线覆盖：快照日 source=scan_snapshot（buy/add 三点价取当日落库）；
  无快照日 source=recompute：无三点计划、label 带"（重算·参考）"、不进统计样本。
"""
import json
import sqlite3

import pandas as pd
import pytest

import strategy.stock_deep as sd


# ─────────────────────────────────────────────
# 夹具与合成数据
# ─────────────────────────────────────────────
def _trade_dates(n: int, end: str = "2026-10-15") -> list:
    rng = pd.bdate_range(end=end, periods=n)
    return [d.strftime("%Y-%m-%d") for d in rng]


def _seed_prices(conn, code: str, dates: list, start: float = 10.0, step: float = 0.02):
    """合成一段温和上行的日线（60+ 根，满足 analyze_stock 最低数据量）。"""
    rows = []
    px = start
    for d in dates:
        o = round(px * 0.995, 2)
        c = round(px * (1 + step), 2)
        h = round(c * 1.01, 2)
        l = round(o * 0.99, 2)
        rows.append((code, d, o, h, l, c, 1_000_000.0, 50_000_000.0,
                     round((c / px - 1) * 100, 2)))
        px = c
    conn.executemany(
        "INSERT OR REPLACE INTO daily_price(code,trade_date,open,high,low,close,"
        "volume,amount,pct_change) VALUES (?,?,?,?,?,?,?,?,?)", rows)


def _seed_stock(conn, code: str, name: str):
    conn.execute(
        "INSERT OR REPLACE INTO stock_info(code,name,market,is_active) VALUES (?,?,?,1)",
        (code, name, "主板"))


@pytest.fixture()
def conn(isolated_database):
    c = sqlite3.connect(isolated_database, timeout=30)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


# ─────────────────────────────────────────────
# 1) 新表 schema：独立、无三点价列、PK 幂等
# ─────────────────────────────────────────────
def test_daily_level_table_schema_and_upsert(conn):
    sd._ensure_daily_level_table(conn)
    conn.commit()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(stock_deep_daily_level)")}
    assert {"scan_date", "code", "level", "score", "base_score", "rhythm_hint"} <= cols
    # 关键红线：不得混入三点价/候选列 —— 推荐链数据源保持纯净
    assert not {"entry_price", "stop_loss", "take_profit", "reasons", "name"} & cols

    rows = [{"code": "601026", "level": "buy", "score": 5, "base_score": 3,
             "rhythm_hint": "上升初期"}]
    sd._insert_daily_levels(conn, "2026-10-13", rows)
    sd._insert_daily_levels(conn, "2026-10-13",
                            [{"code": "601026", "level": "hold", "score": 1,
                              "base_score": 3, "rhythm_hint": "回调初期"}])
    conn.commit()
    got = conn.execute(
        "SELECT level, score FROM stock_deep_daily_level "
        "WHERE scan_date='2026-10-13' AND code='601026'").fetchall()
    assert len(got) == 1 and got[0]["level"] == "hold"   # PK(scan_date,code) 覆盖写


# ─────────────────────────────────────────────
# 2) 扫描写入侧：全票 level 落新表；stock_deep_signal 只收候选（链路不变）
# ─────────────────────────────────────────────
def _fake_light_result(code, name, level, score, ext):
    return {
        "code": code, "name": name, "industry": "X", "insufficient": False,
        "trend": {"pct_above_ma20": ext, "pct_above_ma60": ext},
        "volume_price": {"obv_grad_pct": 0.5, "volume_ratio": 1.1},
        "rhythm": {"position_hint": "上升初期", "current_leg": "上升", "pattern": "波段震荡"},
        "signal": {"level": level, "label": sd._LEVEL_META[level][0],
                   "emoji": sd._LEVEL_META[level][1], "score": score,
                   "base_score": score - 2, "frac20": 0.4, "risk_pct": 5.0,
                   "atr_pct": 3.0, "reasons": ["r1"], "risks": ["k1"],
                   "rhythm_note": "上升初期",
                   "action_plan": {"entry_price": 10.0, "stop_loss": 9.0,
                                   "take_profit": 11.0}},
    }


def test_scan_writes_all_levels_but_signal_keeps_candidates_only(conn, monkeypatch):
    dates = _trade_dates(70)
    pool = {"600001": ("-clean buy-", "buy", 7, 2.0),
            "600002": ("高扩展buy", "buy", 6, 20.0),      # ext>15%：候选剔除但 level 照写
            "600003": ("普通hold", "hold", 0, 1.0),
            "600004": ("ST红票", "buy", 8, 1.0)}          # ST：候选剔除但 level 照写
    for code, (name, *_rest) in pool.items():
        _seed_stock(conn, code, name)
        _seed_prices(conn, code, dates)
    conn.commit()

    monkeypatch.setattr(
        sd, "analyze_stock",
        lambda c_, code, lookback=260, recent_days=45, light=False, as_of=None:
            _fake_light_result(code, pool[code][0], pool[code][1], pool[code][2], pool[code][3]))
    sd._ensure_market_signal_table(conn)
    res = sd.run_full_market_scan(conn, scan_date=dates[-1], as_of=dates[-1], batch=1)

    assert res["candidates"] == 1 and res["levels"] == 4
    sig_codes = {r["code"] for r in conn.execute(
        "SELECT code FROM stock_deep_signal WHERE scan_date=?", (dates[-1],))}
    assert sig_codes == {"600001"}                       # 候选筛选口径逐字节不变
    lv = {r["code"]: r["level"] for r in conn.execute(
        "SELECT code, level FROM stock_deep_daily_level WHERE scan_date=?", (dates[-1],))}
    assert lv == {"600001": "buy", "600002": "buy", "600003": "hold", "600004": "buy"}

    # 重扫覆盖：同日重跑不产生重复行
    sd.run_full_market_scan(conn, scan_date=dates[-1], as_of=dates[-1], batch=1)
    n = conn.execute("SELECT COUNT(*) FROM stock_deep_daily_level WHERE scan_date=?",
                     (dates[-1],)).fetchone()[0]
    assert n == 4


# ─────────────────────────────────────────────
# 3) 时间线快照覆盖 + 重算日硬规则 + chart.plan 对齐
# ─────────────────────────────────────────────
def test_timeline_overlay_snapshot_vs_recompute(conn):
    code = "601026"
    dates = _trade_dates(70)
    _seed_stock(conn, code, "道生天合")
    _seed_prices(conn, code, dates)
    sd._ensure_market_signal_table(conn)
    sd._ensure_daily_level_table(conn)
    conn.commit()

    d_buy, d_hold, d_gap = dates[60], dates[61], dates[62]
    # 当日落库的真实推荐快照（buy，含三点价）
    conn.execute(
        "INSERT OR REPLACE INTO stock_deep_signal(scan_date,code,name,level,score,"
        "base_score,rhythm_hint,entry_price,stop_loss,take_profit,reasons) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (d_buy, code, "道生天合", "buy", 5, 3, "上升初期",
         14.94, 13.74, 15.84, json.dumps(["均线多头排列，趋势向上"], ensure_ascii=False)))
    # 全档位快照（hold 日）；d_gap 两表皆无行 → 必须走重算标注
    sd._insert_daily_levels(conn, d_hold, [
        {"code": code, "level": "hold", "score": 1, "base_score": 3, "rhythm_hint": "回调初期"}])
    conn.commit()

    res = sd.analyze_stock(conn, code, recent_days=45)
    adv = {a["date"]: a for a in res["recent_advice"]}

    a = adv[d_buy]
    assert a["source"] == "scan_snapshot" and a["level"] == "buy"
    assert (a["entry_price"], a["stop_loss"], a["take_profit"]) == (14.94, 13.74, 15.84)
    assert a["plan_source"] == "scan_signal"
    assert a["score"] == 5 and a["base_score"] == 3
    assert "重算" not in a["label"]

    a = adv[d_hold]
    assert a["source"] == "scan_snapshot" and a["level"] == "hold"
    assert a["entry_price"] is None and a["stop_loss"] is None and a["take_profit"] is None
    assert "重算" not in a["label"]

    a = adv[d_gap]
    assert a["source"] == "recompute"
    assert a["label"].endswith("（重算·参考）")
    assert a["entry_price"] is None and a["stop_loss"] is None and a["take_profit"] is None

    # 卡片头部固定文案（前端渲染口径）
    assert res["recent_advice_note"] == sd._RECENT_ADVICE_NOTE

    # chart.plan 与最终时间线逐根对齐：快照 buy 日给落库三点价，重算日三点价为 None
    cdates = res["chart"]["dates"]
    i_buy, i_gap = cdates.index(d_buy), cdates.index(d_gap)
    plan = res["chart"]["plan"]
    assert plan["source"][i_buy] == "scan_snapshot"
    assert plan["entry"][i_buy] == 14.94 and plan["stop"][i_buy] == 13.74
    assert plan["level"][i_buy] == "buy"
    assert plan["source"][i_gap] == "recompute"
    assert plan["entry"][i_gap] is None and plan["stop"][i_gap] is None
    # 与 chart.dates 同长同序（前端按下标取数的前提）
    assert len(plan["entry"]) == len(plan["source"]) == len(cdates)

    # 统计样本：窗口内唯一 buy/add 只能是快照日 d_buy（重算日不进样本）
    ps = res["position_stats"]
    if ps:
        assert {t["signal_date"] for t in ps["trades"]} == {d_buy}
        # plan_source 审计透传：d_buy 三点价来自当日落库候选行
        assert {t["plan_source"] for t in ps["trades"]} == {"scan_signal"}


# ─────────────────────────────────────────────
# 4) compute_position_stats 硬规则：recompute 不进统计；旧调用方（无 source）兼容
# ─────────────────────────────────────────────
def test_position_stats_recompute_exclusion(conn):
    code = "600111"
    dates = _trade_dates(70)
    _seed_stock(conn, code, "样本股")
    _seed_prices(conn, code, dates)
    conn.commit()
    df = sd._compute(sd.load_history(conn, code, lookback=260))
    base = df.iloc[-30]
    row = {"date": df.index[-30], "close": float(base["close"]),
           "pct_change": float(base["pct_change"]),
           "level": "buy", "label": "建议买入", "emoji": "🟢", "brief": "x",
           "entry_price": round(float(base["close"]), 2),
           "stop_loss": round(float(base["close"]) * 0.93, 2),
           "take_profit": round(float(base["close"]) * 1.10, 2)}

    # 无 source（外部研究脚本旧口径）→ 照常进样本，行为不变
    stats_legacy = sd.compute_position_stats(df, [dict(row)], include_curve=False)
    assert stats_legacy and stats_legacy["n_trades"] == 1
    assert stats_legacy["trades"][0]["plan_source"] is None   # 旧调用方无该字段 → None
    # source=recompute → 硬规则排除 → 无任何可回测信号返回 None
    stats_recompute = sd.compute_position_stats(
        df, [dict(row, source="recompute")], include_curve=False)
    assert stats_recompute is None
    # source=scan_snapshot → 进样本；plan_source 审计字段逐笔透传（裁定3精修小改）
    stats_snapshot = sd.compute_position_stats(
        df, [dict(row, source="scan_snapshot", plan_source="recompute_fallback")],
        include_curve=False)
    assert stats_snapshot and stats_snapshot["n_trades"] == 1
    assert stats_snapshot["trades"][0]["plan_source"] == "recompute_fallback"


# ─────────────────────────────────────────────
# 5) 历史回放（as_of 指定）不被当前库快照污染 —— 回补因果性保持
# ─────────────────────────────────────────────
def test_as_of_replay_skips_snapshot_overlay(conn):
    code = "601026"
    dates = _trade_dates(70)
    _seed_stock(conn, code, "道生天合")
    _seed_prices(conn, code, dates)
    sd._ensure_market_signal_table(conn)
    conn.commit()
    # 即便库里有一条与回放日同日的 buy 快照，as_of 路径也不读它（回放自身即因果快照）
    d_in_window = dates[65]
    conn.execute(
        "INSERT OR REPLACE INTO stock_deep_signal(scan_date,code,level,score,entry_price) "
        "VALUES (?,?,?,?,?)", (d_in_window, code, "buy", 9, 99.99))
    conn.commit()
    res = sd.analyze_stock(conn, code, recent_days=45, as_of=dates[-3])
    adv = {a["date"]: a for a in res["recent_advice"]}
    assert adv[d_in_window].get("source") is None
    assert adv[d_in_window]["entry_price"] != 99.99
