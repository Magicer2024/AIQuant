"""
utils/llm_client.py —— 轻量 LLM 客户端（OpenAI 兼容接口）

配置来源（优先级递减）：
  1. 环境变量 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
  2. config/llm_config.yaml（已 gitignore，勿提交密钥）

设计原则：无密钥或 enabled=false 时 is_llm_available() 返回 False，
调用方应自行降级到本地逻辑，不要让功能硬失败。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests

_CONFIG_CACHE: Optional[Dict[str, Any]] = None

_DEFAULTS: Dict[str, Any] = {
    "model": "deepseek-chat",
    "api_key": "",
    "base_url": "https://api.deepseek.com",
    "default_temperature": 0.3,
    "default_max_tokens": 2048,
    "timeout": 60,
    "enabled": False,
}


def load_llm_config() -> Dict[str, Any]:
    """加载 LLM 配置（带缓存；环境变量覆盖 yaml）"""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    cfg = dict(_DEFAULTS)
    try:
        import yaml
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config", "llm_config.yaml")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if v is not None})
    except Exception:
        pass  # yaml 缺失/损坏时静默用默认值
    cfg["api_key"] = os.environ.get("LLM_API_KEY") or cfg.get("api_key") or ""
    cfg["base_url"] = os.environ.get("LLM_BASE_URL") or cfg.get("base_url")
    cfg["model"] = os.environ.get("LLM_MODEL") or cfg.get("model")
    _CONFIG_CACHE = cfg
    return cfg


def reset_config_cache():
    """清空配置缓存（测试/热更新用）"""
    global _CONFIG_CACHE
    _CONFIG_CACHE = None


def is_llm_available() -> bool:
    """LLM 是否可用（开关开启 + 密钥非空）"""
    cfg = load_llm_config()
    return bool(cfg.get("enabled") and cfg.get("api_key"))


def chat_completion(messages: List[Dict[str, str]], *,
                    temperature: Optional[float] = None,
                    max_tokens: Optional[int] = None,
                    timeout: Optional[int] = None,
                    json_mode: bool = False) -> str:
    """调用 OpenAI 兼容 /chat/completions，返回 content 文本。

    :param messages: [{"role": "system"/"user", "content": "..."}]
    :param json_mode: True 时要求模型输出 JSON 对象（response_format=json_object）
    :raises RuntimeError: LLM 未配置/禁用
    :raises requests.HTTPError: 接口返回非 2xx
    """
    cfg = load_llm_config()
    if not is_llm_available():
        raise RuntimeError("LLM 未配置或已禁用（检查 config/llm_config.yaml 与环境变量 LLM_API_KEY）")

    base = str(cfg["base_url"]).rstrip("/")
    # base_url 已带 /v1 时直接拼端点，否则补 /v1（DeepSeek 两种形式都兼容）
    url = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")

    payload: Dict[str, Any] = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": cfg.get("default_temperature", 0.3) if temperature is None else temperature,
        "max_tokens": cfg.get("default_max_tokens", 2048) if max_tokens is None else max_tokens,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout or cfg.get("timeout", 60),
    )
    resp.raise_for_status()
    data = resp.json()
    return (data["choices"][0]["message"]["content"] or "").strip()
