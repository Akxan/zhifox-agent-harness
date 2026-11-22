"""网页上"设置 → 语音转文字"背后的接口：/api/voice/……（功能本身见 ../voice/）。

  POST /api/voice/settings   改用哪一种（auto / openai / local / off）、OpenAI 的模型、本机服务的地址和模型名，
                             或者改用哪个自定义模型接口（endpoint：它的编号；空的表示自己填地址）

状态里的 voice（见 state）：现在的设置、按它实际会用哪一种（active）、有没有 OpenAI 的密钥（只有有没有，没有密钥本身）、
每个 OpenAI 模型每分钟多少钱（pricing.TRANSCRIBE_PRICES，页面照着写，不自己存一份）、最长转几分钟、
能选的自定义模型接口（只有本机和内网的：外面的网关按分钟怎么收费不知道，见 ../voice/__init__.py；没有密钥，只有名字和地址）。

这些只是设置，不拿对话的锁：聊天里正在转一段语音时改了，下一段按新的来。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..config import VOICE_MAX_SECONDS
from ..i18n import t
from ..pricing import TRANSCRIBE_PRICES
from ..providers import every
from ..voice import PROVIDERS, check_model, check_url
from ..voice.settings import DEFAULT_MODEL, LOCAL_DEFAULT_MODEL
from ..voice.transcribe import active, has_key

if TYPE_CHECKING:
    from .server import WebApp

PLATFORMS = ("telegram",)  # 哪些聊天平台会把语音交过来（见 channels/telegram.py 的 _audio）


class VoiceAPI:
    def __init__(self, web: WebApp):
        self.web = web

    def state(self) -> dict:
        voice = self.web.app.settings.voice
        return {
            **voice,
            "active": active(voice),
            "has_key": has_key(),
            "models": [{"id": name, "price": price, "recommended": name == DEFAULT_MODEL} for name, price in TRANSCRIBE_PRICES.items()],
            "local_default": LOCAL_DEFAULT_MODEL,
            "max_minutes": VOICE_MAX_SECONDS // 60,
            "platforms": list(PLATFORMS),
            "endpoints": [{"id": item[0], "name": item[1], "url": item[2]} for item in _local_endpoints()],
        }

    def route(self, action: str, body: dict) -> tuple[int, dict]:
        if action == "settings":
            return self._settings(body)
        return 404, {"error": t("没有这个操作")}

    def _settings(self, body: dict) -> tuple[int, dict]:
        changes: dict = {}
        if "provider" in body:
            if body["provider"] not in PROVIDERS:
                return 400, {"error": t("参数不正确")}
            changes["provider"] = body["provider"]
        if "model" in body:
            if body["model"] not in TRANSCRIBE_PRICES:
                return 400, {"error": t("参数不正确")}
            changes["model"] = body["model"]
        for name, check in (("url", check_url), ("local_model", check_model)):
            if name in body:
                value = body[name]
                if not isinstance(value, str):
                    return 400, {"error": t("参数不正确")}
                problem = check(value.strip())
                if problem:
                    return 400, {"error": problem, "field": name}
                changes[name] = value.strip()
        if "endpoint" in body:
            value = body["endpoint"]
            if not isinstance(value, str) or (value and value not in {item[0] for item in _local_endpoints()}):
                return 400, {"error": t("只能选本机或内网的自定义模型接口（它可能刚被删掉了）。"), "field": "endpoint"}
            changes["endpoint"] = value
        if not changes:
            return 400, {"error": t("参数不正确")}
        voice = {**self.web.app.settings.voice, **changes}
        if voice["provider"] == "local" and not voice["url"] and not voice["endpoint"]:
            return 400, {"error": t("用你自己运行的兼容服务，要先填上它的地址。"), "field": "url"}
        try:
            self.web.app.settings.set_voice(changes)
        except OSError as exc:
            return 500, {"error": t("设置没能保存：{error}", error=str(exc) or type(exc).__name__)}
        self.web.publish_state()
        return 200, {"ok": True}


def _local_endpoints() -> list[tuple[str, str, str]]:
    """能拿来转语音的自定义模型接口：（编号，名字，地址），只有本机和内网的（见 voice/transcribe.endpoint_of）。"""
    return [(provider.id, provider.label, provider.endpoint.base_url) for provider in every() if provider.endpoint is not None and provider.endpoint.local]
