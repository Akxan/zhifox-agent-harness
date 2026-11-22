"""语音转文字的设置：用哪一种、OpenAI 用哪个模型、本机的服务在哪儿、叫什么模型。

存在 data/settings.json 的 "voice" 里（见 ../settings.py），和默认的一样时不写。长这样：

    {"provider": "auto", "model": "gpt-transcribe", "url": "", "local_model": "", "endpoint": ""}

  provider     auto / openai / local / off（意思见 __init__.py）
  model        用 OpenAI 时的模型：只能是 pricing.TRANSCRIBE_PRICES 里的一个（那里有每分钟的价格）
  url          本机的服务的根地址，后面接 /audio/transcriptions，比如 http://127.0.0.1:8000/v1
  local_model  本机的服务上的模型名（各家服务器叫法不一样）；不填就发 whisper-1（大多数兼容服务器认这个名字）
  endpoint     不填地址、改用一个自定义模型接口（endpoints/）时，那个接口的编号（custom-ollama）；空的就用 url。
               只认本机和内网的接口（见 transcribe.endpoint_of）：接口被删了、改成了外面的地址，就当作没填

为什么本机的服务地址能在页面上改，而 OpenAI 的接口地址只能用环境变量改（见 config.Config.transcribe_api）：
OpenAI 的密钥会发到它那里去；本机的服务我们从来不发任何密钥，发过去的只有你自己的那段语音。
地址也要合格：明文的 http:// 只许连这台电脑自己（localhost、127.0.0.1、::1），别的一律要 https://，
免得你的声音在网上不加密地传；网址里不许带用户名和密码（会被存进设置文件）。
"""

from __future__ import annotations

import re
import urllib.parse

from ..i18n import t
from ..net.websocket import is_loopback
from ..pricing import TRANSCRIBE_PRICES

PROVIDERS = ("auto", "openai", "local", "off")
DEFAULT_MODEL = "gpt-transcribe"  # OpenAI 现在推荐的转写模型（https://developers.openai.com/api/docs/guides/speech-to-text）
LOCAL_DEFAULT_MODEL = "whisper-1"
DEFAULTS = {"provider": "auto", "model": DEFAULT_MODEL, "url": "", "local_model": "", "endpoint": ""}
MAX_URL_CHARS = 500
_MODEL = re.compile(r"[\w.:/-]{1,100}", re.ASCII)  # 本机服务上的模型名：Systran/faster-whisper-small、large-v3、whisper-1……
_ENDPOINT = re.compile(r"[A-Za-z0-9_.-]{1,80}")  # 自定义接口的编号（providers 里的 id，custom-…）


def voice_settings(saved: object) -> dict:
    """设置文件里的 "voice"，只认这五项、这几种值；不认识的、不合格的按默认的来（文件被改坏了也不出错）。"""
    saved = saved if isinstance(saved, dict) else {}
    provider = saved.get("provider")
    model = saved.get("model")
    url = saved.get("url")
    local_model = saved.get("local_model")
    endpoint = saved.get("endpoint")
    return {
        "provider": provider if provider in PROVIDERS else DEFAULTS["provider"],
        "model": model if model in TRANSCRIBE_PRICES else DEFAULT_MODEL,
        "url": url.strip() if isinstance(url, str) and not check_url(url.strip()) else "",
        "local_model": local_model.strip() if isinstance(local_model, str) and not check_model(local_model.strip()) else "",
        "endpoint": endpoint if isinstance(endpoint, str) and _ENDPOINT.fullmatch(endpoint) else "",
    }


def check_url(url: str) -> str:
    """本机服务的地址合不合格。合格返回空字符串，不合格返回给人看的原因。空的地址算合格（表示还没填）。"""
    if not url:
        return ""
    if len(url) > MAX_URL_CHARS or any(not char.isprintable() or char.isspace() for char in url):
        return t("地址里不能有空格和看不见的字符，最长 {n} 个字。", n=MAX_URL_CHARS)
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        parts.port  # noqa: B018 —— 端口写得不对（不是数字、太大）时这里才会抛出 ValueError
    except ValueError:
        return t("这不是一个能用的地址。")
    if parts.scheme not in ("http", "https") or not host:
        return t("地址要以 http:// 或 https:// 开头，比如 http://127.0.0.1:8000/v1")
    if parts.username or parts.password:
        return t("地址里不能带用户名和密码。")
    if parts.query or parts.fragment:
        return t("地址里不能带 ? 和 # 后面的部分。")
    if parts.scheme == "http" and not is_loopback(host):
        return t("明文的 http:// 只能连这台电脑自己（localhost、127.0.0.1）。别的地址请用 https://，免得语音在网上不加密地传。")
    return ""


def check_model(name: str) -> str:
    """本机服务上的模型名合不合格（空的算合格，表示用默认的）。"""
    if not name or _MODEL.fullmatch(name):
        return ""
    return t("模型名只能有字母、数字和 . _ - / :，最长 100 个字。")
