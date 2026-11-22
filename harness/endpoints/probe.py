"""测试连接、同步模型列表：问一个接口"你在吗，有哪些模型，每个能做什么"。

全部从智狐的程序发出去，页面从来不直接连你填的地址（页面的安全策略也不许它连）：页面只把表单交给服务器，
服务器检查过地址（urls.check）、用 client.make 建好客户端再去问。

只发不花钱的请求：GET /models，以及几种常见服务器自己的"介绍一下这个模型"（Ollama 的 /api/version、/api/show，
LM Studio 的 /api/v0/models）。不发聊天请求：外面的网关是按 token 收钱的，"测试连接"不该花你的钱。
这几个额外的请求问错了服务器（不是 Ollama 却问了 /api/show）也只是一个 404，照名字猜就是了。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import openai

from ..config import ENDPOINT_DETECT_MODELS, ENDPOINT_PROBE_SECONDS, MAX_ENDPOINT_MODELS
from ..i18n import t
from . import detect, errors
from .client import make
from .spec import Endpoint, EndpointError, EndpointModel, check_model_id
from .urls import root_of

OLLAMA, LMSTUDIO = "ollama", "lmstudio"


@dataclass
class Found:
    models: list[EndpointModel] = field(default_factory=list)
    listed: bool = True  # 服务器有没有 /models。没有的话（有的网关就是不给），模型名要你自己填
    server: str = ""  # 认出来是哪种服务器：ollama / lmstudio / 空的（认不出来）


def probe(endpoint: Endpoint) -> Found:
    """连一次这个接口。连不上、密钥不对抛出 EndpointError（说明见 errors.explain）。"""
    client = make(endpoint, timeout=ENDPOINT_PROBE_SECONDS, max_retries=0)
    try:
        listing = list(client.models.list())
    except openai.APIStatusError as exc:
        if exc.status_code in (404, 405, 501):
            return Found(listed=False, server=_server(client, endpoint))
        raise EndpointError(errors.explain(exc, endpoint)) from None
    except openai.APIError as exc:
        raise EndpointError(errors.explain(exc, endpoint)) from None
    except (ValueError, TypeError, AttributeError):  # 回答不是模型列表的样子（比如是一个网页）
        raise EndpointError(t("{provider} 回答的不是 OpenAI 格式的模型列表。请检查接口地址，一般以 /v1 结尾。", provider=endpoint.name)) from None
    models: list[EndpointModel] = []
    for item in listing:
        model = detect.from_listing(item)
        if model is None or any(known.id == model.id for known in models):
            continue
        try:
            model.id = check_model_id(model.id)  # 服务器给的名字也按同样的规矩查：有空格、太长的存不进去，干脆不列
        except EndpointError:
            continue
        models.append(model)
        if len(models) >= MAX_ENDPOINT_MODELS:
            break
    found = Found(models=models, server=_server(client, endpoint))
    if found.server == OLLAMA:
        for model in models[:ENDPOINT_DETECT_MODELS]:
            detect.apply_ollama(model, _ask(client, endpoint, "post", "/api/show", {"model": model.id}))
    elif found.server == LMSTUDIO:
        found.models = detect.apply_lmstudio(models, _ask(client, endpoint, "get", "/api/v0/models"))
    return found


def _server(client: Any, endpoint: Endpoint) -> str:
    """认一认是哪种服务器：Ollama 有 /api/version，LM Studio 有 /api/v0/models。都不是的话是空的。"""
    version = _ask(client, endpoint, "get", "/api/version")
    if isinstance(version, dict) and isinstance(version.get("version"), str):
        return OLLAMA
    listing = _ask(client, endpoint, "get", "/api/v0/models")
    if isinstance(listing, dict) and isinstance(listing.get("data"), list):
        return LMSTUDIO
    return ""


def _ask(client: Any, endpoint: Endpoint, method: str, path: str, body: dict | None = None) -> Any:
    """问一个"可能有、可能没有"的地址。没有、出错都返回 None：这些只是用来猜得更准，问不到也照样能用。"""
    url = root_of(endpoint.base_url) + path
    try:
        if method == "post":
            return client.post(url, cast_to=object, body=body)
        return client.get(url, cast_to=object)
    except (openai.APIError, ValueError):
        return None
