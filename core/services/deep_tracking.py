"""
core/services/deep_tracking.py —— 深析推荐跟踪编排（T7 从 routes/investor.py 下沉）
=================================================================================

- 跟踪列表/统计（strategy.deep_tracker）
- 手动推进 / 历史跟踪单回补
- 历史扫描日回补的后台独立进程拉起与进度文件读写
- 手动平仓 / 删除跟踪单
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime

from core.db import get_conn
from utils.serialization import sanitize_numeric


class DeepTrackError(Exception):
    """业务校验/状态错误；status 为对应 HTTP 状态码（400/404/409/500）。"""

    def __init__(self, msg: str, status: int = 500):
        super().__init__(msg)
        self.msg = msg
        self.status = status


# 历史扫描日回补脚本与进度文件（项目根/tools、data_cache 下）
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BACKFILL_SCRIPT = os.path.join(_ROOT, "tools", "backfill_deep_scan.py")
_BACKFILL_PROGRESS = os.path.join(_ROOT, "data_cache", "backfill_deep_scan.json")


def list_payload(status: str | None, limit: int, days: int | None) -> dict:
    """跟踪列表 + 汇总统计。

    total 为**符合当前 status 过滤**的总笔数（不是返回条数），前端据此显示
    「加载更多」；days 只影响统计回溯范围，不影响列表。
    """
    from strategy.deep_tracker import ensure_table, get_tracks, get_stats
    with get_conn() as conn:
        ensure_table(conn)
        items = get_tracks(conn, status=status, limit=limit)
        stats = get_stats(conn, days=days)
        last = conn.execute(
            "SELECT MAX(updated_at) AS t FROM deep_track").fetchone()
        # 与 get_tracks 相同的过滤条件，统计「符合条件」的总数
        where, args = "", []
        if status:
            keys = [s.strip() for s in str(status).split(",") if s.strip()]
            if keys:
                where = "WHERE status IN ({})".format(",".join("?" * len(keys)))
                args.extend(keys)
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM deep_track {where}", args).fetchone()["c"]
    return sanitize_numeric({
        "items": items,
        "stats": stats,
        "updated_at": (last["t"] if last else None),
        "total": int(total or 0),
    })


def refresh_payload(backfill_days: int) -> dict:
    """手动推进一次跟踪；backfill_days>0 时改为对最近 N 个扫描日补建跟踪单。"""
    from strategy.deep_tracker import refresh, backfill
    with get_conn() as conn:
        if backfill_days > 0:
            res = backfill(conn, days=min(backfill_days, 180))
            res["mode"] = "backfill"
        else:
            res = refresh(conn)
            res["mode"] = "refresh"
    return sanitize_numeric(res)


def read_backfill_progress() -> dict:
    """读取扫描日回补进度文件；文件不存在/损坏时返回 idle 状态。"""
    if not os.path.exists(_BACKFILL_PROGRESS):
        return {"status": "idle", "progress": 0, "message": "尚未运行"}
    try:
        with open(_BACKFILL_PROGRESS, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"status": "idle", "progress": 0, "message": "进度文件读取失败"}


def _write_progress_atomic(data: dict) -> None:
    """原子写进度文件（临时文件 + os.replace），避免前端读到写了一半的 JSON。"""
    os.makedirs(os.path.dirname(_BACKFILL_PROGRESS), exist_ok=True)
    tmp = _BACKFILL_PROGRESS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, _BACKFILL_PROGRESS)


def start_backfill(days: int, procs: int) -> dict:
    """启动「历史扫描日回补」后台独立进程（接口立即返回）。

    days 回补最近 N 个交易日（1~180）；procs 并行进程数（1~16）。
    独立进程原因：回补一天要对 4500+ 只股票跑全套指标，单进程约 5 分钟/天，
    不可能放在 Flask 请求里；脚本多进程、每日传 as_of 杜绝未来函数。
    """
    cur = read_backfill_progress()
    if cur.get("status") == "running":
        raise DeepTrackError(
            f"回补任务正在运行中（{cur.get('message') or ''}），请等待完成", 409)

    if not os.path.exists(_BACKFILL_SCRIPT):
        raise DeepTrackError(f"回补脚本不存在: {_BACKFILL_SCRIPT}", 500)

    os.makedirs(os.path.dirname(_BACKFILL_PROGRESS), exist_ok=True)

    # 先落 running 再拉进程：进程 spawn 需要几秒，期间轮询若读到上一次残留的
    # success/error 会误判本轮已结束（竞态消除）。
    _write_progress_atomic({
        "status": "running", "progress": 0, "message": "正在启动…",
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "result": None, "error": None, "scanned_dates": [],
    })

    kwargs = {"cwd": _ROOT}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    subprocess.Popen(
        [sys.executable, _BACKFILL_SCRIPT,
         "--days", str(days), "--procs", str(procs),
         "--progress-file", _BACKFILL_PROGRESS],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    return {"started": True, "days": days, "procs": procs,
            "message": f"已启动回补（{days} 个交易日），请在下方查看进度"}


def close_payload(tid: int, price) -> dict:
    """手动平仓：price 为空时用最新收盘价；deep_tracker 抛 ValueError → 400。"""
    from strategy.deep_tracker import close_track
    with get_conn() as conn:
        res = close_track(
            conn, tid,
            price=float(price) if price not in (None, "") else None,
        )
    return sanitize_numeric(res)


def delete_payload(tid: int) -> dict:
    """删除一笔跟踪单（不进入收益统计）；不存在抛 404。"""
    from strategy.deep_tracker import delete_track
    with get_conn() as conn:
        deleted = delete_track(conn, tid)
    if not deleted:
        raise DeepTrackError("跟踪单不存在", 404)
    return {"deleted": tid}
