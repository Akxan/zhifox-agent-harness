"""备用模型：一个模型暂时用不了的时候，这一轮接下来改用同一家的另一个。

什么时候换：一次请求失败了，而且是"等一会儿多半就好"的那种——服务器过载（Anthropic 的 529 overloaded）、
服务器出错（5xx）、请求太频繁（429）。SDK 自己已经重试过两次（max_retries 默认是 2），还是不行才轮到这里。
密钥不对（401）、请求写错了（400）这类换个模型也没用的错误，照旧停下来告诉你。适配层把前一种错误抛成
providers.base.Unavailable，循环（agent.py）认的就是它。

换成哪个：你在设置里给每一家排好的备用模型，按顺序第一个能用的。默认一个都没有，也就是不换：
换模型会改变回答的质量和价格，要你自己决定换不换、换成哪个。

为什么只在同一家之内换。换到另一家的模型，接不上这段对话：
  - 历史的格式不一样（Claude 的内容块和 OpenAI 格式的消息不通用），只能开始一段新的对话；
  - 系统提示词也不一样（"能联网"那一行只写给 Claude），工具也不一样（Claude 用 Anthropic 定义的浏览器、电脑工具包，
    别家用我们自己写的 browser_*、computer_* 替身）。
  一轮做到一半换过去，等于把这一轮已经做过的工具调用和结果都扔掉，还悄悄换了提示词和工具，这不行。
  同一家之内换模型，历史照样能用，App.select_model 本来就允许对话中途这样换。

Claude 的思考内容（thinking 块）带着签名，换模型以后会怎样（claude-api 这个 skill 的
shared/preserved-thinking-migration/causes.md 里 "Switching models mid-conversation" 一节）：
  - 换模型本身从不让请求失败（两种 prefix_mismatch_behavior 下都是 200）。请求里的 model 不算历史的一部分，换它不算"改了历史"；
  - 新模型读不了的思考块，API 会在它看到之前去掉，不计费；历史本身我们一个字都不动，照样原样发过去（自己去删反而会出错）；
  - 所以 Opus 5.5 换成 Sonnet 5.5 时，这一轮接下来的几步看不到 Opus 之前的思考（Sonnet 5.5 读不了 Opus 5.5 的），
    但请求照常成功；下一轮换回 Opus 5.5，它读得了 Sonnet 5.5 留下的思考（在 Claude API 上），什么都不丢。
工具说明书也不能变：一段对话用哪些工具包，是开始时按那个模型定下的（见 providers/claude.py 的 use_tools），
备用模型必须用得了同样的工具包才换（Backend.can_switch_to）。

只换这一轮剩下的步骤：下一轮又从你选的那个模型开始（它可能已经好了）。这段会话记着的仍然是你选的那个模型。
换的时候对话里会说一声（notice），运行记录（trace/）里那几次请求标着 fallback；花的钱照常按实际用的模型记账。

这个文件只做三件小事：检查设置里存的备用模型列表、按设置挑出候选、写那句提示。
"""

from __future__ import annotations

import re
from typing import Any

from .config import FALLBACK_MAX_MODELS
from .i18n import Message, t

_PROVIDER_ID = re.compile(r"[a-z0-9_-]{1,40}")
MAX_MODEL_CHARS = 200
_CLAUDE = re.compile(r"claude-([a-z]+)-(\d+)(?:-(\d+))?")


def fallback_settings(saved: object) -> dict[str, list[str]]:
    """设置里存的备用模型：{提供方: [模型, …]}（按顺序）。只认这种形状，别的（文件被改坏了）按"没有"算。
    同一家里重复的只留第一个，每一家最多 FALLBACK_MAX_MODELS 个，空的列表不留。"""
    result: dict[str, list[str]] = {}
    if not isinstance(saved, dict):
        return result
    for provider, models in saved.items():
        if not isinstance(provider, str) or not _PROVIDER_ID.fullmatch(provider) or not isinstance(models, list):
            continue
        kept: list[str] = []
        for model in models:
            if isinstance(model, str) and model.strip() and len(model) <= MAX_MODEL_CHARS and model.strip() not in kept:
                kept.append(model.strip())
            if len(kept) >= FALLBACK_MAX_MODELS:
                break
        if kept:
            result[provider] = kept
    return result


def candidates(settings: Any, provider: str, model: str) -> list[str]:
    """这一家的模型 model 用不了时，可以改用的模型（按你排的顺序，去掉它自己）。没有备用的就是空的。"""
    return [item for item in settings.fallbacks.get(provider, []) if item != model]


def label(model: str) -> str:
    """模型给人看的简短名字：claude-opus-5-5 → Opus 5.5。认不出来的（别家的模型）原样写。"""
    match = _CLAUDE.fullmatch(model)
    if not match:
        return model
    family, major, minor = match.groups()
    return f"{family.capitalize()} {major}" + (f".{minor}" if minor else "")


def notice(failed: str, backup: str) -> Message:
    """换模型时对话里的那一句。"""
    return t("{failed} 暂时不可用，这一轮接下来改用 {fallback}。", failed=label(failed), fallback=label(backup))
