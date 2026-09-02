"""tools/backfill_deep_scan.py —— 回补历史扫描日 + 补建个股深度跟踪单（独立进程）

为什么必须是独立脚本
────────────────────
回补 N 天要对 N 个历史交易日各跑一次全市场深析扫描（4500+ 只 × 全套指标），
单进程约 65ms/只 → 全市场一天 ~5 分钟 → 30 天就是 2.4 小时，Flask 请求/线程里
既跑不完也会超时。有效解法只有多进程（实测 8 进程提速 3.3x），但 Windows 的
spawn 模式会重新导入主模块，在 Flask 进程内起 ProcessPoolExecutor 会导致应用
被重复启动。所以这里做成独立脚本，由 Flask 用 subprocess 拉起，脚本内部用
进程池，进度写到 JSON 文件供前端轮询。

正确性前提
──────────
每天扫描都传 as_of=该交易日，指标只由**当天及之前**的行情算出。若不截断，
等于让每一天都预知未来（look-ahead bias），回补出的胜率/收益全部虚高、不可用。

用法
────
  python tools/backfill_deep_scan.py --days 30
  python tools/backfill_deep_scan.py --start 2026-06-01 --end 2026-08-28 --procs 8
  python tools/backfill_deep_scan.py --days 60 --progress-file data_cache/xxx.json
  python tools/backfill_deep_scan.py --days 30 --dry-run     # 只报计划不执行
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import DB_PATH  # noqa: E402
from strategy.stock_deep import _scan_one, _ensure_market_signal_table, _insert_market_signals  # noqa: E402

LOOKBACK = 180
DEFAULT_PROCS = 8
# 分片数 = 进程数 × 该系数，片更细便于负载均衡（避免某片都是慢股票拖尾巴）
CHUNKS_PER_PROC = 4


# ─────────────────────────────────────────────
# 子进程工作函数（必须模块顶层，才能被 spawn pickle）
# ─────────────────────────────────────────────
def _scan_chunk(args):
    """一个进程处理一批股票。返回候选列表。

    必须在 __main__ 保护下由进程池调用；每片内部串行，片间并行。
    """
    as_of, codes = args
    out = []
    for code in codes:
        r = _scan_one(code, LOOKBACK, as_of)
        if r:
            out.append(r)
    return out


def _chunks(seq, n):
    n = max(1, min(n, len(seq)))
    size, rem = divmod(len(seq), n)
    res, i = [], 0
    for k in range(n):
        take = size + (1 if k < rem else 0)
        if take:
            res.append(seq[i:i + take])
            i += take
    return res


# ─────────────────────────────────────────────
# 进度文件（供 Flask 轮询）
# ─────────────────────────────────────────────
class Progress:
    def __init__(self, path: str):
        self.path = path
        self.data = {
            "status": "running", "progress": 0, "message": "初始化",
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": None, "result": None, "error": None,
            "scanned_dates": [],
        }
        self.write()

    def write(self, **kw):
        self.data.update(kw)
        self.data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)      # 原子替换，避免前端读到半截文件
        except Exception:
            pass

    def fail(self, msg: str):
        self.write(status="error", error=msg, message="失败：" + msg)

    def done(self, result: dict):
        self.write(status="success", progress=100, message="完成", result=result)


def _log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────
def get_target_dates(conn, days=None, start=None, end=None):
    """取目标区间内的交易日（倒序取最近 days 个，或按 start/end 区间）。"""
    sql = "SELECT DISTINCT trade_date FROM daily_price WHERE 1=1"
    args = []
    if start:
        sql += " AND trade_date >= ?"
        args.append(start)
    if end:
        sql += " AND trade_date <= ?"
        args.append(end)
    sql += " ORDER BY trade_date DESC"
    if days:
        sql += " LIMIT ?"
        args.append(int(days))
    rows = [r[0] for r in conn.execute(sql, args).fetchall()]
    rows.reverse()          # 从老到新，保证出场推进顺序正确
    return rows


def scan_one_day(conn, scan_date: str, codes: list, ex, procs: int,
                 on_batch=None) -> dict:
    """扫描单个交易日并把全部候选落库。ex 为进程池（None 时串行）。

    ⚠ 这里**不做 top_n 截断**：daily_top_n 的截断由 deep_tracker.sync_from_scan 负责
    （取 top_n 建跟踪单）。与每日盘后 run_full_market_scan 的口径保持一致 ——
    现有 6 天数据每天就是全量 1000+ 行，若在这里截断会让回补数据与既有数据口径分裂。

    on_batch(done, total)：每完成一个分片回调一次。这里必须用 as_completed 而不是
    ex.map —— ex.map 会阻塞到整批结束，导致一天（约 45 秒）内进度条纹丝不动，
    用户会以为卡死；分片回调让进度条能平滑推进。
    """
    _ensure_market_signal_table(conn)
    conn.execute("DELETE FROM stock_deep_signal WHERE scan_date = ?", (scan_date,))
    conn.commit()

    t0 = time.time()
    n_chunks = 1 if ex is None else max(1, CHUNKS_PER_PROC * procs)
    parts = _chunks(codes, n_chunks)
    cands = []

    if ex is None:
        for i, p in enumerate(parts):
            try:
                cands.extend(_scan_chunk((scan_date, p)) or [])
            except Exception:
                pass
            if on_batch:
                on_batch(i + 1, len(parts))
    else:
        from concurrent.futures import as_completed
        futs = [ex.submit(_scan_chunk, (scan_date, p)) for p in parts]
        done = 0
        for fu in as_completed(futs):
            try:
                cands.extend(fu.result() or [])
            except Exception:
                pass
            done += 1
            if on_batch:
                on_batch(done, len(futs))

    if cands:
        _insert_market_signals(conn, scan_date, cands)
        conn.commit()
    return {"scan_date": scan_date, "candidates": len(cands),
            "elapsed_s": round(time.time() - t0, 1)}


def main():
    ap = argparse.ArgumentParser(description="回补历史深析扫描日 + 补建跟踪单")
    ap.add_argument("--days", type=int, default=30, help="回补最近 N 个交易日（默认 30）")
    ap.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD（与 --days 二选一）")
    ap.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD")
    ap.add_argument("--procs", type=int, default=DEFAULT_PROCS, help=f"进程数（默认 {DEFAULT_PROCS}，1=串行）")
    ap.add_argument("--progress-file", default=None, help="进度 JSON 文件路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不执行")
    args = ap.parse_args()

    prog_path = args.progress_file or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data_cache", "backfill_deep_scan.json")
    prog = Progress(prog_path)
    t_start = time.time()

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        dates = get_target_dates(conn, days=None if args.start else args.days,
                                 start=args.start, end=args.end)
        if not dates:
            prog.fail("目标区间内没有交易日")
            _log("没有可回补的交易日")
            return 1

        # 已存在扫描结果的日期跳过（重跑安全）
        have = {r[0] for r in conn.execute(
            "SELECT DISTINCT scan_date FROM stock_deep_signal").fetchall()}
        todo = [d for d in dates if d not in have]
        codes = [r["code"] for r in conn.execute(
            """
            SELECT DISTINCT d.code FROM daily_price d
            JOIN stock_info i ON i.code = d.code
            WHERE i.is_active = 1 ORDER BY d.code
            """).fetchall()]

        _log(f"目标 {len(dates)} 个交易日，已有扫描 {len(have)} 天，待扫 {len(todo)} 天，"
             f"股票池 {len(codes)} 只，进程数 {args.procs}")
        if args.dry_run:
            est = len(todo) * len(codes) * 0.020 / max(1, args.procs)
            _log(f"[dry-run] 预估耗时 {est/60:.1f} 分钟（按 20ms/只、{args.procs} 进程）")
            prog.done({"dry_run": True, "todo": len(todo), "est_minutes": round(est / 60, 1)})
            return 0

        if not todo:
            _log("所有日期已有扫描结果，直接进入建单阶段")

        # 进程池只创建一次，所有日期复用（避免每天付一次 spawn 开销）
        from concurrent.futures import ProcessPoolExecutor
        ex = ProcessPoolExecutor(max_workers=args.procs) if args.procs > 1 else None
        scan_res = []
        try:
            n_todo = max(1, len(todo))
            for i, d in enumerate(todo):
                prog.write(progress=round(i * 90 / n_todo, 1),
                           message=f"扫描 {d}（{i+1}/{len(todo)}）")

                # 扫描占 0~90%，建单/推进占剩余 10%；当天内部再按分片细分，
                # 避免一整天（约 45 秒）里进度条完全静止。
                def _on_batch(done, total, _i=i, _d=d):
                    frac = (_i + (done / total if total else 1)) / n_todo
                    prog.write(progress=round(frac * 90, 1),
                               message=f"扫描 {_d}（{_i+1}/{len(todo)}）· {done}/{total} 批")

                r = scan_one_day(conn, d, codes, ex, args.procs, on_batch=_on_batch)
                scan_res.append(r)
                prog.data["scanned_dates"].append(d)
                _log(f"扫描 {d}：候选 {r['candidates']}，{r['elapsed_s']}s")
        finally:
            if ex is not None:
                ex.shutdown(wait=True)

        # 建单 + 推进出场（与 deep_tracker.backfill 同口径）
        from strategy.deep_tracker import sync_from_scan, update_open_tracks
        prog.write(progress=92, message="补建跟踪单…")
        added = skipped = 0
        for d in dates:
            try:
                s = sync_from_scan(conn, d)
                added += s["added"]
                skipped += s["skipped"]
            except Exception as e:
                _log(f"建单失败 {d}: {e}")
        prog.write(progress=96, message="推进出场结算…")
        u = update_open_tracks(conn, limit=5000)

        result = {
            "days": len(dates), "scanned": len(todo),
            "added": added, "skipped": skipped,
            "filled": u["filled"], "expired": u["expired"], "closed": u["closed"],
            "holding": u["holding"], "watching": u["watching"],
            "errors": u.get("errors", 0),
            "elapsed_s": round(time.time() - t_start, 1),
        }
        prog.done(result)
        _log(f"完成：扫描 {len(todo)} 天，新增 {added} 单，建仓 {u['filled']}，"
             f"出场 {u['closed']}，异常 {u.get('errors', 0)}")
        return 0
    except Exception as e:
        import traceback
        traceback.print_exc()
        prog.fail(f"{type(e).__name__}: {e}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
