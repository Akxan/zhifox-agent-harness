"""模型提供方的登记表：有哪几家、各自怎么连接、怎么取得模型列表。

两种提供方：
  - 内置的四家（PROVIDERS）：Claude、GPT、Gemini、DeepSeek。要接入一家新的、支持 OpenAI 格式的厂商，在 PROVIDERS 里加一项就够了。
  - 你自己加的自定义接口（Ollama、LM Studio、公司的网关……，见 harness/endpoints/）。它们存在 data/endpoints.json，
    程序启动时由 build_app 交给这里（use_custom），之后在设置里加、改、删，这里马上就看得到。

找一家用 find(编号)，列出全部用 every()；PROVIDERS 只有内置的那四家。为什么自定义接口的登记放在模块里（而不是
跟着 App 走）：Agent 文件的检查、聊天通道、调度器这些地方原来就直接查这张表，一个智狐进程里也只有一个 App。

每一家能做什么（看不看得懂图片、读不读得了 PDF、能不能用工具、上下文多大）用 caps(provider, model) 问：
内置的几家按"家"算，自定义接口按模型算。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import anthropic
import openai

from ..config import Config
from ..endpoints import Endpoint, EndpointError
from ..endpoints import client as endpoint_client
from ..endpoints import errors as endpoint_errors
from ..endpoints import probe as endpoint_probe
from ..i18n import t
from ..tools import ToolRegistry
from .base import Backend, ProviderError
from .claude import AnthropicBackend
from .openai_compat import OpenAICompatBackend

__all__ = [
    "PROVIDERS",
    "Backend",
    "Caps",
    "ClientFactory",
    "Provider",
    "ProviderError",
    "caps",
    "continues",
    "every",
    "find",
    "has_key",
    "hint",
    "list_models",
    "make_backend",
    "make_client",
    "resolve",
    "switch_model",
    "use_custom",
]


@dataclass(frozen=True)
class Provider:
    id: str
    label: str  # 给用户看的名字
    kind: str  # 用哪个适配层："anthropic" 或 "openai"
    key_env: str  # API key 保存在哪个环境变量里（自定义接口是空的：它的密钥在 data/endpoints.json 里）
    base_url: str | None  # 服务器地址；None 表示用 SDK 默认的
    key_page: str  # 去哪里申请 API key（自定义接口是空的）
    # 能收哪些附件（文本文件哪一家都能收）。默认都不能：新接入的一家先当它只认文字，确认支持了再打开
    vision: bool = False  # 看得懂图片
    # 读得了 PDF。OpenAI 格式的接口里没有通用的 PDF 写法：GPT 有自己的写法（见 openai_compat.py），
    # Gemini 和 DeepSeek 的兼容接口文档里没有写，所以目前只有 Claude 和 GPT 能读
    pdf: bool = False
    # 有没有联网搜索。联网搜索是 Anthropic 在它的服务器上执行的工具（见 tools/web.py），别家拿不到，
    # 系统提示词里"可以联网"那一行也只写给有的那一家，不然模型会去调用一个不存在的工具
    web: bool = False
    # 自定义接口（harness/endpoints/）的设置；内置的几家是 None。它的能力按模型记，用 caps() 问
    endpoint: Endpoint | None = field(default=None, compare=False, repr=False)

    @property
    def custom(self) -> bool:
        return self.endpoint is not None


@dataclass(frozen=True)
class Caps:
    """一个模型能做什么。"""

    vision: bool
    pdf: bool
    tools: bool  # 能不能用工具（函数调用）。内置的几家都能
    context: int | None  # 上下文上限（token）；None 按 config 里的表（见 compaction.context_window）


PROVIDERS: dict[str, Provider] = {
    provider.id: provider
    for provider in [
        Provider(
            "anthropic",
            "Claude (Anthropic)",
            "anthropic",
            "ANTHROPIC_API_KEY",
            None,
            "https://platform.claude.com",
            vision=True,
            pdf=True,
            web=True,
        ),
        Provider(
            "openai", "GPT (OpenAI)", "openai", "OPENAI_API_KEY", None, "https://platform.openai.com", vision=True, pdf=True
        ),
        Provider(
            "gemini",
            "Gemini (Google)",
            "openai",
            "GEMINI_API_KEY",
            "https://generativelanguage.googleapis.com/v1beta/openai/",
            "https://aistudio.google.com",
            vision=True,
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


# ---- 自定义接口的登记 ---------------------------------------------------------------


class CustomSource(Protocol):
    """自定义接口从哪儿来（endpoints.EndpointStore）。"""

    def list(self) -> list[Endpoint]: ...
    def learned(self, provider_id: str, model: str, *, tools: bool | None = None, stream_usage: bool | None = None) -> None: ...


_custom: CustomSource | None = None


def use_custom(source: CustomSource | None) -> None:
    """换上自定义接口的来源（build_app 启动时交进来；测试结束时换回 None）。"""
    global _custom
    _custom = source


def _from_endpoint(endpoint: Endpoint) -> Provider:
    return Provider(
        id=endpoint.provider_id,
        label=endpoint.name,
        kind="openai",
        key_env="",
        base_url=endpoint.base_url,
        key_page="",
        vision=any(model.vision for model in endpoint.models),
        pdf=any(model.pdf for model in endpoint.models),
        endpoint=endpoint,
    )


def _customs() -> list[Provider]:
    return [_from_endpoint(endpoint) for endpoint in (_custom.list() if _custom is not None else [])]


def every() -> list[Provider]:
    """全部提供方：内置的四家在前，自定义接口按添加的顺序排在后面。"""
    return [*PROVIDERS.values(), *_customs()]


def find(provider_id: object) -> Provider | None:
    """按编号找一家（内置的或者自定义的）。没有这一家（比如自定义接口被删掉了）返回 None。"""
    if not isinstance(provider_id, str):
        return None
    if provider_id in PROVIDERS:
        return PROVIDERS[provider_id]
    return next((provider for provider in _customs() if provider.id == provider_id), None)


def resolve(text: str) -> tuple[Provider, str]:
    """"提供方/模型"→（Provider，模型名）。和 Agent 文件里的 model 是同一种写法：anthropic/claude-opus-5-5、
    custom-ollama/qwen3:8b。只按第一个 / 分开（vLLM 的模型名里常有 /：custom-vllm/meta-llama/Llama-3.1-8B）。
    给别的程序（命令行的 --model、评测）用；自定义接口要先 use_custom（build_app 会做）。找不到时抛出 ProviderError。"""
    provider_id, slash, model = text.strip().partition("/")
    provider = find(provider_id) if slash and model.strip() else None
    if provider is None:
        known = "、".join(item.id for item in every())
        raise ProviderError(t("“{spec}”不是“提供方/模型”的写法，或者没有这个提供方。可以用：{known}。", spec=text, known=known))
    return provider, model.strip()


def caps(provider: Provider, model: str) -> Caps:
    """这一家的这个模型能做什么。"""
    if provider.endpoint is None:
        return Caps(vision=provider.vision, pdf=provider.pdf, tools=True, context=None)
    found = provider.endpoint.caps(model)
    return Caps(vision=found.vision, pdf=found.pdf, tools=found.tools, context=found.window)


def has_key(provider: Provider) -> bool:
    """这一家能不能用了：内置的几家看 API 密钥设置了没有；自定义接口的密钥是可选的（本地的服务器一般不要），总算有。"""
    if provider.endpoint is not None:
        return True
    if os.environ.get(provider.key_env):
        return True
    if provider.kind == "anthropic":
        # Anthropic 还支持另外两种凭证：登录令牌，以及 `ant auth login` 保存的登录信息
        return bool(os.environ.get("ANTHROPIC_AUTH_TOKEN")) or (Path.home() / ".config" / "anthropic").is_dir()
    return False


def hint(provider: Provider) -> str:
    """给页面看的密钥：只有最后 4 位（"…abcd"）；没有设置是空的。"""
    if provider.endpoint is not None:
        return provider.endpoint.hint()
    value = os.environ.get(provider.key_env, "") if provider.key_env else ""
    return f"…{value[-4:]}" if value else ""


def continues(backend: Any, provider: Provider, model: str) -> bool:
    """换到这一家的这个模型，这段对话能不能接着聊（同一条规矩：App.select_model、换模型的面板都照它）。

    同一家之内可以：历史的格式一样。但能不能用工具要一样——工具说明书在一段对话里不变（见 claude.py 的说明），
    一个能用工具的模型聊到一半换成不能用的（或者反过来），只能开始新的一段。内置的几家都能用工具，所以只有自定义接口会碰到。
    """
    if provider.id != backend.provider:
        return False
    return caps(provider, model).tools == getattr(backend, "tools_supported", True)


def saved_before(provider: Provider, saved: list, dropped: int, mark: int) -> list | None:
    """一段保存下来的历史（Backend.export 的样子，压缩掉了 dropped 条）在 mark 这个位置之前的那一截：mark 必须是
    一轮的开头、而且还在历史里（和 Backend.turn_at 同一个规矩），或者正好是末尾。不是的返回 None。
    不用为此建一个适配层：保存的会话不一定是现在用的这一家（见 fork.py）。历史的格式只看是哪一类接口（make_backend 也这样分）。"""
    backend = AnthropicBackend if provider.kind == "anthropic" else OpenAICompatBackend
    return backend.saved_before(saved, dropped, mark)


def switch_model(backend: Any, provider: Provider, model: str) -> None:
    """同一家之内换模型（continues 为真时）：换上模型名；自定义接口还要换上这个模型自己的能力（看不看得懂图、上下文多大）。"""
    backend.model = model
    if provider.endpoint is not None and isinstance(backend, OpenAICompatBackend):
        found = caps(provider, model)
        backend.vision, backend.pdf, backend.context_window = found.vision, found.pdf, found.context


# ---- 连接、模型列表、适配层 ------------------------------------------------------------


def make_client(provider: Provider, timeout: float | None = None) -> Any:
    """按当前环境变量里的 key 创建一个连接这家的客户端。timeout：一次请求最多等多久（秒），不给用 SDK 默认的。

    还没有 key 时也能创建（填一个占位的），这样程序可以先启动，让用户在界面上填写；
    真正发请求时服务器会回答"key 无效"，界面会提示去设置。
    自定义接口的客户端由 endpoints/client.py 建：别的几家的密钥不带过去、不跟重定向。
    它不能再用 SDK 的 with_options 改：那会照着环境变量重建一个客户端，OPENAI_ORG_ID 这些又回来了，所以 timeout 在这里给。
    """
    if provider.endpoint is not None:
        return endpoint_client.make(provider.endpoint, timeout=timeout)
    if provider.kind == "anthropic":
        client = anthropic.Anthropic() if has_key(provider) else anthropic.Anthropic(api_key="missing")  # 有的话自己从环境变量里读取凭证
    else:
        client = openai.OpenAI(api_key=os.environ.get(provider.key_env) or "missing", base_url=provider.base_url)
    return client.with_options(timeout=timeout) if timeout is not None else client


def list_models(provider: Provider, client: Any) -> list[str]:
    """向这一家的服务器询问有哪些模型可用（界面上的"同步模型"）。

    自定义接口不走这里（它要的不只是名字，还有每个模型能做什么），见 App.sync_models 和 endpoints/probe.py。
    """
    try:
        ids = [model.id.removeprefix("models/") for model in client.models.list()]
    except (anthropic.APIError, openai.APIError) as exc:
        if isinstance(exc, (anthropic.AuthenticationError, openai.AuthenticationError)):
            raise ProviderError(t("{provider} 的 API 密钥无效。", provider=provider.label)) from exc
        if isinstance(exc, (anthropic.APIConnectionError, openai.APIConnectionError)):
            raise ProviderError(t("连不上 {provider} 的服务器，请检查网络。", provider=provider.label)) from exc
        raise ProviderError(t("获取 {provider} 的模型列表失败：{error}", provider=provider.label, error=exc)) from exc

    if provider.kind == "anthropic":
        available = [model for model in CLAUDE_MODELS if model in ids]
        return available or CLAUDE_MODELS
    if provider.id == "gemini":
        ids = [model for model in ids if model.startswith("gemini")]
    return sorted(model for model in ids if not any(word in model for word in _NOT_CHAT))


def probe_models(provider: Provider) -> endpoint_probe.Found:
    """问一个自定义接口有哪些模型、每个能做什么（不花钱，见 endpoints/probe.py）。出错抛出 ProviderError。"""
    assert provider.endpoint is not None
    try:
        return endpoint_probe.probe(provider.endpoint)
    except EndpointError as exc:
        raise ProviderError(exc.message) from None


def make_backend(
    provider: Provider, model: str, config: Config, tools: ToolRegistry, system_prompt: str, client: Any
) -> Backend:
    if provider.kind == "anthropic":
        return AnthropicBackend(client, model, config, tools, system_prompt)
    endpoint = provider.endpoint
    if endpoint is None:
        return OpenAICompatBackend(
            client, provider.id, provider.label, model, config, tools, system_prompt, vision=provider.vision, pdf=provider.pdf
        )
    found = caps(provider, model)

    def learned(name: str, **flags: bool) -> None:
        if _custom is not None:
            _custom.learned(provider.id, name, **flags)

    def explain(exc: openai.APIError, name: str) -> str:
        # 按这个接口现在的设置说（对话进行中你可能改了它的名字、地址；密钥也要按现在的那个遮掉）
        latest = find(provider.id)
        return endpoint_errors.explain(exc, latest.endpoint if latest is not None and latest.endpoint is not None else endpoint, name)

    return OpenAICompatBackend(
        client,
        provider.id,
        provider.label,
        model,
        config,
        tools,
        system_prompt,
        vision=found.vision,
        pdf=found.pdf,
        tools_supported=found.tools,
        context_window=found.context,
        stream_usage=endpoint.stream_usage,
        degrade=learned,
        explain=explain,
    )
