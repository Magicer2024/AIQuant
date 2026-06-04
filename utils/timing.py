"""
utils/timing.py ? Request-level performance monitoring.

Adds before/after request hooks to log response times.
Slow requests (>0.5s) are logged at WARNING level.
"""
import time
import logging
from flask import g, request

logger = logging.getLogger("timing")


def init_app(app):
    """Register timing hooks on a Flask application."""

    @app.before_request
    def _start_timer():
        g.start_time = time.time()

    @app.after_request
    def _log_time(response):
        if hasattr(g, "start_time"):
            elapsed = time.time() - g.start_time
            level = logging.WARNING if elapsed > 0.5 else logging.INFO
            logger.log(level, "%s %s - %.3fs", request.method, request.path, elapsed)
        return response
