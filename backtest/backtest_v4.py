"""
backtest_v4.py - 超跌反弹策略 v4
策略信号用 strategies.py 中久经验证的 engine，
只改动：止盈止损规则 + 仓位管理 + 大盘择时
"""
import sys
sys.path.insert(0, 'e:/小项目/jiaoyi')

import pandas as pd
import numpy as np
from datetime import datetime, date, timedelta
from typing import Optional
import time
import sqlite3

from core.db import get_all_stocks, get_conn
from strategy.strategies import strategy_oversold_rebound
import baostock as bs

# ─────────────────────────────────────────────
# 核心参数（v4 新默认值）
# ─────────────────────────────────────────────
INIT_CAPITAL     = 100_000
MAX_POSITIONS    = 4
MAX_HOLD_DAYS    = 8
MIN_HOLD_DAYS    = 3
STOP_LOSS        = -0.06    # 最优止损
POSITION_RATIO   = 0.80
SINGLE_POS_RATIO = 0.40    # 最优单股上限 40%
COMMISSION_RATE  = 0.0001
MIN_COMMISSION   = 5.0
EXCLUDE_CODES    = ("688", "301")

MIN_MKT_CAP = 60 * 1e8
MAX_MKT_CAP = 260 * 1e8
PROFIT_YEAR = 2025
PROFIT_QTR  = 4


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────
def _offset_date(date_str: str, days: int) -> str:
    return (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")

def calc_commission(price: float, shares: int) -> float:
    return max(price * shares * COMMISSION_RATE, MIN_COMMISSION)

def _pos_ratio(score: float) -> float:
    """信号分 1.8~4.0 → 仓位 20%~40%"""
    return 0.20 + (score - 1.8) / (4.0 - 1.8) * 0.20


# ─────────────────────────────────────────────
# 基本面过滤
# ─────────────────────────────────────────────
def build_fundamental_filter(trade_date: str) -> set:
    with get_conn() as conn:
        try:
            cached = {r[0] for r in conn.execute(
                "SELECT code FROM stock_universe_cache WHERE trade_date=? AND pass=1",
                (trade_date,)
            ).fetchall()}
        except:
            cached = set()
    if cached:
        print(f"  [基本面] 缓存: {len(cached)} 只")
        return cached

    print(f"  [基本面] 查询 baostock...")
    lg = bs.login()
    if lg.error_code != '0':
        return set()

    with get_conn() as conn:
        try:
            stocks_list = [(r[0], r[1]) for r in conn.execute(
                "SELECT code, name FROM stock_info WHERE is_active=1"
            ).fetchall()]
        except:
            stocks_list = []

    passed = []
    for idx, (code, name) in enumerate(stocks_list):
        if any(kw in name for kw in ['ST', '*ST', '退']):
            continue
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
            if not (net_profit and net_profit > 0 and total_share):
                continue
            mkt_est = total_share * 10
            if not (MIN_MKT_CAP <= mkt_est <= MAX_MKT_CAP):
                continue
            passed.append(code)
        except:
            continue

    bs.logout()
    print(f"  [基本面] {len(passed)}/{len(stocks_list)} 通过")

    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_universe_cache (
                trade_date TEXT, code TEXT, name TEXT, pass INTEGER,
                filter_reason TEXT, PRIMARY KEY (trade_date, code))
        """)
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
    warm = _offset_date(start_date, -60)
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT code, trade_date, open, high, low, close, volume, amount, pct_change, turnover
            FROM daily_price WHERE trade_date>=? AND trade_date<=?
            ORDER BY code, trade_date
        """, (warm, end_date)).fetchall()

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

    print(f"      价格加载: {len(result)} 只 × {warm}~{end_date}")
    return result


def load_index_ma(start_date: str, end_date: str) -> dict:
    """用 baostock 加载上证指数 MA5/MA20"""
    warm = _offset_date(start_date, -60)
    lg = bs.login()
    if lg.error_code != '0':
        return {}
    rs = bs.query_history_k_data_plus(
        "sh.000001", "date,close",
        start_date=warm, end_date=end_date,
        frequency='d', adjustflag='3'
    )
    if rs.error_code != '0':
        bs.logout()
        return {}
    data = []
    while rs.next():
        data.append(rs.get_row_data())
    bs.logout()
    if not data:
        return {}
    df = pd.DataFrame(data, columns=rs.fields)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df[df["close"] > 0].copy()
    df["date"] = pd.to_datetime(df["date"])
    df.set_index("date", inplace=True)
    df = df.sort_index()
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    return {
        idx.date(): {
            "close": float(row["close"]),
            "ma5": float(row["ma5"]) if not pd.isna(row["ma5"]) else None,
            "ma20": float(row["ma20"]) if not pd.isna(row["ma20"]) else None,
        }
        for idx, row in df.iterrows()
    }


def get_close(code: str, cur_date, price_data: dict) -> Optional[float]:
    df = price_data.get(code)
    if df is None:
        return None
    ts = pd.Timestamp(cur_date)
    return float(df.loc[ts, "close"]) if ts in df.index else None


def get_series(code: str, cur_date: date, price_data: dict, lookback: int) -> Optional[pd.DataFrame]:
    df = price_data.get(code)
    if df is None:
        return None
    ts = pd.Timestamp(cur_date)
    if ts not in df.index:
        return None
    pos = df.index.get_loc(ts)
    return df.iloc[max(0, pos-lookback+1):pos+1].copy()


# ─────────────────────────────────────────────
# 主回测
# ─────────────────────────────────────────────
def run_oversold_v4(
    start_date: str = "2025-04-03",
    end_date: str = "2026-04-02",
    init_capital: float = INIT_CAPITAL,
    position_ratio: float = POSITION_RATIO,
    max_positions: int = MAX_POSITIONS,
    max_hold_days: int = MAX_HOLD_DAYS,
    min_hold_days: int = MIN_HOLD_DAYS,
    stop_loss: float = STOP_LOSS,
    # 策略参数（透传给 strategy_oversold_rebound）
    drop_threshold: float = 0.12,
    vol_20d_min: float = 80_000_000,
    rsi_low: float = 35.0,
    rsi_high: float = 50.0,
    new_low_window: int = 5,
    score_threshold: float = 1.8,
    # v4 专属
    use_market_timing: bool = True,
    use_trailing_stop: bool = True,
    trailing_pct: float = 0.10,
    single_pos_ratio: float = 0.40,
    verbose: bool = True,
) -> dict:
    t0 = time.time()
    if end_date is None:
        end_date = date.today().strftime("%Y-%m-%d")

    fund_filter = build_fundamental_filter(end_date)

    stocks_df = get_all_stocks()
    stocks_df = stocks_df[~stocks_df["code"].str.startswith(EXCLUDE_CODES)]
    stocks_df = stocks_df[stocks_df["code"].isin(fund_filter)]
    codes = stocks_df["code"].tolist()
    code_to_name = dict(zip(stocks_df["code"], stocks_df["name"]))

    if verbose:
        print(f"\n{'='*65}")
        print(f"  超跌反弹 v4  |  {start_date} ~ {end_date}")
        print(f"  通过: {len(codes)} 只 | score>={score_threshold} sl={stop_loss*100:.0f}% "
              f"tr={trailing_pct*100:.0f}% pos={single_pos_ratio*100:.0f}% "
              f"择时={use_market_timing}")
        print(f"{'='*65}")

    # 价格
    if verbose:
        print("\n[1/4] 加载价格...")
    price_data = preload_all_prices(codes, start_date, end_date)
    if not price_data:
        return {"error": "no price data"}

    # 大盘
    if verbose:
        print("[2/4] 加载大盘...")
    index_data = load_index_ma(start_date, end_date)
    if verbose and index_data:
        print(f"      上证: {len(index_data)} 交易日")

    # 信号
    if verbose:
        print("[3/4] 计算买入信号...")
    sig_index = {}
    signal_count = 0
    for code in codes:
        df = price_data.get(code)
        if df is None or len(df) < 25:
            continue
        try:
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
        except:
            pass
    if verbose:
        print(f"      信号: {signal_count} 个")

    # 交易日
    all_dates = set()
    for df in price_data.values():
        all_dates.update(pd.to_datetime(df.index).date)
    trading_dates = sorted([
        d for d in all_dates
        if d >= date.fromisoformat(start_date) and d <= date.fromisoformat(end_date)
    ])
    if not trading_dates:
        return {"error": "no trading dates"}

    if verbose:
        print(f"[4/4] 逐日回测 ({len(trading_dates)} 个交易日)...")

    capital = float(init_capital)
    positions = {}
    all_trades = []
    partial_log = []
    daily_equity = []
    skip_stats = {"market": 0, "pos_full": 0}

    prog = max(1, len(trading_dates) // 20)

    for di, cur_date in enumerate(trading_dates):
        if verbose and di % prog == 0:
            pv = sum(
                get_close(code, cur_date, price_data) * p["shares"]
                for code, p in positions.items()
                if get_close(code, cur_date, price_data) is not None
            )
            eq = capital + pv
            print(f"      {di/len(trading_dates)*100:.0f}% | 资金:{capital:,.0f} 持仓:{len(positions)}只 equity:{eq:,.0f}")

        # 大盘择时
        market_ok = True
        if use_market_timing and index_data:
            mkt = index_data.get(cur_date)
            if mkt and mkt["ma5"] and mkt["ma20"]:
                market_ok = mkt["ma5"] > mkt["ma20"]

        # ── 持仓卖出 ─────────────────────────
        for code in list(positions.keys()):
            pos = positions[code]
            ep = pos["entry_price"]
            shares = pos["shares"]
            ei = pos["entry_idx"]
            half = pos.get("half_sold", False)
            high = pos.get("high_price", ep)

            cp = get_close(code, cur_date, price_data)
            if cp is None:
                continue

            ret = (cp - ep) / ep
            hold = di - ei
            can_sell = cur_date > pos["entry_date"]
            high = max(high, cp)

            lb = get_series(code, cur_date, price_data, 30)
            ma5_t = ma5_y = cl_y = lo10_y = v_y = v3_y = v5_y = None
            if lb is not None and len(lb) >= 6:
                cs = lb["close"]; vs = lb["volume"]
                m5a = cs.rolling(5).mean()
                v3a = vs.rolling(3).mean(); v5a = vs.rolling(5).mean()
                lo10a = cs.rolling(10).min()
                ma5_t = m5a.iloc[-1] if not pd.isna(m5a.iloc[-1]) else None
                ma5_y = m5a.iloc[-2] if len(m5a) >= 2 and not pd.isna(m5a.iloc[-2]) else None
                cl_y = cs.iloc[-2] if len(cs) >= 2 else None
                lo10_y = lo10a.iloc[-2] if len(lo10a) >= 2 and not pd.isna(lo10a.iloc[-2]) else None
                v_y = vs.iloc[-2] if len(vs) >= 2 and not pd.isna(vs.iloc[-2]) else None
                v3_y = v3a.iloc[-2] if len(v3a) >= 2 and not pd.isna(v3a.iloc[-2]) else None
                v5_y = v5a.iloc[-2] if len(v5a) >= 2 and not pd.isna(v5a.iloc[-2]) else None

            exit_reason = None
            exit_price = cp

            if can_sell and hold >= min_hold_days:
                # A. 止损
                if ret <= stop_loss:
                    exit_reason = "止损"
                # B. 全程跟踪止损（半仓后，从高点回落）
                elif use_trailing_stop and half:
                    if high > 0 and (high - cp) / high >= trailing_pct:
                        exit_reason = "跟踪止盈"
                # C. 首次止盈半仓（从高回落3%，未卖过半）
                elif use_trailing_stop and not half:
                    if high > 0 and (high - cp) / high >= 0.03:
                        hs = shares // 2
                        if hs > 0:
                            comm = calc_commission(cp, hs)
                            net = cp * hs - comm
                            capital += net
                            positions[code]["half_sold"] = True
                            positions[code]["shares"] = shares - hs
                            positions[code]["high_price"] = high
                            partial_log.append({
                                "code": code, "name": code_to_name.get(code, code),
                                "entry_date": pos["entry_date"].strftime("%Y-%m-%d"),
                                "exit_date": cur_date.strftime("%Y-%m-%d"),
                                "entry_price": round(ep, 2),
                                "exit_price": round(cp, 2),
                                "shares": hs,
                                "pnl_pct": round(ret * 100, 2),
                                "pnl_amt": round(net - ep * hs, 2),
                                "commission": round(comm, 2),
                                "hold_days": hold,
                                "exit_reason": "半仓止盈",
                                "win": True, "partial": True,
                            })
                # D. 缩量破底
                if exit_reason is None and hold >= 3:
                    if (v_y and v3_y and v5_y and cl_y and lo10_y and lb is not None):
                        if v3_y < v5_y and cl_y < (lb["close"].iloc[-3] if len(lb) >= 3 else cl_y) and cl_y < lo10_y:
                            exit_reason = "缩量破底"

            # E. 到期
            if exit_reason is None and hold >= max_hold_days:
                exit_reason = "到期平仓"

            if exit_reason:
                rem = positions[code]["shares"]
                if rem > 0:
                    comm = calc_commission(exit_price, rem)
                    net = exit_price * rem - comm
                    capital += net
                    all_trades.append({
                        "code": code, "name": code_to_name.get(code, code),
                        "entry_date": pos["entry_date"].strftime("%Y-%m-%d"),
                        "exit_date": cur_date.strftime("%Y-%m-%d"),
                        "entry_price": round(ep, 2),
                        "exit_price": round(exit_price, 2),
                        "shares": rem,
                        "pnl_pct": round(ret * 100, 2),
                        "pnl_amt": round(net - ep * rem, 2),
                        "commission": round(comm, 2),
                        "hold_days": hold,
                        "exit_reason": exit_reason,
                        "win": ret > 0,
                        "partial": False,
                    })
                del positions[code]

        # ── 新开仓 ─────────────────────────
        if len(positions) < max_positions and market_ok:
            candidates = sig_index.get(cur_date, {})
            # 过滤已有持仓 + score门槛
            valid = {
                c: row for c, row in candidates.items()
                if c not in positions
                and row.get("BUY_SCORE", 0) >= score_threshold
            }
            if valid:
                sorted_codes = sorted(valid.keys(), key=lambda c: valid[c].get("BUY_SCORE", 0), reverse=True)
                for code in sorted_codes:
                    if len(positions) >= max_positions:
                        skip_stats["pos_full"] += 1
                        break
                    if code not in fund_filter:
                        continue
                    row = valid[code]
                    cp = row.get("close")
                    if cp is None or cp <= 0:
                        continue

                    pos_r = min(single_pos_ratio, _pos_ratio(row.get("BUY_SCORE", 1.8)))
                    budget = min(capital * position_ratio, capital * pos_r)
                    shares = int(budget / cp / 100) * 100
                    if shares < 100:
                        continue
                    cost = cp * shares
                    comm = calc_commission(cp, shares)
                    total_cost = cost + comm
                    if total_cost > capital:
                        shares = int(capital * pos_r / cp / 100) * 100
                        if shares < 100:
                            continue
                        total_cost = cp * shares + calc_commission(cp, shares)
                        if total_cost > capital:
                            continue

                    positions[code] = {
                        "shares": shares,
                        "entry_price": cp,
                        "entry_date": cur_date,
                        "entry_idx": di,
                        "high_price": cp,
                        "half_sold": False,
                    }
                    capital -= total_cost
        elif not market_ok:
            skip_stats["market"] += 1

        # 更新最高价
        for code, pos in positions.items():
            cp = get_close(code, cur_date, price_data)
            if cp is not None:
                positions[code]["high_price"] = max(pos.get("high_price", cp), cp)

        # 每日权益
        pv = sum(
            get_close(code, cur_date, price_data) * p["shares"]
            for code, p in positions.items()
            if get_close(code, cur_date, price_data) is not None
        )
        daily_equity.append({"date": cur_date.strftime("%Y-%m-%d"), "equity": round(capital + pv, 0)})

    # 清仓
    last = trading_dates[-1]
    for code, pos in list(positions.items()):
        cp = get_close(code, last, price_data)
        if cp:
            sh = pos["shares"]
            comm = calc_commission(cp, sh)
            net = cp * sh - comm
            capital += net
            ret = (cp - pos["entry_price"]) / pos["entry_price"]
            all_trades.append({
                "code": code, "name": code_to_name.get(code, code),
                "entry_date": pos["entry_date"].strftime("%Y-%m-%d"),
                "exit_date": last.strftime("%Y-%m-%d"),
                "entry_price": round(pos["entry_price"], 2),
                "exit_price": round(cp, 2),
                "shares": sh,
                "pnl_pct": round(ret * 100, 2),
                "pnl_amt": round(net - pos["entry_price"] * sh, 2),
                "commission": round(comm, 2),
                "hold_days": 0,
                "exit_reason": "期末清仓",
                "win": cp > pos["entry_price"],
                "partial": False,
            })
    positions.clear()

    # 统计
    wins = [t for t in all_trades if t.get("win")]
    losses = [t for t in all_trades if not t.get("win")]
    wl = [t["pnl_pct"] for t in wins]
    ll = [t["pnl_pct"] for t in losses]
    avg_win = np.mean(wl) if wl else 0.0
    avg_loss = np.mean(ll) if ll else 0.0
    pf = abs(sum(wl) / sum(ll)) if ll and sum(ll) != 0 else 0.0

    df_eq = pd.DataFrame(daily_equity).set_index("date")["equity"]
    peak = df_eq.cummax()
    dd = (df_eq - peak) / peak * 100
    max_dd = dd.min()
    n_yrs = max((date.fromisoformat(end_date) - date.fromisoformat(start_date)).days / 365.0, 0.01)
    ann_ret = (df_eq.iloc[-1] / INIT_CAPITAL - 1) / n_yrs * 100

    if verbose:
        elapsed = time.time() - t0
        print(f"\n  完成 ({elapsed:.1f}s)")
        print(f"  总交易: {len(all_trades)} 笔  胜率: {len(wins)/len(all_trades)*100:.1f}%")
        print(f"  总收益: {(df_eq.iloc[-1]/INIT_CAPITAL-1)*100:+.2f}%  年化: {ann_ret:+.2f}%")
        print(f"  最大回撤: {max_dd:.2f}%  盈亏比: {pf:.2f}  均盈: {avg_win:+.2f}%  均亏: {avg_loss:+.2f}%")
        print(f"  半仓止盈: {len(partial_log)} 次  大盘空头跳过: {skip_stats['market']} 次")

    return {
        "total_trades": len(all_trades),
        "win_rate": round(len(wins)/len(all_trades)*100, 2) if all_trades else 0,
        "total_return": round((df_eq.iloc[-1]/INIT_CAPITAL-1)*100, 2),
        "annual_return": round(ann_ret, 2),
        "max_drawdown": round(max_dd, 2),
        "profit_factor": round(pf, 3),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "avg_hold_days": round(np.mean([t["hold_days"] for t in all_trades]), 1) if all_trades else 0,
        "final_capital": round(df_eq.iloc[-1], 0),
        "init_capital": INIT_CAPITAL,
        "wins": len(wins), "losses": len(losses),
        "partial_exits": len(partial_log),
        "trades": all_trades,
        "elapsed": time.time() - t0,
        "skip_stats": skip_stats,
    }


# ─────────────────────────────────────────────
# 参数扫描
# ─────────────────────────────────────────────
def grid_scan(start_date: str, end_date: str):
    from backtest.backtest_v3 import run_oversold_v3

    configs = [
        # name, func, params
        ("v3基准(固定止盈)",        "v3", dict(stop_loss=-0.06, take_profit_lo=0.08, take_profit_hi=0.12, score_threshold=1.8)),
        ("v4 tr10% pos30% sl=-5%",  "v4", dict(score_threshold=1.8, stop_loss=-0.05, trailing_pct=0.10, single_pos_ratio=0.30)),
        ("v4 tr10% pos40% sl=-5%",  "v4", dict(score_threshold=1.8, stop_loss=-0.05, trailing_pct=0.10, single_pos_ratio=0.40)),
        ("v4 tr12% pos40% sl=-5%",  "v4", dict(score_threshold=1.8, stop_loss=-0.05, trailing_pct=0.12, single_pos_ratio=0.40)),
        ("v4 tr15% pos40% sl=-5%",  "v4", dict(score_threshold=1.8, stop_loss=-0.05, trailing_pct=0.15, single_pos_ratio=0.40)),
        ("v4 tr10% pos50% sl=-5%",  "v4", dict(score_threshold=1.8, stop_loss=-0.05, trailing_pct=0.10, single_pos_ratio=0.50)),
        ("v4 tr10% pos50% 无择时",  "v4", dict(score_threshold=1.8, stop_loss=-0.05, trailing_pct=0.10, single_pos_ratio=0.50, use_market_timing=False)),
        ("v4 tr12% pos50% sl=-6%",  "v4", dict(score_threshold=1.8, stop_loss=-0.06, trailing_pct=0.12, single_pos_ratio=0.50)),
        ("v4 score2.0 tr10% pos40%", "v4", dict(score_threshold=2.0, stop_loss=-0.05, trailing_pct=0.10, single_pos_ratio=0.40)),
        ("v4 score2.5 tr10% pos40%", "v4", dict(score_threshold=2.5, stop_loss=-0.05, trailing_pct=0.10, single_pos_ratio=0.40)),
    ]

    print(f"\n{'='*78}")
    print(f"  v3 vs v4 参数扫描  |  {start_date} ~ {end_date}")
    print(f"{'='*78}")
    print(f"  {'配置':<28} {'交易':>5} {'胜率':>6} {'总收益':>8} {'年化':>8} {'最大回撤':>9} {'盈亏比':>7} {'均盈':>7} {'均亏':>8}")
    print(f"  {'-'*78}")
    results = []
    for label, fk, params in configs:
        if fk == "v3":
            r = run_oversold_v3(start_date=start_date, end_date=end_date, verbose=False, **params)
        else:
            r = run_oversold_v4(start_date=start_date, end_date=end_date, verbose=False, **params)
        ann = r.get("annual_return", r["total_return"])
        print(f"  {label:<28} {r['total_trades']:>5} {r['win_rate']:>6.1f}% {r['total_return']:>+8.2f}% {ann:>+8.2f}% {r['max_drawdown']:>9.2f}% {r['profit_factor']:>7.2f} {r['avg_win_pct']:>+7.2f}% {r['avg_loss_pct']:>+8.2f}%")
        results.append((label, r))
    print(f"{'='*78}")
    return results


if __name__ == '__main__':
    grid_scan('2025-04-03', '2026-04-02')
