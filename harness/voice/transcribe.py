"""发出去转文字：选用哪一种服务（choose），把一段语音交给它（transcribe），拿回文字。

两种服务说的是同一种话，OpenAI 的转写接口（https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create）：

    POST <根地址>/audio/transcriptions        multipart/form-data
      file             语音文件。文件名要带扩展名、类型要对：服务器靠它们认格式
      model            模型名
      response_format  json（gpt-4o-*-transcribe 只认这一种，别的模型也都认）
    回答：{"text": "转出来的文字", "usage": {"type": "duration", "seconds": 27}}
          usage 也可能是按 token 算的那种（{"type": "tokens", …}），或者没有

能收的格式：flac、mp3、mp4、mpeg、mpga、m4a、ogg、wav、webm，最大 25 MB（https://developers.openai.com/api/docs/guides/speech-to-text）。
Telegram 的语音消息是 OGG 容器里的 Opus 编码：文档列了 ogg，没有单独提 Opus，我们没有用真的 OpenAI 试过（只对着本地的假服务器测过）。

密钥：OpenAI 那一种带 Authorization 头，密钥来自环境变量 OPENAI_API_KEY（.env），不进设置、不进状态、不进日志；
出错的说明里先把它换成 <hidden>（multipart.post 的 hide）。自己填地址的本机服务一个密钥都不发；从"自定义模型接口"里选的，
带的是那个接口自己的密钥（有的话），只发给它自己的地址，和对话时一样（OpenAI 的密钥绝不会发过去）。
发出去走 net/multipart.py：有超时、回答有大小上限、重定向到别的域名时不带 Authorization 头、明文的 http:// 只许连本机。
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from ..config import VOICE_MAX_BYTES, VOICE_TEXT_CHARS, VOICE_TIMEOUT, Config
from ..i18n import t
from ..net import http, multipart
from ..providers.base import brief
from ..safe import clean_text
from .settings import LOCAL_DEFAULT_MODEL

if TYPE_CHECKING:
    from ..endpoints import Endpoint

KEY_ENV = "OPENAI_API_KEY"
# 能收的格式（扩展名）和平台常说的类型。扩展名不在这里面、类型也认不出来的，不发出去：发了也是白花一次请求
FORMATS = ("flac", "mp3", "mp4", "mpeg", "mpga", "m4a", "ogg", "wav", "webm")
_BY_TYPE = {
    "audio/ogg": "ogg", "audio/opus": "ogg", "application/ogg": "ogg",
    "audio/mpeg": "mp3", "audio/mp3": "mp3",
    "audio/mp4": "m4a", "audio/m4a": "m4a", "audio/x-m4a": "m4a", "video/mp4": "mp4",
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/vnd.wave": "wav",
    "audio/webm": "webm", "video/webm": "webm",
    "audio/flac": "flac", "audio/x-flac": "flac",
}  # fmt: skip
_TYPE_OF = {"ogg": "audio/ogg", "mp3": "audio/mpeg", "mpeg": "audio/mpeg", "mpga": "audio/mpeg", "m4a": "audio/mp4", "mp4": "audio/mp4",
            "wav": "audio/wav", "webm": "audio/webm", "flac": "audio/flac"}


class VoiceError(Exception):
    """转不了：没打开、太长、格式不对、服务出错了……说明可以直接给人看（是 t() 写的），里面没有密钥。"""


@dataclass(frozen=True)
class Transcriber:
    """这一次用哪个服务转。"""

    provider: str  # "openai" 或 "local"
    model: str
    url: str  # 根地址，后面接 /audio/transcriptions
    key: str = ""  # OpenAI 的，或者自定义接口自己的；repr 里不显示（万一被打印出来）
    via: str = ""  # 从哪个自定义接口来的（它的名字）；自己填地址的是空的

    def __repr__(self) -> str:
        return f"Transcriber(provider={self.provider!r}, model={self.model!r}, url={self.url!r}, via={self.via!r})"


@dataclass(frozen=True)
class Heard:
    """转出来的结果：文字，和服务器说这段语音有多少秒（没说是 None，记账时用平台说的时长）。"""

    text: str
    seconds: float | None = None


def has_key() -> bool:
    return bool(os.environ.get(KEY_ENV))


def endpoint_of(voice: dict) -> Endpoint | None:
    """设置里选的那个自定义接口（本机或内网的）。没选、被删掉了、改成了外面的地址，都是 None。"""
    from ..providers import find  # 用到时才导入：providers 要导入一大串

    provider = find(voice.get("endpoint")) if voice.get("endpoint") else None
    endpoint = getattr(provider, "endpoint", None)
    return endpoint if endpoint is not None and endpoint.local else None


def active(voice: dict) -> str:
    """按现在的设置，实际会用哪一种："openai"、"local"，用不了是 ""（关着、选了 OpenAI 却没有密钥、本机的地址没填）。"""
    provider = voice.get("provider")
    if provider == "auto":
        provider = "openai" if has_key() else "off"
    if provider == "openai":
        return "openai" if has_key() else ""
    if provider == "local":
        if voice.get("endpoint"):  # 选的是一个自定义接口：它还在、还是本机或内网的才能用
            return "local" if endpoint_of(voice) is not None else ""
        return "local" if voice.get("url") else ""
    return ""


def choose(voice: dict, config: Config) -> Transcriber | None:
    """按设置（Settings.voice）选这一次用的服务。用不了返回 None。密钥在这一刻从环境变量里取：刚在页面上填的也算。"""
    kind = active(voice)
    if kind == "openai":
        return Transcriber("openai", voice.get("model") or "gpt-transcribe", config.transcribe_api.rstrip("/"), os.environ.get(KEY_ENV, ""))
    if kind == "local":
        model = voice.get("local_model") or LOCAL_DEFAULT_MODEL
        endpoint = endpoint_of(voice)
        if endpoint is not None:
            return Transcriber("local", model, endpoint.base_url.rstrip("/"), endpoint.api_key, via=endpoint.name)
        return Transcriber("local", model, str(voice["url"]).rstrip("/"))
    return None


def upload_name(name: str, media_type: str) -> tuple[str, str]:
    """上传时用的（文件名，类型）。文件名带着能认的扩展名就用它；不带的话按类型补一个。都认不出来时抛出 VoiceError。"""
    stem = PurePosixPath(name or "voice").name or "voice"
    extension = PurePosixPath(stem).suffix.lower().lstrip(".")
    if extension in FORMATS:
        return stem, _TYPE_OF[extension]
    guessed = _BY_TYPE.get((media_type or "").split(";")[0].strip().lower())
    if guessed:
        return f"{PurePosixPath(stem).stem or 'voice'}.{guessed}", _TYPE_OF[guessed]
    raise VoiceError(t("这种音频（{kind}）转不了文字，只支持这几种格式：{formats}。", kind=media_type or "?", formats=", ".join(FORMATS)))


def transcribe(transcriber: Transcriber, audio: bytes, name: str, media_type: str, timeout: float = VOICE_TIMEOUT) -> Heard:
    """把一段语音交给 transcriber 转成文字。name、media_type 先经过 upload_name。转不了抛出 VoiceError。"""
    url = transcriber.url + "/audio/transcriptions"
    # 自定义接口的地址保存时已经按它自己的规矩查过（endpoints/urls.py：外面的要 https，明文的只许本机和内网），这里不再查一遍
    if not transcriber.via and not http.trusted(url):
        # OpenAI 的地址被环境变量改成了明文的、别的电脑上的地址：密钥会被不加密地发出去，不发
        raise VoiceError(t("语音转文字的地址不安全（明文的 http:// 只能连这台电脑自己），没有发出去。"))
    headers = {"Authorization": f"Bearer {transcriber.key}"} if transcriber.key else {}
    hide = [transcriber.key] if transcriber.key else []
    try:
        reply = multipart.post(
            url,
            fields={"model": transcriber.model, "response_format": "json"},
            files=[multipart.File("file", name, media_type, audio)],
            headers=headers,
            timeout=timeout,
            limit=VOICE_MAX_BYTES,
            hide=hide,
        )
    except http.NetError as exc:
        raise VoiceError(t("连不上语音转文字的服务：{error}", error=http.scrub(str(exc), hide))) from None
    if not reply.ok:
        raise VoiceError(_explain(transcriber, reply, hide))
    data = reply.data if isinstance(reply.data, dict) else {}
    text = data.get("text")
    if not isinstance(text, str):
        raise VoiceError(t("语音转文字的服务没有返回文字。"))
    return Heard(clean_text(text).strip()[:VOICE_TEXT_CHARS], _seconds(data.get("usage")))


def _seconds(usage: object) -> float | None:
    """回答里 usage 说的秒数（按时长计费的模型才有：{"type": "duration", "seconds": 27}）。"""
    if not isinstance(usage, dict) or usage.get("type") != "duration":
        return None
    seconds = usage.get("seconds")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
        return None
    return float(seconds)


def _explain(transcriber: Transcriber, reply: http.Reply, hide: list[str]) -> str:
    """服务回答了一个错误：翻译成看得懂的话。对方的原话（OpenAI 格式是 {"error": {"message": …}}）遮住密钥、截短。"""
    error = reply.data.get("error") if isinstance(reply.data, dict) else None
    said = error.get("message") if isinstance(error, dict) else error if isinstance(error, str) else reply.text
    said = brief(http.scrub(str(said or ""), hide)) or "?"
    status = reply.status
    if status in (401, 403):
        if transcriber.provider == "openai":
            return t("OpenAI 不认这个 API 密钥，或者它没有使用语音转文字的权限。请在智狐网页的设置里检查。")
        return t("你自己运行的语音转文字服务拒绝了请求（HTTP {status}）：{error}", status=status, error=said)
    if status == 404:
        return t("语音转文字的服务上找不到模型 {model}（或者地址不对）。请在设置里检查。", model=transcriber.model)
    if status == 413:
        return t("这段语音太大了，语音转文字的服务不收。")
    if status == 429:
        return t("语音转文字的服务说请求太频繁，或者账号额度不足。过一会儿再试。")
    if 400 <= status < 500:
        return t("语音转文字的服务不接受这段语音：{error}", error=said)
    return t("语音转文字的服务出错了（HTTP {status}）：{error}", status=status, error=said)
