"""
quant.py  ——  量化交易核心库（内部模块）
========================================
本文件不再作为独立程序运行，所有功能已整合到 app.py：

  - 定时调度（07:00 同步 / 09:00 扫描）→ app.py 内置 schedule 线程
  - 手动触发数据同步               → POST /api/system/sync
  - 手动触发策略扫描               → POST /api/system/scan
  - 历史评分补算                  → GET  /api/sync/recalc_all_scores

用量方式（仅供 app.py import 调用）：
  import quant
  quant.scan_job(force_recalc=False, use_v4=True)
"""

import pandas as pd
import time
import schedule
import threading
import math
from datetime import datetime, date, timedelta
import requests

from core.db import init_db, get_all_stocks, get_daily_price, save_signals, save_scan_signals, db_stats, get_stock_count_in_db, get_conn, get_index_daily
from core.db import get_north_money
from core.sync import daily_sync, initial_sync, update_stock_list, sync_strategy_score
from strategy.strategies import (
    strategy_volume_breakout,
    strategy_ma_convergence,
    strategy_price_volume_divergence,
    strategy_bottom_fishing,
    strategy_whale_accumulation,
    strategy_oversold_rebound,
    fuse_signals,
    DEFAULT_WEIGHTS,
)

# v4 超跌反弹阈值（原生 0~4 分，需 >= 1.8 才触发）
V4_SCORE_THRESHOLD = 1.8

# ======================
# 策略参数（2026-04-02 回测优化验证后的最佳参数）
# ======================
START_CAPITAL      = 10000
POSITION_NUM       = 3
POSITION_PER_STOCK = 0.5   # 单股最大仓位50%（资金小，100股起买，30%仅够买33元以下个股）
STOP_LOSS          = -0.06   # 止损-6%（原-3%太窄，跳空低开后频繁触发）
TAKE_PROFIT        = 0.20   # 止盈+20%（原+7%太短，持仓期内无法兑现）
# 5策略融合阈值（总分0-50，阈值28过滤前5%优质股，回测验证最优）
FUSION_THRESHOLD   = 28.0    # 原20.0
# 历史回填 stock_signal 的融合分门槛（低于回测阈值，以便面板显示更丰富）
SIG_BACKFILL_THRESHOLD = 15.0

# ======================
# 微信机器人 KEY（必填！）
# ======================
WX_PUSH_KEY = "把你的微信机器人key填在这里"

# 扫描截止时间：15:00 前完成才推送微信
SCAN_DEADLINE_HOUR   = 15
SCAN_DEADLINE_MINUTE = 0


# ─────────────────────────────────────────────
# 微信推送
# ─────────────────────────────────────────────

def send_wechat(msg: str):
    """发送企业微信群机器人消息"""
    try:
        url  = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WX_PUSH_KEY}"
        data = {"msgtype": "text", "text": {"content": msg}}
        resp = requests.post(url, json=data, timeout=10)
        if resp.status_code == 200:
            print(f"[{_now()}] 微信推送成功")
        else:
            print(f"[{_now()}] 微信推送失败，状态码: {resp.status_code}")
    except Exception as e:
        print(f"[{_now()}] 微信推送异常: {e}")


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _is_before_deadline() -> bool:
    """当前时间是否在 15:00 之前"""
    now = datetime.now()
    return (now.hour, now.minute) < (SCAN_DEADLINE_HOUR, SCAN_DEADLINE_MINUTE)


# ─────────────────────────────────────────────
# 辅助函数
# ─────────────────────────────────────────────
def _build_trigger_list(s1: float, s2: float, s3: float, s4: float, s5: float) -> list[str]:
    """各策略得分 >= 2.0 视为触发，返回触发策略名列表"""
    names = ["放量突破", "均线粘合", "量价背离", "抄底", "主力建仓"]
    scores = [s1, s2, s3, s4, s5]
    return [n for n, sc in zip(names, scores) if sc >= 2.0]


# ─────────────────────────────────────────────
# 策略分析（基于数据库数据）
# ─────────────────────────────────────────────

def analyze_stock_from_db(code: str, name: str = "", min_score: float = None) -> dict | None:
    """
    从数据库读取完整历史行情，运行5个真实量化策略，
    返回信号或 None（综合分 < min_score 时返回 None）

    5策略（各0-3分 → 各0-10分）：
      1. 放量突破  2. 均线粘合  3. 量价背离  4. 抄底  5. 主力建仓
    综合分 = 5策略加权平均（等权），满分50

    """
    if min_score is None:
        min_score = FUSION_THRESHOLD

    try:
        df = get_daily_price(code)
        if df.empty or len(df) < 30:
            return None

        # ── 运行5个策略 ────────────────────────────
        s1 = strategy_volume_breakout(df)
        s2 = strategy_ma_convergence(df)
        s3 = strategy_price_volume_divergence(df)
        s4 = strategy_bottom_fishing(df)
        s5 = strategy_whale_accumulation(df)

        # 先取出各策略当日原始分（0-3）
        def last_score(s_df):
            v = float(s_df.iloc[-1]["BUY_SCORE"])
            return round(min(10.0, v / 3.0 * 10.0), 1) if not pd.isna(v) else 0.0

        s1_sc = last_score(s1)
        s2_sc = last_score(s2)
        s3_sc = last_score(s3)
        s4_sc = last_score(s4)
        s5_sc = last_score(s5)

        # 融合（加权融合，fuse_signals 直接返回总分 0-50）
        fused = fuse_signals([s1, s2, s3, s4, s5], weights=DEFAULT_WEIGHTS,
)
        last = fused.iloc[-1]
        fusion_score = round(float(last.get("FUSION_SCORE", 0)), 2)
        # 过滤（阈值 min_score）
        if fusion_score < min_score:
            return None

        price      = round(float(last["close"]), 2)
        trade_date = str(df.index[-1].date()) if hasattr(df.index[-1], "date") else str(df.index[-1])[:10]
        buy_money  = int(START_CAPITAL * POSITION_PER_STOCK)
        buy_volume = int(buy_money // (price * 100) * 100)

        return {
            "code":        code,
            "name":        name,
            "price":       price,
            "trade_date":  trade_date,
            "score":       round(fusion_score, 2),         # 0-50 综合分
            "raw_score":   round(fusion_score, 2),         # 融合分
            "buy_money":   buy_money,
            "buy_volume":  buy_volume,
            "stop_loss":   round(price * (1 + STOP_LOSS), 2),
            "take_profit": round(price * (1 + TAKE_PROFIT), 2),
            # 各策略0-10分（用于展示）
            "s1_vol_break": s1_sc,   # 放量突破
            "s2_ma_conv":   s2_sc,   # 均线粘合
            "s3_pv_div":    s3_sc,   # 量价背离
            "s4_bottom":    s4_sc,   # 抄底
            "s5_whale":     s5_sc,   # 主力建仓
            # 触发策略列表（各策略得分 >= 2.0 视为触发）
            "trigger_list": _build_trigger_list(s1_sc, s2_sc, s3_sc, s4_sc, s5_sc),
        }
    except Exception:
        return None


# ─────────────────────────────────────────────
# v4 超跌反弹策略分析
# ─────────────────────────────────────────────
def _check_market_timing_v4(trade_date: str) -> bool:
    """
    检查大盘择时（MA5 > MA20 = 多头市场）。
    与 backtest_v4.py 中 use_market_timing=True 逻辑一致。
    使用沪深300指数(000300)的 index_daily 数据判断。
    """
    try:
        # 往前多加载60天以保证MA20有足够数据
        warm = (datetime.strptime(trade_date, "%Y-%m-%d") -
                timedelta(days=60)).strftime("%Y-%m-%d")
        idx_df = get_index_daily("000300", start_date=warm, end_date=trade_date)
        if idx_df is None or idx_df.empty or "close" not in idx_df.columns:
            return True   # 无指数数据时，放行（避免误杀）
        idx_df = idx_df.sort_index()
        close = idx_df["close"]
        ma5 = close.rolling(5).mean()
        ma20 = close.rolling(20).mean()
        if ma5.empty or ma20.empty:
            return True
        # 取最新非NA值
        last_ma5 = ma5.dropna().iloc[-1] if not ma5.dropna().empty else None
        last_ma20 = ma20.dropna().iloc[-1] if not ma20.dropna().empty else None
        if last_ma5 is None or last_ma20 is None:
            return True
        return bool(last_ma5 > last_ma20)
    except Exception:
        return True   # 出错时放行


def analyze_stock_v4(code: str, name: str = "",
                     min_score: float = None) -> dict | None:
    """
    基于 v4 超跌反弹策略（strategy_oversold_rebound）分析股票。
    买入条件（全满足才触发）：
      1. 近20日跌幅 >= 12%
      2. 收盘价 > MA5
      3. 近20日日均成交额 >= 8000万元
      4. 3日均量 > 5日均量（温和放量）
      5. RSI(14) 在 35~50 区间
      6. 股价不创5日新低
      7. 大盘择时：MA5 > MA20（多头市场才推荐）

    返回：
      - v4_score: 连续打分（0~4），需 >= 1.8 才触发
      - v4_score_scaled: 映射到 0~50（与现有融合分体系一致）
      - 各条件独立打分（供前端展示）
    """
    if min_score is None:
        min_score = V4_SCORE_THRESHOLD

    try:
        df = get_daily_price(code)
        if df.empty or len(df) < 30:
            return None

        # ── 运行 v4 超跌反弹策略 ─────────────────────────────
        s = strategy_oversold_rebound(df)

        last = s.iloc[-1]
        v4_score = float(last.get("BUY_SCORE", 0))
        buy_signal = bool(last.get("BUY_SIGNAL", False))

        # v4 连续分（0~4）映射到展示分（0~50）
        v4_score_scaled = round(v4_score / 4.0 * 50.0, 2)

        if not buy_signal and v4_score < min_score:
            return None

        price = round(float(last["close"]), 2)
        trade_date = str(df.index[-1].date()) if hasattr(df.index[-1], "date") else str(df.index[-1])[:10]

        # ── 大盘择时判断（与 backtest_v4.py 一致）────────────────
        if not _check_market_timing_v4(trade_date):
            return None   # 空头市场，不推荐开仓

        # 各条件独立得分（0~10 分制，供前端展示）
        # v4 的 buy_score 是 4 个子项各 0~1 的总和（0~4分）
        # 我们把各子项分数拆出来（从 BUY_SCORE 的构成反推）
        # 但 v4 没有独立列存储子项分数，只能显示总评分
        drop_thr = -0.12
        vol_20d_min = 80_000_000
        rsi_low, rsi_high = 35.0, 50.0

        close = df["close"]
        amount = df.get("amount", df["volume"] * df["close"])
        volume = df["volume"]
        ma5 = close.rolling(5).mean()
        vol3 = volume.rolling(3).mean()
        vol5 = volume.rolling(5).mean()
        amt20 = amount.rolling(20).mean()
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.clip(lower=1e-9)
        rsi = 100 - (100 / (1 + rs))
        ret_20d = close / close.shift(20).clip(lower=1e-9) - 1
        score_drop = ((-ret_20d) / abs(drop_thr)).clip(lower=0, upper=1.0)
        ma5_strength = ((close - ma5) / ma5.clip(lower=1e-9)).clip(lower=0)
        score_ma5 = (ma5_strength / 0.02).clip(lower=0, upper=1.0)
        vol_ratio = vol3 / vol5.clip(lower=1e-9)
        score_vol = ((vol_ratio - 1) / 0.5).clip(lower=0, upper=1.0)
        score_rsi_s = ((rsi - rsi_low) / (rsi_high - rsi_low)).clip(lower=0, upper=1.0)

        row = s.iloc[-1]
        s_drop = float(score_drop.iloc[-1]) if not pd.isna(float(score_drop.iloc[-1])) else 0
        s_ma5 = float(score_ma5.iloc[-1]) if not pd.isna(float(score_ma5.iloc[-1])) else 0
        s_vol = float(score_vol.iloc[-1]) if not pd.isna(float(score_vol.iloc[-1])) else 0
        s_rsi = float(score_rsi_s.iloc[-1]) if not pd.isna(float(score_rsi_s.iloc[-1])) else 0

        buy_money = int(START_CAPITAL * POSITION_PER_STOCK)
        buy_volume = int(buy_money // (price * 100) * 100)

        return {
            "code": code,
            "name": name,
            "price": price,
            "trade_date": trade_date,
            "score": v4_score_scaled,       # 映射到 0~50
            "raw_score": round(v4_score, 2),  # v4 原生 0~4 分
            "buy_money": buy_money,
            "buy_volume": buy_volume,
            "stop_loss": round(price * (1 + STOP_LOSS), 2),
            "take_profit": round(price * (1 + TAKE_PROFIT), 2),
            # v4 条件得分（0~10，展示用）
            "s1_vol_break": round(s_vol * 10, 1),   # 放量
            "s2_ma_conv": round(s_ma5 * 10, 1),    # 站上MA5
            "s3_pv_div": round(s_drop * 10, 1),     # 下跌深度
            "s4_bottom": round(s_rsi * 10, 1),      # RSI区间
            # 不符合 v4 结构意义的填 0
            "s5_whale": 0.0,
            # 触发策略统一显示为"超跌反弹"
            "trigger_list": ["超跌反弹"] if buy_signal else [],
        }
    except Exception:
        return None


# ─────────────────────────────────────────────
# 全市场扫描
# ─────────────────────────────────────────────

def scan_all_stocks(force_recalc: bool = False,
                   use_v4: bool = False) -> list[dict]:
    """
    扫描数据库中所有股票，返回按评分排序的前 N 只
    :param force_recalc: True 则先对所有股票强制重算策略分，再筛选
    :param use_v4: True 则使用 v4 超跌反弹策略，False 则使用5策略融合
    """
    stocks_df = get_all_stocks()
    if stocks_df.empty:
        print(f"[{_now()}] 数据库股票列表为空，请先运行数据同步")
        return []

    # 排除科创板（688开头）和创业板（301开头），用户无权限
    stocks_df = stocks_df[
        ~stocks_df["code"].str.startswith("688") &
        ~stocks_df["code"].str.startswith("301")
    ]

    total = len(stocks_df)

    # ── 强制重算策略分 ──────────────────────────────
    if force_recalc:
        print(f"[{_now()}] 开始强制重算策略分（全市场 {total} 只）...")
        for i, row in stocks_df.iterrows():
            ok = sync_strategy_score(row["code"], verbose=False)
            if (i + 1) % 500 == 0:
                print(f"  重算进度: {i + 1}/{total}")
        print(f"[{_now()}] 策略分重算完成")

    strategy_label = "v4超跌反弹" if use_v4 else "5策略融合"
    print(f"[{_now()}] 开始策略扫描（{strategy_label}）...")

    hits = []
    for i, row in stocks_df.iterrows():
        if use_v4:
            item = analyze_stock_v4(row["code"], row["name"])
        else:
            item = analyze_stock_from_db(row["code"], row["name"])
        if item:
            hits.append(item)

        if (i + 1) % 500 == 0:
            print(f"  扫描进度: {i + 1}/{total}，已命中: {len(hits)}")

    # 返回给外部（微信推送/实盘）：top 3；全部命中给 stock_signal 保存
    top = sorted(hits, key=lambda x: x["score"], reverse=True)[:POSITION_NUM]
    all_hits = sorted(hits, key=lambda x: x["score"], reverse=True)
    print(f"[{_now()}] 扫描完成，命中 {len(hits)} 只，精选 {len(top)} 只")
    return top, all_hits


# ─────────────────────────────────────────────
# 策略扫描任务（每天 14:15）
# ─────────────────────────────────────────────

def _backfill_stock_signal_progress(name_width=0):
    """
    扫描完全部股票后，把历史所有融合分>=SIG_BACKFILL_THRESHOLD的日期
    写入 stock_signal 表（INSERT OR REPLACE，幂等）。
    用于历史推荐面板数据对比。
    """
    import json as _json
    stocks_df = get_all_stocks()
    if stocks_df.empty:
        return 0
    stocks = list(zip(stocks_df["code"], stocks_df["name"]))
    records = []
    total = len(stocks)
    for idx, (code, name) in enumerate(stocks):
        if (idx + 1) % 200 == 0:
            print(f"  stock_signal回填进度: {idx+1}/{total}")
        try:
            df = get_daily_price(code)
            if df is None or len(df) < 30:
                continue
            s1 = strategy_volume_breakout(df)
            s2 = strategy_ma_convergence(df)
            s3 = strategy_price_volume_divergence(df)
            s4 = strategy_bottom_fishing(df)
            s5 = strategy_whale_accumulation(df)
            fused = fuse_signals([s1, s2, s3, s4, s5], weights=DEFAULT_WEIGHTS)
            for _, row in fused.iterrows():
                fs = float(row.get("FUSION_SCORE", 0) or 0)
                if fs < SIG_BACKFILL_THRESHOLD:
                    continue
                trade_date = str(row.name.date()) if hasattr(row.name, "date") else str(row.name)[:10]
                price = round(float(row["close"]), 2)
                buy_money = int(START_CAPITAL * POSITION_PER_STOCK)
                buy_volume = int(buy_money // (price * 100) * 100)
                stop_loss = round(price * (1 + STOP_LOSS), 2)
                take_profit = round(price * (1 + TAKE_PROFIT), 2)
                trigger_list = []
                if float(row.get("VOL_SCORE", 0) or 0) >= 2.0:
                    trigger_list.append("放量突破")
                if float(row.get("MA_SCORE", 0) or 0) >= 2.0:
                    trigger_list.append("均线粘合")
                if float(row.get("DIVERGE_SCORE", 0) or 0) >= 2.0:
                    trigger_list.append("量价背离")
                if float(row.get("BOTTOM_SCORE", 0) or 0) >= 2.0:
                    trigger_list.append("抄底")
                if float(row.get("WHALE_SCORE", 0) or 0) >= 2.0:
                    trigger_list.append("主力建仓")
                records.append({
                    "scan_date":     trade_date,
                    "trade_date":    trade_date,
                    "code":          code,
                    "name":          name or code,
                    "price":         price,
                    "fusion_score":  round(fs, 2),
                    "vol_score":     round(float(row.get("VOL_SCORE", 0) or 0), 1),
                    "ma_score":      round(float(row.get("MA_SCORE", 0) or 0), 1),
                    "diverge_score": round(float(row.get("DIVERGE_SCORE", 0) or 0), 1),
                    "bottom_score":  round(float(row.get("BOTTOM_SCORE", 0) or 0), 1),
                    "whale_score":   round(float(row.get("WHALE_SCORE", 0) or 0), 1),
                    "trigger_list":  _json.dumps(trigger_list, ensure_ascii=False),
                    "buy_price":     price,
                    "stop_loss":     stop_loss,
                    "take_profit":   take_profit,
                    "buy_volume":    buy_volume,
                    "buy_money":     buy_money,
                    "sent_wechat":   0,
                    "created_at":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })
        except Exception as e:
            print(f"  [{code}] backfill失败: {e}")

    if records:
        with get_conn() as conn:
            conn.executemany("""
                INSERT OR REPLACE INTO stock_signal
                  (scan_date, trade_date, code, name, price, fusion_score,
                   vol_score, ma_score, diverge_score, bottom_score, whale_score,
                   trigger_list, buy_price, stop_loss, take_profit,
                   buy_volume, buy_money, sent_wechat, created_at)
                VALUES
                  (:scan_date, :trade_date, :code, :name, :price, :fusion_score,
                   :vol_score, :ma_score, :diverge_score, :bottom_score, :whale_score,
                   :trigger_list, :buy_price, :stop_loss, :take_profit,
                   :buy_volume, :buy_money, :sent_wechat, :created_at)
            """, records)
    print(f"  stock_signal 回填完成: 共 {len(records)} 条推荐记录")
    return len(records)


def scan_job(force_recalc: bool = False, use_v4: bool = True):
    """
    每日 14:15 执行策略扫描
    - 15:00 前完成 → 推送微信 + 本地保存
    - 15:00 后完成 → 仅本地保存，不推送
    :param force_recalc: True 则先对全市场强制重算策略分再筛选
    :param use_v4: True 则使用 v4 超跌反弹策略，False 则使用5策略融合
    """
    print(f"\n{'='*50}")
    strategy_name = "v4超跌反弹" if use_v4 else "5策略融合"
    print(f"[{_now()}] 策略扫描任务启动（{date.today()}）  force_recalc={force_recalc}  策略={strategy_name}")
    print(f"{'='*50}")

    # 1. 先重算全市场策略分（仅 force_recalc=True 时）
    if force_recalc:
        print("[Force Recalc] 开始重算全市场策略分...")
        stocks_df = get_all_stocks()
        if not stocks_df.empty:
            ok_cnt = fail_cnt = 0
            for idx, (code, name) in enumerate(zip(stocks_df["code"], stocks_df["name"])):
                if (idx + 1) % 200 == 0:
                    print(f"  进度: {idx+1}/{len(stocks_df)}  成功:{ok_cnt}  失败:{fail_cnt}")
                ok = sync_strategy_score(code, verbose=False)
                if ok:
                    ok_cnt += 1
                else:
                    fail_cnt += 1
            print(f"  重算完成: {ok_cnt} 成功 / {fail_cnt} 失败")
        print("[Force Recalc] 重算完成，开始筛选...")

    # top 3 用于微信推送/实盘；全部命中用于 stock_signal 面板
    top_picks, all_hits = scan_all_stocks(force_recalc=False, use_v4=use_v4)
    buy_list = top_picks
    dt = date.today().strftime("%Y-%m-%d")

    # ── stock_signal 面板数据：保存所有命中股 ──────────────────────
    if all_hits:
        sig_limit = 200  # 每策略每天最多保存200条
        save_scan_signals(all_hits[:sig_limit], sent_wechat=False)
        print(f"[{_now()}] stock_signal 面板数据已保存: {len(all_hits[:sig_limit])} 条")

    # 判断是否在截止时间前完成
    should_push = _is_before_deadline()

    if not buy_list:
        msg = f"A股小波段扫描【{dt}】（{strategy_name}）\n\n"
        msg += "今日无符合条件股票，空仓观望\n"
        if use_v4:
            msg += "v4策略：超跌反弹（近20日跌幅≥12% + 站上MA5 + 温和放量 + RSI35~50）\n"
            msg += f"信号分阈值>={V4_SCORE_THRESHOLD}/4触发"
        else:
            msg += "5策略：放量突破·均线粘合·量价背离·抄底·主力建仓\n"
            msg += f"融合阈值>={FUSION_THRESHOLD}/50触发"
        save_signals([], sent_wechat=False)
        if should_push:
            send_wechat(msg)
        print(msg)
        return

    # 构建消息
    msg = f"A股小波段【{dt}】交易计划（{strategy_name}）\n\n"

    # force_recalc 模式下：回填历史推荐到 stock_signal
    if force_recalc:
        print("[Force Recalc] 回填历史推荐到 stock_signal...")
        sig_count = _backfill_stock_signal_progress()
        print(f"[Force Recalc] stock_signal 回填完成: {sig_count} 条")

    save_scan_signals(buy_list, sent_wechat=should_push)

    if use_v4:
        # ── v4 超跌反弹消息 ───────────────────────────────
        for i, d in enumerate(buy_list):
            def si(v):
                if v is None: return 0.0
                if isinstance(v, float) and math.isnan(v): return 0.0
                return round(float(v), 1)
            raw = d.get('raw_score', 0)
            s_vol = si(d.get('s1_vol_break'))
            s_ma5 = si(d.get('s2_ma_conv'))
            s_drop = si(d.get('s3_pv_div'))
            s_rsi = si(d.get('s4_bottom'))
            cond = f"放量{s_vol}·MA5{s_ma5}·跌幅{s_drop}·RSI{s_rsi}"
            msg += f"{i+1}. {d['code']} {d['name']}  [{cond}  v4评分{raw}/4]\n"
            msg += f"   现价: {d['price']} 元\n"
            msg += f"   买入: {d['buy_money']} 元 | {int(d['buy_volume'])} 股\n"
            msg += f"   止损: {d['stop_loss']} | 止盈: {d['take_profit']}\n\n"
        msg += "v4策略：超跌反弹（近20日跌幅≥12% + 站上MA5 + 温和放量 + RSI35~50）\n"
        msg += f"信号分阈值>={V4_SCORE_THRESHOLD}/4触发\n"
        msg += f"规则: 持股3-8天 | {int(STOP_LOSS*100)}%止损 | +{int(TAKE_PROFIT*100)}%止盈\n"
    else:
        # ── 5策略融合消息 ───────────────────────────────
        for i, d in enumerate(buy_list):
            def si(v):
                if v is None: return 0.0
                if isinstance(v, float) and math.isnan(v): return 0.0
                return round(float(v), 1)
            s1 = si(d.get('s1_vol_break'))
            s2 = si(d.get('s2_ma_conv'))
            s3 = si(d.get('s3_pv_div'))
            s4 = si(d.get('s4_bottom'))
            s5 = si(d.get('s5_whale'))
            cond = f"放量{s1}·均线{s2}·背离{s3}·抄底{s4}·主力{s5}"
            msg += f"{i+1}. {d['code']} {d['name']}  [{cond}  综合{round(d['score'],1)}/50]\n"
            msg += f"   现价: {d['price']} 元\n"
            msg += f"   买入: {d['buy_money']} 元 | {int(d['buy_volume'])} 股\n"
            msg += f"   止损: {d['stop_loss']} | 止盈: {d['take_profit']}\n\n"
        msg += "5策略：放量突破·均线粘合·量价背离·抄底·主力建仓\n"
        msg += f"融合阈值>={FUSION_THRESHOLD}/50触发\n"
        msg += f"规则: 持股3-8天 | {int(STOP_LOSS*100)}%止损 | +{int(TAKE_PROFIT*100)}%止盈\n"

    msg += "(仅供参考，投资有风险)"

    # 保存到数据库
    save_signals(buy_list, sent_wechat=should_push)

    if should_push:
        send_wechat(msg)
        print(f"[{_now()}] 扫描完成，已推送微信")
    else:
        print(f"[{_now()}] 扫描完成（超过15:00），仅本地保存，不推送微信")

    print("\n" + msg)


# ─────────────────────────────────────────────
# 每日数据同步任务（每天 16:00）
# ─────────────────────────────────────────────

def sync_job():
    """每日 16:00 同步收盘数据到数据库"""
    print(f"\n[{_now()}] 触发每日数据同步...")
    try:
        daily_sync()
        # R6: 同步完成后清理 CSV 缓存（只删7天前的旧文件）
        try:
            from core.data_fetcher import cleanup_cache
            removed = cleanup_cache(days_old=7)
            if removed > 0:
                print(f"    [R6] 已清理 {removed} 个旧缓存文件")
        except Exception as e:
            print(f"    [R6] 清理缓存失败（不影响同步）: {e}")
    except Exception as e:
        print(f"[{_now()}] 数据同步异常: {e}")


# ─────────────────────────────────────────────
# 首次运行初始化（后台线程）
# ─────────────────────────────────────────────

def _bg_init():
    """
    后台初始化：数据库为空时自动拉取历史数据
    在独立线程中运行，不阻塞主程序
    """
    try:
        stock_cnt = get_stock_count_in_db()
        if stock_cnt < 100:
            print(f"\n[INIT] 数据库股票数量不足（{stock_cnt}），开始初始化历史数据...")
            print("[INIT] 这是首次运行，将拉取 2024-01-01 至今的历史数据")
            print("[INIT] 预计耗时 30~60 分钟，期间策略扫描将读取已有数据\n")
            initial_sync()
        else:
            print(f"[INIT] 数据库已有 {stock_cnt} 只股票数据，跳过初始化")
    except Exception as e:
        print(f"[INIT] 初始化异常: {e}")


# ─────────────────────────────────────────────
# 启动入口
# ─────────────────────────────────────────────

if __name__ == "__main__":
    # 1. 初始化数据库表结构
    init_db()

    # 2. 打印状态
    stats = db_stats()
    print("=" * 55)
    print("  Windows 量化交易助手 v2（数据库版）")
    print(f"  初始资金: {START_CAPITAL} 元  |  每仓: {int(POSITION_PER_STOCK*100)}%  |  最多 {POSITION_NUM} 只")
    print(f"  止损: {int(STOP_LOSS*100)}%  |  止盈: {int(TAKE_PROFIT*100)}%")
    print("-" * 55)
    print(f"  数据库: {stats['有行情股票数']} 只股票 / {stats['行情记录总数']} 条记录")
    print(f"  最新数据: {stats['最新日期']}  |  大小: {stats['数据库大小(MB)']} MB")
    print("-" * 55)
    print("  调度计划:")
    print("    07:00  数据同步")
    print("    09:00  策略扫描")
    print("=" * 55)

    if WX_PUSH_KEY == "把你的微信机器人key填在这里":
        print("\n[警告] 未填写微信机器人 KEY，推送功能不可用")

    # 3. 后台初始化（不阻塞）
    init_thread = threading.Thread(target=_bg_init, daemon=True)
    init_thread.start()

    # 4. 注册定时任务
    schedule.every().day.at("07:00").do(sync_job)  # 早上先同步数据
    schedule.every().day.at("09:00").do(scan_job)  # 策略扫描

    print("\n[定时任务] 已注册:")
    print("  07:00 数据同步")
    print("  09:00 策略扫描")
    print("\n程序运行中，请勿关闭此窗口...\n")

    # 5. 主循环
    while True:
        schedule.run_pending()
        time.sleep(30)
