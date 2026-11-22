"""自定义接口出错时给人看的话。

和内置的几家（openai_compat._explain）比，多说几句"怎么办"：连不上的多半是服务器没开（Ollama 要 ollama serve，
LM Studio 要在"开发者"一页里启动服务器），证书不对的多半是公司网关用了自己签的证书，重定向是地址没写对。

出错说明里可能带着密钥（有的网关会把收到的密钥原样写在 401 的说明里），所以先把它换成 <hidden> 再给人看。
"""

from __future__ import annotations

from typing import Any

import openai

from ..i18n import Message, t
from ..net.http import scrub
from ..providers.base import brief, too_large
from .spec import Endpoint


def detail(exc: openai.APIError, endpoint: Endpoint) -> str:
    """API 自己说的出错原因，去掉密钥、整理成一小段（见 providers/base.brief）。"""
    return brief(scrub(str(getattr(exc, "message", "") or exc), [endpoint.api_key]))


def explain(exc: Exception, endpoint: Endpoint, model: str = "") -> Message:
    """把一个请求失败的原因翻译成看得懂、知道怎么办的一句话。"""
    name, url = endpoint.name, endpoint.base_url
    if isinstance(exc, openai.APITimeoutError):
        return t("{provider} 太久没有回答（{url}）。本地的大模型第一次加载要等一会儿，可以再试一次。", provider=name, url=url)
    if isinstance(exc, openai.APIConnectionError):
        cause = f"{exc.__cause__ or ''} {exc.__context__ or ''}"
        if "CERTIFICATE_VERIFY_FAILED" in cause or "certificate verify failed" in cause.lower():
            return t("{provider} 的 HTTPS 证书验证不过（{url}）。公司网关用的是自己签的证书的话，请把它加进系统信任的证书里。", provider=name, url=url)
        if endpoint.local:
            return t("连不上 {provider}（{url}）。请确认它开着：Ollama 运行 ollama serve；LM Studio 在“开发者”一页里点“启动服务器”；vLLM、llama.cpp 看启动它的那个终端。", provider=name, url=url)
        return t("连不上 {provider}（{url}），请检查网络和地址。", provider=name, url=url)
    if isinstance(exc, openai.AuthenticationError):
        return t("{provider} 不认这个 API 密钥（401）。请在设置的“自定义模型接口”里重新填写。", provider=name)
    if isinstance(exc, openai.PermissionDeniedError):
        return t("{provider} 拒绝了这个请求（403）：{error}", provider=name, error=detail(exc, endpoint))
    if isinstance(exc, openai.NotFoundError) and model:
        return t("{provider} 上找不到模型 {model}。在 Ollama 里要先 ollama pull 它；也可以在换模型的面板里同步一次模型列表。", provider=name, model=model)
    if isinstance(exc, openai.NotFoundError):
        return t("{provider} 在这个地址上找不到 OpenAI 格式的接口（404）：{url}。地址一般以 /v1 结尾。", provider=name, url=url)
    if isinstance(exc, openai.RateLimitError):
        return t("{provider} 提示请求太频繁，或者额度不足：{error}", provider=name, error=detail(exc, endpoint))
    if isinstance(exc, openai.APIStatusError) and 300 <= exc.status_code < 400:
        location = _location(exc)
        if location:
            return t("{provider} 把请求转到了 {location}（{status}）。智狐不跟着跳转：请直接把接口地址改成最终的那一个。", provider=name, location=location, status=exc.status_code)
        return t("{provider} 把请求转到了别处（{status}）。智狐不跟着跳转：请直接把接口地址改成最终的那一个。", provider=name, status=exc.status_code)
    if isinstance(exc, openai.APIStatusError) and exc.status_code == 413:
        return too_large(name)
    if isinstance(exc, (openai.BadRequestError, openai.UnprocessableEntityError)):
        text = detail(exc, endpoint)
        if model and _about_images(text):
            return t("{provider} 的 {model} 好像看不了图片：{error}。可以在设置的“自定义模型接口”里把这个模型的“看得懂图片”关掉。", provider=name, model=model, error=text)
        return t("请求被 {provider} 拒绝：{error}", provider=name, error=text)
    if isinstance(exc, openai.APIStatusError):
        return t("{provider} 的 API 出错（状态码 {status}）：{error}", provider=name, status=exc.status_code, error=detail(exc, endpoint))
    return t("调用 {provider} 失败：{error}", provider=name, error=scrub(str(exc), [endpoint.api_key]))


def _location(exc: openai.APIStatusError) -> str:
    response: Any = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    location = headers.get("location", "") if headers is not None else ""
    return brief(location) if isinstance(location, str) else ""


def _about_images(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in ("image", "vision", "multimodal", "image_url"))
