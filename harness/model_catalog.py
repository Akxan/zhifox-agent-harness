"""模型目录：网页顶栏的"换模型"面板和终端的 /model 要显示的东西。

为什么单独一个文件：换模型的面板要回答好几个问题——每一家有没有密钥、能选哪些模型、每个模型能做什么、
多少钱、换过去会怎样。这些规矩以前散在几处（服务器里算模型列表，页面上自己判断要不要提醒"会开始新对话"），
现在只在这里算一份，网页和终端都照着它画（"同一条规则只写在一处"）：

  - 能选哪些模型：同步到的列表；Claude 在同步之前先用内置的几个（CLAUDE_MODELS）；现在用的那个总在里面，
    哪怕它不在同步到的列表里（比如列表是很久以前同步的）。
  - 每个模型能做什么：按 Provider 上的标记（vision、pdf、web）算。联网搜索还要看 config.web（可以整个关掉）。
    一个都没有的写"只认文字"，免得你以为它也看得懂图片。分时段计价的（DeepSeek）另外标一句。
  - 多少钱：pricing.price_of，你在设置里填的优先，其次是内置的；都没有就是 None，页面写"没有价格"。
  - 换过去会怎样（switch）：和 App.select_model 是同一条规矩——同一家之内接着聊；换到另一家，历史的格式不通用，
    开始新的一段。这段对话已经聊过的话，页面先在原地问一声（旧的这段已经存好了，随时可以回去接着聊）。
    比较的是这段对话的适配层用的是哪一家（agent.backend.provider），和 select_model 一样。

格式（catalog 返回 Catalog，里面给人看的话是 t() 译好的 Message，webui/models_api.py 再换成页面能就地重译的样子）：

  Catalog
    provider, model   现在用的是哪一家的哪个模型
    started           这段对话聊起来了没有（发过消息了）
    persona           这段对话的角色（用一个 Agent 开始的对话）：Persona；普通的对话是 None
    groups            [Group, …]，按 PROVIDERS 的顺序

  Persona   name 名字；model 它文件里选的"提供方/模型"（没选、或者它现在用不了，是空的）
            换模型不换角色：同一家接着聊，换到另一家，新的一段也用它开始（见 App.select_model）

  Group（一家）
    id, label        编号（anthropic……）和给人看的名字
    has_key          有没有密钥；key_hint 只有最后 4 位（"…abcd"），从不带密钥本身
    synced           模型列表是不是同步来的（False：没同步过，Claude 用的是内置的几个，别家是空的）
    switch           换到这一家的模型会怎样：continue 同一家，对话接着进行 / fresh 另一家，这段还没聊，直接开始新的 /
                     leave 另一家，这段聊过了：开始新的一段之前先问一声
    models           [Model, …]

  Model（一个模型）
    id               模型名（claude-opus-5-5……）
    current          是不是现在用的
    tags             [Message, …]：能做什么（能联网、看得懂图片、能读 PDF / 只认文字、分时段计价）
    price            {input, output, cache_read, cache_write}，美元 / 每百万 token；没有价格是 None
    offpeak          闲时价（分时段计价的模型），格式和 price 一样；不分时段的是 None
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any

from .i18n import Message, k, t
from .pricing import price_of
from .providers import CLAUDE_MODELS, PROVIDERS, Provider, has_key
from .settings import key_hint

# 换到这一家会怎样（见开头的说明）
CONTINUE, FRESH, LEAVE = "continue", "fresh", "leave"

# 每个模型能做什么，按 Provider 上的标记。顺序就是页面上的顺序
WEB = k("能联网")
VISION = k("看得懂图片")
PDF = k("能读 PDF")
TEXT_ONLY = k("只认文字")
OFFPEAK = k("分时段计价")


@dataclass
class Model:
    id: str
    current: bool
    tags: list[Message]
    price: dict[str, float] | None
    offpeak: dict[str, float] | None


@dataclass
class Group:
    id: str
    label: str
    has_key: bool
    key_hint: str
    synced: bool
    switch: str
    models: list[Model] = field(default_factory=list)


@dataclass
class Persona:
    name: str
    model: str


@dataclass
class Catalog:
    provider: str
    model: str
    started: bool
    persona: Persona | None
    groups: list[Group]


def models_of(settings: Any, provider_id: str) -> list[str]:
    """一家可以选择的模型：同步到的列表；Claude 在同步之前先用内置的几个；现在用的那个总在最前面（不在列表里的话）。"""
    models = list(settings.models.get(provider_id) or (CLAUDE_MODELS if provider_id == "anthropic" else []))
    if provider_id == settings.provider and settings.model not in models:
        models.insert(0, settings.model)
    return models


def tags_of(provider: Provider, model: str, web: bool, offpeak: bool) -> list[Message]:
    """一个模型能做什么，给人看的几个词。web：这次运行开没开联网搜索（config.web）。"""
    tags = []
    if web and provider.web:
        tags.append(t(WEB))
    if provider.vision:
        tags.append(t(VISION))
    if provider.pdf:
        tags.append(t(PDF))
    if not provider.vision and not provider.pdf:
        tags.append(t(TEXT_ONLY))
    if offpeak:
        tags.append(t(OFFPEAK))
    return tags


def catalog(app: Any) -> Catalog:
    """现在的模型目录。只读内存里的设置和环境变量，不联网，对话进行中也可以调用。"""
    settings, backend = app.settings, app.agent.backend
    started = bool(backend.export())
    groups = []
    for provider in PROVIDERS.values():
        if provider.id == backend.provider:
            switch = CONTINUE
        else:
            switch = LEAVE if started else FRESH
        models = []
        for name in models_of(settings, provider.id):
            price = price_of(name, settings.prices)
            cheaper = price_of(name, settings.prices, offpeak=True)
            offpeak = cheaper if cheaper != price else None
            models.append(
                Model(
                    id=name,
                    current=provider.id == settings.provider and name == settings.model,
                    tags=tags_of(provider, name, app.config.web, offpeak is not None),
                    price=price,
                    offpeak=offpeak,
                )
            )
        groups.append(
            Group(
                id=provider.id,
                label=provider.label,
                has_key=has_key(provider),
                key_hint=key_hint(provider.key_env),
                synced=bool(settings.models.get(provider.id)),
                switch=switch,
                models=models,
            )
        )
    persona = None
    if app.conversation.agent:
        found = app.persona if app.persona is not None and app.persona.name == app.conversation.agent else None
        persona = Persona(name=app.conversation.agent, model=found.spec.model if found is not None else "")
    return Catalog(provider=settings.provider, model=settings.model, started=started, persona=persona, groups=groups)


def money(value: float) -> str:
    """一个价格的写法：$4、$0.30、$0.006。终端用；页面按界面语言自己写数字（见 page/scripts/models.js 的 usd）。

    整数不写小数；别的至少两位小数，很便宜的（命中缓存的输入，或者你自己填的）有几位写几位，最多六位，
    不会把 0.0001 写成 $0。
    """
    if value == int(value):
        return f"${int(value)}"
    whole, _, cents = f"{value:.6f}".rstrip("0").partition(".")
    return f"${whole}.{cents.ljust(2, '0')}"


def lines(found: Catalog) -> list[tuple[str, str]]:
    """终端 /model 要打印的几行：（文字，样式）。样式是 plain 或 info（暗一点的说明）。"""
    out: list[tuple[str, str]] = []
    for group in found.groups:
        status = t("已设置密钥 {hint}", hint=group.key_hint).strip() if group.has_key else t("未设置密钥")
        out.append((f"{group.label}  [{status}]", "plain"))
        if not group.has_key:
            continue
        if not group.models:
            out.append(("    " + t("还没有模型列表：在网页界面的换模型面板里点“同步模型列表”。"), "info"))
        for model in group.models:
            mark = "✓" if model.current else " "
            price = f"{money(model.price['input'])} / {money(model.price['output'])}" if model.price else t("没有价格")
            out.append((f"  {mark} {model.id}  ·  {' · '.join(model.tags)}  ·  {price}", "plain"))
    out.append((t("价格是美元 / 每百万 token（输入 / 输出）。"), "info"))
    if found.persona is not None:
        out.append((t("这段对话的角色是「{name}」。换模型以后角色不变。", name=found.persona.name), "info"))
    shortcut = "⌘." if sys.platform == "darwin" else "Ctrl+."  # 和页面上的写法一样（见 page/scripts/models.js 的 MODELS_KEY）
    out.append((t("换模型：在网页界面里点顶上的模型按钮（快捷键 {key}）。选择会保存下来，终端版也会跟着用。", key=shortcut), "info"))
    return out
