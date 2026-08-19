"""
mine_mid_entry.py - 中线买入端改进方向只读挖掘（go/no-go 证据，不改任何线上代码）

背景：出场端已优化到位（launch=0.06/trail=0.10/止损上限-12%，全历史回测胜率
上限 ~41%），瓶颈在买入端——当前中线信号 = composite 分"上穿 65 当日"触发，
容易追在动能释放点。本脚本固定出场口径（与线上 evaluate_exit_by_prices 一致），
只变换买入条件，对比胜率/均值/PF：

候选方向（2026-08-19 与用户确认全测）：
  ① 上穿阈值提高：65 → 70 / 75
  ② MA5>MA10>MA20>MA60 多头排列设为硬条件
  ③ 市场环境闸门：弱市日（全市场当日平均涨幅 <= -0.5%）不出信号；
     火热日（>= +1%，与强势突破 _surge_market_ok 同口径反向）不出信号
  ④ 突破确认入场：信号日后 5 个交易日内收盘突破信号日高点×1.002 才买入
     （次日开盘建仓），未确认则放弃——避免追高

出场口径（与线上一致）：entry=T+1 开盘，收盘判定，stop=max(信号close-2.5×ATR,
entry×0.88)，浮盈 +6% 启动移动止盈、自高点回撤 10% 清仓，maxhold=60。
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

# ── 市场环境（按交易日的全市场平均涨幅）──
pct_chg = close.groupby(code).pct_change()
mkt_by_date = pct_chg.groupby(df["trade_date"]).mean().sort_index()
mkt_ma5_by_date = mkt_by_date.rolling(5).mean()
date_arr = df["trade_date"].to_numpy()
unique_dates = pd.DatetimeIndex(pd.unique(date_arr)).sort_values()
mkt_arr = mkt_by_date.reindex(unique_dates).to_numpy()
mkt_ma5_arr = mkt_ma5_by_date.reindex(unique_dates).to_numpy()
date_to_i = {d: i for i, d in enumerate(unique_dates)}
# 逐行映射（向量化：date_arr → 唯一日期序号）
date_idx = np.searchsorted(unique_dates.to_numpy(), date_arr)
mkt_day = mkt_arr[date_idx]           # 当日全市场平均涨幅
mkt_ma5 = mkt_ma5_arr[date_idx]       # 5 日均

print("signals computed. building variants...", flush=True)

code_arr = df["code"].to_numpy()
atr_s = ATR.to_numpy()
close_s = close.to_numpy()
high_s = high.to_numpy()
open_arr = df["open"].to_numpy()
high_arr = df["high"].to_numpy()
close_arr = df["close"].to_numpy()

K = 2.5
MAXHOLD = 60
LAUNCH, TRAIL, STOP_CAP = 0.06, 0.10, 0.12
TEST_START = np.datetime64("2020-01-01")
CONFIRM_WINDOW = 5
CONFIRM_MULT = 1.002


def cross_sig(thr):
    s = COMPOSITE.to_numpy()
    prev = np.roll(s, 1); prev[0] = np.nan
    # shift(1) 需按 code 分组：首行置 NaN
    first_row = np.zeros(len(df), dtype=bool)
    di_all = np.arange(len(df))
    # code 首行：同 code 上一行不存在
    same_prev = np.zeros(len(df), dtype=bool)
    same_prev[1:] = code_arr[1:] == code_arr[:-1]
    prev_score = np.where(same_prev, np.roll(s, 1), np.nan)
    return (s >= thr) & (prev_score < thr)


SIG65 = cross_sig(65)
SIG70 = cross_sig(70)
SIG75 = cross_sig(75)
MA_BULL = ma_bull.to_numpy()
GATE_WEAK = mkt_day > -0.005          # 弱市闸门：当日全市场均涨 > -0.5% 才放行
GATE_HOT = mkt_day < 0.01             # 火热闸门：当日全市场均涨 < +1% 才放行


def simulate_from(sig_idx, confirm=False):
    """固定出场口径模拟。confirm=True：先等收盘突破信号日高点才建仓。

    返回 (rets, reasons, valid_mask)——向量化：
      非确认：entry=信号次日开盘（与线上一致）
      确认：信号后 1~5 日内首个 close > sig_high×1.002 的次日开盘为 entry
    """
    sig_idx = np.asarray(sig_idx)
    n = len(sig_idx)
    if n == 0:
        return np.array([]), np.array([]), np.array([], dtype=bool)

    if not confirm:
        entry_di = sig_idx + 1
        entry_ok = (entry_di < len(df)) & (code_arr[entry_di] == code_arr[sig_idx])
        entry_di = np.clip(entry_di, 0, len(df) - 1)
    else:
        # 确认后 1..5 日窗口：close > 信号日高点 × CONFIRM_MULT
        cw = sig_idx[:, None] + np.arange(1, CONFIRM_WINDOW + 1)[None, :]
        cw_clip = np.clip(cw, 0, len(df) - 1)
        cw_ok = code_arr[cw_clip] == code_arr[sig_idx][:, None]
        cw_close = np.where(cw_ok, close_arr[cw_clip], np.nan)
        target = high_s[sig_idx][:, None] * CONFIRM_MULT
        hit = ~np.isnan(cw_close) & (cw_close > target)
        has_confirm = hit.any(axis=1)
        first_off = np.argmax(hit, axis=1) + 1          # 1-based offset
        confirm_di = sig_idx + first_off
        entry_di = confirm_di + 1                        # 确认次日开盘建仓（T+1）
        entry_ok = has_confirm & (entry_di < len(df)) & (
            code_arr[entry_di] == code_arr[sig_idx])
        entry_di = np.clip(entry_di, 0, len(df) - 1)

    # 未来窗口矩阵（从 entry 日起 MAXHOLD 天）
    idx = entry_di[:, None] + np.arange(0, MAXHOLD)[None, :]
    idx_clip = np.clip(idx, 0, len(df) - 1)
    same = code_arr[idx_clip] == code_arr[entry_di][:, None]
    F_high = np.where(same, high_arr[idx_clip], np.nan)
    F_close = np.where(same, close_arr[idx_clip], np.nan)

    entry = np.where(same[:, 0], open_arr[entry_di], np.nan)
    valid_entry = same[:, 0] & (entry > 0) & entry_ok
    full_len = same[:, MAXHOLD - 1]
    valid = valid_entry & full_len

    # 止损：信号日 close - k×ATR（ATR 缺失兜底 -8%），叠加宽度上限（相对 entry）
    sig_stop = np.where(np.isfinite(atr_s[sig_idx]) & (atr_s[sig_idx] > 0),
                        close_s[sig_idx] - K * atr_s[sig_idx],
                        close_s[sig_idx] * 0.92)
    stop = np.maximum(sig_stop, entry * (1 - STOP_CAP))

    launch_line = entry * (1 + LAUNCH)
    cum_high = np.fmax.accumulate(np.where(np.isnan(F_high), -np.inf, F_high), axis=1)
    active = cum_high >= launch_line[:, None]
    trail_line = np.where(active, cum_high * (1 - TRAIL), 0.0)
    off = np.arange(0, MAXHOLD)[None, :]
    check = off >= 1                                    # entry 当日(off=0)不可卖（T+1）
    stop_hit = check & ~np.isnan(F_close) & (F_close <= stop[:, None])
    trail_hit = check & ~np.isnan(F_close) & (F_close <= trail_line)
    first_stop = np.argmax(stop_hit, axis=1)
    first_trail = np.argmax(trail_hit, axis=1)
    has_stop = stop_hit.any(axis=1)
    has_trail = trail_hit.any(axis=1)

    rets = np.full(n, np.nan)
    reasons = np.full(n, "", dtype=object)
    m_expire = valid & ~has_stop & ~has_trail
    rets[m_expire] = F_close[m_expire, MAXHOLD - 1] / entry[m_expire] - 1.0
    reasons[m_expire] = "expire"
    m_trail = valid & has_trail & (~has_stop | (first_trail < first_stop))
    rows = np.where(m_trail)[0]
    rets[rows] = F_close[rows, first_trail[rows]] / entry[rows] - 1.0
    reasons[rows] = "trail"
    m_stop = valid & has_stop & (~has_trail | (first_stop <= first_trail))
    rows = np.where(m_stop)[0]
    rets[rows] = F_close[rows, first_stop[rows]] / entry[rows] - 1.0
    reasons[rows] = "stop"
    return rets, reasons, valid


def report(label, sig_mask, confirm=False, min_n=100):
    sig_idx = np.where(sig_mask)[0]
    rets, reasons, valid = simulate_from(sig_idx, confirm=confirm)
    is_test = date_arr[sig_idx] >= TEST_START
    out = []
    for tag, m in (("all", valid), ("test", valid & is_test)):
        r = rets[m]
        n = len(r)
        if n < min_n:
            out.append((tag, n, np.nan, np.nan, np.nan))
            continue
        win = (r > 0).mean() * 100
        mean = r.mean() * 100
        w = r[r > 0]; l = r[r <= 0]
        aw = w.mean() if len(w) else 0.0
        al = abs(l.mean()) if len(l) else 1e-9
        pf = aw / al if al > 1e-9 else 99.0
        out.append((tag, n, win, mean, pf))
    # 信号密度（日均，test 窗口）
    n_days_test = len(unique_dates[unique_dates >= TEST_START])
    n_sig_test = int((date_arr[sig_idx] >= TEST_START).sum())
    daily = n_sig_test / max(n_days_test, 1)
    a, t = out
    print(f"{label:<42s} | 全历史 n={a[1]:>7d} 胜率={a[2]:5.1f}% 均值={a[3]:+6.3f}% PF={a[4]:4.2f}"
          if not np.isnan(a[2]) else f"{label:<42s} | 全历史 样本不足", flush=True)
    print(f"{'':42s} | test    n={t[1]:>7d} 胜率={t[2]:5.1f}% 均值={t[3]:+6.3f}% PF={t[4]:4.2f}"
          f" | test日均信号 {daily:.0f} 只"
          if not np.isnan(t[2]) else f"{'':42s} | test 样本不足", flush=True)


bull = MA_BULL
print("\n── ① 上穿阈值 ──", flush=True)
report("base: 上穿65（现状）", SIG65)
report("上穿70", SIG70)
report("上穿75", SIG75)

print("\n── ② 多头排列硬条件 ──", flush=True)
report("上穿65 + 多头排列", SIG65 & bull)
report("上穿70 + 多头排列", SIG70 & bull)

print("\n── ③ 市场环境闸门 ──", flush=True)
report("上穿65 + 弱市闸门(日>-0.5%)", SIG65 & GATE_WEAK)
report("上穿65 + 火热闸门(日<+1%)", SIG65 & GATE_HOT)
report("上穿65 + 双闸门", SIG65 & GATE_WEAK & GATE_HOT)

print("\n── ④ 突破确认入场 ──", flush=True)
report("上穿65 + 确认(5日破信号日高点)", SIG65, confirm=True)
report("上穿70 + 确认", SIG70, confirm=True)
report("上穿70 + 多头排列 + 确认", SIG70 & bull, confirm=True)

print("\n── 组合最优候选 ──", flush=True)
report("上穿70 + 多头 + 双闸门 + 确认", SIG70 & bull & GATE_WEAK & GATE_HOT, confirm=True)
report("上穿65 + 多头 + 双闸门 + 确认", SIG65 & bull & GATE_WEAK & GATE_HOT, confirm=True)

print("\n── 补充假设（趋势位置过滤）──", flush=True)
dist_ma20 = (close / MA20 - 1.0).to_numpy()
ma60_slope = (MA60 > gshift(MA60, 10)).to_numpy()     # MA60 较 10 日前走高
report("上穿65 + 偏离MA20<=8%（不追高）", SIG65 & (dist_ma20 <= 0.08))
report("上穿65 + 偏离MA20<=5%", SIG65 & (dist_ma20 <= 0.05))
report("上穿65 + MA60斜率向上", SIG65 & ma60_slope)
report("上穿65 + 偏离<=8% + MA60斜率", SIG65 & (dist_ma20 <= 0.08) & ma60_slope)
report("上穿65 + 弱市闸门 + 偏离<=8%", SIG65 & GATE_WEAK & (dist_ma20 <= 0.08))
report("上穿65 + 弱市闸门 + 偏离<=5%", SIG65 & GATE_WEAK & (dist_ma20 <= 0.05))
report("上穿65 + 偏离<=5% + 确认", SIG65 & (dist_ma20 <= 0.05), confirm=True)
report("上穿65 + 弱市闸门 + 偏离<=5% + 确认", SIG65 & GATE_WEAK & (dist_ma20 <= 0.05), confirm=True)

print("\ndone.", flush=True)
