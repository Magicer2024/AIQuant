#!/usr/bin/env python3
"""
tools/fix_history.py —— 全量历史量价数据修正工具
==================================================
背景：
  daily_price 表中部分股票的历史量价数据不正确（价格跳变、成交量缺失、
  OHLC 关系错误、复权不一致等），需要一次性全量修正。

修正策略：
  1. 对每只股票，从 东方财富 + 腾讯 两个独立异构数据源拉取全历史前复权 K 线
  2. 与数据库现有数据对比，检测：缺失日期 / 价格偏差 / 成交量缺失 / OHLC 错位
  3. 用参考数据覆盖写入（ON CONFLICT UPDATE）
  4. 断点续传：进度文件记录已完成股票，中断后自动跳过

数据源：
  - 腾讯 web.ifzq.gtimg.cn（主源）：前复权日 K，无 token，稳定
  - 东方财富 push2his（备源）：前复权日 K，偶尔反爬，加重试+随机延迟

用法：
  python tools/fix_history.py                          # 全量修正（腾讯优先）
  python tools/fix_history.py --codes 600519 000001    # 修正指定股票
  python tools/fix_history.py --dry-run                # 只检测不写入
  python tools/fix_history.py --source em              # 只用东方财富
  python tools/fix_history.py --source tx              # 只用腾讯（默认）
  python tools/fix_history.py --source both            # 两源交叉
  python tools/fix_history.py --reset                  # 重置进度
  python tools/fix_history.py --report                 # 查看上次报告
  python tools/fix_history.py --threads 4 --rate 2.0   # 4线程，每只间隔2s
"""
from __future__ import annotations

import sys
import time
import json
import random
import logging
import argparse
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

import pandas as pd
import requests
from requests.adapters import HTTPAdapter

# ── 项目路径 ──
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.db import get_conn, init_db, get_all_stocks

logger = logging.getLogger("fix_history")

# ── 常量 ──
_PROGRESS_FILE = PROJECT_ROOT / "data_cache" / "fix_history_progress.json"
_REPORT_FILE = PROJECT_ROOT / "data_cache" / "fix_history_report.json"

_EM_UT = "bd1d9ddb04089700cf9c27f6f7426281"
_EM_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

_TX_FQKLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_TX_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://gu.qq.com/",
}

# 问题检测阈值
_PRICE_TOL = 0.02       # 价格允许 2% 误差（复权精度差异）
_MAX_PRICE = 2000.0     # 价格上限


# ═══════════════════════════════════════════════
# 东方财富数据源
# ═══════════════════════════════════════════════

def _em_secid(code: str) -> str:
    code = str(code).strip()
    if code.startswith(("6", "9", "5")):
        return f"1.{code}"
    return f"0.{code}"


def fetch_em_kline(code: str, max_retries: int = 3) -> pd.DataFrame:
    """东方财富 push2his 拉单股全历史日 K（前复权），带指数退避重试"""
    fqt = "1"  # 前复权
    secid = _em_secid(code)
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid, "ut": _EM_UT,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101", "fqt": fqt,
        "beg": "0", "end": "20500101", "lmt": "10000",
    }

    for attempt in range(max_retries):
        try:
            # 每次新建 session（避免连接复用触发反爬）
            sess = requests.Session()
            sess.headers.update(_EM_HEADERS)
            time.sleep(random.uniform(0.3, 1.5))  # 随机延迟
            resp = sess.get(url, params=params, timeout=20)
            sess.close()
            resp.raise_for_status()
            data = resp.json()
            klines = (data or {}).get("data", {}).get("klines") or []
            if not klines:
                return pd.DataFrame()
            return _parse_em_klines(klines)
        except Exception as e:
            wait = (attempt + 1) * random.uniform(2, 5)
            logger.debug("东财 %s 第%d次失败(等%.1fs): %s", code, attempt + 1, wait, e)
            time.sleep(wait)

    return pd.DataFrame()


def _parse_em_klines(klines: list[str]) -> pd.DataFrame:
    rows = [k.split(",") for k in klines]
    df = pd.DataFrame(rows, columns=[
        "trade_date", "open", "close", "high", "low",
        "volume", "amount", "amplitude", "pct_change", "turnover", "turnover_5d",
    ])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    for col in ["open", "close", "high", "low", "volume", "amount", "pct_change", "turnover"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    # 东财 push2his 的 volume 字段(f56)单位是「手」，统一 ×100 转「股」，
    # 与腾讯源(fetch_tx_kline 已 ×100)及 daily_price 入库口径一致。否则会写入 100 倍偏小的成交量。
    df["volume"] = df["volume"].fillna(0) * 100
    return df[["trade_date", "open", "high", "low", "close",
               "volume", "amount", "pct_change", "turnover"]].sort_values("trade_date").reset_index(drop=True)


# ═══════════════════════════════════════════════
# 腾讯数据源
# ═══════════════════════════════════════════════

def _tx_code(code: str) -> str | None:
    code = str(code).strip().zfill(6)
    if code.startswith(("60", "68")):
        return f"sh{code}"
    if code.startswith(("00", "30")):
        return f"sz{code}"
    if code.startswith(("43", "83", "87", "92")):
        return f"bj{code}"
    # 其他：根据首数字猜测
    if code[0] == "6":
        return f"sh{code}"
    if code[0] in ("0", "3"):
        return f"sz{code}"
    return f"sh{code}"


def fetch_tx_kline(code: str) -> pd.DataFrame:
    """
    腾讯 web.ifzq 拉单股全历史日 K（前复权）

    接口特点：
      - 单次最多返回 ~800 行
      - 不传 end_date 时返回最近 800 行
      - 传入 end_date 返回 end_date 之前的 800 行
      - 需要从最近往前分段拉取
    """
    tx = _tx_code(code)
    if tx is None:
        return pd.DataFrame()

    all_rows = []
    seen_dates = set()
    # 从最近往前拉：每次请求 800 行，end_date 设为上次最早的日期前一天
    end_date = None  # None = 最新

    for segment in range(20):  # 最多 20 段 ≈ 16000 天 ≈ 65 年
        # 参数格式：code,day,start,end,count,qfq
        # start 留空表示从最早开始，end 留空或指定日期
        start_str = ""
        end_str = end_date if end_date else datetime.now().strftime("%Y-%m-%d")
        param = f"{tx},day,{start_str},{end_str},800,qfq"

        url = f"{_TX_FQKLINE_URL}?param={param}"
        try:
            sess = requests.Session()
            sess.headers.update(_TX_HEADERS)
            time.sleep(random.uniform(0.2, 0.5))
            resp = sess.get(url, timeout=15)
            sess.close()
            payload = resp.json()
        except Exception as e:
            logger.debug("腾讯 %s 第%d段请求失败: %s", code, segment + 1, e)
            break

        try:
            node = payload.get("data", {}).get(tx, {})
            rows = node.get("qfqday") or node.get("day") or []
        except (KeyError, TypeError):
            break

        if not rows:
            break

        new_count = 0
        earliest_date = None
        for r in rows:
            try:
                dt_str = str(r[0])
                if dt_str in seen_dates:
                    continue
                seen_dates.add(dt_str)
                o = float(r[1])
                c = float(r[2])
                h = float(r[3])
                l = float(r[4])
                v = float(r[5]) * 100.0 if len(r) > 5 else 0.0  # 手→股
                amt = v * c if c > 0 else 0.0
                all_rows.append({
                    "trade_date": dt_str,
                    "open": o, "high": h, "low": l, "close": c,
                    "volume": v, "amount": amt,
                    "pct_change": 0.0, "turnover": 0.0,
                })
                new_count += 1
                if earliest_date is None or dt_str < earliest_date:
                    earliest_date = dt_str
            except (ValueError, TypeError, IndexError):
                continue

        if new_count == 0:
            break

        # 下一段：end_date = 最早日期前一天
        if earliest_date:
            prev = pd.Timestamp(earliest_date) - pd.Timedelta(days=1)
            end_date = prev.strftime("%Y-%m-%d")
        else:
            break

        # 如果返回行数 < 800，说明已拉完
        if len(rows) < 800:
            break

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.drop_duplicates(subset=["trade_date"], keep="first")
    return df.sort_values("trade_date").reset_index(drop=True)


# ═══════════════════════════════════════════════
# 数据库读取
# ═══════════════════════════════════════════════

def fetch_db_kline(code: str) -> pd.DataFrame:
    with get_conn() as conn:
        df = pd.read_sql_query(
            "SELECT trade_date, open, high, low, close, volume, amount, pct_change, turnover "
            "FROM daily_price WHERE code = ? ORDER BY trade_date",
            conn, params=(code,)
        )
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df.sort_values("trade_date").reset_index(drop=True)


# ═══════════════════════════════════════════════
# 问题检测
# ═══════════════════════════════════════════════

def detect_issues(db_df: pd.DataFrame, ref_df: pd.DataFrame, code: str) -> dict:
    """对比数据库和参考数据，检测各类问题"""
    if db_df.empty or ref_df.empty:
        return _empty_issues()

    db_dates = set(db_df["trade_date"].dt.strftime("%Y-%m-%d"))
    ref_dates = set(ref_df["trade_date"].dt.strftime("%Y-%m-%d"))

    missing_dates = ref_dates - db_dates
    extra_dates = db_dates - ref_dates

    # 对齐日期做对比
    db_indexed = db_df.set_index("trade_date")
    ref_indexed = ref_df.set_index("trade_date")
    common = db_indexed.index.intersection(ref_indexed.index)

    price_wrong = 0
    volume_wrong = 0
    ohlc_invalid = 0
    details = []

    for dt in common:
        db_r = db_indexed.loc[dt]
        ref_r = ref_indexed.loc[dt]
        dt_str = dt.strftime("%Y-%m-%d")

        db_c = float(db_r.get("close", 0) or 0)
        ref_c = float(ref_r.get("close", 0) or 0)

        # 价格偏差
        if ref_c > 0 and db_c > 0:
            pct_diff = abs(db_c - ref_c) / ref_c
            if pct_diff > _PRICE_TOL:
                price_wrong += 1
                if len(details) < 50:
                    details.append(f"价格偏差: {dt_str} db={db_c:.2f} ref={ref_c:.2f} ({pct_diff:.1%})")

        # 成交量
        db_v = float(db_r.get("volume", 0) or 0)
        ref_v = float(ref_r.get("volume", 0) or 0)
        if ref_v > 0 and db_v == 0:
            volume_wrong += 1
            if len(details) < 50:
                details.append(f"成交量缺失: {dt_str} db=0 ref={ref_v:.0f}")

        # OHLC
        db_o = float(db_r.get("open", 0) or 0)
        db_h = float(db_r.get("high", 0) or 0)
        db_l = float(db_r.get("low", 0) or 0)
        if all(v > 0 for v in (db_o, db_h, db_l, db_c)):
            if db_h < max(db_o, db_c, db_l) - 0.01 or db_l > min(db_o, db_c, db_h) + 0.01:
                ohlc_invalid += 1
                if len(details) < 50:
                    details.append(f"OHLC错位: {dt_str} O={db_o:.2f} H={db_h:.2f} L={db_l:.2f} C={db_c:.2f}")

    total = len(missing_dates) + price_wrong + volume_wrong + ohlc_invalid
    return {
        "missing": len(missing_dates),
        "price_wrong": price_wrong,
        "volume_wrong": volume_wrong,
        "ohlc_invalid": ohlc_invalid,
        "total_issues": total,
        "details": details,
    }


def _empty_issues():
    return {"missing": 0, "price_wrong": 0, "volume_wrong": 0,
            "ohlc_invalid": 0, "total_issues": 0, "details": []}


# ═══════════════════════════════════════════════
# 修正写入
# ═══════════════════════════════════════════════

def fix_stock_data(code: str, ref_df: pd.DataFrame, issues: dict,
                   dry_run: bool = False) -> dict:
    """用参考数据修正数据库"""
    if ref_df.empty or issues["total_issues"] == 0:
        return {"fixed_rows": 0, "inserted": 0, "updated": 0}

    if dry_run:
        return {"fixed_rows": issues["total_issues"],
                "inserted": issues["missing"],
                "updated": issues["price_wrong"] + issues["volume_wrong"] + issues["ohlc_invalid"]}

    db_df = fetch_db_kline(code)
    db_dates = set(db_df["trade_date"].dt.strftime("%Y-%m-%d")) if not db_df.empty else set()

    records = []
    inserted = 0
    updated = 0

    for _, row in ref_df.iterrows():
        dt = row["trade_date"]
        dt_str = dt.strftime("%Y-%m-%d")
        o = float(row.get("open", 0) or 0)
        h = float(row.get("high", 0) or 0)
        l = float(row.get("low", 0) or 0)
        c = float(row.get("close", 0) or 0)
        v = float(row.get("volume", 0) or 0)
        a = float(row.get("amount", 0) or 0)
        pct = float(row.get("pct_change", 0) or 0)
        tr = float(row.get("turnover", 0) or 0)

        if c <= 0 or c > _MAX_PRICE:
            continue
        if not (h >= max(o, c, l) - 0.01 and l <= min(o, c, h) + 0.01):
            continue

        need_fix = False
        if dt_str not in db_dates:
            need_fix = True
            inserted += 1
        else:
            # 数据库存在 → 检查是否需要修正
            db_row = db_df[db_df["trade_date"] == dt].iloc[0]
            db_c = float(db_row.get("close", 0) or 0)
            db_v = float(db_row.get("volume", 0) or 0)

            if db_c > 0 and c > 0 and abs(db_c - c) / c > _PRICE_TOL:
                need_fix = True
                updated += 1
            elif v > 0 and db_v == 0:
                need_fix = True
                updated += 1
            else:
                db_o = float(db_row.get("open", 0) or 0)
                db_h = float(db_row.get("high", 0) or 0)
                db_l = float(db_row.get("low", 0) or 0)
                if (db_o > 0 and db_h > 0 and db_l > 0 and db_c > 0 and
                    (db_h < max(db_o, db_c, db_l) - 0.01 or
                     db_l > min(db_o, db_c, db_h) + 0.01)):
                    need_fix = True
                    updated += 1

        if need_fix:
            records.append({
                "code": code, "trade_date": dt_str,
                "open": o, "high": h, "low": l, "close": c,
                "volume": v, "amount": a,
                "pct_change": pct, "turnover": tr,
            })

    if not records:
        return {"fixed_rows": 0, "inserted": 0, "updated": 0}

    with get_conn() as conn:
        conn.executemany("""
            INSERT INTO daily_price
              (code,trade_date,open,high,low,close,volume,amount,pct_change,turnover)
            VALUES
              (:code,:trade_date,:open,:high,:low,:close,:volume,:amount,:pct_change,:turnover)
            ON CONFLICT(code, trade_date) DO UPDATE SET
              open=excluded.open, high=excluded.high, low=excluded.low,
              close=excluded.close, volume=excluded.volume, amount=excluded.amount,
              pct_change=excluded.pct_change, turnover=excluded.turnover
        """, records)

    return {"fixed_rows": len(records), "inserted": inserted, "updated": updated}


# ═══════════════════════════════════════════════
# 单股修正
# ═══════════════════════════════════════════════

def fix_one_stock(code: str, source: str = "tx",
                  dry_run: bool = False, verbose: bool = False) -> dict:
    result = {
        "code": code, "status": "ok",
        "em_rows": 0, "tx_rows": 0, "db_rows": 0,
        "issues": None,
        "fixed": {"fixed_rows": 0, "inserted": 0, "updated": 0},
        "error": None,
    }

    try:
        db_df = fetch_db_kline(code)
        result["db_rows"] = len(db_df)

        em_df = pd.DataFrame()
        tx_df = pd.DataFrame()
        ref_df = pd.DataFrame()

        if source in ("em", "both"):
            em_df = fetch_em_kline(code)
            result["em_rows"] = len(em_df)

        if source in ("tx", "both"):
            tx_df = fetch_tx_kline(code)
            result["tx_rows"] = len(tx_df)

        # 选择参考源
        if source == "both":
            if not em_df.empty and not tx_df.empty:
                ref_df = em_df  # 东财更权威
            elif not em_df.empty:
                ref_df = em_df
            elif not tx_df.empty:
                ref_df = tx_df
        elif source == "em":
            ref_df = em_df
        else:
            ref_df = tx_df

        if ref_df.empty:
            result["status"] = "no_ref"
            result["error"] = f"{source} 无参考数据"
            return result

        issues = detect_issues(db_df, ref_df, code)
        result["issues"] = {k: v for k, v in issues.items() if k != "details"}

        if issues["total_issues"] > 0:
            if verbose:
                d = "; ".join(issues["details"][:3])
                logger.info("  %s 问题 %d: %s", code, issues["total_issues"], d)
            fix_r = fix_stock_data(code, ref_df, issues, dry_run=dry_run)
            result["fixed"] = fix_r
            if verbose and fix_r["fixed_rows"] > 0:
                logger.info("  %s 修正 %d 行 (插%d+更%d)",
                           code, fix_r["fixed_rows"], fix_r["inserted"], fix_r["updated"])

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        logger.error("  %s 异常: %s", code, e)

    return result


# ═══════════════════════════════════════════════
# 进度管理
# ═══════════════════════════════════════════════

_progress_lock = Lock()


def _load_progress() -> dict:
    if _PROGRESS_FILE.exists():
        try:
            data = json.loads(_PROGRESS_FILE.read_text(encoding="utf-8"))
            # 兼容旧格式：补充缺失的 failed 键
            if "failed" not in data:
                data["failed"] = []
            return data
        except Exception:
            pass
    return {"completed": [], "failed": []}


def _save_progress(progress: dict):
    _PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PROGRESS_FILE.write_text(json.dumps(progress, ensure_ascii=False, indent=2),
                              encoding="utf-8")


def _mark_done(code: str, progress: dict, failed: bool = False):
    with _progress_lock:
        if failed:
            if code not in progress["failed"]:
                progress["failed"].append(code)
        else:
            if code not in progress["completed"]:
                progress["completed"].append(code)
        if (len(progress["completed"]) + len(progress["failed"])) % 50 == 0:
            _save_progress(progress)


# ═══════════════════════════════════════════════
# 批量修正
# ═══════════════════════════════════════════════

def run_fix_history(codes: list[str] | None = None,
                    source: str = "tx",
                    threads: int = 2,
                    dry_run: bool = False,
                    verbose: bool = False,
                    rate_limit: float = 1.0) -> dict:
    init_db()

    if codes is None:
        all_stocks = get_all_stocks()
        if all_stocks is None or all_stocks.empty:
            print("[ERROR] 无法获取股票列表")
            return {}
        codes = all_stocks["code"].astype(str).tolist()

    total = len(codes)

    # 断点续传
    progress = _load_progress()
    done_set = set(progress["completed"]) | set(progress["failed"])
    remaining = [c for c in codes if c not in done_set]

    mode_str = "检测(dry-run)" if dry_run else "修正(写入)"
    print(f"\n{'═' * 60}")
    print(f"  全量历史修正工具")
    print(f"  股票: {total}  剩余: {len(remaining)}  数据源: {source}  并发: {threads}")
    print(f"  模式: {mode_str}  限速: {rate_limit}s/只")
    print(f"{'═' * 60}\n")

    if not remaining:
        print("  全部已完成！使用 --reset 重新来过")
        return _gen_report(progress)

    stats = {
        "total": total, "remaining": len(remaining),
        "processed": 0, "fixed_stocks": 0, "no_issues": 0, "errors": 0,
        "total_fixed_rows": 0, "total_inserted": 0, "total_updated": 0,
        "start_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    issue_stocks = []

    t0 = time.time()
    batch_idx = [0]  # mutable counter for rate limiting

    def _worker(code: str) -> dict:
        # 限速
        idx = batch_idx[0]
        batch_idx[0] += 1
        if idx > 0:
            time.sleep(rate_limit / threads)

        r = fix_one_stock(code, source=source, dry_run=dry_run, verbose=verbose)
        is_fail = r["status"] == "error"
        _mark_done(code, progress, failed=is_fail)
        return r

    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = {executor.submit(_worker, c): c for c in remaining}

        for fut in as_completed(futures):
            code = futures[fut]
            try:
                r = fut.result()
            except Exception as e:
                r = {"code": code, "status": "error", "error": str(e)}

            stats["processed"] += 1

            if r["status"] == "error":
                stats["errors"] += 1
                if verbose:
                    print(f"  [ERR] {code}: {r.get('error', '')}")
            elif r.get("issues", {}).get("total_issues", 0) > 0:
                stats["fixed_stocks"] += 1
                fix = r.get("fixed", {})
                stats["total_fixed_rows"] += fix.get("fixed_rows", 0)
                stats["total_inserted"] += fix.get("inserted", 0)
                stats["total_updated"] += fix.get("updated", 0)
                issue_stocks.append({"code": code, "issues": r["issues"], "fixed": fix})
                if verbose:
                    iss = r["issues"]
                    print(f"  [FIX] {code}: {iss['total_issues']}问题 "
                          f"(缺{iss['missing']} 价{iss['price_wrong']} "
                          f"量{iss['volume_wrong']} OHLC{iss['ohlc_invalid']}) "
                          f"→ {fix['fixed_rows']}行")
            else:
                stats["no_issues"] += 1

            # 进度
            done = stats["processed"]
            if done % 20 == 0 or done == stats["remaining"]:
                elapsed = time.time() - t0
                speed = done / max(elapsed, 1)
                eta = (stats["remaining"] - done) / max(speed, 0.01)
                pct = done / stats["remaining"] * 100
                print(f"  [{done}/{stats['remaining']}] {pct:.0f}% "
                      f"{speed:.1f}只/s ETA {eta/60:.0f}min "
                      f"修正{stats['fixed_stocks']}只/{stats['total_fixed_rows']}行 "
                      f"错误{stats['errors']}")

    _save_progress(progress)
    elapsed = time.time() - t0
    stats["elapsed_s"] = round(elapsed, 1)
    stats["end_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n{'═' * 60}")
    print(f"  完成!")
    print(f"  处理: {stats['processed']}/{total}")
    print(f"  有问题: {stats['fixed_stocks']}  无问题: {stats['no_issues']}  错误: {stats['errors']}")
    print(f"  修正: {stats['total_fixed_rows']}行 (插{stats['total_inserted']}+更{stats['total_updated']})")
    print(f"  耗时: {elapsed/60:.1f}min")
    print(f"{'═' * 60}")

    report = {"stats": stats, "issue_stocks": issue_stocks[:200]}
    _REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  报告: {_REPORT_FILE}\n")
    return report


def _gen_report(progress: dict) -> dict:
    return {"completed": len(progress["completed"]),
            "failed": len(progress["failed"])}


def reset_progress():
    if _PROGRESS_FILE.exists():
        _PROGRESS_FILE.unlink()
        print("进度已重置")
    else:
        print("无进度文件")


def show_report():
    if not _REPORT_FILE.exists():
        print("无报告，请先运行修正")
        return
    report = json.loads(_REPORT_FILE.read_text(encoding="utf-8"))
    s = report.get("stats", {})
    print(f"\n{'═' * 60}")
    print(f"  上次报告: {s.get('start_time', '?')} → {s.get('end_time', '?')}")
    print(f"  处理: {s.get('processed', 0)}/{s.get('total', 0)}")
    print(f"  修正: {s.get('fixed_stocks', 0)}只 / {s.get('total_fixed_rows', 0)}行")
    print(f"  错误: {s.get('errors', 0)}")
    print(f"  耗时: {s.get('elapsed_s', 0) / 60:.1f}min")
    for item in report.get("issue_stocks", [])[:15]:
        iss = item["issues"]
        fix = item["fixed"]
        print(f"    {item['code']}: 问题{iss.get('total_issues', 0)} "
              f"→ 修正{fix.get('fixed_rows', 0)}行")
    print(f"{'═' * 60}")


# ═══════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="AIQuant 全量历史量价修正工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python tools/fix_history.py                     # 全量修正（腾讯源）
  python tools/fix_history.py --codes 600519      # 修正指定股票
  python tools/fix_history.py --dry-run           # 只检测
  python tools/fix_history.py --source both       # 双源交叉
  python tools/fix_history.py --schedule          # 每天10:00定时运行
  python tools/fix_history.py --schedule --time 09:30  # 每天09:30运行
  python tools/fix_history.py --reset             # 重置进度
  python tools/fix_history.py --report            # 查看报告""")
    parser.add_argument("--codes", nargs="+", help="指定股票代码")
    parser.add_argument("--source", choices=["em", "tx", "both"], default="tx",
                        help="数据源(默认 tx)")
    parser.add_argument("--threads", type=int, default=2, help="并发数(默认 2)")
    parser.add_argument("--rate", type=float, default=1.0, help="限速秒/只(默认 1.0)")
    parser.add_argument("--dry-run", action="store_true", help="只检测不写入")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细输出")
    parser.add_argument("--reset", action="store_true", help="重置进度")
    parser.add_argument("--report", action="store_true", help="查看报告")
    parser.add_argument("--schedule", action="store_true", help="启用定时调度模式")
    parser.add_argument("--time", default="10:00", help="定时运行时间 HH:MM (默认 10:00)")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.reset:
        reset_progress()
        return
    if args.report:
        show_report()
        return
    
    if args.schedule:
        run_scheduled(args.time, args.source, args.threads, args.rate, args.verbose)
        return

    run_fix_history(
        codes=args.codes, source=args.source,
        threads=args.threads, dry_run=args.dry_run,
        verbose=args.verbose, rate_limit=args.rate,
    )


def run_scheduled(run_time: str, source: str, threads: int, rate: float, verbose: bool):
    """定时调度模式：每天指定时间自动运行全量修正"""
    import schedule
    import signal
    
    def job():
        logger.info(f"[定时任务] 开始执行全量历史修正 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        try:
            run_fix_history(
                codes=None,
                source=source,
                threads=threads,
                dry_run=False,
                verbose=verbose,
                rate_limit=rate,
            )
            logger.info(f"[定时任务] 执行完成 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        except Exception as e:
            logger.error(f"[定时任务] 执行失败: {e}")
    
    # 设置定时任务
    schedule.every().day.at(run_time).do(job)
    
    logger.info(f"定时调度模式已启动，每天 {run_time} 自动执行全量修正")
    logger.info("按 Ctrl+C 停止")
    
    # 优雅退出
    def signal_handler(sig, frame):
        logger.info("收到停止信号，正在退出...")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 主循环
    try:
        while True:
            schedule.run_pending()
            time.sleep(60)  # 每分钟检查一次
    except KeyboardInterrupt:
        logger.info("调度器已停止")


if __name__ == "__main__":
    main()
