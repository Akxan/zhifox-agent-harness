"""一个元素身上最后算数的是哪几条声明：行内的 style 加上网页里 <style> 块的规则（fetch/readable.py 认藏起来的字用）。

为什么要看 <style> 块：只看行内的 style，网页写一句 <style>.x{display:none}</style> 就绕过去了。规则在整页里先收齐
（readable.py 先扫一遍），所以写在 body 最后的 <style> 也算数；<template>、<noscript> 里的不生效，不算（readable.py 的 _Tokens）。
外部的样式表（<link rel=stylesheet>）不下载、不看。

哪些规则算数。我们不搭整棵网页的树、不看屏幕多宽，所以分成两种：
  确定用得上的：不在条件里（@media (max-width…) 这类），选择器只有一段、只有标签 class id（.a、#b、div.a、.a.b，逗号分开的几个）。
    只有这种能把字藏起来。
  说不准的：@media（只在窄屏上藏的，宽屏上看得见）、@supports、@container 里的，<style media="…"> 里的，
    选择器有好几段（.open .panel：外层是什么我们不看）、带条件（:hover、:not(.show)、[aria-expanded]）的。
    它们只能让字重新看得见：有一条落在这个元素上（只比最后一段的标签 class id），而且它要是用上了这个元素就看得见，就当作看得见。
    Tailwind、Bootstrap 都是"先藏起来，宽屏再显示"（class="hidden md:block"），网页上的标签页、展开收起靠外层的 class 切换，
    这些都靠这一条才不会被当成藏起来。宁可漏掉一段藏起来的字，也不丢掉你看得到的正文。
  只给打印用的（@media print）、@font-face、@keyframes 里的不算。
先后和浏览器一样：!important 的最大，然后行内的 style，然后选择器越具体越大（id > class > 标签），一样大的后写的算数。
"说不准的"那一条要比现在算数的那条大（或者一样大）才换得上：section{display:block} 这种重置样式盖不过 .x{display:none}。
两个上限（config.py）：<style> 块最多读 FETCH_MAX_STYLE_CHARS 个字，拿元素对规则一共最多对 FETCH_MAX_STYLE_CHECKS 次。
超了只是少认出一些藏起来的字，故意写满规则的网页拖不住读网页的那一步。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..config import FETCH_MAX_STYLE_CHARS, FETCH_MAX_STYLE_CHECKS
from . import css, hidden

_Rank = tuple[bool, bool, tuple[int, int, int]]  # （!important，是不是行内的，具体程度）
_SEEN_MAX = 4096  # 记住多少种算过的元素，满了从头记（只是省时间，不影响结果）


@dataclass(frozen=True)
class _Rule:
    selector: css.Selector
    declarations: tuple[css.Declaration, ...]  # 只有 hidden.PROPERTIES 里的，简写已经拆开
    order: int  # 在整页里是第几条：一样大的后写的算数


class Styles:
    """一个网页里所有 <style> 块的规则，按落在谁身上（id、class、标签）分好，读每个元素时查。"""

    def __init__(self, sheets: Iterable[tuple[str, str]] = ()):
        """sheets：（<style> 的 media 属性，里面的 CSS），按网页里的先后。太长的只读前面 FETCH_MAX_STYLE_CHARS 个字。"""
        self.sure: dict[str, list[_Rule]] = {}  # 确定用得上的（见文件开头）
        self.unsure: dict[str, list[_Rule]] = {}  # 说不准的：只能让它重新看得见
        self._seen: dict[tuple[str, str, str, str, hidden.Ink], hidden.Look] = {}  # 算过的元素（见 look）
        self._checks = FETCH_MAX_STYLE_CHECKS  # 还能拿元素对多少次规则：用完了，剩下的元素只看行内的 style
        budget = FETCH_MAX_STYLE_CHARS
        order = 0
        for media, sheet in sheets:
            answer = css.media(media)
            if answer == "never" or budget <= 0:
                continue
            sheet, budget = sheet[:budget], budget - len(sheet)
            for rule in css.rules(sheet, conditional=answer == "maybe"):
                declarations = tuple(hidden.longhands(css.declarations(rule.body)))
                if not declarations:
                    continue  # 和看不看得见无关的规则（颜色以外的样子、动画……）不用拆选择器
                for selector in css.selectors(rule.selectors):
                    order += 1
                    sure = selector.simple and not rule.conditional
                    (self.sure if sure else self.unsure).setdefault(_key(selector), []).append(_Rule(selector, declarations, order))

    def look(self, tag: str, attributes: dict[str, str], parent: hidden.Ink) -> hidden.Look:
        """这个元素（标签名、属性）看不看得见，parent 是外层传下来的。"""
        style, own_id, classes = attributes.get("style", ""), attributes.get("id", ""), attributes.get("class", "")
        if not style and (not self.sure or self._checks <= 0) and not parent.blank:
            return hidden.Look(gone=False, ink=parent)  # 最常见的：网页没有能藏字的规则、它也没写 style
        key = (tag, own_id, classes, style, parent)  # 一个网页里同样的元素（列表项、卡片）一再出现，算一次就够
        seen = self._seen.get(key)
        if seen is None:
            if len(self._seen) >= _SEEN_MAX:
                self._seen.clear()
            seen = self._seen[key] = self._look(tag, style, own_id, classes.split(), parent)
        return seen

    def _look(self, tag: str, style: str, own_id: str, classes: list[str], parent: hidden.Ink) -> hidden.Look:
        inline = hidden.longhands(css.declarations(style)) if style else []
        element = (tag, own_id, classes)
        sure = self._matching(self.sure, *element)
        if not inline and not sure and not parent.blank:
            return hidden.Look(gone=False, ink=parent)  # 最常见的：什么都没写，照着外层
        winners: dict[str, tuple[_Rank, int, str]] = {}  # 属性 → （先后，第几条，值）
        for rule in sorted(sure, key=lambda rule: rule.order):
            for item in rule.declarations:
                _offer(winners, item, (item.important, False, rule.selector.specificity), rule.order)
        for index, item in enumerate(inline):
            _offer(winners, item, (item.important, True, (0, 0, 0)), index)
        props = {name: value for name, (_, _, value) in winners.items()}
        verdict = hidden.look(props, parent)
        if verdict.rank() == (False, False, False):
            return verdict
        trials = [verdict]
        for rule in self._matching(self.unsure, *element):
            trial = dict(props)
            for item in rule.declarations:
                current = winners.get(item.name)
                if current is None or (item.important, False, rule.selector.specificity) >= current[0]:
                    trial[item.name] = item.value
            if trial != props:
                trials.append(hidden.look(trial, parent))
        return min(trials, key=hidden.Look.rank)  # 几种可能里最看得见的那一种（一样的话照确定的那个）

    def _matching(self, index: dict[str, list[_Rule]], tag: str, own_id: str, classes: list[str]) -> list[_Rule]:
        """落在这个元素上的规则（只比最后一段的标签、class、id）。

        每对一条规则记一次数，一个网页一共最多 FETCH_MAX_STYLE_CHECKS 次：故意写几万条规则、几万个各不相同的元素的网页，
        对下去要花几分钟，读网页的那一步就卡住了。用完以后只是少认出一些藏起来的字。
        """
        if not index or self._checks <= 0:
            return []
        keys = {tag, "*", *("." + name for name in classes)}
        if own_id:
            keys.add("#" + own_id)
        found = []
        for key in keys:
            rules = index.get(key, ())
            self._checks -= len(rules) + 1
            for rule in rules:
                selector = rule.selector
                if (
                    selector.tag in ("", tag)
                    and all(name in classes for name in selector.classes)
                    and all(name == own_id for name in selector.ids)
                ):
                    found.append(rule)
        return found


def _offer(winners: dict[str, tuple[_Rank, int, str]], item: css.Declaration, rank: _Rank, order: int) -> None:
    current = winners.get(item.name)
    if current is None or (rank, order) >= current[:2]:
        winners[item.name] = (rank, order, item.value)


def _key(selector: css.Selector) -> str:
    """一条规则按什么查：有 id 按 id，有 class 按第一个 class，再不然按标签名（"*" 是谁都算）。"""
    if selector.ids:
        return "#" + selector.ids[0]
    if selector.classes:
        return "." + selector.classes[0]
    return selector.tag or "*"

