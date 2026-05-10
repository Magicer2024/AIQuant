"""
routes/ —— Flask 蓝图路由模块
"""
from flask import Blueprint

system_bp = Blueprint("system", __name__, url_prefix="/api/system")
stock_bp = Blueprint("stock", __name__, url_prefix="/api/stock")
backtest_bp = Blueprint("backtest", __name__, url_prefix="/api/backtest")
strategy_bp = Blueprint("strategy", __name__, url_prefix="/api/strategies")
position_bp = Blueprint("position", __name__, url_prefix="/api/position")
signal_bp = Blueprint("signal", __name__, url_prefix="/api/signals")
trade_bp = Blueprint("trade", __name__, url_prefix="/api/trades")
account_bp = Blueprint("account", __name__, url_prefix="/api/account")
optimizer_bp = Blueprint("optimizer", __name__, url_prefix="/api/optimizer")
sync_bp = Blueprint("sync", __name__, url_prefix="/api/sync")
strategy_lab_bp = Blueprint("strategy_lab", __name__, url_prefix="/api/strategy-lab")
