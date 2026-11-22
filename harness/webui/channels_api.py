from __future__ import annotations

from typing import TYPE_CHECKING

from .. import i18n
from ..channels.base import ChannelError
from ..i18n import t
from ..permissions import MODES
from ..sandbox import CHOICES as SANDBOX_CHOICES
from .tasks_api import _valid_answer

if TYPE_CHECKING:
    from .server import Handler
    from .webapp import WebApp


def route_channel(handler: Handler, web: WebApp, action: str, body: dict) -> None:
    """聊天通道的几个操作（见 channels/hub.py）。参数不对回答 400，现在做不了（token 不对、确认已经答复过了……）回答 409。

    这些都不拿对话的锁：聊天里的对话和网页上的对话各跑各的，你正在网页上聊的时候也能连上 Telegram、答复它的确认。
    """
    hub, platform = web.channels, body.get("platform")
    checks = {
        "connect": lambda: _valid_credentials(body),
        "pair": lambda: True,
        "cancel": lambda: True,
        "approval": lambda: body.get("mode") in MODES,
        "sandbox": lambda: body.get("level") in SANDBOX_CHOICES,
        "enable": lambda: isinstance(body.get("enabled"), bool),
        "unbind": lambda: True,
        "remove": lambda: True,
        "confirm": lambda: isinstance(body.get("chat"), str) and _valid_answer(body),
        "stop": lambda: isinstance(body.get("chat"), str),
    }
    if action not in checks:
        handler._send_json(404, {"error": "not found"})
        return
    if not isinstance(platform, str) or not platform or not checks[action]():
        handler._send_json(400, {"error": t("参数不正确")})
        return
    reply: dict = {"ok": True}
    try:
        if action == "connect":
            reply["bot"] = hub.connect(platform, body["credentials"] if isinstance(body.get("credentials"), dict) else {"token": body["token"]})
        elif action == "pair":
            reply.update(hub.pair(platform))
        elif action == "cancel":
            hub.cancel_pairing(platform)
        elif action == "approval":
            hub.set_approval(platform, body["mode"])
        elif action == "sandbox":
            hub.set_sandbox(platform, body["level"])
        elif action == "enable":
            hub.set_enabled(platform, body["enabled"])
        elif action == "unbind":
            hub.unbind(platform)
        elif action == "remove":
            hub.remove(platform)
        elif action == "confirm":
            hub.confirm(platform, body["chat"], body["confirm_id"], body["allow"])
        else:
            hub.stop_chat(platform, body["chat"])
    except ChannelError as exc:
        handler._send_json(409, {"error": i18n.message_of(exc)})
        return
    handler._send_json(200, reply)


def _valid_credentials(body: dict) -> bool:
    """连接一个聊天平台时填的东西：credentials 是 {名字: 文字}（最多 8 项，每项最多 400 个字），至少有一项不是空的
    （没重填的那几项是空的，接着用存着的，见 channels/hub.merge）。只有一个 token 的旧写法（Telegram）也认。"""
    given = body.get("credentials")
    if given is None and "token" in body:
        given = {"token": body["token"]}
    if not isinstance(given, dict) or not 0 < len(given) <= 8:
        return False
    if not all(isinstance(name, str) and len(name) <= 40 and isinstance(value, str) and len(value.strip()) <= 400 for name, value in given.items()):
        return False
    return any(value.strip() for value in given.values())
