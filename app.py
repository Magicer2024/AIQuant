"""AIQuant Flask 入口：应用构造无数据库、网络及后台线程副作用。"""
import os
import uuid

from flask import Flask, g, request, send_from_directory, make_response
from flask_cors import CORS
from config import settings
from utils.api import ok, fail
from utils.timing import init_app as init_timing


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        TESTING=settings.TESTING,
        SCHEDULER_ENABLED=settings.SCHEDULER_ENABLED,
        AUTO_SYNC_ENABLED=settings.AUTO_SYNC_ENABLED,
        QLIB_ENABLED=settings.QLIB_ENABLED,
        SIGNAL_MODEL_MODE=settings.SIGNAL_MODEL_MODE,
    )
    app.config.update(config or {})
    if app.config["TESTING"]:
        app.config.update(SCHEDULER_ENABLED=False, AUTO_SYNC_ENABLED=False, QLIB_ENABLED=False)
    CORS(app)

    from routes.system import system_bp
    from routes.sync import sync_bp
    from routes.scoring import scoring_bp
    from routes.screen import screen_bp
    from routes.backtest import backtest_bp
    from routes.optimizer import optimizer_bp
    from routes.investor import investor_bp
    for bp in (system_bp, sync_bp, scoring_bp, screen_bp, backtest_bp, optimizer_bp, investor_bp):
        app.register_blueprint(bp)
    init_timing(app)

    @app.before_request
    def request_context():
        g.request_id = uuid.uuid4().hex
        if request.path not in {"/api/health", "/api/ready"}:
            try:
                settings.require_signal_model_ready(app.config["SIGNAL_MODEL_MODE"])
            except RuntimeError as exc:
                return fail(str(exc), 503)
        # 测试浏览器可以读数据，但不能因为首页加载隐式发起同步或调度。
        if app.config["TESTING"] and request.method == "POST" and (
            request.path.startswith("/api/sync") or "scheduler" in request.path
        ):
            return fail("测试模式禁止同步与调度", 403)

    @app.after_request
    def response_context(response):
        response.headers["X-Request-ID"] = g.request_id
        response.headers["X-Signal-Model-Mode"] = app.config["SIGNAL_MODEL_MODE"]
        return response

    @app.route("/")
    @app.route("/dashboard")
    def dashboard():
        response = make_response(send_from_directory(app.root_path, "dashboard.html"))
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    @app.route("/reports/<path:filename>")
    def serve_report(filename):
        return send_from_directory(os.path.join(app.root_path, "reports"), filename)

    @app.route("/api/health")
    def health():
        return ok({"status": "ok", "message": "AIQuant 个人股票评分系统"})

    @app.route("/api/ready")
    def ready():
        from core.db import get_conn
        try:
            with get_conn(readonly=True) as conn:
                versions = [r[0] for r in conn.execute("SELECT version FROM schema_migration ORDER BY version")]
            required = {"001_safe_baseline"}
            if app.config["SIGNAL_MODEL_MODE"] != "legacy":
                required.add("002_unified_signal_model")
            if not required.issubset(versions):
                return fail("数据库迁移未完成", 503)
            try:
                settings.require_signal_model_ready(app.config["SIGNAL_MODEL_MODE"])
            except RuntimeError as exc:
                return fail(str(exc), 503)
            return ok({"status": "ready", "migrations": versions,
                       "signal_model_mode": app.config["SIGNAL_MODEL_MODE"],
                       "auto_sync_enabled": app.config["AUTO_SYNC_ENABLED"]})
        except Exception:
            return fail("数据库尚未就绪，请显式初始化", 503)

    @app.errorhandler(404)
    def not_found(error):
        return fail(f"未找到接口：{request.path}", 404)

    @app.errorhandler(500)
    def server_error(error):
        return fail("服务器内部错误", 500)

    return app


def initialize_runtime(app):
    """仅服务启动显式调用；导入 app 或创建测试客户端不会执行。"""
    settings.require_signal_model_ready(app.config["SIGNAL_MODEL_MODE"])
    from core.db import init_db
    from routes.investor import init_investor_tables
    from utils.logger import setup_logging
    setup_logging()
    init_db()
    init_investor_tables()
    # 进程重启时回收上次中断遗留的 running 任务（标记 interrupted），
    # 无条件执行以覆盖调度器关闭的场景（方案 D：进程中断不留永久 running）。
    try:
        from core.repository import task_repo
        recovered = task_repo.recover_interrupted()
        if recovered:
            app.logger.info(f"回收中断任务 {len(recovered)} 个（标记 interrupted）")
    except Exception:
        app.logger.exception("任务中断回收失败，继续启动")
    if app.config["QLIB_ENABLED"]:
        try:
            from qlib_engine import init_qlib
            init_qlib()
        except Exception:
            app.logger.exception("Qlib 初始化失败，基础功能继续可用")
    if app.config["SCHEDULER_ENABLED"]:
        from scheduler.runner import start_scheduler
        from scheduler.state import SCHEDULER_RUNNING
        if not SCHEDULER_RUNNING["enabled"]:
            start_scheduler()
            SCHEDULER_RUNNING["enabled"] = True
            app.logger.info("每日 19:00 盘后自动数据同步已启动")


app = create_app()

if __name__ == "__main__":
    initialize_runtime(app)
    app.run(host=settings.FLASK_HOST, port=int(os.getenv("FLASK_PORT", settings.FLASK_PORT)),
            debug=False, use_reloader=False, threaded=True)
