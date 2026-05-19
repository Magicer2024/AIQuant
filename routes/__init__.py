"""
routes/ -- Flask Blueprint route modules
"""
from flask import Blueprint

system_bp = Blueprint("system", __name__, url_prefix="/api/system")
sync_bp = Blueprint("sync", __name__, url_prefix="/api/sync")
