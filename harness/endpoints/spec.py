"""一个自定义接口长什么样，以及页面交来的表单怎么检查。

  Endpoint        一个接口：编号、名字、地址、可选的密钥、接口支持什么、模型列表
  EndpointModel   接口上的一个模型：看不看得懂图片、能不能用工具、读不读得了 PDF、上下文上限、价格

为什么能力按模型记，不像内置的几家那样按"家"记：同一个 Ollama 上，qwen3 能用工具、看不了图，gemma3 看得了图，
llama2 连工具都用不了。内置的几家（见 providers/__init__.py 的 PROVIDERS）同一家的模型能力差不多，按家记就够了。

价格：None 表示"按默认的"——本机和内网的接口不花钱（算 0），外面的网关不知道多少钱（算"没有价格"，不计入估算）。
你填了就按你填的。所以把地址从本机改到公司的网关上，原来"不计费"的模型会自动变成"没有价格"，不会悄悄记成 0 元。

编号（id）：从名字取（只留英文字母、数字和连字符），之后不再变，改名字也不变——会话文件、Agent 文件（model: custom-ollama/qwen3）、
命令行的 --model 都靠它认出是哪个接口。真正用来区分"哪一家"的是加了前缀的 provider_id（custom-ollama），
前缀保证它永远不会和内置的几家（anthropic、openai……）撞名，以后再加一家内置的也不会。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

from ..config import CUSTOM_CONTEXT_WINDOW, MAX_ENDPOINT_MODELS
from ..i18n import Message, t
from ..safe import clean_text
from . import urls

PREFIX = "custom-"  # provider_id = PREFIX + id
MAX_NAME = 40
MAX_MODEL_CHARS = 200
MIN_CONTEXT, MAX_CONTEXT = 1024, 100_000_000
MAX_PRICE = 100_000.0  # 美元 / 每百万 token。再大多半是填错了
# 密钥只认可见的 ASCII 字符（不能有空格、换行、中文），和 .env 里的密钥一样。本地的网关常用很短的口令（lm-studio），所以不限最短
_KEY = re.compile(r"[\x21-\x7e]{1,1000}")
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class EndpointError(ValueError):
    """表单或者文件里的东西不对。说明是一句译好的话，可以直接给人看。"""

    def __init__(self, message: Message):
        super().__init__(message)
        self.message = message


@dataclass
class EndpointModel:
    id: str
    vision: bool = False
    tools: bool = True  # 默认当它能用：现在的大多数模型都能；不能的话，第一次请求被拒绝时会自动关掉（见 openai_compat.py）
    pdf: bool = False  # OpenAI 自己的 PDF 写法（"file" 内容块）。本地的服务器一般不认，转发到 GPT 的网关才认
    context: int | None = None  # 上下文上限；None 按 CUSTOM_CONTEXT_WINDOW
    price: dict[str, float] | None = None  # None 按默认（见开头的说明）

    @property
    def window(self) -> int:
        return self.context or CUSTOM_CONTEXT_WINDOW

    def saved(self) -> dict:
        data: dict[str, Any] = {"id": self.id, "vision": self.vision, "tools": self.tools, "pdf": self.pdf}
        if self.context:
            data["context"] = self.context
        if self.price is not None:
            data["price"] = self.price
        return data


@dataclass
class Endpoint:
    id: str
    name: str
    base_url: str
    api_key: str = ""
    # 流式回答里能不能要用量（stream_options）。有的服务器不认这个参数、直接报 400：第一次被拒绝时记成 False，以后不再发
    stream_usage: bool = True
    models: list[EndpointModel] = field(default_factory=list)

    @property
    def provider_id(self) -> str:
        return PREFIX + self.id

    @property
    def where(self) -> str:
        return urls.where_is(urlsplit(self.base_url).hostname or "")

    @property
    def local(self) -> bool:
        return self.where != urls.REMOTE

    @property
    def origin(self) -> str:
        return urls.check(self.base_url).origin

    def model(self, name: str) -> EndpointModel | None:
        return next((model for model in self.models if model.id == name), None)

    def caps(self, name: str) -> EndpointModel:
        """一个模型能做什么。列表里没有的（Agent 文件里写的、命令行给的）按默认的算：只认文字、能用工具。"""
        return self.model(name) or EndpointModel(id=name)

    def price_of(self, name: str) -> dict[str, float] | None:
        """一个模型的价格（美元 / 每百万 token）：填了按填的；没填的话本机和内网的是 0，外面的是 None（不知道）。"""
        model = self.model(name)
        price = model.price if model is not None else None
        if price is None:
            return {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0} if self.local else None
        return {
            "input": float(price["input"]),
            "output": float(price["output"]),
            "cache_read": float(price.get("cache_read", price["input"])),
            "cache_write": float(price.get("cache_write", price["input"])),
        }

    def saved(self) -> dict:
        data: dict[str, Any] = {"id": self.id, "name": self.name, "base_url": self.base_url}
        if self.api_key:
            data["api_key"] = self.api_key
        if not self.stream_usage:
            data["stream_usage"] = False
        data["models"] = [model.saved() for model in self.models]
        return data

    def hint(self) -> str:
        """给页面看的密钥：只有最后 4 位（和内置几家的 settings.key_hint 一样）。没有密钥是空的。"""
        return f"…{self.api_key[-4:]}" if self.api_key else ""


# ---- 检查 -----------------------------------------------------------------------


def slug(name: str, taken: set[str]) -> str:
    """从名字取一个编号：小写英文字母、数字、连字符，最长 24 个字符；取不出来（全是中文）用 endpoint；重了加 -2、-3。"""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24].strip("-") or "endpoint"
    candidate, number = base, 2
    while candidate in taken:
        candidate = f"{base}-{number}"
        number += 1
    return candidate


def check_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EndpointError(t("请给这个接口起个名字，比如 Ollama。"))
    name = " ".join(clean_text(value).split())
    if len(name) > MAX_NAME:
        raise EndpointError(t("名字最多 {n} 个字符。", n=MAX_NAME))
    return name


def check_key(value: object) -> str:
    if not isinstance(value, str):
        raise EndpointError(t("参数不正确"))
    key = value.strip()
    if key and not _KEY.fullmatch(key):
        raise EndpointError(t("这不像是一个 API 密钥：里面不能有空格、换行或中文"))
    return key


def check_model_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EndpointError(t("模型名不能是空的。"))
    name = value.strip()
    if len(name) > MAX_MODEL_CHARS or any(char.isspace() or ord(char) < 32 for char in name):
        raise EndpointError(t("模型名“{model}”不对：不能有空格，最多 {n} 个字符。", model=clean_text(name[:60]), n=MAX_MODEL_CHARS))
    return clean_text(name)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def check_price(value: object, model: str) -> dict[str, float] | None:
    """None（按默认）或者 {input, output[, cache_read]}，都是 0 到 MAX_PRICE 之间的数。"""
    if value is None:
        return None
    problem = EndpointError(t("{model} 的价格不对：输入和输出都要填 0 或者更大的数（美元 / 每百万 token）。", model=model))
    if not isinstance(value, dict):
        raise problem
    price: dict[str, float] = {}
    for name in ("input", "output", "cache_read"):
        if name not in value or value[name] is None:
            if name == "cache_read":
                continue
            raise problem
        number = _number(value[name])
        if number is None or not 0 <= number <= MAX_PRICE:
            raise problem
        price[name] = number
    return price


def check_context(value: object, model: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_CONTEXT <= value <= MAX_CONTEXT:
        raise EndpointError(t("{model} 的上下文上限要填 {low} 到 {high} 之间的整数（token），不知道的话空着。", model=model, low=MIN_CONTEXT, high=MAX_CONTEXT))
    return value


def check_model(value: object) -> EndpointModel:
    if not isinstance(value, dict):
        raise EndpointError(t("参数不正确"))
    name = check_model_id(value.get("id"))
    vision, tools, pdf = value.get("vision"), value.get("tools"), value.get("pdf")
    if not isinstance(vision, bool) or not isinstance(tools, bool) or not isinstance(pdf, bool):  # 逐个判断，类型检查才知道下面三个都是 bool
        raise EndpointError(t("参数不正确"))
    return EndpointModel(
        id=name, vision=vision, tools=tools, pdf=pdf,
        context=check_context(value.get("context"), name), price=check_price(value.get("price"), name),
    )


def check_models(value: object) -> list[EndpointModel]:
    if not isinstance(value, list):
        raise EndpointError(t("参数不正确"))
    if len(value) > MAX_ENDPOINT_MODELS:
        raise EndpointError(t("一个接口最多记 {n} 个模型。", n=MAX_ENDPOINT_MODELS))
    models: list[EndpointModel] = []
    for item in value:
        model = check_model(item)
        if any(known.id == model.id for known in models):
            raise EndpointError(t("模型“{model}”写了两次。", model=model.id))
        models.append(model)
    return models


def from_form(form: dict, existing: Endpoint | None, taken: set[str]) -> Endpoint:
    """页面交来的表单 → 一个接口。existing 是正在编辑的那一个（新建的是 None），taken 是已经用掉的编号。

    密钥（api_key）有三种写法：一段文字（新的密钥）、空字符串（不用密钥）、{"keep": true}（不改，和原来一样）。
    "不改"只在地址的协议、主机、端口都没变时才行：换了服务器还带着旧的密钥，等于把密钥交给了另一台服务器。
    """
    name = check_name(form.get("name"))
    try:
        checked = urls.check(form.get("base_url"))
    except urls.UrlError as exc:
        raise EndpointError(exc.message) from None
    key = keep_or_new_key(form.get("api_key"), existing, checked.origin)
    endpoint = Endpoint(
        id=existing.id if existing is not None else slug(name, taken),
        name=name,
        base_url=checked.url,
        api_key=key,
        stream_usage=existing.stream_usage if existing is not None and existing.origin == checked.origin else True,
        models=check_models(form.get("models", [])),
    )
    return endpoint


def draft(form: dict, existing: Endpoint | None) -> Endpoint:
    """还没保存的表单 → 一个只用来"测试连接"的接口：只查地址和密钥（规矩和 from_form 一样），名字没填也行，模型列表不要。"""
    try:
        checked = urls.check(form.get("base_url"))
    except urls.UrlError as exc:
        raise EndpointError(exc.message) from None
    name = form.get("name")
    shown = " ".join(clean_text(name).split())[:MAX_NAME] if isinstance(name, str) and name.strip() else checked.url
    return Endpoint(id="draft", name=shown, base_url=checked.url, api_key=keep_or_new_key(form.get("api_key"), existing, checked.origin))


def keep_or_new_key(value: object, existing: Endpoint | None, origin: str) -> str:
    """表单里的密钥一栏 → 真正要用的密钥（见 from_form 的说明）。"""
    if isinstance(value, dict) and value.get("keep") is True:
        if existing is None or not existing.api_key:
            return ""
        if existing.origin != origin:
            raise EndpointError(t("换了服务器的地址，请重新填一次 API 密钥（不需要密钥的话清空它）：旧的密钥不会发给新的地址。"))
        return existing.api_key
    return check_key(value)


def from_saved(data: object) -> Endpoint:
    """endpoints.json 里的一项 → 一个接口。读不懂抛出 EndpointError（文件被改坏了）。"""
    if not isinstance(data, dict):
        raise EndpointError(t("参数不正确"))
    ident = data.get("id")
    if not isinstance(ident, str) or not _SLUG.fullmatch(ident) or len(ident) > 40:
        raise EndpointError(t("参数不正确"))
    try:
        checked = urls.check(data.get("base_url"))
    except urls.UrlError as exc:
        raise EndpointError(exc.message) from None
    stream_usage = data.get("stream_usage", True)
    return Endpoint(
        id=ident,
        name=check_name(data.get("name")),
        base_url=checked.url,
        api_key=check_key(data.get("api_key", "")),
        stream_usage=stream_usage if isinstance(stream_usage, bool) else True,
        models=check_models(data.get("models", [])),
    )


def merged(old: list[EndpointModel], found: list[EndpointModel]) -> list[EndpointModel]:
    """同步来的模型列表：服务器上现在有的那些，按服务器的顺序；以前就有的保留你改过的设置（能力、上下文、价格），
    新来的用猜出来的（见 detect.py）。服务器上已经没有的去掉。以前没填上下文上限、这次服务器说了的，补上。"""
    known = {model.id: model for model in old}
    out = []
    for model in found:
        mine = known.get(model.id)
        out.append(model if mine is None else replace(mine, context=mine.context or model.context))
    return out
