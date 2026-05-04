"""
routes/data_source.py —— 多数据源管理API

路由：
  GET  /api/data/sources          → 数据源列表
  GET  /api/data/health           → 数据源健康检查
  POST /api/data/switch           → 切换主数据源
  GET  /api/data/price/<code>     → 获取行情（自动选择数据源）
"""

from flask import Blueprint, jsonify, request

from ministries.rites.data_source_manager import get_data_source_manager, DataSourceType
from ministries.rites.akshare_source import AkshareDataSource
from ministries.rites.tushare_source import TushareDataSource

data_source_bp = Blueprint("data_source", __name__, url_prefix="/api/data")

# 注册额外数据源
_ds_manager = get_data_source_manager()
_ds_manager.register(DataSourceType.AKSHARE, AkshareDataSource())
_ds_manager.register(DataSourceType.TUSHARE, TushareDataSource())


@data_source_bp.route("/sources", methods=["GET"])
def get_sources():
    """获取数据源列表"""
    sources = []
    for st, source in _ds_manager.sources.items():
        status = source.check_health()
        sources.append({
            "type": st.value,
            "name": source.get_name(),
            "available": status.is_available,
            "quality": status.quality.value,
            "last_check": status.last_check,
        })

    return jsonify({
        "success": True,
        "primary": _ds_manager.primary.value,
        "sources": sources,
    })


@data_source_bp.route("/health", methods=["GET"])
def health_check():
    """数据源健康检查"""
    results = _ds_manager.health_check_all()
    return jsonify({
        "success": True,
        "results": [
            {
                "type": r.source_type.value,
                "name": r.name,
                "available": r.is_available,
                "quality": r.quality.value,
                "latency_ms": r.latency_ms,
            }
            for r in results
        ],
    })


@data_source_bp.route("/switch", methods=["POST"])
def switch_source():
    """切换主数据源"""
    data = request.get_json() or {}
    source_type = data.get("source", "local")

    type_map = {
        "local": DataSourceType.LOCAL,
        "akshare": DataSourceType.AKSHARE,
        "tushare": DataSourceType.TUSHARE,
    }

    st = type_map.get(source_type)
    if not st or st not in _ds_manager.sources:
        return jsonify({"success": False, "error": f"未知数据源: {source_type}"}), 400

    _ds_manager.set_primary(st)
    return jsonify({
        "success": True,
        "message": f"主数据源已切换为 {source_type}",
        "primary": source_type,
    })


@data_source_bp.route("/price/<code>", methods=["GET"])
def get_price(code):
    """获取股票行情"""
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    source = request.args.get("source")

    st = None
    if source:
        type_map = {
            "local": DataSourceType.LOCAL,
            "akshare": DataSourceType.AKSHARE,
            "tushare": DataSourceType.TUSHARE,
        }
        st = type_map.get(source)

    data = _ds_manager.get_daily_price(code, start_date, end_date, st)
    return jsonify({
        "success": True,
        "code": code,
        "count": len(data),
        "data": data[:10] if data else [],  # 只返回前10条
    })
