"""
Flask 后端 API
提供数据接口给前端可视化界面
"""
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

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

app = Flask(__name__)
CORS(app)

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


@app.route("/")
def index():
    return send_from_directory(".", "仪表板.html")


@app.route("/仪表板.html")
def dashboard():
    return send_from_directory(".", "仪表板.html")


@app.route("/agent")
def agent_dashboard():
    """多Agent流水线控制台"""
    return send_from_directory(".", "agent-dashboard.html")


@app.route("/agents")
def agent_dashboard_alias():
    """多Agent流水线控制台（别名）"""
    return send_from_directory(".", "agent-dashboard.html")


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
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False, threaded=True)
