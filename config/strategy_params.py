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
    # 剔除当日大跌/跌停（2026-08 修复：净买占比>=10% 但当日大跌/跌停的信号是
    # 明确负期望子集——历史 T+1 -1.0%/胜率47%、T+2 -3.6%/34%、T+5 -5.8%/25%，
    # 麦迪科技 603990 2026-08-13 跌停日即因此误入今日短线推荐）。
    # 当日跌幅 <= 该值（%）不产生隔日动量信号；设 0/None 关闭本过滤。
    "max_down_pct": -7.0,
    "stop_loss_pct": -0.04,      # 止损 -4%
    "take_profit_pct": 0.065,    # 止盈 +6.5%（盈亏比≈1.6 稳超 1.5，规避信号灯 avoid 的浮点边界）
}


# ── 强势突破信号线（首页「次日强势观察」栏目专用，独立于今日推荐） ──
# 目标：次日（T+1）涨停或大涨的高弹性观察池。
# Phase 1 样本外验证（2026-01~07，tools/mine_surge_next_day.py，主板 OC 现实口径）：
#   「大阳突破」族是唯一 OC 均值为正的候选：当日 +5%~9.8% 未涨停（次日开盘买得到）
#   + 收盘创 20 日新高 + 量比>=1.5 + 收盘位于当日振幅上段，叠加市值<150亿：
#     验证窗 OC 胜率 49.9%、均值 +0.43%、次日大涨率(OC>=5%) 12.9%、盘中触涨停 6.1%、日均 4.4 只。
#   关键负期望子集：全市场当日平均涨幅 >= +1%（火热日）追突破 → OC 均值 -1.38%，
#   由 max_market_pct 门控拦截。首板/连板族次日触涨停率更高但 OC 均值 <=0（隔夜
#   跳空吃掉溢价），不采用。enabled=False 即一键下线。
SURGE_BREAKOUT = {
    "enabled": True,
    "min_pct": 5.0,              # 当日涨幅下限（%）：大阳才具隔日惯性
    "max_pct": 9.8,              # 当日涨幅上限（%）：涨停次日高开买不到，剔除
    "vol_ratio_min": 1.5,        # 量比下限（对前 5 日均量）：确认放量
    "close_pos_min": 0.7,        # 收盘位置下限（当日振幅内）：收在上段不回落
    "new_high_window": 20,       # 突破前高回看窗口（交易日）
    "max_mktcap": 150e8,         # 流通市值上限（元）：小盘弹性大，验证窗 +0.43% 主力
    "max_market_pct": 1.0,       # 当日全市场平均涨幅 >= 该值（%）不产生信号（火热日追高负期望）
    "lhb_required": True,        # 龙虎榜硬过滤：必须信号日同日上榜且净买>0 才产生信号（用户确认
                                 # 宁缺毋滥；两窗验证大涨率 25%/触涨停 21.4%，代价是日均 1~1.3 只）
    "stop_loss_pct": -0.04,      # 止损 -4%
    "take_profit_pct": 0.08,     # 止盈 +8%（盈亏比 2.0，博弈大涨不留恋）
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


# ── 中/长线市场环境（regime）天花板（2026-08）────────────────────────
# 长线是只做多的趋势策略，在 cold（广度+指数斜率均差的普跌市）里负期望；
# 短线已有 cold→0/cool→2 的 regime 天花板（P1-2.3，routes/investor.py 展示层），
# 此处补齐中/长线同源控制。键：regime → 当日最多推荐条数；None = 不受 regime 限制。
# 注：与短线一致，本天花板只在「今日推荐」展示层生效，不影响 stock_signal 写库与复盘。
MID_LONG_REGIME_CAP = {
    "mid":  {"cold": 0, "cool": 2, "neutral": None, "warm": None, "hot": None, "unknown": None},
    "long": {"cold": 0, "cool": 1, "neutral": None, "warm": None, "hot": None, "unknown": None},
}


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
        # 2026-08-20 方案A（.cache/boost_report.html 退出网格 6池×71配置）：-6% → -5%。
        # 现网口径池（conf22+低扩展排序 Top4）移动止损族 sl-5 全窗/近期窗一致优于 sl-6；
        # 候选池与选股链不动，仅退出侧调参。
        "default": -0.05, "type": float,
        "min": -0.12, "max": -0.02, "label": "短线止损比例",
    },
    "short_take_profit": {
        # 快进快出：+20% → +8%（诊断 T+3/T+5 转负，edge 只在 T+1 附近）
        # 2026-08 P3-2：+8% → +10%（回测：止盈放宽后触发止盈占比 12.3%→7.7%，
        # 让利润奔跑吃 T+3/T+5 正漂移；胜率再 +0.4pp，均值基本持平）
        # 2026-08 移动止盈落地（tools/exit_analysis.py S4 池 6131 笔同池对比）：
        #   固定止盈 mean -0.326% → 移动止盈(启动+10%, 回撤8%, T5) mean -0.072%；
        #   卖飞分析：固定止盈触发的 6150 笔若持有到 T+5，82% 胜率、均值 +14.5%、
        #   58% 仍 >+10% —— 好票被固定止盈掐断。故 take_profit 语义改为
        #   「移动止盈启动线」：浮盈（持仓期最高价）首次达到 +10% 后启用移动止盈，
        #   不再达价即卖。与 short_trailing_pct 配合。
        # 2026-08-20 方案A（boost_report.html）：启动线 +10% → +8%。现网 max_return
        # 均值 +5.16%/中位 +3.48%，+10% 启动线多数票终生无法激活（利润仍在回吐）；
        # 网格近期窗启动+8/+10 均居前，+8% 激活率更高、更早锁盈。
        "default": 0.08, "type": float,
        "min": 0.04, "max": 0.40, "label": "短线止盈启动线(浮盈达此值启用移动止盈)",
    },
    "short_trailing_pct": {
        # 短线移动止盈回撤比例（2026-08 落地，docs 见 short_take_profit 注释）：
        # 浮盈达 short_take_profit 后，移动止盈线 = 持仓最高价 × (1-此值)，收盘跌破清仓。
        # 回测（S4 池 6131 笔）：trail8_T5 -0.072% > trail10_T5 -0.081% >
        # noTP_T3 -0.158% > fixed10_T3 -0.326%；trail5 太紧（移止触发 13.9% 易被洗出）。
        # 移动止盈线只上移不下移，锁住已实现浮盈的同时让利润奔跑。
        # 2026-08-20 方案A（boost_report.html）：回撤 8% → 3%。现网口径池近期窗 Top8
        # 清一色「回撤3% 持10」，全窗最优族亦为小回撤；启动线下调后 3% 回撤负责快速锁盈。
        "default": 0.03, "type": float,
        "min": 0.03, "max": 0.20, "label": "短线移动止盈回撤比例(自高点)",
    },
    "short_max_hold_days": {
        # 2026-08-09 由 1 调整为 3：P0-1.1 的 1 天基于 v1 诊断（v1 T+3 转负才快进快出）；
        # v2 转正后 edge 在 T+3/T+5（回测 OC：T+1 +0.10% → T+3 +0.181% → T+5 +0.226%），
        # 8-04/8-05 实盘验证 T1 负（-0.52%/-1.43%）但 T2/T3 回正（+1.10%/+1.24%）——
        # 1 天了结恰卖在回踩确认期最低点。3 天吃满 T+3 edge，止损/止盈不变。
        # 2026-08 移动止盈落地：3 → 5（S4 池回测 trail8_T5 -0.072% 优于 trail8_T3
        # -0.142%，移动止盈需要更长奔跑空间；让利润奔跑由移动止盈线兜底，到期只是最后防线）
        # 2026-08-20 方案A（boost_report.html）：5 → 10。网格中「持10」在所有池全窗/
        # 近期窗全面优于「持5」（近期窗 Top8 全为持10；test 窗持10 亏损也小于持5），
        # 与复盘评估的 10 日窗口对齐。
        "default": 10, "type": int,
        "min": 1, "max": 10, "label": "短线最大持仓天数",
    },
    "short_conf_gate": {
        # 短线置信门控（S2/S4 口径，docs/short-reco-dynamic-count-plan.md）：
        # fusion_score 低于此值的 short 信号不推荐（宁缺毋滥，弱日自然出 0）。
        # 2020+ 全市场回测：Top4 不加门控 +10.5% → 加门控22 +14.1%（组合口径）。
        # 2026-08-20 Top3 化网格（tools/eval_short_top3b.py）复核：g24 对账窗
        # 均值 +0.66% 优于 g22 的 -0.02%，但 6个月窗仅 +0.03pp、3个月窗
        # （含5-6月急跌段）反而 -0.74%→-1.14%——改善集中在近期强市段，
        # 未达准入阈值(≥1pp)，维持 22 不动，避免过拟合近期窗口。
        "default": 22.0, "type": float,
        "min": 0.0, "max": 50.0, "label": "短线置信门控(融合分下限)",
    },
    "short_top_n": {
        # 每日短线推荐数量上限（2026-08-20 由 4 → 3，用户要求精选）：
        # tools/eval_short_top3.py 6个月回测（方案A退出）：
        # Top3 g22 低扩展 均值 -1.00% vs Top4 -1.19%（+0.19pp），
        # 对账窗 -0.02% vs +0.14% 基本持平；ext 排序质量梯度验证
        # （Top1 对账窗 +1.68%/PF1.67）确认低扩展度排序前位更优。
        # 仅作用于 short 组；mid/long 仍为每日前 4。
        "default": 3, "type": int,
        "min": 1, "max": 8, "label": "每日短线推荐数量上限",
    },
    "short_down_market_gate": {
        # 短线恐慌日闸门（2026-08-20 T1 辅助过滤，tools/eval_short_t1_filter.py）：
        # 信号日全市场平均涨跌幅 < 此值才放行抄底推荐（恐慌日买入：全市场下跌日
        # T1 反弹概率高，<-1% 桶 T1 胜率 59.8%；全市场上涨日推等于追高，
        # >=+1% 桶 T1 胜率仅 38.6%）。实装语义复核（eval_short_t1_filter2.py，
        # 先过滤再取 Top3 含替补）：test 窗 T1 胜率 42.1→49.5%（+7.4pp），
        # 但对账窗(2026-07-20起)强市段均值反向（+0.35→+0.07%，上涨日大赚票被切）
        # ——闸门本质是弱市减亏工具而非全天候增强。
        # 2026-08-20 改为按大盘 regime 自动切换（用户决策）：仅信号日当天
        # regime 为 cold/cool（冰点/偏冷，core/market_regime.py 与前端信号灯
        # 同口径、历史可复现）时启用本闸门，neutral/warm/hot 不过滤——
        # 弱市减亏、强市不误伤。设 >=99 完全禁用。不适用于「隔日动量」信号线。
        "default": 0.0, "type": float,
        "min": -5.0, "max": 100.0, "label": "短线恐慌日闸门(市场宽度上限%)",
    },
    "short_dev_ma5_max": {
        # 短线 MA5 偏离上限 %（2026-08-20 T1 辅助过滤）：信号日收盘距 MA5
        # 偏离 <= 此值才放行（短线超跌确认；dev5<=-2% 桶 train T1 胜率 55.1%）。
        # 与恐慌日闸门组合（C 条件）：test 窗 T1 胜率 42.1→49.5%（+7.4pp）、
        # 6个月窗 42.6→49.4%。单独使用（无恐慌日闸门）会被替补票稀释而劣化，
        # 必须组合使用。随闸门按 regime 自动切换（cold/cool 日启用，
        # 见 short_down_market_gate 注释）。设 >=99 禁用。
        # 不适用于「隔日动量」信号线。
        "default": -2.0, "type": float,
        "min": -15.0, "max": 100.0, "label": "短线MA5偏离上限(%)",
    },
    "mid_partial_tp": {
        # 中线移动止盈启动线（2026-08 落地：中/长线同短线改为移动止盈，让利润奔跑）：
        # 浮盈（持仓期最高价）达此值后启用移动止盈，不再达 mid 自带 ATR 止盈价即卖。
        # 2026-08-19 +10% → +6%：出场跟踪显示启动线过高导致多数持仓从未激活移动止盈，
        # 下行途中仅宽 ATR 止损保护，56 个持仓段浮亏合计 -147%（均 -2.63%）。
        # 回测（tools/backtest_mid_exit_grid.py：全历史 composite BUY_SIGNAL 89.7 万笔，
        # hold=60，收盘判定与 evaluate_exit_by_prices 同口径，test=2020+ 36.8 万笔）：
        #   launch=0.10 → test 均值+0.972% PF1.42 胜率45.5%（trail出场占比53.7%）
        #   launch=0.06 → test 均值+0.925% PF1.73 胜率40.9%（trail出场占比64.8%）
        # 均值小幅让渡（-0.05pp）换 PF +22% 与更早锁盈，消除"无保护深浮亏"尾部。
        "default": 0.06, "type": float,
        "min": 0.04, "max": 0.40, "label": "中线移动止盈启动线(浮盈)",
    },
    "mid_stop_max_width": {
        # 中线止损宽度上限（相对建仓价，2026-08-19 落地）：信号自带止损 = close - k×ATR，
        # k=2.5 时宽度可达 -20%，持仓深浮亏长期挂着。评估出场时叠加宽度上限：
        # effective_stop = max(信号止损价, entry×(1-此值))。
        # 回测（同 mid_partial_tp，cap 网格 None/0.12/0.10）：cap=0.12 时 test 均值
        # +0.924%≈无cap +0.925%，PF 1.73→1.79（单笔亏损收窄，均值无损）。
        # 仅作用于中线出场评估（出场跟踪/推荐复盘同口径），不改动信号生成。
        "default": 0.12, "type": float,
        "min": 0.05, "max": 0.30, "label": "中线止损宽度上限(相对建仓价)",
    },
    "mid_anchor_lookback": {
        # 中线入场锚点回看天数（2026-08-19 入场改"三过滤确认"风格）：
        # 信号 = composite 分上穿 65 为锚点（回看 N 日内最近一次）
        #        + 当日收盘首次突破锚点日高点 × mid_confirm_mult
        #        + 偏离 MA20 ≤ mid_dev_ma20_max + 弱市闸门（mid_weak_market_gate）。
        # 挖掘（tools/mine_mid_entry.py 89.7 万信号网格 + tools/verify_mid_three_filter.py
        # 落地口径复验，出场同线上 launch=0.06/trail=0.10/cap=0.12）：
        #   旧"上穿当日"入场: test 胜率40.1% 均值+0.930% PF1.79（日均235信号）
        #   三过滤组合:       test 胜率43.2% 均值+1.385% PF1.74（日均61信号）
        # 胜率 +3.1pp、单笔期望 +49%，准入阈值（胜率+1pp 且 PF>=1）达标。
        # 高胜率风格（回调低吸+小止盈）经 tools/mine_mid_highwin.py 证伪：
        # 胜率>50% 的配置 PF 全部<1（期望值守恒），不采纳。
        "default": 5, "type": int,
        "min": 1, "max": 10, "label": "中线锚点回看天数",
    },
    "mid_confirm_mult": {
        # 确认突破倍数：当日收盘 > 锚点日高点 × 此值才算有效确认（同短线突破确认
        # 1.002 口径，过滤贴线假突破）。回测按 1.002 验证，不建议大幅调高（信号骤减）。
        "default": 1.002, "type": float,
        "min": 1.0, "max": 1.02, "label": "中线确认突破倍数(锚点日高点)",
    },
    "mid_dev_ma20_max": {
        # 偏离 MA20 上限（不追高过滤）：挖掘显示 ≤5% 时 test 均值
        # +0.930%→+1.103%、PF 1.79→1.86（胜率持平），是三过滤中期望贡献最大的一项。
        "default": 0.05, "type": float,
        "min": 0.02, "max": 0.15, "label": "中线偏离MA20上限",
    },
    "mid_weak_market_gate": {
        # 弱市闸门（百分点，与 daily_price.pct_change 单位一致）：当日全市场平均涨幅
        # <= 此值时不出中线信号。挖掘：叠加后组合均值 +1.233%→复验 +1.385%，
        # 弱市日入场的负贡献被剔除。异常/缺数据时默认放行（不阻断信号线）。
        "default": -0.5, "type": float,
        "min": -3.0, "max": 0.0, "label": "中线弱市闸门(全市场均涨%)",
    },
    "mid_trailing_pct": {
        # 中线移动止盈回撤比例：持仓期 10~60 日正常波动大于短线，回撤阈值放宽到 10%
        # （短线 8%）；与 evaluate_exit 快照版中线默认（OVERSOLD_REBOUND_V4 回撤10%）对齐。
        "default": 0.10, "type": float,
        "min": 0.03, "max": 0.30, "label": "中线移动止盈回撤比例(自高点)",
    },
    "long_partial_tp": {
        # 长线移动止盈启动线：long 自带 take_profit=现价×1.5（+50% 目标价）不宜直接当
        # 启动线（等于长期无保护），改为浮盈 +20% 即启用移动止盈，锁住利润后让趋势奔跑。
        "default": 0.20, "type": float,
        "min": 0.05, "max": 0.50, "label": "长线移动止盈启动线(浮盈)",
    },
    "long_trailing_pct": {
        # 长线移动止盈回撤比例：持仓 60+ 日波动更大，回撤阈值放宽到 15%（趋势破坏确认）。
        "default": 0.15, "type": float,
        "min": 0.05, "max": 0.40, "label": "长线移动止盈回撤比例(自高点)",
    },
    "mid_atr_stop_mult": {
        # 中线止损 ATR 倍数（2026-08 回测 tools/backtest_midlong_exit.py：全历史 raw composite
        # BUY_SIGNAL ~90 万笔，盈亏比固定 2.5，hold=60）：
        #   k=2.0 → 胜率34.8% 均值+2.30% PF2.50 止损率63%
        #   k=2.5 → 胜率36.6% 均值+2.97% PF2.38 止损率58%
        #   k=3.0 → 胜率38.5% 均值+3.51% PF2.24 止损率52%
        # 放宽止损减少震荡误扫（止损率↓、胜率↑、均值↑），代价是 PF 略降（单笔亏损变大）。
        # 取 2.5 折中；回滚：DB 覆盖或改回 2.0。
        "default": 2.5, "type": float,
        "min": 1.0, "max": 4.0, "label": "中线止损 ATR 倍数",
    },
    "long_ma_stop_mult": {
        # 长线止损 MA120 倍数（2026-08 回测 tools/backtest_midlong_exit.py：全历史 raw 趋势信号
        # ~515 万笔，移动止盈 +20%启动/15%回撤，hold=120）：
        #   0.99 → 胜率44.8% 均值+3.97% PF1.95 止损率52%
        #   0.95 → 胜率49.3% 均值+4.65% PF1.67 止损率44%
        #   0.93 → 胜率51.4% 均值+4.97% PF1.55 止损率40%
        # 0.99 过近（52% 止损率 = 1% 级 MA 波动即扫出，来回被洗）。放宽到 0.95 显著降误扫、
        # 抬胜率/均值；代价 PF 1.95→1.67。取 0.95 折中；回滚：DB 覆盖或改回 0.99。
        "default": 0.95, "type": float,
        "min": 0.85, "max": 0.99, "label": "长线止损 MA120 倍数",
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
