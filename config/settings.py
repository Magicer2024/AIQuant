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
DB_PATH = os.path.join(BASE_DIR, "core", "quant.db")

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
