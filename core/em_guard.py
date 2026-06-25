"""
core/em_guard.py —— 东方财富接口调用护栏
========================================
解决：每次打开首页都请求东财接口导致被反爬封 IP

三道防线：
  1. 持久化缓存（SQLite）   按 (接口名, 参数hash) 缓存响应，TTL 内直接返回
  2. 频次限制（滑动窗口）   任意接口 N 分钟内最多 M 次调用
  3. 每日配额               每类接口每天最多 K 次调用

接口语义：
  - cached_fetch(name, params, fetcher, ttl=120)
      name     接口标识，如 "realtime_all" / "kline" / "klines_by_date"
      params   dict，参数会参与缓存 key
      fetcher  实际拉数据的函数 () -> Any（支持 DataFrame / dict）
      ttl      缓存有效期（秒），默认 120s
      返回 (data, source) 其中 source ∈ {"cache", "fresh", "stale", "fallback"}

  - guard_status() -> dict
      返回当前所有接口的：最后调用时间、缓存命中数、剩余配额

设计原则：
  - 缓存命中：0 次网络请求
  - 频次超限：返回 stale 缓存（旧的，但能用） + 标记
  - 配额用尽：直接返回 stale 缓存，且不再发请求
"""
from __future__ import annotations

import json
import sqlite3
import hashlib
import threading
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Any

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 护栏数据库（独立 SQLite，避免与业务库耦合）
# ─────────────────────────────────────────────
_DB_PATH = Path(__file__).parent.parent / "data_cache" / "em_guard.db"
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_guard_lock = threading.RLock()  # 保护护栏内存状态


def _ensure_db() -> None:
    """建表（首次调用时）"""
    with sqlite3.connect(_DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS em_cache (
                cache_key    TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                params_json  TEXT NOT NULL,
                payload      BLOB NOT NULL,        -- 序列化后的数据
                payload_type TEXT NOT NULL,        -- 'dataframe' / 'json'
                created_at   REAL NOT NULL,        -- 时间戳
                expires_at   REAL NOT NULL,        -- 时间戳
                size_bytes   INTEGER DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_emc_name ON em_cache(name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_emc_exp  ON em_cache(expires_at)")

        # 调用日志（用于频次限制 + 每日配额）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS em_calls (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT NOT NULL,
                at        REAL NOT NULL,           -- 时间戳
                cache_hit INTEGER DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_emc_name_at ON em_calls(name, at)")


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(_DB_PATH, timeout=10)


# ─────────────────────────────────────────────
# 配额配置：每类接口的窗口限制
# ─────────────────────────────────────────────
# 经验值：东财单 IP 半小时内同类请求 ≤ 10 次比较安全
# 缓存命中不计配额
GUARD_CONFIG: dict[str, dict] = {
    # name: {"window_sec": 1800, "max_in_window": 10, "daily_quota": 50, "default_ttl": 120}
    "realtime_all":     {"window_sec": 1800, "max_in_window": 10, "daily_quota": 50,  "default_ttl": 120},  # 盘前/盘后全 A 最新价
    "klines_by_date":   {"window_sec": 1800, "max_in_window": 8,  "daily_quota": 40,  "default_ttl": 86400},  # 按日拉全 A 1 天数据
    "kline":            {"window_sec": 60,   "max_in_window": 30, "daily_quota": 1000,"default_ttl": 86400},  # 单只 K 线（高并发，限紧）
    "stock_zh_a_spot":  {"window_sec": 1800, "max_in_window": 10, "daily_quota": 50,  "default_ttl": 120},
}

# 旧缓存最大保留时间（防止积压）：24 小时
_STALE_TTL = 86400


# ─────────────────────────────────────────────
# 缓存 key 生成
# ─────────────────────────────────────────────
def _make_key(name: str, params: dict) -> str:
    """生成稳定 hash key（参数排序后序列化）"""
    # 过滤掉 _= 类的随机字段，避免每次 key 都不同
    clean = {k: v for k, v in (params or {}).items() if k not in ("_", "timestamp")}
    blob = json.dumps(clean, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.md5(f"{name}|{blob}".encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────
# 频次限制 + 配额查询
# ─────────────────────────────────────────────
def _recent_call_count(name: str, since_ts: float) -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM em_calls WHERE name=? AND at >= ?",
            (name, since_ts),
        ).fetchone()
        return int(row[0] or 0)


def _today_call_count(name: str) -> int:
    """今日 0 点至今的调用次数（仅计非 cache_hit）"""
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM em_calls WHERE name=? AND at >= ? AND cache_hit=0",
            (name, midnight),
        ).fetchone()
        return int(row[0] or 0)


def _log_call(name: str, cache_hit: bool) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO em_calls (name, at, cache_hit) VALUES (?, ?, ?)",
            (name, time.time(), 1 if cache_hit else 0),
        )


# ─────────────────────────────────────────────
# 缓存读写
# ─────────────────────────────────────────────
def _read_cache(key: str) -> tuple[Any, str] | None:
    """
    读缓存
      返回 (payload, level) 其中 level ∈ {"fresh", "stale"}
      level=fresh:  未过期，可直接用
      level=stale:  已过期，但还能兜底用（避免被反爬）
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT payload, payload_type, expires_at, created_at FROM em_cache WHERE cache_key=?",
            (key,),
        ).fetchone()
    if not row:
        return None

    payload_blob, payload_type, expires_at, created_at = row
    now = time.time()
    level = "fresh" if now < expires_at else "stale"

    if payload_type == "dataframe":
        import pickle
        data = pickle.loads(payload_blob)
    elif payload_type == "json":
        data = json.loads(payload_blob.decode("utf-8"))
    else:
        data = payload_blob

    return data, level


def _write_cache(key: str, name: str, params: dict, data: Any, ttl: int) -> None:
    """写缓存（DataFrame → pickle，dict → json）"""
    now = time.time()
    if isinstance(data, type(_import_pandas_df())):
        import pickle
        payload_blob = pickle.dumps(data)
        payload_type = "dataframe"
    else:
        payload_blob = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        payload_type = "json"

    with _conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO em_cache
              (cache_key, name, params_json, payload, payload_type, created_at, expires_at, size_bytes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            key, name, json.dumps(params, ensure_ascii=False, default=str),
            payload_blob, payload_type, now, now + ttl, len(payload_blob),
        ))


def _import_pandas_df():
    """懒加载 pandas（避免模块导入时强依赖）"""
    import pandas as pd
    return pd.DataFrame()


# ─────────────────────────────────────────────
# 核心：cached_fetch
# ─────────────────────────────────────────────
def cached_fetch(name: str, params: dict, fetcher: Callable[[], Any],
                 ttl: int | None = None) -> tuple[Any, str]:
    """
    带护栏的东财接口调用

    :return: (data, source)
        source 含义：
          "fresh"     缓存命中（TTL 内）
          "network"   实际发起了网络请求
          "stale"     频次/配额超限，返回的是旧缓存
          "empty"     没有任何数据可用（缓存和网络都失败）

    决策流程：
      1. 有 fresh 缓存 → 直接返回（不消耗配额）
      2. 无缓存 / 缓存过期 → 检查配额
         a. 配额允许 → 调 fetcher，缓存结果，记录调用
         b. 配额超限 → 返回 stale 缓存（兜底，不发请求）
      3. fetcher 抛错 → 返回 stale 缓存（兜底）
    """
    cfg = GUARD_CONFIG.get(name, {"window_sec": 1800, "max_in_window": 10, "default_ttl": 120})
    if ttl is None:
        ttl = cfg.get("default_ttl", 120)

    key = _make_key(name, params)
    _ensure_db()

    # 1) 尝试 fresh 缓存
    cached = _read_cache(key)
    if cached is not None:
        data, level = cached
        if level == "fresh":
            _log_call(name, cache_hit=True)
            logger.debug("[guard] %s cache HIT, 0 网络请求", name)
            return data, "fresh"

    # 2) 配额检查
    now = time.time()
    window_ago = now - cfg.get("window_sec", 1800)
    in_window = _recent_call_count(name, window_ago)
    today_n = _today_call_count(name)
    max_window = cfg.get("max_in_window", 10)
    daily_quota = cfg.get("daily_quota", 50)

    allow = (in_window < max_window) and (today_n < daily_quota)
    if not allow:
        if cached is not None:
            data, _ = cached
            logger.warning(
                "[guard] %s 配额超限 (窗口 %d/%d, 今日 %d/%d)，返回 stale 缓存",
                name, in_window, max_window, today_n, daily_quota,
            )
            return data, "stale"
        # 没缓存可用：返回空 + 标记
        logger.error("[guard] %s 配额超限且无缓存，放弃", name)
        return _empty_payload(params), "empty"

    # 3) 实际调接口
    try:
        data = fetcher()
    except Exception as e:
        logger.error("[guard] %s fetcher 失败: %s", name, e)
        if cached is not None:
            data, _ = cached
            return data, "stale"
        return _empty_payload(params), "empty"

    # 4) 写缓存
    if data is not None and not _is_empty(data):
        _write_cache(key, name, params, data, ttl)
        _log_call(name, cache_hit=False)
        return data, "network"

    # fetcher 返回空（不写缓存）
    _log_call(name, cache_hit=False)
    if cached is not None:
        data, _ = cached
        return data, "stale"
    return data, "network"


def _is_empty(data: Any) -> bool:
    """判断数据是否为空（DataFrame 空 / dict 空）"""
    if data is None:
        return True
    if hasattr(data, "empty"):  # DataFrame
        return bool(data.empty)
    if isinstance(data, (list, dict, str)):
        return len(data) == 0
    return False


def _empty_payload(params: dict) -> Any:
    """构造空 payload（与调用方类型匹配）"""
    return None


# ─────────────────────────────────────────────
# 清理过期缓存
# ─────────────────────────────────────────────
def cleanup_expired(max_age: int = _STALE_TTL) -> int:
    """清理超过 max_age 秒的旧缓存，返回清理条数"""
    cutoff = time.time() - max_age
    with _conn() as conn:
        cur = conn.execute("DELETE FROM em_cache WHERE created_at < ?", (cutoff,))
        return cur.rowcount


# ─────────────────────────────────────────────
# 状态查询
# ─────────────────────────────────────────────
def guard_status() -> dict:
    """返回所有接口的护栏状态（供前端展示）"""
    _ensure_db()
    now = time.time()
    status = {}

    for name, cfg in GUARD_CONFIG.items():
        window_ago = now - cfg.get("window_sec", 1800)
        in_window = _recent_call_count(name, window_ago)
        today_n = _today_call_count(name)
        max_window = cfg.get("max_in_window", 10)
        daily_quota = cfg.get("daily_quota", 50)

        with _conn() as conn:
            last_row = conn.execute(
                "SELECT MAX(at) FROM em_calls WHERE name=?", (name,),
            ).fetchone()
            last_at = float(last_row[0]) if last_row and last_row[0] else None

            # 缓存条数
            n_cache = conn.execute(
                "SELECT COUNT(*) FROM em_cache WHERE name=? AND expires_at > ?",
                (name, now),
            ).fetchone()[0]

        status[name] = {
            "in_window":     in_window,
            "max_window":    max_window,
            "today":         today_n,
            "daily_quota":   daily_quota,
            "last_call_at":  datetime.fromtimestamp(last_at).strftime("%Y-%m-%d %H:%M:%S") if last_at else None,
            "cached_count":  n_cache,
            "remaining_window": max(0, max_window - in_window),
            "remaining_today":  max(0, daily_quota - today_n),
        }
    return status


def force_clear_cache(name: str | None = None) -> int:
    """清空缓存：name=None 清空全部"""
    _ensure_db()
    with _conn() as conn:
        if name is None:
            cur = conn.execute("DELETE FROM em_cache")
        else:
            cur = conn.execute("DELETE FROM em_cache WHERE name=?", (name,))
        return cur.rowcount
