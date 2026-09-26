"""
AIQuant settings —— 个人股票评分系统配置
"""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── 数据同步 ──
HISTORY_START = "2024-01-01"
SYNC_THREADS = 4
SYNC_BATCH_SIZE = 100
SYNC_RETRY_COUNT = 2
SYNC_TIMEOUT = 30

# ── 数据库 ──
PRODUCTION_DB_PATH = os.path.join(BASE_DIR, "core", "quant.db")
DB_PATH = os.path.abspath(os.getenv("AIQUANT_DB_PATH", PRODUCTION_DB_PATH))
TESTING = os.getenv("AIQUANT_TESTING", "0") == "1"
SCHEDULER_ENABLED = not TESTING and os.getenv("AIQUANT_SCHEDULER_ENABLED", "1") == "1"
AUTO_SYNC_ENABLED = not TESTING and os.getenv("AIQUANT_AUTO_SYNC_ENABLED", "1") == "1"
QLIB_ENABLED = not TESTING and os.getenv("AIQUANT_QLIB_ENABLED", "0") == "1"
SIGNAL_MODEL_MODE = os.getenv("SIGNAL_MODEL_MODE", "legacy")
if SIGNAL_MODEL_MODE not in {"legacy", "shadow", "v2"}:
    raise ValueError("SIGNAL_MODEL_MODE 必须为 legacy、shadow 或 v2")


def require_signal_model_ready(mode=None):
    """正式切换须完成闭环与旁路验收；开发期间拒绝半接线的 v2 运行。"""
    if (mode or SIGNAL_MODEL_MODE) == "v2":
        raise RuntimeError("v2 尚未完成发布、交易与任务闭环验收，请保持 legacy 或 shadow")


def install_test_guards():
    """测试进程及其子进程禁止生产库连接和外网访问，覆盖直接 sqlite/socket 调用。"""
    import sys
    from pathlib import Path
    from urllib.parse import unquote, urlsplit

    if not TESTING or getattr(sys, "_aiquant_test_guards", False):
        return
    sys._aiquant_test_guards = True

    def guard(event, args):
        if event == "sqlite3.connect":
            target = str(args[0])
            if target == ":memory:" or target.startswith("file::memory:"):
                return
            if target.startswith("file:"):
                target = unquote(urlsplit(target).path)
                if os.name == "nt" and target.startswith("/"):
                    target = target[1:]
            path = Path(target).resolve()
            root = os.getenv("AIQUANT_TEST_ROOT")
            if path == Path(PRODUCTION_DB_PATH).resolve() or not root or not path.is_relative_to(Path(root).resolve()):
                raise RuntimeError(f"测试禁止连接非隔离数据库：{path}")
        elif event in {"socket.connect", "socket.getaddrinfo"}:
            address = args[1] if event == "socket.connect" else args[0]
            host = address[0] if isinstance(address, tuple) else address
            if host not in {"localhost", "127.0.0.1", "::1", None}:
                raise RuntimeError(f"测试禁止外网请求：{host}")

    sys.addaudithook(guard)


install_test_guards()

# ── Flask ──
FLASK_HOST = "0.0.0.0"
FLASK_PORT = 5000
FLASK_DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"

# ── 策略挖掘 ──
MINING = {
    "ic_lookback_days": 60,
    "ic_forward_days": 5,
    "ic_min_threshold": 0.03,
    "candidate_thresholds": [0.3, 0.5, 0.7, 0.85],
    "sample_stocks": 300,
    "top_n_rules": 50,
    "min_trades": 5,
}

# ── 回测 ──
BACKTEST = {
    "account": 1_000_000,
    "benchmark": "SH000300",
    "deal_price": "close",
    "open_cost": 0.0005,
    "close_cost": 0.0015,
    "min_cost": 5.0,
    "topk": 30,
    "n_drop": 5,
}
