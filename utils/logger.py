"""
utils/logger.py —— 统一日志初始化
==================================
- 控制台：INFO 级别，彩色简洁输出
- 文件：DEBUG 级别，按日轮转（保留 14 天）
- 所有模块统一使用 logging.getLogger(__name__) 获取 logger

用法：
  # 在 app.py 启动时调用一次
  from utils.logger import setup_logging
  setup_logging()

  # 各模块中
  import logging
  logger = logging.getLogger(__name__)
  logger.info("同步完成: %d 行", rows)
"""
import os
import logging
from logging.handlers import TimedRotatingFileHandler

_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
_LOG_FILE = "aiquant.log"
_RETENTION_DAYS = 14


def setup_logging(level: int = logging.INFO):
    """初始化全局日志配置（仅需在入口调用一次）。

    - 控制台：INFO+，简洁格式
    - 文件 logs/aiquant.log：DEBUG+，按日轮转，保留 14 天
    """
    os.makedirs(_LOG_DIR, exist_ok=True)
    log_path = os.path.join(_LOG_DIR, _LOG_FILE)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # 避免重复添加 handler（Flask reloader 等场景）
    if root.handlers:
        return

    # 控制台 handler
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    root.addHandler(console)

    # 文件 handler（按日轮转）
    file_handler = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        interval=1,
        backupCount=_RETENTION_DAYS,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s (%(filename)s:%(lineno)d): %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(file_handler)

    # 降低第三方库噪音
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
