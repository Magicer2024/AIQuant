"""
verify_mid_three_filter.py - 三过滤组合 × 线上出场口径 预验证（只读）

验证落地组合（与 mine_mid_entry.py 口径一致）：
  入场：composite 上穿65 为锚点（回看 5 日内）+ 当日收盘 > 锚点日高点×1.002
        + 当日偏离MA20 ≤5% + 弱市闸门（当日全市场均涨 > -0.5%）
  出场：线上 evaluate_exit_by_prices 口径 —— stop=max(锚点close-2.5×ATR, entry×0.88)，
        浮盈 +6% 启动移动止盈、自高点回撤 10% 清仓，maxhold=60，收盘判定，T+1。

准入标准（记忆 03e3db0c）：胜率 ≥ 现状 +1pp 且 PF ≥ 1.0。
现状基准（mine_mid_entry）：胜率 40.1%、均值 +0.930%、PF 1.79。
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
    return s.groupby(code).transform(lambda x: getattr(x.rolling(p), fn)())


def gewm(s, **kw):
    return s.groupby(code).transform(lambda x: x.ewm(**kw).mean())


def gshift(s, p=1):
    return s.groupby(code).shift(p)


def gcross_up(a, b):
    return (gshift(a) < gshift(b)) & (a >= b)


print("computing indicators...", flush=True)
MA5 = gro(close, 5); MA10 = gro(close, 10); MA20 = gro(close, 20); MA60 = gro(close, 60)
ema12 = gewm(close, span=12, adjust=False); ema26 = gewm(close, span=26, adjust=False)
MACD_DIF = ema12 - ema26
MACD_DEA = gewm(MACD_DIF, span=9, adjust=False)
delta = close.groupby(code).diff()
gain = delta.where(delta > 0, 0.0); loss = -delta.where(delta < 0, 0.0)
RSI14 = 100 - 100 / (1 + gewm(gain, com=13, adjust=False) /
                     gewm(loss, com=13, adjust=False).replace(0, np.nan))
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
prev_close = gshift(close); prev_high_s = gshift(high); prev_low = gshift(low)
tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()],
               axis=1).max(axis=1)
ATR = tr.groupby(code).transform(lambda x: x.rolling(14).mean())
plus_dm = np.where((high - prev_high_s) > (prev_low - low),
                   np.maximum(high - prev_high_s, 0), 0)
minus_dm = np.where((prev_low - low) > (high - prev_high_s),
                    np.maximum(prev_low - low, 0), 0)
pdm_s = pd.Series(plus_dm, index=df.index).groupby(code).transform(
    lambda x: x.rolling(14).mean())
mdm_s = pd.Series(minus_dm, index=df.index).groupby(code).transform(
    lambda x: x.rolling(14).mean())
plus_di = 100 * pdm_s / ATR; minus_di = 100 * mdm_s / ATR
dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-10)
DMI_ADX = dx.groupby(code).transform(lambda x: x.rolling(6).mean())

score = ((MA5 > MA10) & (MA10 > MA20) & (MA20 > MA60)).astype(float) * 15
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
COMPOSITE = score.clip(0, 100).to_numpy()

code_arr = df["code"].to_numpy()
date_arr = df["trade_date"].to_numpy()
open_arr = df["open"].to_numpy()
high_arr = df["high"].to_numpy()
close_arr = df["close"].to_numpy()
atr_s = ATR.to_numpy()
close_s = close.to_numpy()
ma20_arr = MA20.to_numpy()
n = len(df)

# ── 锚点：composite 上穿 65（按 code 分组 shift）──
same_prev = np.zeros(n, dtype=bool)
same_prev[1:] = code_arr[1:] == code_arr[:-1]
prev_score = np.where(same_prev, np.roll(COMPOSITE, 1), np.nan)
cross65 = (COMPOSITE >= 65) & (prev_score < 65)

# ── 弱市闸门 ──
pct_chg = close.groupby(code).pct_change()
mkt_by_date = pct_chg.groupby(df["trade_date"]).mean()
unique_dates = pd.DatetimeIndex(pd.unique(date_arr)).sort_values()
mkt_arr = mkt_by_date.reindex(unique_dates).to_numpy()
date_idx = np.searchsorted(unique_dates.to_numpy(), date_arr)
GATE_WEAK = mkt_arr[date_idx] > -0.005

# ── 三过滤信号：锚点回看 5 日 + 确认突破 + 偏离≤5% + 弱市闸门 ──
LOOKBACK, CONFIRM_MULT, DEV_MAX = 5, 1.002, 0.05
cross_idx = np.where(cross65)[0]
# 每根 bar 到最近锚点的距离（同股）
anchor_age = np.full(n, 10 ** 9)
anchor_high = np.full(n, np.nan)
anchor_close = np.full(n, np.nan)
anchor_atr = np.full(n, np.nan)
ci = 0
for i in range(n):
    while ci < len(cross_idx) and cross_idx[ci] < i:
        ci += 1
    j = ci - 1
    if j >= 0 and code_arr[cross_idx[j]] == code_arr[i] and i - cross_idx[j] <= LOOKBACK:
        a = cross_idx[j]
        anchor_age[i] = i - a
        anchor_high[i] = high_arr[a]
        anchor_close[i] = close_s[a]
        anchor_atr[i] = atr_s[a]

confirm_ok = anchor_age <= LOOKBACK
confirm = confirm_ok & (close_arr > anchor_high * CONFIRM_MULT)
dev_ok = np.isfinite(ma20_arr) & (close_arr / ma20_arr - 1.0 <= DEV_MAX)
SIG_raw = confirm & dev_ok & GATE_WEAK
# 同锚点只取首个确认日（与 mine_mid_entry confirm 变体口径一致）
SIG = SIG_raw.copy()
last_anchor_kept = {}
for i in np.where(SIG_raw)[0]:
    key = (code_arr[i], int(anchor_age[i]), i - int(anchor_age[i]))
    if key in last_anchor_kept:
        SIG[i] = False
    else:
        last_anchor_kept[key] = i

# ── 出场模拟（线上口径）──
K, MAXHOLD = 2.5, 60
LAUNCH, TRAIL, STOP_CAP = 0.06, 0.10, 0.12
TEST_START = np.datetime64("2020-01-01")

sig_idx = np.where(SIG)[0]
m = len(sig_idx)
entry_di = sig_idx + 1
ok = (entry_di < n) & (code_arr[np.clip(entry_di, 0, n - 1)] == code_arr[sig_idx])
entry_di = np.clip(entry_di, 0, n - 1)
entry = np.where(ok, open_arr[entry_di], np.nan)

idx = entry_di[:, None] + np.arange(0, MAXHOLD)[None, :]
idx_clip = np.clip(idx, 0, n - 1)
same = code_arr[idx_clip] == code_arr[entry_di][:, None]
F_high = np.where(same, high_arr[idx_clip], np.nan)
F_close = np.where(same, close_arr[idx_clip], np.nan)
valid = ok & (entry > 0) & same[:, MAXHOLD - 1]

sig_stop = np.where(np.isfinite(anchor_atr[sig_idx]) & (anchor_atr[sig_idx] > 0),
                    anchor_close[sig_idx] - K * anchor_atr[sig_idx],
                    anchor_close[sig_idx] * 0.92)
stop = np.maximum(sig_stop, entry * (1 - STOP_CAP))
launch_line = entry * (1 + LAUNCH)
cum_high = np.fmax.accumulate(np.where(np.isnan(F_high), -np.inf, F_high), axis=1)
active = cum_high >= launch_line[:, None]
trail_line = np.where(active, cum_high * (1 - TRAIL), 0.0)
off = np.arange(0, MAXHOLD)[None, :]
check = off >= 1
stop_hit = check & ~np.isnan(F_close) & (F_close <= stop[:, None])
trail_hit = check & ~np.isnan(F_close) & (F_close <= trail_line)
has_stop = stop_hit.any(axis=1); has_trail = trail_hit.any(axis=1)
first_stop = np.argmax(stop_hit, axis=1); first_trail = np.argmax(trail_hit, axis=1)

rets = np.full(m, np.nan)
m_exp = valid & ~has_stop & ~has_trail
rets[m_exp] = F_close[m_exp, MAXHOLD - 1] / entry[m_exp] - 1.0
m_t = valid & has_trail & (~has_stop | (first_trail < first_stop))
rows = np.where(m_t)[0]
rets[rows] = F_close[rows, first_trail[rows]] / entry[rows] - 1.0
m_s = valid & has_stop & (~has_trail | (first_stop <= first_trail))
rows = np.where(m_s)[0]
rets[rows] = F_close[rows, first_stop[rows]] / entry[rows] - 1.0

n_days_test = len(unique_dates[unique_dates >= TEST_START])
for tag, msk in (("all", valid), ("test", valid & (date_arr[sig_idx] >= TEST_START))):
    r = rets[msk]
    win = (r > 0).mean() * 100
    mean = r.mean() * 100
    w = r[r > 0]; l = r[r <= 0]
    aw = w.mean() if len(w) else 0.0
    al = abs(l.mean()) if len(l) else 1e-9
    pf = aw / al if al > 1e-9 else 99.0
    print(f"{tag:5s} n={len(r):>7d} 胜率={win:5.2f}% 均值={mean:+6.3f}% PF={pf:4.2f}",
          flush=True)
n_sig_test = int((date_arr[sig_idx] >= TEST_START).sum())
print(f"test日均信号 {n_sig_test / max(n_days_test, 1):.0f} 只 "
      f"(基准现状 235，top-4 需求充足)", flush=True)
print("准入: 胜率>=41.1% 且 PF>=1.0", flush=True)
