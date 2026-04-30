"""
backtest_v3.py - 超跌反弹策略 v3 完整回测引擎
============================================
整合用户所有条件的最终版本：

【基本面硬门槛】（预过滤）
  - 非 ST / *ST / 退市风险警示
  - 总市值：60亿 ~ 260亿
  - 净利润 > 0
  - 扣非净利润 > 0

【技术面选股条件】（由 strategy_oversold_rebound v3 计算）
  - 20日区间跌幅 >= 12%
  - 现价 > MA5（站上5日线）
  - 近20日日均成交额 >= 8000万元
  - 近3日均量 > 近5日均量（温和放量）
  - RSI(14)：35 ~ 50
  - 股价不创近5日新低（底部平台确认）

【卖出规则】
  A. 持仓最短 2 天（避免日内波动）
  B. 止损 -6%：无条件清仓
  C. 止盈 +8%~+12%：减半仓；剩余跟踪 MA5
  D. 连续2日缩量阴跌 + 破10日低点：强制清仓
  E. 最长持仓 10 天（到期强制平仓）

【仓位规则】
  - 单只上限 18% 总资金
  - 同时持仓最多 6 只
  - 弱行情留 20% 现金
"""

import pandas as pd
import numpy as np
from datetime import datetime, date, timedelta
from typing import Optional
import time

from core.db import get_all_stocks, get_conn
from strategy.strategies import strategy_oversold_rebound
import baostock as bs

# ─────────────────────────────────────────────
# 核心参数
# ─────────────────────────────────────────────
INIT_CAPITAL     = 100_000
MAX_POSITIONS    = 6
MAX_HOLD_DAYS    = 10
MIN_HOLD_DAYS    = 2
STOP_LOSS        = -0.06
TAKE_PROFIT_LO  = 0.08
TAKE_PROFIT_HI  = 0.12
POSITION_RATIO   = 0.80   # 80% 仓位，留 20% 现金
SINGLE_POS_RATIO = 0.18   # 单股最高 18%
COMMISSION_RATE  = 0.0001
MIN_COMMISSION   = 5.0
EXCLUDE_CODES    = ("688", "301")   # 排除科创板、创业板

# 基本面过滤
MIN_MKT_CAP = 60 * 1e8   # 60亿
MAX_MKT_CAP = 260 * 1e8  # 260亿
PROFIT_YEAR = 2025
PROFIT_QTR  = 4


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────
def _offset_date(date_str: str, days: int) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=days)
    return d.strftime("%Y-%m-%d")


def calc_commission(price: float, shares: int) -> float:
    return max(price * shares * COMMISSION_RATE, MIN_COMMISSION)


# ─────────────────────────────────────────────
# 基本面过滤：baostock 批量预查
# ─────────────────────────────────────────────
def build_fundamental_filter(trade_date: str) -> set:
    """
    用 baostock 批量查询全市场基本面，返回通过过滤的股票代码 set。
    结果缓存到 stock_universe_cache 表。
    """
    # 读缓存（直接用 sqlite3 cursor）
    with get_conn() as conn:
        try:
            rows = conn.execute(
                "SELECT code FROM stock_universe_cache WHERE trade_date=? AND pass=1",
                (trade_date,)
            ).fetchall()
            cached = set(r[0] for r in rows)
        except Exception:
            cached = set()
    if cached:
        print(f"  [基本面] 缓存命中: {len(cached)} 只通过")
        return cached

    print(f"  [基本面] 缓存未命中，查询 baostock（全市场）...")
    lg = bs.login()
    if lg.error_code != '0':
        print(f"  [基本面] baostock 登录失败: {lg.error_msg}")
        return set()

    # 读本地股池（直接用 sqlite3）
    with get_conn() as conn:
        try:
            rows2 = conn.execute(
                "SELECT code, name FROM stock_info WHERE is_active=1"
            ).fetchall()
            stocks_list = [(r[0], r[1]) for r in rows2]
        except Exception:
            stocks_list = []

    passed = []
    total = len(stocks_list)

    for idx, (code, name) in enumerate(stocks_list):

        # 名称过滤 ST
        if any(kw in name for kw in ['ST', '*ST', '退']):
            continue

        # baostock 格式
        bs_code = f"sh.{code}" if code.startswith('6') else f"sz.{code}"

        try:
            rs = bs.query_profit_data(code=bs_code, year=PROFIT_YEAR, quarter=PROFIT_QTR)
            if rs.error_code != '0':
                continue
            df_pf = rs.get_data()
            if df_pf.empty:
                continue

            pf = df_pf.iloc[0]
            net_profit = float(pf['netProfit']) if pf['netProfit'] not in ('', 'None', None) else None
            total_share = float(pf['totalShare']) if pf['totalShare'] not in ('', 'None', None) else None

            if net_profit is None or net_profit <= 0:
                continue

            # 市值估算过滤（总股本 × 10元 ≈ 总市值）
            if total_share:
                mkt_est = total_share * 10
                if not (MIN_MKT_CAP <= mkt_est <= MAX_MKT_CAP):
                    continue
            else:
                continue

            passed.append(code)

        except Exception:
            continue

        if len(passed) % 300 == 0:
            print(f"    已过滤: {len(passed)}/{idx+1}/{total} 通过")

    bs.logout()
    print(f"  [基本面] 查询完成: {len(passed)}/{total} 只通过")

    # 写入缓存
    with get_conn() as conn:
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stock_universe_cache (
                    trade_date TEXT, code TEXT, name TEXT, pass INTEGER,
                    filter_reason TEXT,
                    PRIMARY KEY (trade_date, code))
            """)
        except Exception:
            pass
        conn.execute("DELETE FROM stock_universe_cache WHERE trade_date=?", (trade_date,))
        for c in passed:
            conn.execute(
                "INSERT OR IGNORE INTO stock_universe_cache (trade_date, code, pass) VALUES (?,?,1)",
                (trade_date, c)
            )
        conn.commit()

    return set(passed)


# ─────────────────────────────────────────────
# 数据加载
# ─────────────────────────────────────────────
def preload_all_prices(codes: list, start_date: str, end_date: str) -> dict:
    warm_start = _offset_date(start_date, days=-60)
    sql = """
        SELECT code, trade_date, open, high, low, close, volume, amount, pct_change, turnover
        FROM daily_price
        WHERE trade_date >= ? AND trade_date <= ?
        ORDER BY code, trade_date
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (warm_start, end_date)).fetchall()

    price_data = {}
    for r in rows:
        code = r["code"]
        if code not in price_data:
            price_data[code] = []
        price_data[code].append(dict(r))

    result = {}
    for code, rows_list in price_data.items():
        df = pd.DataFrame(rows_list)
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df.set_index("trade_date", inplace=True)
        df.sort_index(inplace=True)
        df.drop(columns=["code"], errors="ignore", inplace=True)
        result[code] = df

    print(f"      价格预加载完成: {len(result)} 只 × {warm_start}~{end_date}")
    return result


def get_close_from_mem(code: str, cur_date, price_data: dict) -> Optional[float]:
    df = price_data.get(code)
    if df is None:
        return None
    ts = pd.Timestamp(cur_date)
    if ts in df.index:
        return float(df.loc[ts, "close"])
    return None


def get_row_from_mem(code: str, cur_date, price_data: dict) -> Optional[pd.Series]:
    df = price_data.get(code)
    if df is None:
        return None
    ts = pd.Timestamp(cur_date)
    if ts in df.index:
        return df.loc[ts]
    return None


def get_series_from_mem(code: str, cur_date: date, price_data: dict, lookback: int) -> Optional[pd.DataFrame]:
    df = price_data.get(code)
    if df is None:
        return None
    ts = pd.Timestamp(cur_date)
    if ts not in df.index:
        return None
    idx_pos = df.index.get_loc(ts)
    start_pos = max(0, idx_pos - lookback + 1)
    return df.iloc[start_pos:idx_pos + 1].copy()


# ─────────────────────────────────────────────
# 主回测函数
# ─────────────────────────────────────────────
def run_oversold_v3(
    start_date: str = "2025-04-03",
    end_date: str = "2026-04-02",
    init_capital: float = INIT_CAPITAL,
    position_ratio: float = POSITION_RATIO,
    max_positions: int = MAX_POSITIONS,
    max_hold_days: int = MAX_HOLD_DAYS,
    min_hold_days: int = MIN_HOLD_DAYS,
    stop_loss: float = STOP_LOSS,
    take_profit_lo: float = TAKE_PROFIT_LO,
    take_profit_hi: float = TAKE_PROFIT_HI,
    # 策略信号参数
    drop_threshold: float = 0.12,
    vol_20d_min: float = 80_000_000,
    rsi_low: float = 35.0,
    rsi_high: float = 50.0,
    new_low_window: int = 5,
    score_threshold: float = 1.8,
    verbose: bool = True,
) -> dict:
    """
    超跌反弹 v3 完整回测引擎。
    基本面过滤 + 技术面信号 + 自定义止盈止损规则。
    """
    if end_date is None:
        end_date = date.today().strftime("%Y-%m-%d")

    t0 = time.time()

    # ── 1. 基本面过滤 ────────────────────────────────
    fund_filter = build_fundamental_filter(end_date)

    # ── 2. 加载候选股票 ───────────────────────────────
    stocks_df = get_all_stocks()
    stocks_df = stocks_df[~stocks_df["code"].str.startswith(EXCLUDE_CODES)]
    # 基本面过滤
    stocks_df = stocks_df[stocks_df["code"].isin(fund_filter)]
    codes = stocks_df["code"].tolist()
    code_to_name = dict(zip(stocks_df["code"], stocks_df["name"]))

    if verbose:
        print(f"\n{'='*65}")
        print(f"  超跌反弹 v3  |  区间: {start_date} ~ {end_date}")
        print(f"  基本面通过: {len(codes)} 只")
        print(f"  止损:{stop_loss*100:.0f}% 止盈:{take_profit_lo*100:.0f}~{take_profit_hi*100:.0f}% "
              f"持仓{MIN_HOLD_DAYS}~{max_hold_days}天 单股{SINGLE_POS_RATIO*100:.0f}% "
              f"仓位{position_ratio*100:.0f}%")
        print(f"{'='*65}")

    # ── 3. 批量加载价格数据 ─────────────────────────
    if verbose:
        print("\n[1/4] 批量加载价格数据...")
    price_data = preload_all_prices(codes, start_date, end_date)
    if not price_data:
        return {"error": "无价格数据"}

    # ── 4. 收集交易日 ───────────────────────────────
    all_dates = set()
    for sig_df in price_data.values():
        all_dates.update(pd.to_datetime(sig_df.index).date)
    trading_dates = sorted([
        d for d in all_dates
        if d >= date.fromisoformat(start_date) and d <= date.fromisoformat(end_date)
    ])
    if not trading_dates:
        return {"error": "无交易日"}

    # ── 5. 构建信号索引 ─────────────────────────────
    if verbose:
        print("\n[2/4] 计算买入信号（v3：跌幅+MA5+成交量+RSI+底部平台）...")
    sig_index = {}
    signal_count = 0

    for code in codes:
        df = price_data.get(code)
        if df is None or len(df) < 25:
            continue
        try:
            # 使用 v3 策略（参数化）
            sig_df = strategy_oversold_rebound(
                df,
                drop_threshold=drop_threshold,
                vol_20d_min=vol_20d_min,
                rsi_low=rsi_low,
                rsi_high=rsi_high,
                new_low_window=new_low_window,
                score_threshold=score_threshold,
            )
            sig_df = sig_df[sig_df.index >= pd.Timestamp(start_date)]
            for idx, row in sig_df.iterrows():
                d = pd.Timestamp(idx).date()
                if d not in sig_index:
                    sig_index[d] = {}
                sig_index[d][code] = row
                if row.get("BUY_SIGNAL"):
                    signal_count += 1
        except Exception:
            pass

    if verbose:
        print(f"      信号计算完成: 共 {signal_count} 个买入信号")

    # ── 6. 逐日回测 ────────────────────────────────
    if verbose:
        print("\n[3/4] 逐日回测...")
    capital = float(init_capital)
    positions = {}
    all_trades = []
    partial_exit_log = []
    daily_equity = []

    total_dates = len(trading_dates)
    progress_step = max(1, total_dates // 20)

    for di, cur_date in enumerate(trading_dates):
        if verbose and di % progress_step == 0:
            pos_value = sum(
                get_close_from_mem(code, cur_date, price_data) * pos["shares"]
                for code, pos in positions.items()
                if get_close_from_mem(code, cur_date, price_data) is not None
            )
            equity = capital + pos_value
            print(f"      进度 {di/total_dates*100:.0f}% ({di}/{total_dates}) "
                  f"资金:{capital:,.0f} 持仓:{len(positions)}只 equity:{equity:,.0f}")

        # ── 6a. 处理持仓卖出 ─────────────────────────
        for code in list(positions.keys()):
            pos = positions[code]
            shares = pos["shares"]
            entry_price = pos["entry_price"]
            entry_date = pos["entry_date"]
            entry_idx = pos["entry_idx"]
            half_sold = pos.get("half_sold", False)

            close_price = get_close_from_mem(code, cur_date, price_data)
            if close_price is None:
                continue

            ret = (close_price - entry_price) / entry_price
            hold_days = di - entry_idx
            can_sell = (cur_date > entry_date)

            lookback_df = get_series_from_mem(code, cur_date, price_data, lookback=30)

            ma5_today = None
            ma5_yesterday = None
            close_yesterday = None
            low_10d_yesterday = None
            vol_yesterday_vol = None
            vol_3d_yesterday = None
            vol_5d_yesterday = None
            vol_series = None
            close_series = None

            if lookback_df is not None and len(lookback_df) >= 6:
                close_series = lookback_df["close"]
                vol_series = lookback_df["volume"]
                ma5_all = close_series.rolling(5).mean()
                vol3_all = vol_series.rolling(3).mean()
                vol5_all = vol_series.rolling(5).mean()
                low10_all = close_series.rolling(10).min()

                ma5_today = ma5_all.iloc[-1] if not pd.isna(ma5_all.iloc[-1]) else None
                ma5_yesterday = ma5_all.iloc[-2] if len(ma5_all) >= 2 and not pd.isna(ma5_all.iloc[-2]) else None
                close_yesterday = close_series.iloc[-2] if len(close_series) >= 2 else None
                low_10d_yesterday = low10_all.iloc[-2] if len(low10_all) >= 2 and not pd.isna(low10_all.iloc[-2]) else None
                vol_yesterday_vol = vol_series.iloc[-2] if len(vol_series) >= 2 and not pd.isna(vol_series.iloc[-2]) else None
                vol_3d_yesterday = vol3_all.iloc[-2] if len(vol3_all) >= 2 and not pd.isna(vol3_all.iloc[-2]) else None
                vol_5d_yesterday = vol5_all.iloc[-2] if len(vol5_all) >= 2 and not pd.isna(vol5_all.iloc[-2]) else None

            exit_reason = None
            exit_price = close_price

            if can_sell and hold_days >= MIN_HOLD_DAYS:
                # ── A. 止损 -6% ──
                if ret <= stop_loss:
                    exit_reason = "止损"

                # ── B/C. 分批止盈 ──
                elif take_profit_lo <= ret <= take_profit_hi:
                    if not half_sold:
                        half_shares = shares // 2
                        if half_shares > 0:
                            comm = calc_commission(close_price, half_shares)
                            net = close_price * half_shares - comm
                            capital += net
                            positions[code]["half_sold"] = True
                            positions[code]["shares"] = shares - half_shares
                            partial_exit_log.append({
                                "code": code, "name": code_to_name.get(code, code),
                                "entry_date": entry_date.strftime("%Y-%m-%d"),
                                "exit_date": cur_date.strftime("%Y-%m-%d"),
                                "entry_price": round(entry_price, 2),
                                "exit_price": round(close_price, 2),
                                "shares": half_shares,
                                "pnl_pct": round(ret * 100, 2),
                                "pnl_amt": round(net - entry_price * half_shares, 2),
                                "commission": round(comm, 2),
                                "hold_days": hold_days,
                                "exit_reason": "止盈半仓",
                                "win": True,
                                "partial": True,
                            })

                # ── C. 跟踪止损（MA5）─
                if half_sold and exit_reason is None:
                    if ma5_today is not None and close_price < ma5_today:
                        exit_reason = "跟踪止盈"

                # ── D. 连续2日缩量阴跌破10日低点 ──
                if exit_reason is None and hold_days >= 3:
                    if (vol_yesterday_vol is not None and
                        vol_3d_yesterday is not None and
                        vol_5d_yesterday is not None and
                        close_yesterday is not None and
                        low_10d_yesterday is not None and
                        close_series is not None):
                        vol_shrink = vol_3d_yesterday < vol_5d_yesterday
                        price_down = close_yesterday < close_series.iloc[-3] if len(close_series) >= 3 else False
                        broke_low = close_yesterday < low_10d_yesterday
                        if vol_shrink and price_down and broke_low:
                            exit_reason = "缩量破底"

            # ── E. 到期强制平仓 ──
            if exit_reason is None and hold_days >= max_hold_days:
                exit_reason = "到期平仓"

            # ── 执行卖出 ──
            if exit_reason:
                remaining_shares = positions[code]["shares"]
                if remaining_shares > 0:
                    comm = calc_commission(exit_price, remaining_shares)
                    net = exit_price * remaining_shares - comm
                    pnl_amt = net - entry_price * remaining_shares
                    capital += net

                    all_trades.append({
                        "code": code, "name": code_to_name.get(code, code),
                        "entry_date": entry_date.strftime("%Y-%m-%d"),
                        "exit_date": cur_date.strftime("%Y-%m-%d"),
                        "entry_price": round(entry_price, 2),
                        "exit_price": round(exit_price, 2),
                        "shares": remaining_shares,
                        "pnl_pct": round(ret * 100, 2),
                        "pnl_amt": round(pnl_amt, 2),
                        "commission": round(comm, 2),
                        "hold_days": hold_days,
                        "exit_reason": exit_reason,
                        "win": ret > 0,
                        "partial": False,
                    })
                del positions[code]

        # ── 6b. 新买入 ───────────────────────────────
        if len(positions) < max_positions:
            available_cash = capital * position_ratio
            single_max = capital * SINGLE_POS_RATIO

            candidates = sig_index.get(cur_date, {})
            sorted_codes = sorted(
                candidates.keys(),
                key=lambda c: candidates[c].get("BUY_SCORE", 0),
                reverse=True
            )

            for code in sorted_codes:
                if len(positions) >= max_positions:
                    break
                if code in positions:
                    continue
                # 基本面二次确认（盘中突发利空）
                if code not in fund_filter:
                    continue

                close_price = get_close_from_mem(code, cur_date, price_data)
                if close_price is None or close_price <= 0:
                    continue

                budget = min(available_cash, single_max)
                shares = int(budget / close_price / 100) * 100
                if shares < 100:
                    continue

                cost = close_price * shares
                comm = calc_commission(close_price, shares)
                total_cost = cost + comm

                if total_cost > capital * position_ratio:
                    shares = int(capital * SINGLE_POS_RATIO / close_price / 100) * 100
                    if shares < 100:
                        continue
                    total_cost = close_price * shares + calc_commission(close_price, shares)
                    if total_cost > capital:
                        continue

                positions[code] = {
                    "shares": shares,
                    "entry_price": close_price,
                    "entry_date": cur_date,
                    "entry_idx": di,
                    "half_sold": False,
                }
                capital -= total_cost

        # ── 6c. 记录每日权益 ─────────────────────────
        pos_value = sum(
            get_close_from_mem(code, cur_date, price_data) * pos["shares"]
            for code, pos in positions.items()
            if get_close_from_mem(code, cur_date, price_data) is not None
        )
        daily_equity.append({
            "date": cur_date.strftime("%Y-%m-%d"),
            "equity": round(capital + pos_value, 0),
        })

    # ── 7. 期末清仓 ────────────────────────────────
    last_date = trading_dates[-1]
    for code, pos in list(positions.items()):
        close_price = get_close_from_mem(code, last_date, price_data)
        if close_price:
            shares = pos["shares"]
            comm = calc_commission(close_price, shares)
            net = close_price * shares - comm
            capital += net
            ret = (close_price - pos["entry_price"]) / pos["entry_price"]
            all_trades.append({
                "code": code, "name": code_to_name.get(code, code),
                "entry_date": pos["entry_date"].strftime("%Y-%m-%d"),
                "exit_date": last_date.strftime("%Y-%m-%d"),
                "entry_price": round(pos["entry_price"], 2),
                "exit_price": round(close_price, 2),
                "shares": shares,
                "pnl_pct": round(ret * 100, 2),
                "pnl_amt": round(net - pos["entry_price"] * shares, 2),
                "commission": round(comm, 2),
                "hold_days": 0,
                "exit_reason": "期末平仓",
                "win": close_price > pos["entry_price"],
                "partial": False,
            })
    positions.clear()

    # ── 8. 统计 ───────────────────────────────────
    wins   = [t for t in all_trades if t.get("win")]
    losses = [t for t in all_trades if not t.get("win")]
    wl = [t["pnl_pct"] for t in wins]
    ll = [t["pnl_pct"] for t in losses]
    avg_win  = np.mean(wl) if wl else 0.0
    avg_loss = np.mean(ll) if ll else 0.0
    pf = abs(sum(wl) / sum(ll)) if ll and sum(ll) != 0 else 0.0
    total_ret = (capital - INIT_CAPITAL) / INIT_CAPITAL * 100

    df_eq = pd.DataFrame(daily_equity).set_index("date")["equity"]
    peak = df_eq.cummax()
    drawdown = (df_eq - peak) / peak * 100
    max_dd = drawdown.min()

    if verbose:
        elapsed = time.time() - t0
        print(f"\n  回测完成 ({elapsed:.1f}s)")
        print(f"  总交易: {len(all_trades)} 笔  胜率: {len(wins)/len(all_trades)*100:.1f}%")
        print(f"  总收益: {total_ret:+.2f}%  最大回撤: {max_dd:.2f}%")
        print(f"  盈亏比: {pf:.2f}  均盈: {avg_win:+.2f}%  均亏: {avg_loss:+.2f}%")
        print(f"  部分止盈: {len(partial_exit_log)} 次")

    return {
        "total_trades": len(all_trades),
        "win_rate": round(len(wins)/len(all_trades)*100, 2) if all_trades else 0,
        "total_return": round(total_ret, 2),
        "max_drawdown": round(max_dd, 2),
        "profit_factor": round(pf, 3),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "avg_hold_days": round(np.mean([t["hold_days"] for t in all_trades]), 1) if all_trades else 0,
        "final_capital": round(capital, 0),
        "init_capital": INIT_CAPITAL,
        "wins": len(wins),
        "losses": len(losses),
        "partial_exits": len(partial_exit_log),
        "trades": all_trades,
        "elapsed": time.time() - t0,
    }


# ─────────────────────────────────────────────
# 参数扫描
# ─────────────────────────────────────────────
def param_scan(start_date: str, end_date: str):
    configs = [
        #  name                      sl    tp_lo  tp_hi
        ('v3基准 止损-6%止盈8-12%',  -0.06, 0.08,  0.12),
        ('v3激进 止损-5%止盈10-15%', -0.05, 0.10,  0.15),
        ('v3保守 止损-8%止盈6-10%',  -0.08, 0.06,  0.10),
        ('v3宽止盈 止损-6%止盈8-20%', -0.06, 0.08,  0.20),
        ('v3紧止损-4%止盈6-8%',     -0.04, 0.06,  0.08),
    ]

    print('='*72)
    print('超跌反弹 v3 参数扫描: %s ~ %s' % (start_date, end_date))
    print('='*72)
    print('%-25s %5s %6s %7s %9s %7s %6s %7s' % (
        '配置', '止损', '交易', '胜率', '总收益', '最大回撤', '盈亏比', '均盈%'))
    print('-'*72)

    results = []
    for name, sl, tplo, tphi in configs:
        r = run_oversold_v3(
            start_date=start_date, end_date=end_date,
            stop_loss=sl, take_profit_lo=tplo, take_profit_hi=tphi,
            verbose=False
        )
        print('%-25s %5.0f%% %6d %6.1f%% %8.2f%% %7.2f%% %6.2f %+6.2f%%' % (
            name, sl*100, r['total_trades'], r['win_rate'],
            r['total_return'], r['max_drawdown'],
            r['profit_factor'], r['avg_win_pct']))
        results.append((name, r))

    print('='*72)
    return results


if __name__ == '__main__':
    # 1年回测
    param_scan('2025-04-03', '2026-04-02')