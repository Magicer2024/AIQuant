"""
Flask 后端 API
提供数据接口给前端可视化界面
"""
from flask import Flask, jsonify, request, send_from_directory, redirect
from flask_cors import CORS
from flask_sock import Sock

from routes.system import system_bp
from routes.stock import stock_bp
from routes.backtest import backtest_bp
from routes.strategy import strategy_bp
from routes.signal import signal_bp
from routes.position import position_bp
from routes.trade import trade_bp
from routes.account import account_bp
from routes.optimizer import optimizer_bp
from routes.sync import sync_bp
from routes.agents import agents_bp
from routes.risk import risk_bp
from routes.strategy_config import strategy_config_bp
from routes.audit import audit_bp
from routes.governance import governance_bp
from routes.data_source import data_source_bp
from routes.backtest_new import backtest_new_bp
from routes.market_ws import market_ws_bp, register_ws_routes
from routes.scoring import scoring_bp
from routes.trade_execution import trade_exec_bp
from routes.reports import reports_bp
from routes.live import live_bp
from routes.intent import intent_bp
from routes.tasks import tasks_bp
from routes.ai import ai_bp
from routes.broker import broker_bp
from routes.llm import llm_bp
from routes.deployment import deployment_bp
from routes.data_fetch import data_fetch_bp
from routes.chart import chart_bp
from routes.strategy_lab import strategy_lab_bp

app = Flask(__name__)
CORS(app)
sock = Sock(app)

# 注册所有 Blueprint
app.register_blueprint(system_bp)
app.register_blueprint(stock_bp)
app.register_blueprint(backtest_bp)
app.register_blueprint(strategy_bp)
app.register_blueprint(signal_bp)
app.register_blueprint(position_bp)
app.register_blueprint(trade_bp)
app.register_blueprint(account_bp)
app.register_blueprint(optimizer_bp)
app.register_blueprint(sync_bp)
app.register_blueprint(agents_bp)
app.register_blueprint(risk_bp)
app.register_blueprint(strategy_config_bp)
app.register_blueprint(audit_bp)
app.register_blueprint(governance_bp)
app.register_blueprint(data_source_bp)
app.register_blueprint(backtest_new_bp)
app.register_blueprint(market_ws_bp)
app.register_blueprint(scoring_bp)
app.register_blueprint(trade_exec_bp)
app.register_blueprint(reports_bp)
app.register_blueprint(live_bp)
app.register_blueprint(intent_bp)
app.register_blueprint(tasks_bp)
app.register_blueprint(ai_bp)
app.register_blueprint(broker_bp)
app.register_blueprint(llm_bp)
app.register_blueprint(deployment_bp)
app.register_blueprint(data_fetch_bp)
app.register_blueprint(chart_bp)
app.register_blueprint(strategy_lab_bp)

# 注册 WebSocket 路由
register_ws_routes(sock)


@app.route("/")
def index():
    return send_from_directory(".", "dashboard.html")


@app.route("/dashboard")
def dashboard_page():
    """统一仪表板"""
    return send_from_directory(".", "dashboard.html")


@app.route("/仪表板.html")
def old_dashboard():
    return send_from_directory(".", "仪表板.html")


@app.route("/agent")
def agent_dashboard():
    """Agent流水线已合并到控制台"""
    return redirect("/dashboard")


@app.route("/agents")
def agent_dashboard_alias():
    """Agent流水线已合并到控制台（别名）"""
    return redirect("/dashboard")


@app.route("/governance")
def governance_dashboard():
    """三省六部控制台"""
    return send_from_directory(".", "governance-dashboard.html")


@app.route("/market")
def market_dashboard():
    """实时行情监控页面"""
    return send_from_directory(".", "market.html")


@app.route("/quant")
def quant_page():
    """量化选股页面"""
    return send_from_directory(".", "quant.html")


@app.route("/reports/<path:filename>")
def serve_report(filename):
    """提供 reports 目录下的报告文件"""
    return send_from_directory("reports", filename)


@app.route("/api/debug/routes", methods=["GET"])
def debug_routes():
    rules = [(r.rule, r.endpoint, list(r.methods - {"HEAD", "OPTIONS"}))
             for r in app.url_map.iter_rules() if not r.rule.startswith("/static")]
    return jsonify({"total": len(rules), "rules": sorted(rules)})


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "message": "A股量化系统运行中"})


# ── 全局错误处理（确保所有错误返回 JSON）─────────
@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": f"404: {request.path} not found"}), 404


@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": f"500: {str(e)}"}), 500


# ── 启动服务 ─────────────────────────────────────
if __name__ == "__main__":
    print("启动A股量化投资系统后端...")
    print("访问 http://localhost:5000/api/health 测试连通性")
    print("WebSocket 行情: ws://localhost:5000/ws/market")

    # 自动启动行情监控
    from ministries.rites.market_monitor import get_market_monitor
    monitor = get_market_monitor()
    monitor.start()

    # 自动启动实盘监控
    from live.monitor import get_live_monitor
    live = get_live_monitor()
    live.start()

    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False, threaded=True)
