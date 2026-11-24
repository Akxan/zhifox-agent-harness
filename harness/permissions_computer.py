from __future__ import annotations

from .desktop.session import Facts as DesktopFacts
from .desktop.session import trust_of
from .i18n import k, t
from .permissions_cards import _preview, always, offering

# ---- 操作整台电脑 --------------------------------------------------------------

_CLICKS = {("left", 1): k("左键单击"), ("left", 2): k("左键双击"), ("left", 3): k("左键连点三下"), ("right", 1): k("右键单击"), ("middle", 1): k("中键单击")}
_DIRECTIONS = {"up": k("向上"), "down": k("向下"), "left": k("向左"), "right": k("向右")}


def _spot(facts: DesktopFacts, point: tuple[int, int] | None = None) -> str:
    """一个位置，给人看的说法：截图上的坐标；屏幕上的点不一样的话（缩小过、Retina）也写上。"""
    point = point if point is not None else facts.point
    if point is None:
        return t("鼠标现在的位置")
    if facts.on_screen is not None and facts.on_screen != tuple(point) and point is facts.point:
        return t("({x}, {y})（屏幕上的 ({sx}, {sy})）", x=point[0], y=point[1], sx=facts.on_screen[0], sy=facts.on_screen[1])
    return f"({point[0]}, {point[1]})"


def _app(facts: DesktopFacts) -> str:
    return facts.app or t("（看不出是哪个程序）")


def review_computer(facts: DesktopFacts) -> str | None:
    """操作整台电脑的一次动作怎么问你（规矩见文件开头）。返回要显示的说明；不用问（根本不会做、或者什么都不碰）返回 None。

    危险的组合键标着 always：换哪一档、放开了什么都照样要问，卡片上也不给"不再问"。别的动作在你正看着的对话里
    （facts.attended）多给一个选择：截图、放大是 screenshots，点击、打字这些是 actions（desktop/session.py 的 trust_of）；
    定时任务、聊天通道的运行不给。真正放不放开由 allowance 定，这里只管卡片上画什么。"""
    question = _review_computer(facts)
    if question is None:
        return None
    if facts.danger:
        return always(question)
    offer = trust_of(facts.action)
    return offering(question, offer) if facts.attended and offer else question


def _review_computer(facts: DesktopFacts) -> str | None:
    action = facts.action
    if not facts.ready or facts.bad or facts.blocked or action in ("wait", "cursor"):
        return None  # 关着、用不了、参数不对、碰到不许碰的程序：工具会直接回答，什么都不会做。等一等、看鼠标在哪儿：什么都不碰
    front = t("\n  最前面的程序：{app}", app=_app(facts))
    if action == "screenshot":
        return t("截一张整个屏幕的图交给模型。屏幕上的一切它都看得到：别的程序、通知里的消息、打开着的文件。{front}", front=front)
    if action == "zoom":
        region = ", ".join(str(value) for value in facts.region)
        return t("把屏幕上的一块（[{region}]）放大了交给模型看。{front}", region=region, front=front)
    held = t("\n  同时按着：{keys}", keys="+".join(facts.modifiers)) if facts.modifiers else ""
    on = t("\n  落在：{app} 的窗口上", app=_app(facts))
    if action == "click":
        what = t(_CLICKS.get((facts.button, facts.count if facts.button == "left" else 1), _CLICKS[("left", 1)]))
        return t("在电脑上点击鼠标（{what}）：{where}{on}{held}", what=what, where=_spot(facts), on=on, held=held)
    if action == "drag":
        return t("在电脑上按住鼠标左键拖动：从 {start} 到 {end}{on}{held}", start=_spot(facts), end=_spot(facts, facts.end), on=on, held=held)
    if action == "move":
        return t("把鼠标移到 {where}{on}", where=_spot(facts), on=on)
    if action == "press":
        return t("在 {where} 按下鼠标左键（先不松开，接下来通常是拖动）{on}", where=_spot(facts), on=on)
    if action == "release":
        return t("在 {where} 松开鼠标左键{on}", where=_spot(facts), on=on)
    if action == "scroll":
        return t("在电脑上滚动：{where}，{direction} {amount} 格{on}{held}", where=_spot(facts), direction=t(_DIRECTIONS.get(facts.direction, facts.direction)), amount=facts.amount, on=on, held=held)
    # 填进去的每一段都保持是 t() 给的一句话（不和别的字拼起来），页面换语言时才能连里面的一起重译（见 i18n.Message）
    warning = t("\n\n{warning}", warning=t(facts.danger)) if facts.danger else ""
    target = t("\n  打到：{app}（智狐会先把它换回最前面）", app=_app(facts))
    if action == "type":
        newline = t("\n\n{warning}", warning=t("⚠ 内容里有换行：在很多程序里，换行就是按回车（发送、提交）。")) if facts.newline else ""
        return t("在电脑上打字：{target}\n  内容：\n{content}{newline}", target=target, content=_preview(facts.text, 8), newline=newline)
    if action == "key":
        times = t("（{n} 次）", n=facts.repeat) if facts.repeat > 1 else ""
        return t("在电脑上按键：{keys}{times}{target}{warning}", keys=facts.text, times=times, target=target, warning=warning)
    if action == "hold":
        return t("在电脑上按住 {keys} {seconds} 秒{target}{warning}", keys=facts.text, seconds=f"{facts.duration:g}", target=target, warning=warning)
    return t("在电脑上做一个动作：{action}{front}", action=action, front=front)
