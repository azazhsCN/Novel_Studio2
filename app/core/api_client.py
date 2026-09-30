import httpx
from app.core.config import load_config, reload_config


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

    async def chat(self, user_message: str, system_prompt: str = None,
                   temperature: float = None, max_tokens: int = None,
                   stats: dict = None) -> str:
        """发送聊天请求，返回AI回复文本

        stats：可选字典，用于回传 finish_reason / usage（判断是否因长度被截断）
        """
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        messages = []
        sys_prompt = system_prompt or self.system_prompt
        if sys_prompt:
            messages.append({"role": "system", "content": sys_prompt})
        messages.append({"role": "user", "content": user_message})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.temperature,
            "max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
            "stream": False,
            # 思考模式：disabled 关闭 / enabled 开启；关闭时不得同时传 reasoning_effort
            "thinking": {"type": "enabled" if self.thinking else "disabled"},
        }

        async with httpx.AsyncClient(timeout=600.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            if stats is not None:
                choice = (data.get("choices") or [{}])[0]
                stats["finish_reason"] = choice.get("finish_reason", "")
                stats["usage"] = data.get("usage") or {}
            return data["choices"][0]["message"]["content"]

    async def chat_stream(self, user_message: str, system_prompt: str = None,
                          temperature: float = None, max_tokens: int = None,
                          stats: dict = None):
        """流式聊天请求，yield每个token

        stats：可选字典，用于回传 finish_reason（"length" 表示被输出上限截断）
        """
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        messages = []
        sys_prompt = system_prompt or self.system_prompt
        if sys_prompt:
            messages.append({"role": "system", "content": sys_prompt})
        messages.append({"role": "user", "content": user_message})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.temperature,
            "max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
            "stream": True,
            # 思考模式：disabled 关闭 / enabled 开启；关闭时不得同时传 reasoning_effort
            "thinking": {"type": "enabled" if self.thinking else "disabled"},
        }

        async with httpx.AsyncClient(timeout=600.0) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        line = line[6:]
                        if line.strip() == "[DONE]":
                            break
                        import json
                        try:
                            chunk = json.loads(line)
                            choice = (chunk.get("choices") or [{}])[0]
                            if stats is not None and choice.get("finish_reason"):
                                stats["finish_reason"] = choice.get("finish_reason")
                            delta = choice.get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield content
                        except (json.JSONDecodeError, KeyError, IndexError):
                            continue


# 全局单例
api_client = APIClient()
