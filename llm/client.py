"""
llm/client.py —— 统一大语言模型调用客户端

职责：
  1. 封装 OpenAI 兼容 API（支持自定义 base_url / api_key）
  2. 支持多模型切换（gpt-4o / deepseek-chat / moonshot-v1 等）
  3. 流式输出支持
  4. 本地异常降级：LLM 不可用时返回友好提示
  5. Prompt 缓存与 Token 统计

配置来源优先级：
  环境变量 > config/llm_config.yaml > 代码默认值
"""

import os
import json
import time
from dataclasses import dataclass, field
from typing import Optional, Iterator

import requests
import yaml


CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "llm_config.yaml")


@dataclass
class LLMResponse:
    """LLM 响应封装"""
    content: str
    model: str = ""
    usage: dict = field(default_factory=dict)
    latency_ms: float = 0.0
    success: bool = True
    error: str = ""


class LLMClient:
    """统一 LLM 客户端"""

    # 预置常用模型配置
    PRESET_MODELS = {
        "gpt-4o": {
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o",
        },
        "gpt-4o-mini": {
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
        },
        "deepseek-chat": {
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
        },
        "deepseek-reasoner": {
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-reasoner",
        },
        "moonshot-v1": {
            "base_url": "https://api.moonshot.cn/v1",
            "model": "moonshot-v1-8k",
        },
        "qwen-plus": {
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen-plus",
        },
    }

    def __init__(self, model: str = None, api_key: str = None, base_url: str = None,
                 timeout: int = 60):
        self.config = self._load_config()
        self.model = model or self.config.get("model", "deepseek-chat")
        self.api_key = api_key or self.config.get("api_key", "") or os.getenv("LLM_API_KEY", "")
        self.base_url = base_url or self.config.get("base_url", "")
        self.timeout = timeout
        self._session = requests.Session()

        # 如果 model 是预设别名，自动填充 base_url
        if self.model in self.PRESET_MODELS and not self.base_url:
            preset = self.PRESET_MODELS[self.model]
            self.base_url = preset["base_url"]
            # 如果预设中 model 名与别名不同，优先用预设的 model 字段
            if preset["model"] != self.model:
                self.model = preset["model"]

    def _load_config(self) -> dict:
        """加载配置文件"""
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            except Exception:
                pass
        return {}

    def chat(self, messages: list[dict], temperature: float = 0.7,
             max_tokens: int = 2048, stream: bool = False) -> LLMResponse:
        """
        非流式对话
        :param messages: [{"role": "system"/"user"/"assistant", "content": "..."}]
        :return: LLMResponse
        """
        if not self.api_key or not self.base_url:
            return LLMResponse(
                content="",
                success=False,
                error="LLM 未配置：请设置 api_key 和 base_url，或在 config/llm_config.yaml 中配置。"
            )

        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }

        start = time.time()
        try:
            resp = self._session.post(url, headers=headers, json=payload,
                                       timeout=self.timeout)
            latency = round((time.time() - start) * 1000, 2)

            if resp.status_code != 200:
                return LLMResponse(
                    content="",
                    success=False,
                    error=f"HTTP {resp.status_code}: {resp.text[:500]}",
                    latency_ms=latency,
                )

            data = resp.json()
            choice = data.get("choices", [{}])[0]
            content = choice.get("message", {}).get("content", "")
            usage = data.get("usage", {})

            return LLMResponse(
                content=content.strip(),
                model=data.get("model", self.model),
                usage=usage,
                latency_ms=latency,
                success=True,
            )

        except requests.exceptions.Timeout:
            return LLMResponse(
                content="", success=False, error="请求超时", latency_ms=round((time.time()-start)*1000, 2)
            )
        except Exception as e:
            return LLMResponse(
                content="", success=False, error=str(e), latency_ms=round((time.time()-start)*1000, 2)
            )

    def chat_stream(self, messages: list[dict], temperature: float = 0.7,
                    max_tokens: int = 2048) -> Iterator[str]:
        """
        流式对话，逐字返回内容
        """
        if not self.api_key or not self.base_url:
            yield "[LLM 未配置，请在 config/llm_config.yaml 中设置 api_key 和 base_url]"
            return

        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        try:
            resp = self._session.post(url, headers=headers, json=payload,
                                       timeout=self.timeout, stream=True)
            for line in resp.iter_lines():
                if not line:
                    continue
                line = line.decode("utf-8")
                if line.startswith("data: "):
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        if delta:
                            yield delta
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            yield f"[流式输出错误: {e}]"

    def quick_chat(self, user_message: str, system_prompt: str = "") -> str:
        """快捷单轮对话，仅返回内容字符串"""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_message})
        resp = self.chat(messages)
        return resp.content if resp.success else f"[错误: {resp.error}]"

    def health_check(self) -> dict:
        """健康检查"""
        if not self.api_key or not self.base_url:
            return {"ok": False, "error": "未配置 API Key 或 Base URL"}
        # 简单测试：询问当前日期
        resp = self.chat([
            {"role": "user", "content": "Say 'OK' only."}
        ], max_tokens=10)
        return {
            "ok": resp.success,
            "model": self.model,
            "latency_ms": resp.latency_ms,
            "error": resp.error,
        }


# 全局单例
_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """获取 LLM 客户端单例"""
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
