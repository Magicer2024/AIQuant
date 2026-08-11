"""
config/strategy_params.py —— 各策略默认参数
"""
from typing import Dict, Any

# ── 5策略融合权重 ──────────────────────────────
# 顺序：[放量突破, 均线粘合, 量价背离, 抄底, 主力建仓]
DEFAULT_WEIGHTS = [0.30, 0.15, 0.20, 0.20, 0.15]

# 纯抄底：短线推荐链路（core.sync.recalc_all_scores）与 daily_price.fusion_score
# 实际使用的权重。2026-04 与 2026-07-31 两轮回测都是它最优，见下方回测结论。
PURE_BOTTOM_WEIGHTS = [0.00, 0.00, 0.00, 1.00, 0.00]

# ── 融合方式 ───────────────────────────────────
# "weighted_avg" = 归一化加权平均：Σ(分_i×权_i)/Σ权 × 策略数
#   结构缺陷：单项再强也被其他策略低分稀释。熊市权重下「抄底」满分 10/10 仅得
#   15 分 < 高波动阈值 18，数学上永不成信号；入选的都是"样样中不溜"的票。
#   实测 2026-07-31 全市场 4491 只：阈值18 有 55.8% 过线，落库分布仅 18.0~26.1
#   （满分50），排序区分度极低。
# "max"          = 取最大值：max(分_i × 权_i/max(权)) × 5
#   权重退化为置信度折扣，任何单一策略足够强即可独立成信号，专才不再被埋。
#
# 2026-07-31 五组对照回测（tools/eval_fusion_mode.py，过滤链/收益口径完全一致）
# ① 隔日 OC 口径 · 每日Top8 · 样本外验证窗（条数对齐，排除样本量摊薄）
#   组别            短窗 n=184        长窗 n=1432
#   平均+自适应     53.26% +0.564%    47.28% +0.234%   ← 改造前线上口径
#   平均+均衡       53.26% +0.581%    47.49% +0.260%
#   取最大+自适应   59.78% +1.544%    48.88% +0.318%
#   取最大+均衡     58.15% +1.497%    48.71% +0.334%
#   纯抄底          58.70% +1.531%    48.18% +0.354%
# ② 组合口径 · 长窗 2024-02~2026-07 全窗 · 474~481 笔（10日持仓/+12%/-6%/持仓5）
#   组别            胜率    PF      最大回撤   总收益
#   平均+自适应     40.08%  0.867   -50.28%   -34.62%  ← 五组最差
#   平均+均衡       39.53%  0.898   -47.13%   -31.97%
#   取最大+自适应   41.75%  0.887   -45.03%   -36.28%
#   取最大+均衡     42.20%  0.923   -40.07%   -27.29%
#   纯抄底          44.35%  1.066   -27.22%   +29.19%  ← 唯一赚钱，年化+10.81%
#   （短窗组合口径仅 57~64 笔，方向与长窗相反，样本不足不采信）
# 结论：a) 取最大稳定优于加权平均（两窗OC/CC + 长窗组合口径四次同向）；
#      b) 纯抄底显著优于任何五策略融合 → 短线链路固定 PURE_BOTTOM_WEIGHTS；
#      c) 自适应权重无正贡献（组合口径两次对照均为负），只保留自适应阈值。
# 本项仅在将来重新启用多策略融合时生效（纯抄底下两种方式数学等价）。
FUSION_MODE = "max"

# ── P3: 自适应配置 ─────────────────────────────
# 总开关。注意：2026-07-31 起自适应**只影响阈值**（高波动 15→18）与前端市场状态
# 展示，不再改动短线打分权重（回测证明改权重是负贡献，见上）。
ADAPTIVE_WEIGHTS_ENABLED = True

# 以下三档 regime 权重目前只用于前端「市场状态」展示与将来的多策略融合实验，
# 不参与 core.sync.recalc_all_scores 的短线打分。
# 牛市/上升趋势：加重放量突破+均线
BULL_WEIGHTS = [0.35, 0.25, 0.15, 0.10, 0.15]

# 震荡市：默认均衡
RANGE_WEIGHTS = [0.30, 0.15, 0.20, 0.20, 0.15]

# 熊市/下跌趋势：加重抄底+背离
BEAR_WEIGHTS = [0.15, 0.10, 0.30, 0.30, 0.15]

# 融合分阈值（SIG_THRESHOLD）
DEFAULT_SIG_THRESHOLD = 15.0
HIGH_VOL_THRESHOLD = 0.02       # 20日波动率 > 2% 视为高波动
HIGH_VOL_SIG_THRESHOLD = 18.0   # 高波动时提高阈值

# ── 策略1：放量突破 ────────────────────────────
VOLUME_BREAKOUT = {
    "vol_factor": 1.5,    # 放量倍数
    "rise_3d": 0.02,      # 3日涨幅最小值
    "score_min": 1.0,     # 触发所需最少条件数
}

# ── 策略2：均线粘合 ────────────────────────────
MA_CONVERGENCE = {
    "ma_diff_pct": 0.03,  # MA5/10/20 最大偏差
    "score_min": 1.0,
}

# ── 策略3：量价背离 ────────────────────────────
PRICE_VOLUME_DIVERGENCE = {
    "lookback": 20,       # 回溯天数
    "score_min": 1.0,
}

# ── 策略4：抄底 ────────────────────────────────
BOTTOM_FISHING = {
    "drop_5d": -0.08,     # 5日跌幅阈值
    "rsi_threshold": 30,  # RSI 超卖线
    "score_min": 1.0,
}

# ── 策略5：主力建仓 ────────────────────────────
WHALE_ACCUMULATION = {
    "volume_spike": 2.0,  # 成交量突增倍数
    "score_min": 1.0,
}

# ── 超跌反弹 v4 策略 ───────────────────────────
OVERSOLD_REBOUND_V4 = {
    "score_threshold": 1.8,
    "stop_loss": -0.06,
    "trailing_pct": 0.10,
    "single_pos_ratio": 0.40,
    "use_market_timing": True,
    "use_trailing_stop": True,
}

# ── v4 参数扫描网格 ────────────────────────────
V4_PARAM_GRID = [
    {"name": "v4 tr10% pos30% sl=-5%", "score_threshold": 1.8, "stop_loss": -0.05, "trailing_pct": 0.10, "single_pos_ratio": 0.30},
    {"name": "v4 tr10% pos40% sl=-5%", "score_threshold": 1.8, "stop_loss": -0.05, "trailing_pct": 0.10, "single_pos_ratio": 0.40},
    {"name": "v4 tr12% pos40% sl=-5%", "score_threshold": 1.8, "stop_loss": -0.05, "trailing_pct": 0.12, "single_pos_ratio": 0.40},
    {"name": "v4 tr15% pos40% sl=-5%", "score_threshold": 1.8, "stop_loss": -0.05, "trailing_pct": 0.15, "single_pos_ratio": 0.40},
    {"name": "v4 tr10% pos50% sl=-5%", "score_threshold": 1.8, "stop_loss": -0.05, "trailing_pct": 0.10, "single_pos_ratio": 0.50},
]


# ── 短线推荐引擎开关 ───────────────────────────
# "pure_bottom"      = 纯抄底融合分（现网，已叠加趋势闸门优化）
# "oversold_rebound" = 超跌反弹v3（趋势闸门+质量过滤，备选）
# 2026-07-29 决策：不替换引擎，在纯抄底上叠加趋势闸门（MA20向上）修复接飞刀问题。
# 四组回测（近1年全池，隔日OC口径）：
#   纯抄底           n=144733  胜率46.26%  平均-0.043%
#   纯抄底+闸门      n= 44565  胜率46.30%  平均-0.003%  ← 采用（砍掉69%阴跌途中信号）
#   纯抄底+闸门+质量 n= 33838  胜率46.42%  平均-0.002%
#   v3+闸门+质量     n= 17022  胜率46.40%  平均+0.045%（组合口径PF较差，不采用）
# 可选值：
#   "pure_bottom"      v1 已反弹（历史默认；2026-08-09 已由 pure_bottom_v2 转正接管）
#   "pure_bottom_v2"   买回踩平滑版（2026-08-09 转正为线上默认；tools/_eval_pullback.py
#                      34 cohort T+1 OC 胜率 51.5%→55.5%、均值 +0.104%→+0.211%，
#                      候选量约减半、候选 MA20 偏离 +5.44%→+3.85%；切换后已跑
#                      recalc_all_scores 重算 daily_price 分数列与历史 stock_signal）
#   "oversold_rebound" 超跌反弹v3（历史备选）
SHORT_ENGINE = "pure_bottom_v2"

# 短线趋势闸门：过滤下跌途中的假反弹（要求站上均线且均线向上）
SHORT_TREND_GATE = {
    "enabled": True,
    "ma": 20,              # 均线周期
    "slope_lookback": 5,   # 均线斜率回看天数（今日 MA >= N 日前 MA 视为向上）
}

# 推荐质量硬过滤：ST 剔除 + 流动性 + 市值区间（缺 total_shares 自动跳过市值项）
# 2026-08-07 调整：max_mktcap 由 800亿 放宽到 3000亿。依据 tools/eval_quality_filter.py +
# 市值分桶验证（tools/ 分析）：>1500亿 桶信号 OC 均值/盈亏比在长短两窗均最高，800亿 上限
# 砍掉了质量最好的 ~8.7% 信号；放宽对每日 Top8 无劣化（超大盘 fs 排序难进前 8）。
QUALITY_FILTER = {
    "enabled": True,
    "exclude_st": True,
    "min_amt20": 80_000_000,       # 近20日日均成交额下限（元）
    "min_mktcap": 3_000_000_000,   # 总市值下限（元，剔除微盘）
    "max_mktcap": 300_000_000_000,  # 总市值上限（元，放宽到 3000亿：>3000亿的极超大盘仍剔除）
}

# ── 追高否决过滤（2026-08-05 新增）────────────────────────────
# 背景：000815 三连板启动期（7-31/8-03 融合分 23.5/26.3 超阈值）被趋势闸门
#       （MA20 未拐头）挡掉；8-05 涨停打开放量日，抄底型"反弹强度+放量"双满分，
#       融合分 35.74 排当日第 2 进今日推荐。而"连续涨停≥3 后次日开盘买入"历史
#       596 样本回测负期望（次日胜率 41.6%、持有10日均值-5.7%、止损率69%）。
#       故写库前新增硬否决：连板当日/连板后冷却期/涨停打开/急涨，任一命中即不推荐。
CHASE_FILTER = {
    "enabled": True,
    "max_consecutive_limit": 3,   # 当日连续涨停 >= 3 → 当日否决
    "cooldown_consecutive": 3,    # 近 cooldown_days 个交易日内出现过 >= 3 连板 → 否决
    "cooldown_days": 5,           # 连板后遗症冷却窗口（交易日，000815 8-05 落在 7-31~8-04 三连板后）
    "limit_pct": 9.8,             # 涨停判定阈值（主板 10%，留 0.2pct 容差）
    "reject_limit_open": True,    # 当日盘中触板(>=+9.5%)但收盘未封住(<+9.8%) → 涨停打开否决
    "max_ret_3d": 0.25,           # 近3日涨幅上限（>=25% 否决，覆盖 20cm 板急拉）
}


# ── 扩展度否决（2026-08 短线优化 P0-1.2）──────────────────────────
# 背景：diag_short_reco 实测候选均值偏离 MA20 +6.3%、>8% 占 26.9%；
# 高位组（>MA20 8%）T+1 OC 48.3%/+0.04% vs 低位组（<MA20 2%）52.2%/+0.28%，
# 低扩展度入场更优 → 价 > MA20×1.12 硬否决（易均值回归尾部），写库前与 chase 同链。
# enabled=False 即一键降级，不影响任何既有链路。
EXTENSION_FILTER = {
    "enabled": True,
    "max_pct_above_ma20": 0.12,   # 价 > MA20×(1+12%) → 硬否决
}


# ── RSI 甜区过滤器（2026-08 短线优化 P3-1）────────────────────────
# 背景：tools/backtest_enhance.py E3 回测——在 v2 候选上叠加 RSI(14)∈[40,65]，
# 剔除弱势(<40，阴跌途中假回踩)与超买(>65，接反弹高位)后：T+1 胜率 48.6%→49.3%、
# 均值 +0.100%→+0.123%、各周期均值全升（+0.7pp 胜率），是唯一稳赚的入场过滤器
# （E1 市场宽度跳过被证伪、E2 缩量回踩仅边际）。
# ⚠ 口径：RSI 用简单 rolling(14) 均值（与 tools/backtest_enhance.py::rsi14 /
#    tools/backtest_exit.py::rsi14 完全一致），勿改用 strategy/indicators.calc_rsi
#    的 ewm 口径——回测/推荐口径分裂会污染 diag 复验。
# enabled=False 即一键降级，不影响任何既有链路。
RSI_SWEET_SPOT = {
    "enabled": True,
    "lo": 40.0,   # RSI(14) 下限：剔除弱势（<40 的阴跌途中假回踩）
    "hi": 65.0,   # RSI(14) 上限：剔除超买（>65 的接反弹高位）
}


# ── T+1 跳空/追高守卫（2026-08-06 并入突破确认买点）────────────────
# 背景：用户反馈"短线推荐买进去容易挂高"。信号 T 日盘后生成、T+1 开盘买入；
# 若 T+1 最新价相对信号日收盘（buy_price）已累计涨 > max_gap_pct（跳空高开+日内续涨），
# 追入 = 接盘高位 → 操作信号从 buy 降级为 wait（暂缓），提示等回踩，
# 与既有"突破确认买点"（routes/investor.py，等收盘站上推荐日以来高点）合并为同一入场时机守卫。
T1_GAP_GUARD = {
    "enabled": True,
    "max_gap_pct": 2.5,   # 相对信号日收盘价累计涨幅上限（%），超过则暂缓追高
}


# ── 隔日动量信号线（龙虎榜净买占比，独立于超跌反弹并行） ──
# Phase 1 样本外验证（2026-01~07，tools/mine_next_day.py）：
#   净买占比>=10% 且非涨停 → 次日 open->close 胜率 54.3%、均值 +0.95%（OC 现实口径）；
#   龙虎榜盘后公布，只能次日开盘买入，故用 OC 口径而非 CC（含买不到的隔夜跳空）。
# enabled=False 即一键下线，不影响任何现有短线链路。
NEXT_DAY_MOMENTUM = {
    "enabled": True,
    "min_net_buy_ratio": 10.0,   # 龙虎榜净买额占总成交比下限（%）
    "exclude_limit_up": True,    # 剔除当日涨停（次日巨幅高开买不到、日内易回落）
    "stop_loss_pct": -0.04,      # 止损 -4%
    "take_profit_pct": 0.065,    # 止盈 +6.5%（盈亏比≈1.6 稳超 1.5，规避信号灯 avoid 的浮点边界）
}


# ── Phase 1: 模板穷举配置 ──
PHASE1_CONFIG = {
    "ic_min_abs": 0.02,           # IC 过滤阈值
    "top_per_cluster": 2,         # 每类因子保留数
    "thresholds": [0.25, 0.5, 0.75],
    "sample_stocks": 50,          # 快速回测采样数
    "min_trades": 15,             # 最少交易次数
    "top_n_rules": 50,            # 入库数量
    "backtest_start": "20220101",
}

# 交叉信号预定义对 (Phase 1 T3 模板)
CROSS_PAIRS = [
    ("MACD_DIF", "MACD_DEA"),
    ("KDJ_K", "KDJ_D"),
    ("MA5_偏离", "MA20_偏离"),
    ("PDI", "MDI"),
]


def get_strategy_params(name: str) -> Dict[str, Any]:
    """按名称获取策略参数"""
    mapping = {
        "volume_breakout": VOLUME_BREAKOUT,
        "ma_convergence": MA_CONVERGENCE,
        "price_volume_divergence": PRICE_VOLUME_DIVERGENCE,
        "bottom_fishing": BOTTOM_FISHING,
        "whale_accumulation": WHALE_ACCUMULATION,
        "oversold_rebound_v4": OVERSOLD_REBOUND_V4,
    }
    return mapping.get(name, {})


# ─────────────────────────────────────────────
# 运行时参数覆盖层（strategy_param_override 表）
# ─────────────────────────────────────────────
# 优化器建议被采纳（或手动调整）后写入 DB 覆盖层，代码常量退化为默认值；
# 消费方（sync/rec_filters/adaptive_weights）统一走 get_param() 读取。
# min/max 为安全边界：越界的覆盖值一律按边界截断，防止误写把策略打穿。

TUNABLE_PARAMS: Dict[str, Dict[str, Any]] = {
    "sig_threshold": {
        "default": DEFAULT_SIG_THRESHOLD, "type": float,
        "min": 10.0, "max": 30.0, "label": "融合分阈值",
    },
    "high_vol_sig_threshold": {
        "default": HIGH_VOL_SIG_THRESHOLD, "type": float,
        "min": 12.0, "max": 32.0, "label": "高波动融合分阈值",
    },
    "trend_gate_ma": {
        "default": SHORT_TREND_GATE["ma"], "type": int,
        "min": 5, "max": 60, "label": "趋势闸门均线周期",
    },
    "trend_gate_slope_lookback": {
        "default": SHORT_TREND_GATE["slope_lookback"], "type": int,
        "min": 2, "max": 15, "label": "趋势闸门斜率回看天数",
    },
    "short_stop_loss": {
        # 2026-08 短线优化 P0-1.1：-6% → -5%（盈亏比 8/5=1.6 稳过 avoid 一票否决；
        # -3.5% 贴近 _calc_signal 提示的"易被洗出"阈值，故取 -5%）
        # 2026-08 P3-2：-5% → -6%（回测 tools/backtest_exit.py：止损放宽后止损触发率
        # 21%→16%、胜率 47.3%→47.7%，均值基本持平 +0.201%→+0.189%；-6% 仍在
        # _calc_signal"易被洗出"阈值之上，配合 +10% 止盈盈亏比 10/6=1.67 更优）
        "default": -0.06, "type": float,
        "min": -0.12, "max": -0.02, "label": "短线止损比例",
    },
    "short_take_profit": {
        # 快进快出：+20% → +8%（诊断 T+3/T+5 转负，edge 只在 T+1 附近）
        # 2026-08 P3-2：+8% → +10%（回测：止盈放宽后触发止盈占比 12.3%→7.7%，
        # 让利润奔跑吃 T+3/T+5 正漂移；胜率再 +0.4pp，均值基本持平）
        "default": 0.10, "type": float,
        "min": 0.04, "max": 0.40, "label": "短线止盈比例",
    },
    "short_max_hold_days": {
        # 2026-08-09 由 1 调整为 3：P0-1.1 的 1 天基于 v1 诊断（v1 T+3 转负才快进快出）；
        # v2 转正后 edge 在 T+3/T+5（回测 OC：T+1 +0.10% → T+3 +0.181% → T+5 +0.226%），
        # 8-04/8-05 实盘验证 T1 负（-0.52%/-1.43%）但 T2/T3 回正（+1.10%/+1.24%）——
        # 1 天了结恰卖在回踩确认期最低点。3 天吃满 T+3 edge，止损/止盈不变。
        "default": 3, "type": int,
        "min": 1, "max": 10, "label": "短线最大持仓天数",
    },
    "short_conf_gate": {
        # 短线置信门控（S2/S4 口径，docs/short-reco-dynamic-count-plan.md）：
        # fusion_score 低于此值的 short 信号不推荐（宁缺毋滥，弱日自然出 0）。
        # 2020+ 全市场回测：Top4 不加门控 +10.5% → 加门控22 +14.1%（组合口径）。
        "default": 22.0, "type": float,
        "min": 0.0, "max": 50.0, "label": "短线置信门控(融合分下限)",
    },
}

# 覆盖值内存缓存：recalc 全市场逐股调用 get_param，不能每次都查库
_override_cache: Dict[str, Any] = {"data": None, "ts": 0.0}
_OVERRIDE_TTL_S = 60.0


def _load_overrides() -> Dict[str, str]:
    import time as _time
    now = _time.time()
    if _override_cache["data"] is not None and now - _override_cache["ts"] < _OVERRIDE_TTL_S:
        return _override_cache["data"]
    data: Dict[str, str] = {}
    try:
        from core.db import get_conn  # 惰性导入，避免循环依赖
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT param_key, value FROM strategy_param_override").fetchall()
        data = {r["param_key"]: r["value"] for r in rows}
    except Exception:
        data = {}  # 表未建/库不可用时优雅降级为代码默认值
    _override_cache["data"] = data
    _override_cache["ts"] = now
    return data


def invalidate_param_cache():
    """采纳/回滚参数后立即失效缓存，让新值即刻生效"""
    _override_cache["data"] = None
    _override_cache["ts"] = 0.0


def get_param(key: str, default: Any = None) -> Any:
    """读取可调参数：DB 覆盖值优先，越界截断，缺失回退代码默认值"""
    spec = TUNABLE_PARAMS.get(key)
    fallback = spec["default"] if spec else default
    raw = _load_overrides().get(key)
    if raw is None:
        return fallback
    try:
        caster = spec["type"] if spec else (type(fallback) if fallback is not None else str)
        val = caster(float(raw)) if caster in (int, float) else caster(raw)
    except (TypeError, ValueError):
        return fallback
    if spec:
        if spec.get("min") is not None and val < spec["min"]:
            val = spec["type"](spec["min"])
        if spec.get("max") is not None and val > spec["max"]:
            val = spec["type"](spec["max"])
    return val


def get_tunable_params_state() -> Dict[str, Dict[str, Any]]:
    """返回全部可调参数的默认值/当前值/是否被覆盖（供优化器 API 展示）"""
    overrides = _load_overrides()
    return {
        key: {
            "label": spec["label"],
            "default": spec["default"],
            "current": get_param(key),
            "overridden": key in overrides,
        }
        for key, spec in TUNABLE_PARAMS.items()
    }
