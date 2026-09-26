"""兼容旧字段的 JSON 响应合同；状态字段不得由额外参数覆盖。"""
from flask import jsonify, g, has_request_context


def _request_id():
    return getattr(g, "request_id", None) if has_request_context() else None


def _check_extra(extra):
    reserved = {"success", "code", "error", "message", "request_id", "http"}
    if reserved.intersection(extra):
        raise ValueError("额外字段不得覆盖响应状态")


def ok(data=None, *, message="成功", **extra):
    _check_extra(extra)
    payload = {"success": True, "code": 200, "message": message, "request_id": _request_id(), **extra}
    if data is not None:
        payload["data"] = data
    return jsonify(payload)


def fail(message: str, code: int = None, *, http: int = None, **extra):
    """兼容 fail(msg, 500) 和 fail(msg, http=500)，冲突状态直接报错。"""
    _check_extra(extra)
    if code is not None and http is not None and code != http:
        raise ValueError("code 与 http 状态冲突")
    status = http if http is not None else (code if code is not None else 400)
    if not isinstance(status, int) or isinstance(status, bool) or not 400 <= status <= 599:
        raise ValueError("错误响应必须使用 400–599 HTTP 状态")
    return jsonify({"success": False, "code": status, "message": message,
                    "error": message, "request_id": _request_id(), **extra}), status


def identity_meta(cohort=None, data_as_of=None, list_key=None, *, conn=None, **ids):
    """推荐/复盘响应的身份链元数据（方案 F1）。

    统一声明数据来源与稳定身份，便于前端分清正式推荐 / 观察池 / 研究实验，并让每条
    响应可追溯到「信号模型模式 + 数据日期 + 参数版本 + 榜单批次」：
      - signal_model_mode / source：legacy 走旧推荐路径；v2 以统一信号模型为唯一事实来源；
      - cohort：production（正式主榜）/ observation（观察池）/ shadow（旁路研究）；
      - data_as_of：数据/推荐所属交易日；param_version：生成时的参数版本（追溯用）；
      - batch_id / signal_id / recommendation_id / trade_id：统一模型下的稳定 ID。
        legacy 模式无冻结批次/信号运行，这些 ID 为 None（identity_chain_available=False），
        绝不伪造；切到 v2 后由调用方从已发布批次填充。

    conn 可传入调用方已打开的连接以复用（避免请求内额外只读连接的锁竞争）；
    param_version 读取失败时降级为 None，不阻断响应。
    """
    from config.settings import SIGNAL_MODEL_MODE
    meta = {
        "signal_model_mode": SIGNAL_MODEL_MODE,
        "source": ("unified_signal_model" if SIGNAL_MODEL_MODE == "v2"
                   else "legacy_recommendation"),
        "cohort": cohort,
        "data_as_of": data_as_of,
        "list_key": list_key,
        "identity_chain_available": SIGNAL_MODEL_MODE == "v2",
        "batch_id": ids.get("batch_id"),
        "signal_id": ids.get("signal_id"),
        "recommendation_id": ids.get("recommendation_id"),
        "trade_id": ids.get("trade_id"),
    }
    try:
        from strategy.optimizer import current_param_version
        if conn is not None:
            meta["param_version"] = current_param_version(conn)
        else:
            from core.db import get_conn
            with get_conn(readonly=True) as c2:
                meta["param_version"] = current_param_version(c2)
    except Exception:
        meta["param_version"] = None
    return meta
