"""
backtest_mid_exit_grid.py - 中线移动止盈参数网格回测（只读，向量化）

背景：出场跟踪显示中线 56 个持仓段浮亏合计 -147%（均 -2.63%）——移动止盈
启动线 +10% 过高，多数票从未激活移动止盈，下行途中仅有宽 ATR 止损（k=2.5，
可达 -20%）保护。本脚本验证「降低启动线 / 收紧回撤 / 止损宽度上限」的效果。

口径与线上 evaluate_exit_by_prices 完全一致：
  entry = 信号日次日开盘价（T+1 open）
  T+1：买入日当天不可卖出，从第 2 个交易日开始判定
  卖出判定统一用收盘价：收盘跌破止损 → 移动止盈（持仓期最高价 high 达启动线
  后，收盘跌破 最高价×(1-trail)，止盈线只上移）→ 到期(maxhold) 收盘卖出
  止损 = 信号日 close - k×ATR（k=mid_atr_stop_mult），可叠加宽度上限 cap：
  effective_stop = max(signal_stop, entry×(1-cap))

实现：对全部信号日构造 (n_sig, 61) 的 future 窗口矩阵，各参数组合用 numpy
批量判定首个出场日 —— 与逐日 for 循环模拟逐位等价，但快几个数量级。

样本：全历史 composite BUY_SIGNAL（与 backtest_midlong_exit.py 同口径），
train/test 双窗口（2020+ 为 test）防过拟合。
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


print("computing indicators (vectorized)...", flush=True)

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

print("signals computed. building future windows...", flush=True)

code_arr = df["code"].to_numpy()
date_arr = df["trade_date"].to_numpy()
atr_s = ATR.to_numpy()
close_sig = close.to_numpy()
open_arr = df["open"].to_numpy()
high_arr = df["high"].to_numpy()
close_arr = df["close"].to_numpy()

K = 2.5          # mid_atr_stop_mult 当前值
MAXHOLD = 60     # 中线持仓上限
TEST_START = np.datetime64("2020-01-01")

mid_sig_idx = np.where(MID_BUY.to_numpy())[0]
n_total = len(mid_sig_idx)

# ── 构造未来窗口矩阵（同 code 连续，跨 code 处置 NaN 使其永不触发）──
idx = mid_sig_idx[:, None] + np.arange(1, MAXHOLD + 2)[None, :]   # (n, 61)
idx_clipped = np.clip(idx, 0, len(df) - 1)
same_code = code_arr[idx_clipped] == code_arr[mid_sig_idx][:, None]

F_open = np.where(same_code, open_arr[idx_clipped], np.nan)
F_high = np.where(same_code, high_arr[idx_clipped], np.nan)
F_close = np.where(same_code, close_arr[idx_clipped], np.nan)

entry = F_open[:, 0]                    # 次日开盘建仓
valid_entry = same_code[:, 0] & (entry > 0)

# 信号自带止损 = 信号日 close - k×ATR；ATR 缺失兜底 -8%（与 mid_long.py 一致）
sig_stop = np.where(np.isfinite(atr_s[mid_sig_idx]) & (atr_s[mid_sig_idx] > 0),
                    close_sig[mid_sig_idx] - K * atr_s[mid_sig_idx],
                    close_sig[mid_sig_idx] * 0.92)

cum_high = np.fmax.accumulate(np.where(np.isnan(F_high), -np.inf, F_high), axis=1)

is_test = date_arr[mid_sig_idx] >= TEST_START

# valid = 入场有效 且 持仓期满（第 MAXHOLD 天仍是同 code，行情未中途断档）
full_len = same_code[:, MAXHOLD - 1]
valid = valid_entry & full_len
print(f"中线信号数: {n_total}，有效样本（行情满 {MAXHOLD} 日）: {int(valid.sum())}，"
      f"其中 test(2020+): {int((valid & is_test).sum())}", flush=True)


def eval_combo(launch, trail, stop_cap=None):
    """向量化出场模拟，返回 (rets, reasons)——仅对 valid 样本有效。

    与 evaluate_exit_by_prices 逐位等价：off≥2 判定；止损优先于移动止盈；
    启动线用盘中 high 触发，移动止盈线 = cummax(high)×(1-trail) 只上移；到期收盘卖。
    """
    stop = sig_stop if stop_cap is None else np.maximum(
        sig_stop, entry * (1 - stop_cap))
    launch_line = entry * (1 + launch)
    active = cum_high >= launch_line[:, None]
    trail_line = cum_high * (1 - trail)
    # 未激活日的 trail_line 置 0（close 恒正 → 永不触发）
    trail_line = np.where(active, trail_line, 0.0)
    off = np.arange(1, MAXHOLD + 2)[None, :]
    check = off >= 2                              # T+1
    stop_hit = check & ~np.isnan(F_close) & (F_close <= stop[:, None])
    trail_hit = check & ~np.isnan(F_close) & (F_close <= trail_line)
    first_stop = np.argmax(stop_hit, axis=1)
    first_trail = np.argmax(trail_hit, axis=1)
    has_stop = stop_hit.any(axis=1)
    has_trail = trail_hit.any(axis=1)
    # 到期：持仓满 MAXHOLD 日未被触发（口径：i >= max_hold_days 当日收盘卖）
    rets = np.full(len(entry), np.nan)
    reasons = np.full(len(entry), "", dtype=object)
    m_expire = valid & ~has_stop & ~has_trail
    rets[m_expire] = F_close[m_expire, MAXHOLD - 1] / entry[m_expire] - 1.0
    reasons[m_expire] = "expire"
    m_trail = valid & has_trail & (~has_stop | (first_trail < first_stop))
    j = first_trail[m_trail]
    rows = np.where(m_trail)[0]
    rets[rows] = F_close[rows, j] / entry[rows] - 1.0
    reasons[rows] = "trail"
    m_stop = valid & has_stop & (~has_trail | (first_stop <= first_trail))
    j = first_stop[m_stop]
    rows = np.where(m_stop)[0]
    rets[rows] = F_close[rows, j] / entry[rows] - 1.0
    reasons[rows] = "stop"
    return rets, reasons


def report(results, label, mask):
    print(f"\n{label}", flush=True)
    print("  %-32s %8s %7s %8s %7s %7s %7s %8s" %
          ("config", "trades", "win%", "mean%", "PF", "trail%", "stop%", "worst%"), flush=True)
    rows = []
    for key, (rets, reasons) in results.items():
        m = mask & ~np.isnan(rets)
        n = int(m.sum())
        if n < 30:
            continue
        r = rets[m]
        win = (r > 0).mean() * 100
        mean = r.mean() * 100
        w = r[r > 0]; l = r[r <= 0]
        aw = w.mean() if len(w) else 0.0
        al = abs(l.mean()) if len(l) else 1e-9
        pf = aw / al if al > 1e-9 else 99.0
        rn = reasons[m]
        rows.append((key, n, win, mean, pf,
                     (rn == "trail").mean() * 100, (rn == "stop").mean() * 100,
                     r.min() * 100))
    for key, n, win, mean, pf, tp, sp, worst in sorted(rows, key=lambda x: x[0]):
        print("  %-32s %8d %6.1f%% %7.3f%% %6.2f %6.1f%% %6.1f%% %7.2f%%" %
              (key, n, win, mean, pf, tp, sp, worst), flush=True)


# ── 参数网格 ──
# launch: 当前 0.10 → 候选降至 0.04~0.10；trail: 0.08/0.10；stop_cap: None/0.12/0.10
LAUNCHES = (0.04, 0.06, 0.08, 0.10)
TRAILS = (0.08, 0.10)
STOP_CAPS = (None, 0.12, 0.10)

results = {}
for launch in LAUNCHES:
    for trail in TRAILS:
        for cap in STOP_CAPS:
            key = (f"launch={launch:.2f} trail={trail:.2f} "
                   f"cap={'-' if cap is None else f'{cap:.2f}'}")
            results[key] = eval_combo(launch, trail, cap)
            print(f"  simulated {key}", flush=True)

report(results, "全历史（train+test）", valid)
report(results, "test 窗口（2020-01-01 起）", valid & is_test)
print("\ndone.", flush=True)
