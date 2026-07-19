"""
core/data_cleaner.py —— 股票日线数据清洗器
============================================
解决：数据库中出现几天"明显脱离正常价格水平"的数据

常见脏数据来源：
  1. 新股上市首日（无前一日 → 复权基准跳变）
  2. 送转除权日（除权前后价格被错算）
  3. 接口数据断点（某日 OHLC 全 0）
  4. 数据源切换（akshare/baostock 复权方式不同导致跳变）
  5. 测试数据混入

多层防御：
  L1  绝对价格       0 < price <= 2000
  L2  OHLC 关系      high >= max(O,C,L), low <= min(O,C,H)
  L3  单日涨跌幅     |pct_change| <= 30% （科创板 ±20%，主板 ±10%）
  L4  与前一日跳变   |jump| <= 50%
  L5  复权连续性     close / hist_20d_avg ∈ [0.2, 5]
  L6  新股识别       上市前 30 天内 → 标记为新股，不参与过滤（保留原始）
  L7  成交量过滤     volume > 0

使用：
  from core.data_cleaner import clean_dataframe, clean_database

  # 单只股票清洗（清洗后 DataFrame）
  df = clean_dataframe(code, df)

  # 全市场数据库清洗
  stats = clean_database(dry_run=False)
  print(stats)
"""
from __future__ import annotations

import logging
from typing import Tuple
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# 配置：清洗阈值（可按需调整）
# ─────────────────────────────────────────────
CLEAN_CONFIG = {
    "max_price":           2000.0,   # 绝对价格上限（茅台 1500 可通过，3000 必脏）
    "min_price":           0.01,     # 绝对价格下限
    "max_pct_change":      30.0,     # 单日涨跌幅上限（%）
    "max_jump_ratio":      0.50,     # 与前一日跳变上限（50%）
    "min_volume":          0.0,      # 最小成交量（0 = 允许）
    "max_continuity_ratio": 5.0,     # 复权连续性：与 20 日均价比
    "min_continuity_ratio": 0.2,
    "new_stock_days":      30,       # 新股识别窗口（天）
    "history_window":      20,       # 复权连续性参考窗口
    "mode":                "interpolate",  # 脏值处理方式
}


# ─────────────────────────────────────────────
# L1-L7 单点检测函数
# ─────────────────────────────────────────────
def _check_l1_price(value: float) -> bool:
    """L1: 绝对价格合规（0 < v <= 2000）"""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return False
    return CLEAN_CONFIG["min_price"] <= value <= CLEAN_CONFIG["max_price"]


def _check_l2_ohlc(o: float, h: float, l: float, c: float) -> bool:
    """L2: OHLC 关系合规"""
    if not all(_check_l1_price(x) for x in (o, h, l, c)):
        return False
    return h >= max(o, c, l) and l <= min(o, c, h)


def _check_l3_pct_change(pct: float) -> bool:
    """L3: 单日涨跌幅合规（首日无前价 → 跳过）"""
    if pct is None or (isinstance(pct, float) and np.isnan(pct)):
        return True  # 缺数据时不强过滤
    return abs(pct) <= CLEAN_CONFIG["max_pct_change"]


def _check_l4_jump(curr: float, prev: float) -> bool:
    """L4: 与前一日跳变合规"""
    if not _check_l1_price(curr) or not _check_l1_price(prev) or prev <= 0:
        return True  # 缺前价时不强过滤
    return abs((curr - prev) / prev) <= CLEAN_CONFIG["max_jump_ratio"]


def _check_l5_continuity(curr: float, hist_avg: float) -> bool:
    """L5: 复权连续性合规（与历史 20 日均价比）"""
    if not _check_l1_price(curr) or hist_avg is None or hist_avg <= 0:
        return True  # 缺历史均价时不强过滤
    ratio = curr / hist_avg
    return CLEAN_CONFIG["min_continuity_ratio"] <= ratio <= CLEAN_CONFIG["max_continuity_ratio"]


def _is_new_stock_window(idx: int) -> bool:
    """L6: 是否在新股窗口（前 N 天）"""
    return idx < CLEAN_CONFIG["new_stock_days"]


def validate_record(o, h, l, c, vol=0) -> bool:
    """单条日线记录行级校验（非负价格 + L2 OHLC 关系 + L7 负成交量）。

    不含 L1 价格上限检查（不同场景上限不同，由调用方自行判断），
    供批量写入路径（如 _batch_write_daily_price）在写入前做行级过滤，
    无需历史时间序列上下文即可拦截字段错位/负价格/负成交量等明显脏值。
    """
    # 价格不能为 None / NaN / 非正
    for x in (o, h, l, c):
        if x is None or (isinstance(x, float) and np.isnan(x)) or x <= 0:
            return False
    # L2: OHLC 关系合规（high >= max(O,C,L), low <= min(O,C,H)）
    if not (h >= max(o, c, l) and l <= min(o, c, h)):
        return False
    # L7: 负成交量判脏
    if vol is not None and not (isinstance(vol, float) and np.isnan(vol)) and vol < 0:
        return False
    return True


# ─────────────────────────────────────────────
# 脏值处理
# ─────────────────────────────────────────────
def _fill_dirty_value(series: pd.Series, idx: int, mode: str = "interpolate") -> float:
    """
    填充一个被识别的脏值
      - interpolate: 优先前后均值；前后都是脏的 → 扩展搜索更远处
      - previous: 用前一日（最近的干净值）
      - drop: 标记为 NaN（由调用方 drop）
    """
    if mode == "previous":
        return _find_nearest_clean(series, idx, direction="prev")
    if mode == "drop":
        return np.nan
    # 默认 interpolate：先前后均值，再扩展搜索
    n = len(series)
    prev_v = _find_nearest_clean(series, idx, direction="prev")
    next_v = _find_nearest_clean(series, idx, direction="next")
    vals = [v for v in (prev_v, next_v) if _check_l1_price(v)]
    if vals:
        return float(np.mean(vals))
    return np.nan


def _find_nearest_clean(series: pd.Series, idx: int, direction: str = "prev") -> float:
    """在 series 中，从 idx 向指定方向搜索最近的"干净"值"""
    n = len(series)
    if direction == "prev":
        for i in range(idx - 1, -1, -1):
            v = series.iloc[i]
            if _check_l1_price(v) and not pd.isna(v):
                return float(v)
    else:  # next
        for i in range(idx + 1, n):
            v = series.iloc[i]
            if _check_l1_price(v) and not pd.isna(v):
                return float(v)
    return np.nan


# ─────────────────────────────────────────────
# 主入口：清洗单只股票的 DataFrame
# ─────────────────────────────────────────────
def clean_dataframe(code: str, df: pd.DataFrame,
                    verbose: bool = False,
                    new_stock_skip: bool = True) -> Tuple[pd.DataFrame, dict]:
    """
    清洗单只股票的日线数据

    :param code: 股票代码（用于日志）
    :param df:   包含 trade_date, open, high, low, close, volume, pct_change 的 DataFrame
    :param new_stock_skip: 是否跳过新股窗口（前30天）的过滤。
        True  = 适合全量清洗（clean_database），保护新股数据不被误删
        False = 适合增量写入（_write_daily_price），让 L1/L2/L7 对所有行生效
    :return:     (cleaned_df, stats)
        stats = {
            "input_rows": int,
            "output_rows": int,
            "l1_invalid_price": int,
            "l2_invalid_ohlc": int,
            "l3_invalid_pct": int,
            "l4_invalid_jump": int,
            "l5_invalid_continuity": int,
            "l7_zero_volume": int,
            "filled_count": int,
            "dropped_count": int,
        }
    """
    if df is None or df.empty:
        return df, {"input_rows": 0, "output_rows": 0}

    stats = {
        "input_rows": len(df),
        "output_rows": 0,
        "l1_invalid_price": 0,
        "l2_invalid_ohlc": 0,
        "l3_invalid_pct": 0,
        "l4_invalid_jump": 0,
        "l5_invalid_continuity": 0,
        "l7_zero_volume": 0,
        "l7_negative_volume": 0,
        "filled_count": 0,
        "dropped_count": 0,
    }

    df = df.copy().sort_index()  # 按日期升序
    if df.empty:
        return df, {"input_rows": 0, "output_rows": 0}
    n = len(df)

    # === 标记每个位置的脏值类型 ===
    bad_mask = pd.Series(False, index=df.index)
    reasons: list[str] = []

    for i, (idx, row) in enumerate(df.iterrows()):
        o, h, l, c = row.get("open"), row.get("high"), row.get("low"), row.get("close")
        vol = row.get("volume", 0) or 0
        pct = row.get("pct_change")

        # L6: 新股窗口内的数据不强过滤（仅在 new_stock_skip=True 时生效）
        if new_stock_skip and _is_new_stock_window(i):
            continue

        # L1
        if not all(_check_l1_price(x) for x in (o, h, l, c) if x is not None and not pd.isna(x)):
            bad_mask.iloc[i] = True
            stats["l1_invalid_price"] += 1
            continue

        # L2
        if not _check_l2_ohlc(o, h, l, c):
            bad_mask.iloc[i] = True
            stats["l2_invalid_ohlc"] += 1
            continue

        # L3
        if not _check_l3_pct_change(pct):
            bad_mask.iloc[i] = True
            stats["l3_invalid_pct"] += 1
            continue

        # L4
        if i > 0:
            prev_c = df["close"].iloc[i - 1]
            # 前一日如果是 0 元/负数（被修复前），也跳过（防止误判）
            if not _check_l1_price(prev_c) or prev_c <= 0:
                pass  # 前一日是修复前的脏值，跳过 L4
            elif not _check_l4_jump(c, prev_c):
                bad_mask.iloc[i] = True
                stats["l4_invalid_jump"] += 1
                continue

        # L5 复权连续性
        if i >= CLEAN_CONFIG["history_window"]:
            hist_avg = df["close"].iloc[i - CLEAN_CONFIG["history_window"]:i].mean()
            if not _check_l5_continuity(c, hist_avg):
                bad_mask.iloc[i] = True
                stats["l5_invalid_continuity"] += 1
                continue

        # L7: 负成交量必为脏数据（vol < 0）；0 成交量可能是停牌，保留不判脏
        if vol < 0:
            bad_mask.iloc[i] = True
            stats["l7_negative_volume"] += 1
            continue
        if vol == 0:
            stats["l7_zero_volume"] += 1
            # 0 成交量可能是停牌，不判脏

    # === 处理脏值 ===
    n_dirty = int(bad_mask.sum())
    if n_dirty == 0:
        stats["output_rows"] = len(df)
        return df, stats

    mode = CLEAN_CONFIG["mode"]
    if mode == "drop":
        # 直接删除脏行
        df_clean = df[~bad_mask].copy()
        stats["dropped_count"] = n_dirty
    else:
        # interpolate / previous：填充脏值
        df_clean = df.copy()
        # 重建索引到 0..N-1，避免 DatetimeIndex 重复问题
        for col in ["open", "high", "low", "close"]:
            # 找出该列在脏行的位置（用整数位置，不用 index label）
            dirty_positions = np.where(bad_mask.values)[0]
            for pos in dirty_positions:
                if pos < 0 or pos >= len(df_clean):
                    continue
                old_v = df_clean.iloc[pos][col]
                new_v = _fill_dirty_value(df_clean[col], pos, mode=mode)
                if pd.isna(new_v):
                    # 真填不出来（前后都是脏的） → drop
                    df_clean = df_clean.drop(df_clean.index[pos])
                    stats["dropped_count"] += 1
                else:
                    df_clean.iloc[pos, df_clean.columns.get_loc(col)] = new_v
                    stats["filled_count"] += 1
        # 重新计算 pct_change
        if "close" in df_clean.columns and len(df_clean) > 1:
            df_clean["pct_change"] = df_clean["close"].pct_change() * 100

    stats["output_rows"] = len(df_clean)

    if verbose and n_dirty > 0:
        logger.info(
            "[clean] %s: %d 行脏值（价格%d OHLC%d 张幅%d 跳价%d 复权%d），"
            "填充%d 丢弃%d, 剩%d",
            code, n_dirty,
            stats["l1_invalid_price"], stats["l2_invalid_ohlc"],
            stats["l3_invalid_pct"], stats["l4_invalid_jump"],
            stats["l5_invalid_continuity"],
            stats["filled_count"], stats["dropped_count"],
            stats["output_rows"],
        )

    return df_clean, stats


# ─────────────────────────────────────────────
# 数据库批量清洗
# ─────────────────────────────────────────────
def clean_database(batch_size: int = 200, dry_run: bool = False,
                   verbose: bool = True) -> dict:
    """
    批量清洗数据库中的 daily_price 表

    :param batch_size: 每批处理的股票数
    :param dry_run:    True=只统计不修改
    :return:           {"total": N, "modified": M, "deleted": K, "by_type": {...}}
    """
    from core.db import get_conn

    total_stats = {
        "total_stocks": 0,
        "modified_stocks": 0,
        "rows_before": 0,
        "rows_after": 0,
        "rows_dropped": 0,
        "rows_filled": 0,
        "by_l1": 0, "by_l2": 0, "by_l3": 0, "by_l4": 0, "by_l5": 0,
        "by_l7_neg": 0,
    }

    with get_conn() as conn:
        codes = [r["code"] for r in conn.execute(
            "SELECT DISTINCT code FROM daily_price ORDER BY code"
        ).fetchall()]
    total_stats["total_stocks"] = len(codes)

    if verbose:
        print(f"[clean_database] 扫描 {len(codes)} 只股票 (dry_run={dry_run})")

    for i, code in enumerate(codes):
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT trade_date, open, high, low, close, volume, "
                "amount, pct_change, turnover "
                "FROM daily_price WHERE code=? ORDER BY trade_date",
                (code,),
            ).fetchall()
        if not rows:
            continue

        df = pd.DataFrame([dict(r) for r in rows])
        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
        # 防御：去掉 NaT 和重复日期
        df = df.dropna(subset=["trade_date"])
        df = df.drop_duplicates(subset=["trade_date"], keep="last")
        df.set_index("trade_date", inplace=True)

        df_clean, stats = clean_dataframe(code, df, verbose=False)

        # 累加
        total_stats["rows_before"] += stats["input_rows"]
        total_stats["rows_after"] += stats["output_rows"]
        total_stats["rows_dropped"] += stats.get("dropped_count", 0)
        total_stats["rows_filled"] += stats.get("filled_count", 0)
        total_stats["by_l1"] += stats["l1_invalid_price"]
        total_stats["by_l2"] += stats["l2_invalid_ohlc"]
        total_stats["by_l3"] += stats["l3_invalid_pct"]
        total_stats["by_l4"] += stats["l4_invalid_jump"]
        total_stats["by_l5"] += stats["l5_invalid_continuity"]
        total_stats["by_l7_neg"] += stats.get("l7_negative_volume", 0)
        if stats.get("filled_count", 0) + stats.get("dropped_count", 0) > 0:
            total_stats["modified_stocks"] += 1

        # 写回（仅在数据有变化时）
        if not dry_run and (stats.get("filled_count", 0) + stats.get("dropped_count", 0)) > 0:
            _rewrite_stock(code, df_clean)

        if verbose and (i + 1) % 200 == 0:
            print(f"  进度 {i+1}/{len(codes)}")

    if verbose:
        print(f"\n[汇总] 扫描 {total_stats['total_stocks']} 只")
        print(f"  脏值总数: {total_stats['by_l1'] + total_stats['by_l2'] + total_stats['by_l3'] + total_stats['by_l4'] + total_stats['by_l5'] + total_stats['by_l7_neg']}")
        print(f"  L1 绝对价: {total_stats['by_l1']}")
        print(f"  L2 OHLC:   {total_stats['by_l2']}")
        print(f"  L3 张幅:   {total_stats['by_l3']}")
        print(f"  L4 跳价:   {total_stats['by_l4']}")
        print(f"  L5 复权:   {total_stats['by_l5']}")
        print(f"  L7 负量:   {total_stats['by_l7_neg']}")
        print(f"  修复行数:  填充 {total_stats['rows_filled']}, 丢弃 {total_stats['rows_dropped']}")
        print(f"  影响股票:  {total_stats['modified_stocks']}")
    return total_stats


def _rewrite_stock(code: str, df_clean: pd.DataFrame) -> None:
    """把清洗后的数据写回数据库（只改 OHLCV，其他字段不动）"""
    from core.db import get_conn
    if df_clean.empty:
        # 全部脏 → 整只股票删
        with get_conn() as conn:
            conn.execute("DELETE FROM daily_price WHERE code=?", (code,))
        return

    # 批量 executemany 加速
    records = []
    for dt, row in df_clean.iterrows():
        date_str = dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10]
        records.append((
            float(row["open"]), float(row["high"]),
            float(row["low"]), float(row["close"]),
            float(row.get("volume", 0) or 0),
            float(row.get("amount", 0) or 0),
            float(row.get("pct_change", 0) or 0),
            float(row.get("turnover", 0) or 0),
            code, date_str,
        ))
    with get_conn() as conn:
        conn.executemany("""
            UPDATE daily_price
            SET open=?, high=?, low=?, close=?, volume=?,
                amount=?, pct_change=?, turnover=?
            WHERE code=? AND trade_date=?
        """, records)


# ─────────────────────────────────────────────
# 单只股票查询 + 清洗（推荐池用）
# ─────────────────────────────────────────────
def get_cleaned_history(code: str, start_date: str, end_date: str,
                        adjust: str = "qfq") -> pd.DataFrame:
    """
    拉取并清洗单只股票历史数据（盘前/盘后推荐用）

    流程：em_kline.get_stock_history → clean_dataframe
    """
    from core.em_kline import get_stock_history
    df = get_stock_history(code, start_date, end_date, adjust=adjust, use_cache=True)
    if df is None or df.empty:
        return df
    df_clean, stats = clean_dataframe(code, df, verbose=False)
    if stats.get("filled_count", 0) + stats.get("dropped_count", 0) > 0:
        logger.info("[get_cleaned_history] %s 清洗: 填充%d 丢弃%d",
                    code, stats.get("filled_count", 0), stats.get("dropped_count", 0))
    return df_clean
