"""
config/settings.py —— 基础配置
"""
import os

# 数据库
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "core", "quant.db")

# Flask API
API_HOST = "0.0.0.0"
API_PORT = 5000
API_DEBUG = False

# 定时任务（24小时制）
SCHEDULE_SYNC_TIME = "07:00"
SCHEDULE_SCAN_TIME = "09:00"

# 数据同步
SYNC_BATCH_SIZE = 100
SYNC_VERBOSE = False

# 日志
LOG_LEVEL = "INFO"
