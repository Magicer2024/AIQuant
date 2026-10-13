"""
test_em_realtime_tencent.py —— T6 护栏：实时行情腾讯批量兜底
==========================================================
全部离线：mock HTTP 响应与股票池，不打任何真实网络请求。
锁住三件事：
  1. 腾讯单行解析 → 东财兼容字段（单位：成交量手、成交额万元→元）
  2. 批量请求次数 = ceil(股票数/500)，禁止退化为逐只请求
  3. 降级链顺序：东财 clist → 腾讯批量 → akshare
"""
import os
import sys
import math

import pandas as pd
import pytest

# 把项目根加入 import 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import em_realtime as em

# 东财 clist 路径输出的标准 13 列（腾讯兜底必须同构，前端/接口契约不变）
EXPECTED_COLUMNS = [
    "code", "name", "price", "change", "pct_change",
    "open", "preclose", "high", "low",
    "volume", "amount", "turnover", "pe",
]


def _make_tencent_line(code: str, mkt: str = "1") -> str:
    """构造一条 88 字段的腾讯快照文本（只填断言关心的字段）"""
    f = ["x"] * 88
    f[0] = mkt                       # 市场代码
    f[1] = f"名称{code}"             # 名称
    f[2] = code                      # 代码
    f[3] = "11.59"                   # 最新价
    f[4] = "11.78"                   # 昨收
    f[5] = "11.80"                   # 今开
    f[6] = "1078106"                 # 成交量（手）
    f[30] = "20261009161500"         # 快照时间
    f[31] = "-0.19"                  # 涨跌额
    f[32] = "-1.61"                  # 涨跌幅 %
    f[33] = "11.89"                  # 最高
    f[34] = "11.58"                  # 最低
    f[37] = "125972"                 # 成交额（万元）
    f[38] = "0.56"                   # 换手率 %
    f[39] = "5.18"                   # 市盈率
    return f'v_sz{code}="{ "~".join(f) }";'


class _FakeResponse:
    """模拟 requests.Response 的最小子集（text/encoding）"""

    def __init__(self, text: str):
        self.text = text
        self.encoding = "utf-8"


# ─────────────────────────────────────────────
# 1. 单行解析
# ─────────────────────────────────────────────
def test_parse_tencent_quote_line_mapping():
    row = em._parse_tencent_quote_line(_make_tencent_line("000001"))
    assert row is not None
    assert row["code"] == "000001"
    assert row["name"] == "名称000001"
    assert row["price"] == 11.59
    assert row["preclose"] == 11.78
    assert row["open"] == 11.80
    assert row["high"] == 11.89
    assert row["low"] == 11.58
    assert row["pct_change"] == -1.61
    assert row["change"] == -0.19
    assert row["volume"] == 1078106.0          # 手，与东财 f5 同口径
    assert row["amount"] == 125972.0 * 10000.0  # 万元 → 元
    assert row["turnover"] == 0.56
    assert row["pe"] == 5.18


@pytest.mark.parametrize("bad_line", [
    "",                                    # 空行
    "garbage without equals",              # 无 = 分隔
    'v_sz000001="";',                      # 空 body（停牌/无效代码占位）
    'v_sz000001="' + "~".join(["x"] * 30) + '";',  # 字段不足 40
    'v_xx999999="' + "~".join(["x"] * 88) + '";',  # 代码非 6 位数字
])
def test_parse_tencent_quote_line_invalid(bad_line):
    assert em._parse_tencent_quote_line(bad_line) is None


# ─────────────────────────────────────────────
# 2. 批量请求次数（T6 核心验收：禁止逐只）
# ─────────────────────────────────────────────
def test_tencent_batch_request_count_is_ceil_500(monkeypatch):
    n_codes = 1200  # 恰好 ceil=3 批
    codes = [f"{i:06d}" for i in range(n_codes)]
    codes_by_chunk = []  # 记录每个 HTTP 请求实际携带的代码数

    def _fake_get(url, *a, **kw):
        # URL 形如 https://qt.gtimg.cn/q=sz000000,sh600001,...
        syms = url.split("q=", 1)[1]
        codes_by_chunk.append(syms.count(",") + 1)
        body = "".join(
            _make_tencent_line(sym[-6:]) for sym in syms.split(",")
        )
        return _FakeResponse(body)

    # T10：em_realtime 已改从 stock_repo 直接导入，patch 必须跟随真实依赖路径
    import core.repository.stock_repo as stock_repo
    monkeypatch.setattr(stock_repo, "get_all_stocks",
                        lambda: pd.DataFrame({"code": codes}))
    monkeypatch.setattr(em._SESSION, "get", _fake_get)

    df = em._fetch_realtime_all_tencent()

    # 3 个请求覆盖 1200 只，而不是 1200 个请求（逐只退化会立即失败）
    assert len(codes_by_chunk) == math.ceil(n_codes / 500)
    assert codes_by_chunk == [500, 500, 200]
    assert len(df) == n_codes
    assert list(df.columns) == EXPECTED_COLUMNS


def test_tencent_batch_failed_chunk_continues(monkeypatch):
    """单批网络失败不应中断整轮（跳过该批，其余照常）"""
    # T10：patch 目标跟随 em_realtime 的真实导入源 stock_repo
    import core.repository.stock_repo as stock_repo
    codes = [f"{i:06d}" for i in range(600)]
    monkeypatch.setattr(stock_repo, "get_all_stocks",
                        lambda: pd.DataFrame({"code": codes}))

    call_n = {"i": 0}

    def _fake_get(url, *a, **kw):
        call_n["i"] += 1
        if call_n["i"] == 1:
            raise ConnectionError("模拟断连")
        syms = url.split("q=", 1)[1]
        return _FakeResponse("".join(_make_tencent_line(s[-6:])
                                     for s in syms.split(",")))

    monkeypatch.setattr(em._SESSION, "get", _fake_get)
    df = em._fetch_realtime_all_tencent()
    assert len(df) == 100  # 第一批 500 只丢失，第二批保留


# ─────────────────────────────────────────────
# 3. 降级链顺序：东财 → 腾讯 → akshare
# ─────────────────────────────────────────────
def test_fallback_em_down_uses_tencent(monkeypatch):
    tencent_df = pd.DataFrame([{"code": "000001", "price": 11.59}])
    calls = []

    monkeypatch.setattr(em, "_fetch_realtime_all_em",
                        lambda cb=None: (_ for _ in ()).throw(
                            ConnectionError("东财断连")))
    monkeypatch.setattr(em, "_fetch_realtime_all_tencent",
                        lambda cb=None: (calls.append("tencent"), tencent_df)[1])
    monkeypatch.setattr(em, "_fetch_realtime_all_akshare",
                        lambda cb=None: (calls.append("akshare"), pd.DataFrame())[1])

    out = em._fetch_realtime_with_fallback()
    assert out is tencent_df
    assert calls == ["tencent"]  # 腾讯成功后不再打 akshare


def test_fallback_em_and_tencent_down_uses_akshare(monkeypatch):
    akshare_df = pd.DataFrame([{"code": "600519", "price": 1263.0}])

    monkeypatch.setattr(em, "_fetch_realtime_all_em",
                        lambda cb=None: (_ for _ in ()).throw(
                            ConnectionError("东财断连")))
    monkeypatch.setattr(em, "_fetch_realtime_all_tencent",
                        lambda cb=None: pd.DataFrame())  # 腾讯空结果
    monkeypatch.setattr(em, "_fetch_realtime_all_akshare",
                        lambda cb=None: akshare_df)

    out = em._fetch_realtime_with_fallback()
    assert out is akshare_df
