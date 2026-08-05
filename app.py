"""
AIQuant —— 个人股票评分系统
Flask 入口
"""
import os
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from utils.logger import setup_logging
from utils.timing import init_app as init_timing
from utils.api import ok, fail

# 初始化统一日志（控制台 INFO + 文件 DEBUG 按日轮转）
setup_logging()

from routes.system import system_bp
from routes.sync import sync_bp
from routes.scoring import scoring_bp
from routes.screen import screen_bp
from routes.backtest import backtest_bp
from routes.optimizer import optimizer_bp

from routes.investor import investor_bp, init_investor_tables

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
app.register_blueprint(screen_bp)
app.register_blueprint(backtest_bp)
app.register_blueprint(optimizer_bp)

app.register_blueprint(investor_bp)

init_timing(app)

# 个人投资者表（轻量、自动迁移）
try:
    init_investor_tables()
except Exception as e:
    print(f"[investor] 表初始化跳过: {e}")


@app.route("/")
@app.route("/dashboard")
def dashboard():
    return send_from_directory(".", "dashboard.html")


@app.route("/reports/<path:filename>")
def serve_report(filename):
    return send_from_directory(os.path.join(os.path.dirname(__file__), "reports"), filename)


@app.route("/api/health", methods=["GET"])
def health():
    return ok({"status": "ok", "message": "AIQuant 个人股票评分系统"})


@app.errorhandler(404)
def not_found(e):
    return fail(f"404: {request.path} not found", 404)


@app.errorhandler(500)
def server_error(e):
    return fail(f"500: {str(e)}", 500)


if __name__ == "__main__":
    from core.db import init_db
    from scheduler.runner import start_scheduler
    from scheduler.state import SCHEDULER_RUNNING

    init_db()

    # Auto-start daily sync scheduler (runs at 18:00 every trading day)
    if not SCHEDULER_RUNNING["enabled"]:
        SCHEDULER_RUNNING["enabled"] = True
        start_scheduler()
        print("[Scheduler] 每日 18:00 盘后自动数据同步已启动")

    print("AIQuant 个人股票评分系统启动...")
    print("访问 http://localhost:5000/ 打开仪表盘")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False, threaded=True)
