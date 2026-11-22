"""聊天里收到一段语音时的整个过程（channels/session.py 调用 listen）：

    没打开？          → 说怎么打开（设置 → 语音转文字，或者填上 OpenAI 的密钥），不下载
    太长、太大？      → 说上限是多少，不下载、不花钱
    格式认不出来？    → 说能收哪几种
    下载 → 转 → 记账  → 交回转出来的文字和秒数

检查都放在下载之前：语音按分钟收钱，能在发出去之前拦下的都先拦下。每转一条在花费账本里记一笔（来源 voice），
秒数用服务器说的（按时长计费的模型会说），没说就用平台说的时长；价格只在 pricing.transcribe_cost 算。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..config import VOICE_MAX_BYTES, VOICE_MAX_SECONDS
from ..i18n import message_of, t
from ..pricing import transcribe_cost
from .transcribe import Heard, VoiceError, choose, transcribe, upload_name

if TYPE_CHECKING:
    from ..channels.base import InboundAudio

SOURCE = "voice"  # 花费账本里的来源


def not_set_up() -> str:
    """语音转文字没打开时，聊天里回的那句话。"""
    return t("收到了一段语音，但语音转文字没有打开。打开的办法：在智狐网页的「设置 → 语音转文字」里选 OpenAI（要先填 OpenAI 的 API 密钥，按分钟计费），或者选你自己运行的兼容服务（不花钱）。")


def listen(app: Any, audio: InboundAudio, clock=datetime.now) -> Heard:
    """把聊天里的一段语音转成文字。app 是 App（用它的设置、配置和账本）。转不了抛出 VoiceError（说明可以直接发到聊天里）。"""
    transcriber = choose(app.settings.voice, app.config)
    if transcriber is None:
        raise VoiceError(not_set_up())
    if audio.duration > VOICE_MAX_SECONDS:
        raise VoiceError(t("这段语音有 {seconds} 秒，太长了：最长只转 {limit} 分钟（按分钟计费）。可以分成几段发。", seconds=audio.duration, limit=VOICE_MAX_SECONDS // 60))
    if audio.size is not None and audio.size > VOICE_MAX_BYTES:
        raise VoiceError(t("这段语音太大了，最多 {size} MB。", size=VOICE_MAX_BYTES // 1_000_000))
    name, media_type = upload_name(audio.name, audio.media_type)
    try:
        content = audio.fetch()
    except Exception as exc:  # noqa: BLE001 —— 平台的下载错误（ChannelError……）：说明本来就是给人看的
        raise VoiceError(t("没能下载这段语音：{error}", error=message_of(exc))) from None
    heard = transcribe(transcriber, content, name, media_type)
    seconds = heard.seconds if heard.seconds is not None else float(audio.duration)
    cost = transcribe_cost(transcriber.provider, transcriber.model, seconds)
    app.ledger.record_audio(transcriber.provider, transcriber.model, seconds, cost, clock(), source=SOURCE)
    return Heard(heard.text, seconds)
