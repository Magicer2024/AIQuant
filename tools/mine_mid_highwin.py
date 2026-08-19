"""
mine_mid_highwin.py - 中线高胜率风格选型挖掘（只读，go/no-go 证据）

背景：composite 上穿风格胜率天花板 ~43%（趋势跟随低胜率高盈亏比属性）。
用户授权换高胜率风格。高胜率必然是"低吸 + 小止盈"结构，测两个候选：

风格 A 趋势回调低吸：上升趋势中回踩 MA20 附近缩量企稳
  - 趋势闸门：MA20 > MA60 且 MA60 较 10 日前走高（避免下降趋势接刀）
  - 回调区：MA20×0.95 <= close <= MA20×1.02
  - A1 直接入场：回调当日信号，次日开盘买
  - A2 确认入场：昨日在回调区 且 今日收盘 > 昨日高点（反抽确认），次日开盘买
  - 出场网格：固定止盈 T / 固定止损 S（收盘判定，T+1，stop 优先）

风格 B 超跌反弹：长期趋势向上背景下的深度超跌反抽
  - 闸门：MA60 斜率向上
  - 触发：RSI14 < 30 且 当日收盘 > 昨日高点（放量反包阳线）
  - 出场网格：同 A，maxhold 更短（30 日）

信号去重：同股 cooldown=10 个交易日（近似实盘单票容量）。
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


print("computing indicators...", flush=True)
MA20 = gro(close, 20)
MA60 = gro(close, 60)
delta = close.groupby(code).diff()
gain = delta.where(delta > 0, 0.0)
loss = -delta.where(delta < 0, 0.0)
RSI14 = 100 - 100 / (1 + gewm(gain, com=13, adjust=False) /
                     gewm(loss, com=13, adjust=False).replace(0, np.nan))
vol_ma5 = gro(volume, 5)

code_arr = df["code"].to_numpy()
date_arr = df["trade_date"].to_numpy()
open_arr = df["open"].to_numpy()
close_arr = df["close"].to_numpy()
n = len(df)

prev_high = gshift(high).to_numpy()
prev_close = gshift(close).to_numpy()
same_prev = np.zeros(n, dtype=bool)
same_prev[1:] = code_arr[1:] == code_arr[:-1]

# ── 风格 A：趋势回调低吸 ──
ma60_up = (MA60 > gshift(MA60, 10)).to_numpy()
trend_up = (MA20.to_numpy() > MA60.to_numpy()) & ma60_up
ma20_arr = MA20.to_numpy()
in_zone = (close_arr >= ma20_arr * 0.95) & (close_arr <= ma20_arr * 1.02)
shrink = (volume.to_numpy() < vol_ma5.to_numpy() * 0.9)

A1_raw = trend_up & in_zone & np.isfinite(ma20_arr)
A2_raw = trend_up & np.isfinite(ma20_arr) & (close_arr > prev_high) & \
         np.roll(in_zone, 1) & same_prev

# ── 风格 B：超跌反弹 ──
rsi = RSI14.to_numpy()
B_raw = ma60_up & (rsi < 30) & (close_arr > prev_high) & np.isfinite(rsi)

TEST_START = np.datetime64("2020-01-01")
unique_dates = pd.DatetimeIndex(pd.unique(date_arr)).sort_values()


def dedup(mask, cooldown=10):
    """同股信号 cooldown 去重（保留先出现的）。"""
    idx = np.where(mask)[0]
    if len(idx) == 0:
        return mask.copy()
    keep = np.zeros(n, dtype=bool)
    last_kept = {}
    for i in idx:
        c = code_arr[i]
        lk = last_kept.get(c)
        if lk is None or (i - lk) >= cooldown:
            keep[i] = True
            last_kept[c] = i
    return keep


def simulate(sig_mask, take, stop_pct, maxhold):
    """固定止盈/止损模拟（收盘判定，T+1，stop 优先，到期收盘卖出）。"""
    sig_idx = np.where(sig_mask)[0]
    m = len(sig_idx)
    entry_di = sig_idx + 1
    ok = (entry_di < n) & (code_arr[np.clip(entry_di, 0, n - 1)] == code_arr[sig_idx])
    entry_di = np.clip(entry_di, 0, n - 1)
    entry = np.where(ok, open_arr[entry_di], np.nan)

    idx = entry_di[:, None] + np.arange(0, maxhold)[None, :]
    idx_clip = np.clip(idx, 0, n - 1)
    same = code_arr[idx_clip] == code_arr[entry_di][:, None]
    F_close = np.where(same, close_arr[idx_clip], np.nan)

    valid = ok & (entry > 0) & same[:, maxhold - 1]
    stop_line = entry * (1 - stop_pct)
    take_line = entry * (1 + take)
    off = np.arange(0, maxhold)[None, :]
    check = off >= 1
    stop_hit = check & ~np.isnan(F_close) & (F_close <= stop_line[:, None])
    take_hit = check & ~np.isnan(F_close) & (F_close >= take_line[:, None])
    has_stop = stop_hit.any(axis=1)
    has_take = take_hit.any(axis=1)
    first_stop = np.argmax(stop_hit, axis=1)
    first_take = np.argmax(take_hit, axis=1)

    rets = np.full(m, np.nan)
    m_exp = valid & ~has_stop & ~has_take
    rets[m_exp] = F_close[m_exp, maxhold - 1] / entry[m_exp] - 1.0
    m_take = valid & has_take & (~has_stop | (first_take < first_stop))
    rows = np.where(m_take)[0]
    rets[rows] = F_close[rows, first_take[rows]] / entry[rows] - 1.0
    m_stop = valid & has_stop & (~has_take | (first_stop <= first_take))
    rows = np.where(m_stop)[0]
    rets[rows] = F_close[rows, first_stop[rows]] / entry[rows] - 1.0
    return rets, valid, date_arr[sig_idx]


def grid(label, sig_mask, takes, stops, maxhold, min_n=100):
    print(f"\n── {label} ──", flush=True)
    best = None
    for T in takes:
        for S in stops:
            rets, valid, sig_dates = simulate(sig_mask, T, S, maxhold)
            for tag, msk in (("test", valid & (sig_dates >= TEST_START)),):
                r = rets[msk]
                cnt = len(r)
                if cnt < min_n:
                    continue
                win = (r > 0).mean() * 100
                mean = r.mean() * 100
                w = r[r > 0]; l = r[r <= 0]
                aw = w.mean() if len(w) else 0.0
                al = abs(l.mean()) if len(l) else 1e-9
                pf = aw / al if al > 1e-9 else 99.0
                print(f"  T={T*100:>4.0f}% S={S*100:>4.0f}% | n={cnt:>7d} "
                      f"胜率={win:5.1f}% 均值={mean:+6.3f}% PF={pf:4.2f}", flush=True)
                if best is None or (win >= 50 and pf > best[3]) or \
                        (best[2] < 50 and win > best[2]):
                    best = (T, S, win, pf, mean)
    if best:
        print(f"  → 最优: T={best[0]*100:.0f}% S={best[1]*100:.0f}% "
              f"胜率={best[2]:.1f}% PF={best[3]:.2f} 均值={best[4]:+.3f}%", flush=True)


A1 = dedup(A1_raw)
A2 = dedup(A2_raw)
A2s = dedup(A2_raw & shrink)
B = dedup(B_raw)

print(f"信号量(cooldown去重后): A1直接={A1.sum()} A2确认={A2.sum()} "
      f"A2确认+缩量={A2s.sum()} B超跌={B.sum()}", flush=True)

grid("风格A1 回调直接入场 maxhold=60", A1,
     takes=[0.04, 0.06, 0.08, 0.10], stops=[0.04, 0.06, 0.08], maxhold=60)
grid("风格A2 反抽确认入场 maxhold=60", A2,
     takes=[0.04, 0.06, 0.08, 0.10], stops=[0.04, 0.06, 0.08], maxhold=60)
grid("风格A2 确认+缩量 maxhold=60", A2s,
     takes=[0.04, 0.06, 0.08, 0.10], stops=[0.04, 0.06, 0.08], maxhold=60)
grid("风格B 超跌反弹 maxhold=30", B,
     takes=[0.03, 0.05, 0.08], stops=[0.05, 0.08], maxhold=30)

# ── 第二轮：激活式移动锁盈 + 趋势强度过滤 ──
print("\ncomputing ADX / MA20 slope filters...", flush=True)
prev_low = gshift(low).to_numpy()
prev_close_arr = prev_close
high_arr = df["high"].to_numpy()
low_arr = df["low"].to_numpy()
tr = np.maximum(high_arr - low_arr,
                np.maximum(np.abs(high_arr - prev_close_arr),
                           np.abs(low_arr - prev_close_arr)))
tr[~same_prev] = np.nan
tr_s = pd.Series(tr, index=df.index)
ATR14 = tr_s.groupby(code).transform(lambda x: x.rolling(14).mean()).to_numpy()
up_move = high_arr - prev_close_arr
dn_move = prev_close_arr - low_arr
plus_dm = np.where((up_move > dn_move) & same_prev, np.maximum(up_move, 0), np.nan)
minus_dm = np.where((dn_move > up_move) & same_prev, np.maximum(dn_move, 0), np.nan)
pdm14 = pd.Series(plus_dm, index=df.index).groupby(code).transform(
    lambda x: x.rolling(14).mean()).to_numpy()
mdm14 = pd.Series(minus_dm, index=df.index).groupby(code).transform(
    lambda x: x.rolling(14).mean()).to_numpy()
with np.errstate(divide="ignore", invalid="ignore"):
    plus_di = 100 * pdm14 / ATR14
    minus_di = 100 * mdm14 / ATR14
    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
ADX14 = pd.Series(dx, index=df.index).groupby(code).transform(
    lambda x: x.rolling(6).mean()).to_numpy()
ma20_slope_up = (MA20 > gshift(MA20, 5)).to_numpy()

A1_strong = dedup(A1_raw & (ADX14 > 20) & ma20_slope_up)
print(f"A1+趋势强度(ADX>20+MA20斜率) 信号量={A1_strong.sum()}", flush=True)


def simulate_trailing(sig_mask, stop_pct, act, drop, floor, maxhold):
    """激活式移动锁盈：浮盈达 act 后，止盈线 = max(累计高点×(1-drop), entry×(1+floor))。

    硬止损 entry×(1-stop_pct) 优先；到期收盘卖出。收盘判定，T+1。
    """
    sig_idx = np.where(sig_mask)[0]
    m = len(sig_idx)
    entry_di = sig_idx + 1
    ok = (entry_di < n) & (code_arr[np.clip(entry_di, 0, n - 1)] == code_arr[sig_idx])
    entry_di = np.clip(entry_di, 0, n - 1)
    entry = np.where(ok, open_arr[entry_di], np.nan)

    idx = entry_di[:, None] + np.arange(0, maxhold)[None, :]
    idx_clip = np.clip(idx, 0, n - 1)
    same = code_arr[idx_clip] == code_arr[entry_di][:, None]
    F_close = np.where(same, close_arr[idx_clip], np.nan)
    valid = ok & (entry > 0) & same[:, maxhold - 1]

    stop_line = entry * (1 - stop_pct)
    cum_high = np.fmax.accumulate(np.where(np.isnan(F_close), -np.inf, F_close), axis=1)
    activated = cum_high >= entry[:, None] * (1 + act)
    trail_line = np.where(activated,
                          np.maximum(cum_high * (1 - drop), entry[:, None] * (1 + floor)),
                          -np.inf)
    off = np.arange(0, maxhold)[None, :]
    check = off >= 1
    stop_hit = check & ~np.isnan(F_close) & (F_close <= stop_line[:, None])
    trail_hit = check & ~np.isnan(F_close) & (F_close <= trail_line)
    has_stop = stop_hit.any(axis=1)
    has_trail = trail_hit.any(axis=1)
    first_stop = np.argmax(stop_hit, axis=1)
    first_trail = np.argmax(trail_hit, axis=1)

    rets = np.full(m, np.nan)
    m_exp = valid & ~has_stop & ~has_trail
    rets[m_exp] = F_close[m_exp, maxhold - 1] / entry[m_exp] - 1.0
    m_t = valid & has_trail & (~has_stop | (first_trail < first_stop))
    rows = np.where(m_t)[0]
    rets[rows] = F_close[rows, first_trail[rows]] / entry[rows] - 1.0
    m_s = valid & has_stop & (~has_trail | (first_stop <= first_trail))
    rows = np.where(m_s)[0]
    rets[rows] = F_close[rows, first_stop[rows]] / entry[rows] - 1.0
    return rets, valid, date_arr[sig_idx]


def stats_line(prefix, rets, valid, sig_dates, min_n=100):
    msk = valid & (sig_dates >= TEST_START)
    r = rets[msk]
    cnt = len(r)
    if cnt < min_n:
        print(f"  {prefix} | 样本不足", flush=True)
        return
    win = (r > 0).mean() * 100
    mean = r.mean() * 100
    w = r[r > 0]; l = r[r <= 0]
    aw = w.mean() if len(w) else 0.0
    al = abs(l.mean()) if len(l) else 1e-9
    pf = aw / al if al > 1e-9 else 99.0
    print(f"  {prefix} | n={cnt:>7d} 胜率={win:5.1f}% 均值={mean:+6.3f}% PF={pf:4.2f}",
          flush=True)


print("\n── 第二轮A 趋势回调+激活式移动锁盈（A1 全集）──", flush=True)
for S in (0.04, 0.05, 0.06):
    for act, drop, floor in ((0.03, 0.03, 0.0), (0.04, 0.04, 0.01), (0.04, 0.03, 0.01)):
        rets, valid, sd = simulate_trailing(A1, S, act, drop, floor, 60)
        stats_line(f"S={S*100:.0f}% act={act*100:.0f}% drop={drop*100:.0f}% "
                   f"floor={floor*100:.0f}%", rets, valid, sd)

print("\n── 第二轮B 趋势强度过滤+激活式移动锁盈（A1_strong）──", flush=True)
for S in (0.04, 0.05, 0.06):
    for act, drop, floor in ((0.03, 0.03, 0.0), (0.04, 0.04, 0.01), (0.04, 0.03, 0.01)):
        rets, valid, sd = simulate_trailing(A1_strong, S, act, drop, floor, 60)
        stats_line(f"S={S*100:.0f}% act={act*100:.0f}% drop={drop*100:.0f}% "
                   f"floor={floor*100:.0f}%", rets, valid, sd)

print("\n── 第三轮 高胜率候选 + 弱市闸门 ──", flush=True)
pct_chg = close.groupby(code).pct_change()
mkt_by_date = pct_chg.groupby(df["trade_date"]).mean()
unique_dates2 = pd.DatetimeIndex(pd.unique(date_arr)).sort_values()
mkt_arr2 = mkt_by_date.reindex(unique_dates2).to_numpy()
date_idx2 = np.searchsorted(unique_dates2.to_numpy(), date_arr)
mkt_day = mkt_arr2[date_idx2]
GATE_WEAK = mkt_day > -0.005

for S, T in ((0.04, 0.04), (0.06, 0.04)):
    rets, valid, sd = simulate(A1 & GATE_WEAK, T, S, 60)
    stats_line(f"A1+弱市闸门 T={T*100:.0f}% S={S*100:.0f}%", rets, valid, sd)
for S, act, drop, floor in ((0.05, 0.04, 0.03, 0.01), (0.06, 0.04, 0.03, 0.01)):
    rets, valid, sd = simulate_trailing(A1 & GATE_WEAK, S, act, drop, floor, 60)
    stats_line(f"A1+弱市闸门 S={S*100:.0f}% act=4% drop=3% floor=1%", rets, valid, sd)

print("\ndone.", flush=True)
