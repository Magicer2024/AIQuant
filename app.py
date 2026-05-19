"""
AIQuant —— 个人股票评分系统
Flask 入口
"""
import os
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from routes.system import system_bp
from routes.sync import sync_bp
from routes.scoring import scoring_bp
from routes.strategy import strategy_bp
from routes.backtest import backtest_bp

# ── Qlib engine initialization ──────────────────────
try:
    from qlib_engine import init_qlib
    init_qlib()
    print("[Qlib] Engine initialized")
except Exception as e:
    print(f"[Qlib] Initialization skipped: {e}")

app = Flask(__name__)
CORS(app)

# 注册核心 Blueprint
app.register_blueprint(system_bp)
app.register_blueprint(sync_bp)
app.register_blueprint(scoring_bp)
app.register_blueprint(strategy_bp)
app.register_blueprint(backtest_bp)


@app.route("/")
@app.route("/dashboard")
def dashboard():
    return send_from_directory(".", "dashboard.html")


@app.route("/reports/<path:filename>")
def serve_report(filename):
    return send_from_directory(os.path.join(os.path.dirname(__file__), "reports"), filename)


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "message": "AIQuant 个人股票评分系统"})


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": f"404: {request.path} not found"}), 404


@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": f"500: {str(e)}"}), 500


if __name__ == "__main__":
    from core.db import init_db
    init_db()
    print("AIQuant 个人股票评分系统启动...")
    print("访问 http://localhost:5000/ 打开仪表盘")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False, threaded=True)
