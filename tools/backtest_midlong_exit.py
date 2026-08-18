"""
backtest_midlong_exit.py - 中/长线出场参数回测（只读，向量化，验证止损放宽是否有正收益）

背景：recommend_outcome 复盘显示中/长线「0 止盈、止损过多」——中线止损 2×ATR
偏紧被震荡来回扫（PF 0.80~0.96），长线止损 MA120×0.99 过近（1% 级别波动即扫出）。

本脚本向量化重算中/长线信号，按不同出场参数模拟实现收益，回答：
  Q1 中线：止损 2×ATR → 2.5/3×ATR（盈亏比固定 2.5）是否改善胜率/均值/PF？
  Q2 长线：止损 MA120×0.99 → 0.95/0.93（+ 移动止盈 +20%启动/15%回撤）是否改善？

口径（与 tools/backtest_exit.py 一致）：
  entry = 信号日次日开盘价（T+1 open）
  stop/take 用当日 high/low 判定（盘中触发），否则到期按收盘价出场。
"""
import sys, os, sqlite3
import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

DB = os.path.join(PROJECT_ROOT, "core", "quant.db")

conn = sqlite3.connect(DB)
df = pd.read_sql(
    "SELECT code, trade_date, open, high, low, close, volume FROM daily_price", conn)
conn.close()
df["trade_date"] = pd.to_datetime(df["trade_date"])
df = df.sort_values(["code", "trade_date"]).reset_index(drop=True)
code = df["code"]
close = df["close"].astype(float)
high = df["high"].astype(float)
low = df["low"].astype(float)
volume = df["volume"].astype(float)


def gro(s, p, fn="mean"):
    """groupwise rolling over `code`."""
    return s.groupby(code).transform(lambda x: getattr(x.rolling(p), fn)())


def gewm(s, **kw):
    """groupwise ewm over `code`."""
    return s.groupby(code).transform(lambda x: x.ewm(**kw).mean())


def gshift(s, p=1):
    return s.groupby(code).shift(p)


def gcross_up(a, b):
    return (gshift(a) < gshift(b)) & (a >= b)


print("computing indicators (vectorized)...", flush=True)

# ── 中线 composite 所需指标 ──
MA5 = gro(close, 5); MA10 = gro(close, 10); MA20 = gro(close, 20); MA60 = gro(close, 60)
ema12 = gewm(close, span=12, adjust=False); ema26 = gewm(close, span=26, adjust=False)
MACD_DIF = ema12 - ema26
MACD_DEA = gewm(MACD_DIF, span=9, adjust=False)
delta = close.groupby(code).diff()
gain = delta.where(delta > 0, 0.0); loss = -delta.where(delta < 0, 0.0)
RSI14 = 100 - 100 / (1 + (gewm(gain, com=13, adjust=False) /
                          gewm(loss, com=13, adjust=False).replace(0, np.nan)))
low_min9 = gro(low, 9, "min"); high_max9 = gro(high, 9, "max")
rsv = (close - low_min9) / (high_max9 - low_min9 + 1e-10) * 100
KDJ_K = gewm(rsv, alpha=1 / 3, adjust=False)
KDJ_D = gewm(KDJ_K, alpha=1 / 3, adjust=False)
avg_vol5 = gshift(volume).groupby(code).transform(lambda x: x.rolling(5).mean())
VOLUME_RATIO = volume / avg_vol5
direction = np.sign(close.groupby(code).diff().fillna(0))
OBV = (direction * volume).groupby(code).cumsum()
BOLL_MID = gro(close, 20); BOLL_STD = gro(close, 20, "std")
BOLL_UPPER = BOLL_MID + 2 * BOLL_STD
# ATR / DMI
prev_close = gshift(close); prev_high = gshift(high); prev_low = gshift(low)
tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()],
               axis=1).max(axis=1)
ATR = tr.groupby(code).transform(lambda x: x.rolling(14).mean())
plus_dm = np.where((high - prev_high) > (prev_low - low),
                   np.maximum(high - prev_high, 0), 0)
minus_dm = np.where((prev_low - low) > (high - prev_high),
                    np.maximum(prev_low - low, 0), 0)
pdm_s = pd.Series(plus_dm, index=df.index).groupby(code).transform(
    lambda x: x.rolling(14).mean())
mdm_s = pd.Series(minus_dm, index=df.index).groupby(code).transform(
    lambda x: x.rolling(14).mean())
plus_di = 100 * pdm_s / ATR; minus_di = 100 * mdm_s / ATR
dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-10)
DMI_ADX = dx.groupby(code).transform(lambda x: x.rolling(6).mean())

# composite score（与 strategy.strategy_composite 同口径）
ma_bull = (MA5 > MA10) & (MA10 > MA20) & (MA20 > MA60)
score = ma_bull.astype(float) * 15
score += (close > MA20).astype(float) * 10
score += gcross_up(MA5, MA10).astype(float) * 5
score += gcross_up(MACD_DIF, MACD_DEA).astype(float) * 15
score += ((RSI14 > 35) & (RSI14 < 65)).astype(float) * 10
score += gcross_up(KDJ_K, KDJ_D).astype(float) * 5
score += (VOLUME_RATIO > 1.5).astype(float) * 10
obv_ma10 = OBV.groupby(code).transform(lambda x: x.rolling(10).mean())
score += (OBV > obv_ma10).astype(float) * 10
score += (close > BOLL_MID).astype(float) * 10
score += (close < BOLL_UPPER * 0.97).astype(float) * 10
score += (DMI_ADX > 25).astype(float) * 5
COMPOSITE = score.clip(0, 100)
MID_BUY = (COMPOSITE >= 65) & (gshift(COMPOSITE) < 65)

# ── 长线 scan_long_term 向量化 ──
ma60 = gro(close, 60); ma120 = gro(close, 120)
long_score = ((ma60 > ma120) & (close > ma120)).astype(float) * 1.0
long_score += (ma120 > gshift(ma120, 21)).astype(float) * 1.0
vol60 = close.groupby(code).transform(
    lambda x: x.pct_change().rolling(60).std()) * np.sqrt(252)
long_score += (vol60 < 0.35).astype(float) * 0.5
dd250 = 1.0 - close / close.groupby(code).transform(lambda x: x.rolling(250).max())
long_score += (dd250 < 0.40).astype(float) * 0.5
LONG_SIG = long_score >= 2.0

print("signals computed. simulating exits...", flush=True)

# numpy 数组视图：O(1) 取数（df 已按 code,trade_date 排序，di+off 同 code 即同一交易日序列）
code_arr = df["code"].to_numpy()
atr_s = ATR.to_numpy()
ma120_s = ma120.to_numpy()
open_arr = df["open"].to_numpy()
high_arr = df["high"].to_numpy()
low_arr = df["low"].to_numpy()
close_arr = df["close"].to_numpy()


def simulate_fixed(di, maxhold, stop_frac, take_frac):
    """固定止损/止盈：entry=次日开盘(T+1 open)。df 已按 code 排序，di+off 同 code 即同一交易日序列。"""
    if di + 1 >= len(df) or code_arr[di + 1] != code_arr[di]:
        return None
    entry = open_arr[di + 1]
    if not entry or entry <= 0:
        return None
    for off in range(1, maxhold + 1):
        j = di + off
        if j >= len(df) or code_arr[j] != code_arr[di]:
            return None
        hi = high_arr[j]; lo = low_arr[j]; cl = close_arr[j]
        if take_frac is not None and hi >= entry * (1 + take_frac):
            return take_frac, "take"
        if stop_frac is not None and lo <= entry * (1 - stop_frac):
            return -stop_frac, "stop"
        if off == maxhold:
            return cl / entry - 1.0, "expire"
    return None


def simulate_trailing(di, maxhold, stop_price, launch=0.20, trail=0.15):
    """长线：硬止损(stop_price 固定) + 移动止盈(浮盈达 launch 启用，自高点回撤 trail 清仓)。"""
    if di + 1 >= len(df) or code_arr[di + 1] != code_arr[di]:
        return None
    entry = open_arr[di + 1]
    if not entry or entry <= 0:
        return None
    highest = entry; active = False
    for off in range(1, maxhold + 1):
        j = di + off
        if j >= len(df) or code_arr[j] != code_arr[di]:
            return None
        hi = high_arr[j]; cl = close_arr[j]
        if hi > highest:
            highest = hi
        if cl <= stop_price:
            return cl / entry - 1.0, "stop"
        if highest >= entry * (1 + launch):
            active = True
        if active and cl <= highest * (1 - trail):
            return cl / entry - 1.0, "trail"
        if off == maxhold:
            return cl / entry - 1.0, "expire"
    return None


def _add(cells, key, ret, reason):
    c = cells.setdefault(key, {"sum": 0.0, "n": 0, "pos": 0, "w": 0, "l": 0,
                               "wsum": 0.0, "lsum": 0.0, "take": 0, "stop": 0})
    c["sum"] += ret; c["n"] += 1; c["pos"] += (1 if ret > 0 else 0)
    if ret > 0:
        c["w"] += 1; c["wsum"] += ret
    else:
        c["l"] += 1; c["lsum"] += ret
    if reason in ("take", "trail"):
        c["take"] += 1
    if reason == "stop":
        c["stop"] += 1


def report(cells, label):
    print(f"\n{label}", flush=True)
    print("  %-22s %8s %7s %8s %7s %7s %7s" %
          ("config", "trades", "win%", "mean%", "PF", "take%", "stop%"), flush=True)
    for key in sorted(cells, key=lambda k: str(k)):
        c = cells[key]
        if not c or c["n"] < 30:
            continue
        win = c["pos"] / c["n"] * 100
        mean = c["sum"] / c["n"] * 100
        aw = c["wsum"] / c["w"] if c["w"] else 0
        al = abs(c["lsum"] / c["l"]) if c["l"] else 1e-9
        pf = aw / al if al > 1e-9 else 99
        print("  %-22s %8d %6.1f%% %7.3f%% %6.2f %6.1f%% %6.1f%%" %
              (key, c["n"], win, mean, pf, c["take"] / c["n"] * 100,
               c["stop"] / c["n"] * 100), flush=True)


# ── 中线模拟 ──
mid_cells = {}
mid_sig_idx = np.where(MID_BUY.to_numpy())[0]
n_mid_sig = 0
for di in mid_sig_idx:
    if di + 1 >= len(df) or code_arr[di + 1] != code_arr[di]:
        continue
    entry = open_arr[di + 1]
    a = atr_s[di]
    if not entry or entry <= 0 or not np.isfinite(a) or a <= 0:
        continue
    n_mid_sig += 1
    for k in (2.0, 2.5, 3.0):
        stop_frac = k * a / entry
        take_frac = 2.5 * k * a / entry
        for maxhold in (20, 40, 60):
            rr = simulate_fixed(di, maxhold, stop_frac, take_frac)
            if rr is not None:
                _add(mid_cells, f"k={k:.1f}xATR hold={maxhold}", rr[0], rr[1])

print(f"\n中线：signals={n_mid_sig}", flush=True)
report(mid_cells, "中线 composite BUY_SIGNAL 出场对比（盈亏比固定 2.5）")

# ── 长线模拟 ──
long_cells = {}
long_sig_idx = np.where(LONG_SIG.to_numpy())[0]
n_long_sig = 0
for di in long_sig_idx:
    if di + 1 >= len(df) or code_arr[di + 1] != code_arr[di]:
        continue
    entry = open_arr[di + 1]
    m120 = ma120_s[di]
    if not entry or entry <= 0 or not np.isfinite(m120) or m120 <= 0:
        continue
    n_long_sig += 1
    for m in (0.99, 0.95, 0.93):
        stop_price = m120 * m
        for maxhold in (60, 120):
            rr = simulate_trailing(di, maxhold, stop_price)
            if rr is not None:
                _add(long_cells, f"MA120x{m:.2f} hold={maxhold}", rr[0], rr[1])

print(f"\n长线：signals={n_long_sig}", flush=True)
report(long_cells, "长线 MA60/120 趋势出场对比（移动止盈 +20%启动/15%回撤）")

print("\ndone.", flush=True)
