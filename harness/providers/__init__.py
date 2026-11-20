"""模型提供方的登记表：有哪几家、各自怎么连接、怎么取得模型列表。

要接入一家新的、支持 OpenAI 格式的厂商，在 PROVIDERS 里加一项就够了。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic
import openai

from ..config import Config
from ..tools import ToolRegistry
from .base import Backend, ProviderError
from .claude import AnthropicBackend
from .openai_compat import OpenAICompatBackend

__all__ = [
    "PROVIDERS",
    "Backend",
    "ClientFactory",
    "Provider",
    "ProviderError",
    "has_key",
    "list_models",
    "make_backend",
    "make_client",
]


@dataclass(frozen=True)
class Provider:
    id: str
    label: str  # 给用户看的名字
    kind: str  # 用哪个适配层："anthropic" 或 "openai"
    key_env: str  # API key 保存在哪个环境变量里
    base_url: str | None  # 服务器地址；None 表示用 SDK 默认的
    key_page: str  # 去哪里申请 API key


PROVIDERS: dict[str, Provider] = {
    provider.id: provider
    for provider in [
        Provider("anthropic", "Claude (Anthropic)", "anthropic", "ANTHROPIC_API_KEY", None, "https://platform.claude.com"),
        Provider("openai", "GPT (OpenAI)", "openai", "OPENAI_API_KEY", None, "https://platform.openai.com"),
        Provider(
            "gemini",
            "Gemini (Google)",
            "openai",
            "GEMINI_API_KEY",
            "https://generativelanguage.googleapis.com/v1beta/openai/",
            "https://aistudio.google.com",
        ),
        Provider(
            "deepseek", "DeepSeek", "openai", "DEEPSEEK_API_KEY", "https://api.deepseek.com", "https://platform.deepseek.com"
        ),
    ]
}

# 这个程序的请求参数适配过的 Claude 模型。Anthropic 还有别的模型，但它们接受的参数不完全一样，
# 所以同步模型列表时只保留这几个。
CLAUDE_MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2"]

# OpenAI 的模型列表里混着很多不是用来聊天的模型，名字里带这些词的要去掉
_NOT_CHAT = ("embedding", "tts", "whisper", "audio", "image", "dall-e", "realtime", "moderation", "transcribe", "sora")

ClientFactory = Callable[[Provider], Any]


def has_key(provider: Provider) -> bool:
    """这一家的 API key 有没有设置。"""
    if os.environ.get(provider.key_env):
        return True
    if provider.kind == "anthropic":
        # Anthropic 还支持另外两种凭证：登录令牌，以及 `ant auth login` 保存的登录信息
        return bool(os.environ.get("ANTHROPIC_AUTH_TOKEN")) or (Path.home() / ".config" / "anthropic").is_dir()
    return False


def make_client(provider: Provider) -> Any:
    """按当前环境变量里的 key 创建一个连接这家的客户端。

    还没有 key 时也能创建（填一个占位的），这样程序可以先启动，让用户在界面上填写；
    真正发请求时服务器会回答"key 无效"，界面会提示去设置。
    """
    if provider.kind == "anthropic":
        if has_key(provider):
            return anthropic.Anthropic()  # 自己从环境变量里读取凭证
        return anthropic.Anthropic(api_key="missing")
    return openai.OpenAI(api_key=os.environ.get(provider.key_env) or "missing", base_url=provider.base_url)


def list_models(provider: Provider, client: Any) -> list[str]:
    """向这一家的服务器询问有哪些模型可用（界面上的"同步模型"）。"""
    try:
        ids = [model.id.removeprefix("models/") for model in client.models.list()]
    except (anthropic.APIError, openai.APIError) as exc:
        if isinstance(exc, (anthropic.AuthenticationError, openai.AuthenticationError)):
            raise ProviderError(f"{provider.label} 的 API key 无效。") from exc
        if isinstance(exc, (anthropic.APIConnectionError, openai.APIConnectionError)):
            raise ProviderError(f"连接不上 {provider.label} 的服务器，请检查网络。") from exc
        raise ProviderError(f"获取 {provider.label} 的模型列表失败：{exc}") from exc

    if provider.kind == "anthropic":
        available = [model for model in CLAUDE_MODELS if model in ids]
        return available or CLAUDE_MODELS
    if provider.id == "gemini":
        ids = [model for model in ids if model.startswith("gemini")]
    return sorted(model for model in ids if not any(word in model for word in _NOT_CHAT))


def make_backend(
    provider: Provider, model: str, config: Config, tools: ToolRegistry, system_prompt: str, client: Any
) -> Backend:
    if provider.kind == "anthropic":
        return AnthropicBackend(client, model, config, tools, system_prompt)
    return OpenAICompatBackend(client, provider.id, provider.label, model, config, tools, system_prompt)
