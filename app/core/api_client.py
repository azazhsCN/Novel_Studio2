"""AI 接口客户端（OpenAI 兼容）

除了收发请求，这里还负责两件事：
1. **连接失败自动重试**：third-party 中转端点偶发 reset/超时（实测报错
   `All connection attempts failed`），对"尚未产出任何内容"的失败按退避重试；
2. **错误信息可读**：把 httpx 的英文连接错误包装成带操作提示的中文异常
   （`ApiConnectionError`），原异常挂在 `__cause__` 上供日志排查。
"""
import asyncio
import json
import logging

import httpx

from app.core.config import load_config, reload_config

logger = logging.getLogger(__name__)

# 可重试的异常：连接类 + 上游 429/5xx（此时尚未产出任何内容，重试安全）
_RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
)
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class ApiConnectionError(RuntimeError):
    """AI 接口连接失败（网络/代理/端点不可达），消息面向用户可读"""


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, _RETRYABLE_EXCEPTIONS):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    return False


def _friendly_error(exc: Exception) -> ApiConnectionError:
    """把底层异常包装成可读错误，并保留原始异常链"""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        detail = ""
        try:
            body = exc.response.json()
            detail = str(body.get("error") or body.get("detail") or body)[:200]
        except Exception:
            detail = (exc.response.text or "")[:200]
        return ApiConnectionError(f"AI 接口返回 {code}：{detail or '无详细信息'}")
    if isinstance(exc, _RETRYABLE_EXCEPTIONS):
        return ApiConnectionError(
            f"无法连接 AI 接口（网络或代理问题）：{exc or type(exc).__name__}。"
            "请检查本机网络/代理设置后重试；若持续失败，可更换 config.yaml 的 api.base_url"
        )
    return ApiConnectionError(f"AI 接口调用失败：{exc}")


class APIClient:
    """可配置的AI API客户端，兼容OpenAI接口格式"""

    def __init__(self):
        self._reload_config()

    def _reload_config(self):
        cfg = load_config()["api"]
        self.base_url = cfg["base_url"].rstrip("/")
        self.api_key = cfg["api_key"]
        self.model = cfg["model"]
        self.max_tokens = cfg["max_tokens"]
        self.temperature = cfg["temperature"]
        self.system_prompt = cfg.get("system_prompt", "")
        # 连接失败重试次数与退避基数（config.yaml: api.max_retries / api.retry_delay_seconds）
        try:
            self.max_retries = max(0, min(int(cfg.get("max_retries", 2)), 5))
        except (TypeError, ValueError):
            self.max_retries = 2
        try:
            self.retry_delay = max(0.0, min(float(cfg.get("retry_delay_seconds", 1.5)), 30.0))
        except (TypeError, ValueError):
            self.retry_delay = 1.5
        # 思考模式开关（config.yaml: api.thinking），默认关闭。
        # 官方文档：DeepSeek-V4.1-Flash 默认开启思考模式（effort=high）；
        # 但思考模式下 temperature 不生效，而本项目正文生成依赖 temperature 控制文风，
        # 且长文本生成越快越好，故默认走非思考模式。
        # 参见 https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/
        self.thinking = cfg.get("thinking", False)

    def reload(self):
        """强制重新加载配置"""
        reload_config()
        self._reload_config()

    def _payload(self, user_message: str, system_prompt: str = None,
                 temperature: float = None, max_tokens: int = None,
                 stream: bool = False) -> dict:
        messages = []
        sys_prompt = system_prompt or self.system_prompt
        if sys_prompt:
            messages.append({"role": "system", "content": sys_prompt})
        messages.append({"role": "user", "content": user_message})
        return {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.temperature,
            "max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
            "stream": stream,
            # 思考模式：disabled 关闭 / enabled 开启；关闭时不得同时传 reasoning_effort
            "thinking": {"type": "enabled" if self.thinking else "disabled"},
        }

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def chat(self, user_message: str, system_prompt: str = None,
                   temperature: float = None, max_tokens: int = None,
                   stats: dict = None) -> str:
        """发送聊天请求，返回AI回复文本（连接失败自动重试）

        stats：可选字典，用于回传 finish_reason / usage（判断是否因长度被截断）
        """
        url = f"{self.base_url}/chat/completions"
        payload = self._payload(user_message, system_prompt, temperature, max_tokens, stream=False)
        headers = self._headers()

        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=600.0) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                if stats is not None:
                    choice = (data.get("choices") or [{}])[0]
                    stats["finish_reason"] = choice.get("finish_reason", "")
                    stats["usage"] = data.get("usage") or {}
                return data["choices"][0]["message"]["content"]
            except Exception as e:
                if attempt < self.max_retries and _is_retryable(e):
                    delay = self.retry_delay * (2 ** attempt)
                    logger.warning(
                        f"AI 接口调用失败（第{attempt + 1}次）：{type(e).__name__}: {e}；"
                        f"{delay:.1f}s 后重试"
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.error(f"AI 接口调用失败（已尝试{attempt + 1}次）：{type(e).__name__}: {e}")
                raise _friendly_error(e) from e

    async def chat_stream(self, user_message: str, system_prompt: str = None,
                          temperature: float = None, max_tokens: int = None,
                          stats: dict = None):
        """流式聊天请求，yield每个token（尚未产出内容前失败会自动重试）

        stats：可选字典，用于回传 finish_reason（"length" 表示被输出上限截断）
        注意：一旦已经 yield 过内容就不再重试，否则会在界面上产生重复文本。
        """
        url = f"{self.base_url}/chat/completions"
        payload = self._payload(user_message, system_prompt, temperature, max_tokens, stream=True)
        headers = self._headers()

        for attempt in range(self.max_retries + 1):
            yielded_any = False
            try:
                async with httpx.AsyncClient(timeout=600.0) as client:
                    async with client.stream("POST", url, json=payload, headers=headers) as resp:
                        resp.raise_for_status()
                        async for line in resp.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            line = line[6:]
                            if line.strip() == "[DONE]":
                                break
                            try:
                                chunk = json.loads(line)
                                choice = (chunk.get("choices") or [{}])[0]
                                if stats is not None and choice.get("finish_reason"):
                                    stats["finish_reason"] = choice.get("finish_reason")
                                content = choice.get("delta", {}).get("content", "")
                            except (json.JSONDecodeError, KeyError, IndexError):
                                continue
                            if content:
                                yielded_any = True
                                yield content
                return
            except Exception as e:
                if yielded_any or attempt >= self.max_retries or not _is_retryable(e):
                    logger.error(
                        f"AI 流式接口失败（已尝试{attempt + 1}次，已产出内容={yielded_any}）："
                        f"{type(e).__name__}: {e}"
                    )
                    raise _friendly_error(e) from e
                delay = self.retry_delay * (2 ** attempt)
                logger.warning(
                    f"AI 流式接口连接失败（第{attempt + 1}次）：{type(e).__name__}: {e}；"
                    f"{delay:.1f}s 后重试"
                )
                await asyncio.sleep(delay)
                if stats is not None:
                    stats.pop("finish_reason", None)


# 全局单例
api_client = APIClient()
