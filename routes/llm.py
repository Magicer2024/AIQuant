"""
routes/llm.py —— LLM 服务路由

端点：
  POST /api/llm/chat          —— 通用对话
  POST /api/llm/stream        —— 流式对话（SSE）
  POST /api/llm/strategy      —— 策略分析
  POST /api/llm/market        —— 市场情绪分析
  POST /api/llm/intent        —— LLM 增强意图解析
  GET  /api/llm/health        —— LLM 连接健康检查
  GET  /api/llm/models        —— 列出支持的模型
"""

import json

from flask import Blueprint, request, jsonify, Response, stream_with_context

from llm.client import LLMClient, get_llm_client
from llm.advisor import StrategyAdvisor, get_advisor

llm_bp = Blueprint("llm", __name__, url_prefix="/api/llm")


@llm_bp.route("/chat", methods=["POST"])
def chat():
    """通用 LLM 对话"""
    data = request.get_json() or {}
    messages = data.get("messages", [])
    model = data.get("model")
    temperature = data.get("temperature", 0.7)
    max_tokens = data.get("max_tokens", 2048)

    if not messages:
        return jsonify({"success": False, "error": "messages 不能为空"}), 400

    client = LLMClient(model=model) if model else get_llm_client()
    resp = client.chat(messages, temperature=temperature, max_tokens=max_tokens)

    return jsonify({
        "success": resp.success,
        "content": resp.content,
        "model": resp.model,
        "usage": resp.usage,
        "latency_ms": resp.latency_ms,
        "error": resp.error,
    })


@llm_bp.route("/stream", methods=["POST"])
def chat_stream():
    """流式对话（SSE）"""
    data = request.get_json() or {}
    messages = data.get("messages", [])
    model = data.get("model")
    temperature = data.get("temperature", 0.7)

    if not messages:
        return jsonify({"success": False, "error": "messages 不能为空"}), 400

    client = LLMClient(model=model) if model else get_llm_client()

    def generate():
        for chunk in client.chat_stream(messages, temperature=temperature):
            yield f"data: {json.dumps({'chunk': chunk}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@llm_bp.route("/strategy", methods=["POST"])
def strategy_advice():
    """策略分析建议"""
    data = request.get_json() or {}
    code = data.get("code", "")
    market_data = data.get("market_data", {})
    user_query = data.get("query", "")

    if not code:
        return jsonify({"success": False, "error": "code 不能为空"}), 400

    advisor = get_advisor()
    result = advisor.analyze_strategy(code, market_data, user_query)
    return jsonify({"success": True, "code": code, "analysis": result})


@llm_bp.route("/market", methods=["POST"])
def market_analysis():
    """市场情绪分析"""
    data = request.get_json() or {}
    market_summary = data.get("market_summary", {})

    advisor = get_advisor()
    result = advisor.analyze_market(market_summary)
    return jsonify({"success": True, "analysis": result})


@llm_bp.route("/intent", methods=["POST"])
def intent_parse():
    """LLM 增强策略意图解析"""
    data = request.get_json() or {}
    text = data.get("text", "")

    if not text:
        return jsonify({"success": False, "error": "text 不能为空"}), 400

    advisor = get_advisor()
    result = advisor.parse_intent_with_llm(text)
    return jsonify({"success": True, "text": text, "intent": result})


@llm_bp.route("/health", methods=["GET"])
def health_check():
    """LLM 连接健康检查"""
    client = get_llm_client()
    result = client.health_check()
    return jsonify({"success": result["ok"], "detail": result})


@llm_bp.route("/models", methods=["GET"])
def list_models():
    """列出支持的模型"""
    return jsonify({
        "success": True,
        "models": [
            {"id": "gpt-4o", "provider": "OpenAI", "description": "GPT-4o 多模态模型"},
            {"id": "gpt-4o-mini", "provider": "OpenAI", "description": "GPT-4o 轻量版"},
            {"id": "deepseek-chat", "provider": "DeepSeek", "description": "DeepSeek V3 对话模型"},
            {"id": "deepseek-reasoner", "provider": "DeepSeek", "description": "DeepSeek R1 推理模型"},
            {"id": "moonshot-v1", "provider": "Moonshot", "description": "月之暗面 Kimi"},
            {"id": "qwen-plus", "provider": "Alibaba", "description": "通义千问 Plus"},
        ]
    })
