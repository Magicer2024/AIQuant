"""
utils/api.py ? Standardized JSON API response helpers.

Usage:
    return ok(data)          ? {"success": true, "data": data}
    return fail("msg")       ? {"success": false, "error": "msg"}  (HTTP 400)
    return fail("msg", 404)  ? same payload with HTTP 404
"""

from flask import jsonify, Response


def ok(data=None, **extra) -> Response:
    """Return a successful JSON response: {"success": true, "data": ..., **extra}."""
    payload = {"success": True}
    if data is not None:
        payload["data"] = data
    payload.update(extra)
    return jsonify(payload)


def fail(message: str, code: int = 400, **extra) -> Response:
    """Return an error JSON response: {"success": false, "error": message}."""
    payload = {"success": False, "error": message}
    payload.update(extra)
    return jsonify(payload), code
