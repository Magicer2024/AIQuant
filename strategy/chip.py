"""
strategy/chip.py —— 筹码分布（持仓成本分布）因子

原理
----
把每根 K 线的成交量视为"当日新增筹码"，按**三角形分布**撒到价格轴上（顶点取当日
均价，实务上东财/通达信用 (high+low+2*close)/4 近似）；历史筹码按当日换手率衰减
（老筹码被新成交替换掉）。逐日迭代后得到 t 日收盘后的**持仓成本分布**——即"筹码峰"。

    朴素递推：C[t] = C[t-1] * (1 - h[t]) + new[t]      h = 换手率 × 衰减系数

数值实现（关键）
----------------
朴素递推是逐日 Python 循环，4400 只票 × 1500 日会跑到小时级。本模块用**块内解析展开**：

    C[t] = C_blk0 * Π_{u<=t}(1-h[u]) + Σ_{s<=t} new[s] * Π_{u=s+1..t}(1-h[u])
         = E[t] * ( C_blk0 + Σ_{s<=t} new[s]/E[s] )
    其中 E[t] = Π_{u<=t}(1-h[u])

块内走 cumsum，每 `_BLK=256` 天做一次真实递推把数值拉回：
  - 块内 E[t] 单调下降，最小 ≈ 0.95^256 ≈ 2e-6，1/E[s] 最大 ≈ 5e5 → **不溢出**；
  - 若不分块，长历史下 E[0] 会下溢到 0，1/E[0] 直接爆 inf（这是本模块唯一需要小心的
    数值陷阱，故块内必须重新锚定）。

无未来函数
----------
- 价格网格是**全局固定**的绝对刻度（对数分布），不随个股/日期变化 → 增量计算与
  全历史重算结果一致，可落库复用；
- 因子只用 t 日及以前的价量 → 可直接用于回测掩码与线上打分。

⚠ 已知假设与误差
--------------
1. `circ_shares` 取**当前值**（`stock_info` 单行快照），历史增发/解禁会使早年换手率
   有偏差。量级上不改变分布形状，但精确复现某软件数值需要历史股本表。
2. 衰减系数 `decay` 是**自由参数**——不同软件（东财 0.5~1.0）算出的筹码分布差异很大。
   本模块默认 0.65，属于"记忆约 1/(0.65×换手率) 日"的中等偏长口径；诊断实验里应
   把 decay 当作待扫参数，而非定值。
3. 未做除权复权还原。除权跳空会在价格轴上留出假的"筹码断层"——本模块用
   `volume/circ_shares` 自算换手率已弱化该效应，但严格的筹码分布需用**后复权价**计算。

对外接口
--------
    factors = compute_chip_factors(df, decay=0.65)   # df: 单股票日线，需 high/low/close/volume
    factors.columns -> winner, conc, pressure, cost_ratio, lock, center_shift

    compute_chip_factors_df(code_df, circ_shares)    # 便捷包装：自动算换手率
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# ── 全局对数价格网格：一次确定、永久固定（保证无未来函数 + 增量可复用）──
PRICE_LO: float = 0.30      # 0.30 元
PRICE_HI: float = 4000.0    # 4000 元（覆盖 A 股全历史极值）
N_BINS: int = 900
_BLK: int = 256             # 块大小（数值锚定周期）

_EDGES = np.exp(np.linspace(np.log(PRICE_LO), np.log(PRICE_HI), N_BINS + 1))
_MIDS = np.sqrt(_EDGES[:-1] * _EDGES[1:])       # 每档代表价（几何中点）
_LOG_MIDS = np.log(_MIDS)

# 默认衰减系数（换手率被放大的倍数）。0.65 → 记忆约 1/(0.65×换手率) 个交易日
DEFAULT_DECAY: float = 0.65

# 每日最小衰减率（兜底）。低换手大盘股（茅台换手 0.26%×0.65=0.17%/日）不设下限时
# 筹码记忆期长达 ~590 日，分布会摊平成"近三年成交的累积"，峰形消失、因子失真。
# 0.003 → 记忆期被压到 ≤ ~330 日，保证任何标的都有可辨识的筹码结构。
DEFAULT_DECAY_FLOOR: float = 0.003


def turnover_series(df: pd.DataFrame, circ_shares: float | None = None) -> np.ndarray:
    """换手率（小数，非百分数）序列。

    优先用卷自带 `turnover` 列（库中单位是**百分数**），但其 68% 行为 0/空，
    故缺失处回退 `volume / circ_shares`。⚠ 库里 `volume` 单位是**股**、
    `circ_shares` 单位也是**股**，二者相除即为换手率小数（已验证：
    000651 2383万股 / 55.15亿股 = 0.432% ↔ 库 turnover=0.43）。
    """
    n = len(df)
    if "turnover" in df.columns:
        t = pd.to_numeric(df["turnover"], errors="coerce").to_numpy(dtype=float) / 100.0
    else:
        t = np.full(n, np.nan)
    if circ_shares and np.isfinite(circ_shares) and circ_shares > 0:
        vol = pd.to_numeric(df["volume"], errors="coerce").to_numpy(dtype=float)
        calc = vol / float(circ_shares)
        t = np.where(np.isfinite(t) & (t > 0), t, calc)
    # 仍缺失 → 用中位数兜底（不臆断为 0，否则该股筹码永不衰减）
    med = np.nanmedian(t)
    if not np.isfinite(med) or med <= 0:
        med = 0.02
    t = np.where(np.isfinite(t) & (t > 0), t, med)
    return np.clip(t, 0.0, 1.0)


def chip_matrix(high, low, close, turnover, decay: float = DEFAULT_DECAY,
                decay_floor: float = DEFAULT_DECAY_FLOOR, blk: int = _BLK) -> np.ndarray:
    """计算筹码分布矩阵。

    参数
    ----
    high/low/close : 等长价格数组
    turnover       : 换手率**小数**序列
    decay          : 衰减系数（老筹码被替换的速度）
    decay_floor    : 每日最小衰减率，防止低换手股筹码永不衰减（见常量注释）

    返回
    ----
    (T, N_BINS) float64，每行是截至该日收盘后的持仓成本分布（行和 = 1）。
    """
    hi = np.asarray(high, dtype=float)
    lo = np.asarray(low, dtype=float)
    cl = np.asarray(close, dtype=float)
    T = len(cl)
    if T == 0:
        return np.zeros((0, N_BINS))

    # 脏数据兜底：high/low 缺失或为 0 → 回退 close；并保证 low <= high
    ok = np.isfinite(cl) & (cl > 0)
    hi = np.where(np.isfinite(hi) & (hi > 0), hi, cl)
    lo = np.where(np.isfinite(lo) & (lo > 0), lo, cl)
    hi, lo = np.maximum(hi, lo), np.minimum(hi, lo)
    hi = np.where(ok, hi, 1.0)
    lo = np.where(ok, lo, 1.0)
    cl = np.where(ok, cl, 1.0)

    h = np.asarray(turnover, dtype=float) * float(decay)
    h = np.clip(np.maximum(h, float(decay_floor)), 0.0, 0.95)

    # ── 三角形分布：顶点取 (high+low+2*close)/4 ──
    P = _MIDS[None, :]
    peak = np.clip((hi + lo + 2.0 * cl) / 4.0, lo, hi)
    up = (P - lo[:, None]) / np.maximum(peak - lo, 1e-9)[:, None]
    dn = (hi[:, None] - P) / np.maximum(hi - peak, 1e-9)[:, None]
    w = np.minimum(np.clip(up, 0.0, 1.0), np.clip(dn, 0.0, 1.0))
    w *= (P >= lo[:, None]) & (P <= hi[:, None])

    rs = w.sum(axis=1, keepdims=True)
    bad = (rs <= 1e-12).ravel()          # high==low（一字板）→ 退化为点分布
    if bad.any():
        idx = np.abs(_LOG_MIDS[None, :] - np.log(np.maximum(cl, 1e-9))[:, None]).argmin(axis=1)
        w[bad] = 0.0
        w[np.arange(T)[bad], idx[bad]] = 1.0
        rs = w.sum(axis=1, keepdims=True)

    new = (w / rs) * h[:, None]          # 当日新增筹码

    # ── 块内解析展开 + 块间真实递推（数值锚定）──
    out = np.empty((T, N_BINS), dtype=np.float64)
    prev = np.zeros(N_BINS)
    for a in range(0, T, blk):
        b = min(a + blk, T)
        L = np.concatenate([[0.0], np.cumsum(np.log1p(-h[a:b]))])   # 长度 m+1
        E = np.exp(L[1:])                                            # E[t]
        S = np.cumsum(new[a:b] / E[:, None], axis=0)
        Cb = E[:, None] * (prev[None, :] + S)
        Cb /= np.maximum(Cb.sum(axis=1, keepdims=True), 1e-300)
        out[a:b] = Cb
        prev = Cb[-1]
    return out


def chip_factors(C: np.ndarray, close) -> pd.DataFrame:
    """从筹码分布矩阵提取因子。

    全部只用 t 日及以前的信息（无未来函数）。

    因子含义与经济直觉
    ------------------
    winner      获利盘比例 = 成本 ≤ 现价的筹码占比。>0.9 上方几乎真空但也意味着
                全员浮盈（随时兑现），<0.2 深度套牢；短线偏好**中间偏上**区间。
    conc        集中度 = (P90-P10)/(P90+P10)，越小越集中。低位单峰密集 = 主力吸筹完成；
                数值大可视为"筹码发散、无主"。
    pressure    上方压力 = 现价之上的筹码占比 = 1 - winner。越小 = 上行阻力越小。
    cost_ratio  筹码中枢/现价 = P50 / close。< 1 说明**主力成本低于现价**（浮盈状态，
                有兑现压力）；接近 1 说明现价就在成本区（套牢盘与获利盘对半）。
    lock        低位锁仓度 = 现价 ×0.85 以下筹码 / 现价以下筹码。越大说明底部筹码
                **没被换手掉**（主力没走），是"上涨中继"的典型特征。
    center_shift 筹码中枢相对 n_shift 日前的移动幅度（现价口径），>0 = 成本上移
                （新资金在更高价位接手，原来的低位筹码被消化）。
    """
    C = np.asarray(C, dtype=float)
    T = C.shape[0]
    if T == 0:
        return pd.DataFrame(columns=["winner", "conc", "pressure", "cost_ratio",
                                     "lock", "center_shift"])
    cl = np.asarray(close, dtype=float)
    cl = np.where(np.isfinite(cl) & (cl > 0), cl, np.nan)
    clc = cl[:, None]
    mid = _MIDS[None, :]

    below = (mid <= clc)
    winner = (C * below).sum(axis=1)
    pressure = 1.0 - winner

    cum = np.cumsum(C, axis=1)

    def quantile(p: float) -> np.ndarray:
        idx = (cum < p).sum(axis=1).clip(0, N_BINS - 1)
        return _MIDS[idx]

    p10, p50, p90 = quantile(0.10), quantile(0.50), quantile(0.90)
    conc = (p90 - p10) / np.maximum(p90 + p10, 1e-9)
    cost_ratio = p50 / np.maximum(cl, 1e-9)

    low_mask = (mid <= (clc * 0.85))
    below_sum = np.maximum((C * below).sum(axis=1), 1e-12)
    lock = (C * low_mask).sum(axis=1) / below_sum

    center = np.full(T, np.nan)
    n_shift = 20
    if T > n_shift:
        center[n_shift:] = p50[n_shift:] / np.maximum(p50[:-n_shift], 1e-9) - 1.0

    return pd.DataFrame({
        "winner": winner,
        "conc": conc,
        "pressure": pressure,
        "cost_ratio": cost_ratio,
        "lock": lock,
        "center_shift": center,
    })


def compute_chip_factors(df: pd.DataFrame, circ_shares: float | None = None,
                         decay: float = DEFAULT_DECAY,
                         decay_floor: float = DEFAULT_DECAY_FLOOR) -> pd.DataFrame:
    """便捷包装：单股票日线 DataFrame → 筹码因子 DataFrame（index 与 df 对齐）。

    df 需含列：high, low, close, volume（turnover 可选，缺失时由 volume/circ_shares 补）
    """
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["winner", "conc", "pressure", "cost_ratio",
                                     "lock", "center_shift"])
    to = turnover_series(df, circ_shares)
    C = chip_matrix(df["high"], df["low"], df["close"], to,
                    decay=decay, decay_floor=decay_floor)
    f = chip_factors(C, df["close"].to_numpy(dtype=float))
    f.index = df.index
    return f
