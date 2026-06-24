"""
routes/ -- Flask Blueprint route modules
"""
from flask import Blueprint

system_bp = Blueprint("system", __name__, url_prefix="/api/system")
sync_bp = Blueprint("sync", __name__, url_prefix="/api/sync")

# 注：scoring / strategy / backtest 蓝图在各自模块里直接定义并导出，
# 由 app.py 统一注册。本文件只放共用的最小蓝图。
