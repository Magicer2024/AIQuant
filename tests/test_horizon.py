"""tests/test_horizon.py —— 三周期（horizon）维度测试

覆盖：
1. rules_store.derive_horizon 持仓期 → 周期标签推导（10/60 边界）
2. init_db 对 strategy_rules.horizon 的一次性回填（幂等）
3. scan_mid_term / scan_long_term 合成 OHLCV 命中与不命中
4. /api/investor/today 三周期分组结构（无本地 quant.db 时跳过）
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest

import core.db as db
from core.db import get_conn, init_db
from strategy.rules_store import derive_horizon
from strategy.mid_long import scan_mid_term, scan_long_term


# ─────────────────────────────────────────────
# 1. derive_horizon 边界推导
# ─────────────────────────────────────────────
@pytest.mark.parametrize("holding_max,expected", [
    (1, "short"),
    (10, "short"),    # 边界：<=10 短期
    (11, "mid"),
    (60, "mid"),      # 边界：<=60 中期
    (61, "long"),
    (250, "long"),
    (None, "mid"),    # 空值按默认 20 天 → mid
    ("bad", "mid"),   # 非法值兜底 20 天 → mid
])
def test_derive_horizon(holding_max, expected):
    assert derive_horizon(holding_max) == expected


# ─────────────────────────────────────────────
# 2. init_db 一次性回填 strategy_rules.horizon
# ─────────────────────────────────────────────
@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """把数据库句柄重定向到临时文件（与 test_db_write 同款隔离）"""
    db_file = tmp_path / "quant_test.db"
    monkeypatch.setattr(db, "DB_PATH", str(db_file))
    init_db()
    return str(db_file)


def test_init_db_backfills_horizon(tmp_db):
    with get_conn() as conn:
        for name, hmax in [("r_short", 10), ("r_mid", 60), ("r_long", 120)]:
            conn.execute(
                "INSERT INTO strategy_rules (rule_name, rule_type, encoding, holding_max, horizon) "
                "VALUES (?, 'evolved', 'x', ?, NULL)", (name, hmax))
    # init_db 幂等重跑，触发回填
    init_db()
    with get_conn() as conn:
        rows = {r["rule_name"]: r["horizon"] for r in conn.execute(
            "SELECT rule_name, horizon FROM strategy_rules").fetchall()}
    assert rows == {"r_short": "short", "r_mid": "mid", "r_long": "long"}

    # 已回填的行不会被二次覆盖
    with get_conn() as conn:
        conn.execute("UPDATE strategy_rules SET horizon='long' WHERE rule_name='r_short'")
    init_db()
    with get_conn() as conn:
        hz = conn.execute(
            "SELECT horizon FROM strategy_rules WHERE rule_name='r_short'").fetchone()["horizon"]
    assert hz == "long", "已填充的 horizon 不应被回填覆盖"


def test_stock_signal_has_horizon_columns(tmp_db):
    """stock_signal 迁移后应具备 horizon/strategy 列，默认 'short'"""
    with get_conn() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(stock_signal)").fetchall()}
    assert "horizon" in cols
    assert "strategy" in cols


# ─────────────────────────────────────────────
# 合成 OHLCV 工具
# ─────────────────────────────────────────────
def _make_ohlcv(closes, volume=1_000_000):
    """由收盘价序列构造 OHLCV 日线（open=昨收，high/low=±1%），工作日索引"""
    n = len(closes)
    idx = pd.bdate_range("2024-01-02", periods=n)
    close = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": float(volume),
    }, index=idx)


# ─────────────────────────────────────────────
# 3. scan_long_term（纯逻辑，直接合成数据驱动）
# ─────────────────────────────────────────────
def test_scan_long_term_uptrend_hit():
    """稳定慢牛 300 日：MA60>MA120 + 斜率向上 + 低波 + 回撤受控 → 命中"""
    closes = [10 + 0.05 * i for i in range(300)]
    sig = scan_long_term(_make_ohlcv(closes))
    assert sig is not None
    assert sig["horizon"] == "long"
    assert sig["strategy"] == "长线趋势"
    assert 0 < sig["fusion_score"] <= 50
    assert sig["stop_loss"] < sig["buy_price"] < sig["take_profit"]
    assert sig["triggers"], "命中时应带中文理由"


def test_scan_long_term_downtrend_no_hit():
    """单边下跌 300 日（30→12，回撤约 60%）：趋势/斜率/回撤均不达标 → 不命中"""
    closes = [30 - 0.06 * i for i in range(300)]
    assert scan_long_term(_make_ohlcv(closes)) is None


def test_scan_long_term_insufficient_rows():
    """不足 250 行直接返回 None"""
    closes = [10 + 0.05 * i for i in range(100)]
    assert scan_long_term(_make_ohlcv(closes)) is None


# ─────────────────────────────────────────────
# 4. scan_mid_term（run_strategy 用 monkeypatch 隔离 strategy.py 内部细节）
# ─────────────────────────────────────────────
def _fake_strategy_df(df, score_last, buy=True, strong=False, stop=None, take=None):
    """伪造 run_strategy 的输出：最后一行带指定评分/信号"""
    result = df.copy()
    n = len(result)
    result["COMPOSITE_SCORE"] = [50.0] * (n - 1) + [score_last]
    result["BUY_SIGNAL"] = [False] * (n - 1) + [buy]
    result["STRONG_BUY"] = [False] * (n - 1) + [strong]
    # NaN 模拟 ATR 列缺失 → 触发止损止盈兜底
    result["STOP_LOSS"] = [float("nan")] * n if stop is None else stop
    result["TAKE_PROFIT"] = [float("nan")] * n if take is None else take
    result["MA5"] = result["MA10"] = result["MA20"] = 0.0
    result["DMI_ADX"] = 0.0
    return result


def test_scan_mid_term_hit(monkeypatch):
    """评分 70 ≥ 65 且当日 BUY_SIGNAL → 命中；ATR 缺失走 -8%/+20% 兜底"""
    import strategy.strategy as strat
    df = _make_ohlcv([10 + 0.02 * i for i in range(120)])
    monkeypatch.setattr(
        strat, "run_strategy",
        lambda d, name="composite": _fake_strategy_df(d, 70.0))
    sig = scan_mid_term(df)
    assert sig is not None
    assert sig["horizon"] == "mid"
    assert sig["strategy"] == "中线综合"
    assert sig["fusion_score"] == pytest.approx(35.0)  # 70/2 量纲对齐
    close = sig["buy_price"]
    assert sig["stop_loss"] == pytest.approx(round(close * 0.92, 2), abs=0.02)
    assert sig["take_profit"] == pytest.approx(round(close * 1.20, 2), abs=0.05)
    assert any("中线综合评分" in t for t in sig["triggers"])


def test_scan_mid_term_below_threshold(monkeypatch):
    """评分 60 < 65 → 不命中"""
    import strategy.strategy as strat
    df = _make_ohlcv([10 + 0.02 * i for i in range(120)])
    monkeypatch.setattr(
        strat, "run_strategy",
        lambda d, name="composite": _fake_strategy_df(d, 60.0))
    assert scan_mid_term(df) is None


def test_scan_mid_term_no_buy_signal(monkeypatch):
    """评分达标但当日无 BUY_SIGNAL（未上穿）→ 不命中"""
    import strategy.strategy as strat
    df = _make_ohlcv([10 + 0.02 * i for i in range(120)])
    monkeypatch.setattr(
        strat, "run_strategy",
        lambda d, name="composite": _fake_strategy_df(d, 70.0, buy=False))
    assert scan_mid_term(df) is None


def test_scan_mid_term_insufficient_rows():
    """不足 80 行直接返回 None（不触发 run_strategy）"""
    df = _make_ohlcv([10 + 0.02 * i for i in range(50)])
    assert scan_mid_term(df) is None


# ─────────────────────────────────────────────
# 5. /api/investor/today 三周期分组结构（依赖本地真实库，CI 跳过）
# ─────────────────────────────────────────────
_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "quant.db")


@pytest.mark.skipif(not os.path.exists(_DB), reason="需要本地 quant.db 真实数据")
def test_today_groups_structure():
    init_db()  # 模拟服务启动时的迁移（app.py __main__ 分支同样调用）
    from app import app
    app.config["TESTING"] = True
    client = app.test_client()

    resp = client.get("/api/investor/today?limit=5")
    assert resp.status_code == 200
    body = resp.get_json()
    data = (body or {}).get("data") or {}

    # 三周期分组结构
    assert "groups" in data, "响应应包含 groups"
    for key in ("short", "mid", "long"):
        assert key in data["groups"], f"groups 缺少 {key} 组"
        assert isinstance(data["groups"][key], list)
        assert len(data["groups"][key]) <= 5
        for it in data["groups"][key]:
            assert it.get("horizon") == key, "组内记录的 horizon 应与组名一致"

    # 旧前端兼容：items 为 short 组别名
    assert isinstance(data.get("items"), list)
    assert len(data["items"]) == len(data["groups"]["short"])

    # 单组过滤：mid 组不超过 3 条
    resp2 = client.get("/api/investor/today?horizon=mid&limit=3")
    assert resp2.status_code == 200
    d2 = (resp2.get_json() or {}).get("data") or {}
    assert len(d2["groups"]["mid"]) <= 3

    # 非法 horizon 返回错误
    resp3 = client.get("/api/investor/today?horizon=bad")
    body3 = resp3.get_json() or {}
    assert body3.get("success") is False or body3.get("ok") is False
