"""语音转文字（harness/voice/）：聊天里发来的语音先转成文字，再当作你说的话。

全部对着本地的假服务器（tests/fake_transcribe.py 扮演 OpenAI 的转写接口和你自己运行的兼容服务，fake_telegram.py 扮演 Telegram），
不连真的 OpenAI，一分钱都不花。要守住的几条：
  - 设置文件被改坏了也不出错；本机服务的地址：明文的 http:// 只许连这台电脑自己，不许带用户名密码
  - OpenAI 的密钥只发给 OpenAI 的地址，绝不发给你自己运行的服务；不进状态、不进出错的说明
  - 太长、太大、格式认不出来的，在下载和发出去之前就拦下（不花钱）
  - 每转一条在账本里记一笔（来源 voice），金额按 pricing 里每分钟的价格算；本机的服务记 0
  - Telegram 里：聊天里回"（语音转文字）……"，模型拿到带着"（语音转文字）"的文字，网页上的那条消息带着 voice
  - 别的平台收到语音时说"暂时只支持 Telegram"
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest
import test_channels
import test_webui
from fake_api import reply, text
from fake_transcribe import FakeTranscribe, error
from test_channels import until
from test_webui import latest_state

from harness import pricing
from harness.channels.base import InboundAudio
from harness.channels.telegram import TelegramChannel
from harness.config import VOICE_MAX_SECONDS
from harness.ledger import Ledger
from harness.settings import Settings
from harness.voice import (
    DEFAULTS,
    Transcriber,
    VoiceError,
    check_model,
    check_url,
    choose,
    listen,
    transcribe,
    upload_name,
    voice_settings,
)
from harness.voice.transcribe import active

serve = test_webui.serve  # 借用 test_webui.py 的 fixture（pytest 按模块里的名字找 fixture）
KEY = "sk-test-openai-key"
OGG = b"OggS\x00\x02" + b"\x00" * 200  # 看起来像一段 OGG 的字节（假服务器不解码）


@pytest.fixture
def fake():
    server = FakeTranscribe(key=KEY).start()
    yield server
    server.stop()


@pytest.fixture
def local():
    server = FakeTranscribe(key=None, text="local heard this", seconds=None).start()  # 你自己运行的服务：不要密钥
    yield server
    server.stop()


# ---- 设置 -------------------------------------------------------------------


def test_settings_default_to_auto_and_survive_a_broken_file():
    assert voice_settings(None) == DEFAULTS == {"provider": "auto", "model": "gpt-transcribe", "url": "", "local_model": ""}
    broken = {"provider": "telepathy", "model": "gpt-99", "url": "http://example.com/v1", "local_model": "bad name with spaces"}
    assert voice_settings(broken) == DEFAULTS  # 每一项都不合格：按默认的来
    good = {"provider": "local", "model": "whisper-1", "url": " http://127.0.0.1:8000/v1 ", "local_model": "Systran/faster-whisper-small"}
    assert voice_settings(good) == {**good, "url": "http://127.0.0.1:8000/v1"}


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8000/v1", "http://localhost:9000", "http://[::1]:8000/v1", "https://whisper.example.com/v1", "https://10.0.0.5/v1"],
)
def test_good_addresses(url):
    assert check_url(url) == ""


@pytest.mark.parametrize(
    ("url", "words"),
    [
        ("http://192.168.1.20:8000/v1", "只能连这台电脑自己"),  # 局域网里的明文地址：语音会不加密地传
        ("http://127.example.com/v1", "只能连这台电脑自己"),  # 长得像 127 开头的域名，不是这台电脑
        ("https://user:secret@example.com/v1", "用户名和密码"),
        ("https://example.com/v1?key=abc", "? 和 #"),
        ("ftp://example.com/v1", "http:// 或 https://"),
        ("https://", "http:// 或 https://"),
        ("http://127.0.0.1:99999/v1", "不是一个能用的地址"),
        ("http://127.0.0.1:8000/v1 extra", "空格"),
        ("https://example.com/" + "a" * 600, "空格"),
    ],
)
def test_bad_addresses_say_why(url, words):
    assert words in check_url(url)


def test_model_names():
    assert check_model("") == "" and check_model("whisper-1") == "" and check_model("Systran/faster-whisper-large-v3") == ""
    assert check_model("rm -rf /") and check_model("x" * 101) and check_model("名字")


def test_the_settings_file_only_mentions_voice_when_it_was_changed(tmp_path):
    path = tmp_path / "settings.json"
    settings = Settings(path, "anthropic", "claude-opus-5-5")
    settings.save()
    assert "voice" not in json.loads(path.read_text(encoding="utf-8"))
    settings.set_voice({"provider": "off"})
    assert json.loads(path.read_text(encoding="utf-8"))["voice"]["provider"] == "off"
    assert Settings(path, "anthropic", "claude-opus-5-5").voice["provider"] == "off"  # 重启以后还记得


# ---- 选哪一种 ---------------------------------------------------------------


def config(make_config, url: str = "https://api.openai.com/v1"):
    return make_config(transcribe_api=url)


def test_auto_uses_openai_only_when_there_is_a_key(make_config, monkeypatch):
    voice = dict(DEFAULTS)
    assert active(voice) == "" and choose(voice, config(make_config)) is None
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    chosen = choose(voice, config(make_config))
    assert (chosen.provider, chosen.model, chosen.url, chosen.key) == ("openai", "gpt-transcribe", "https://api.openai.com/v1", KEY)
    assert KEY not in repr(chosen)  # 万一被打印出来
    assert choose({**voice, "provider": "off"}, config(make_config)) is None


def test_openai_without_a_key_is_not_usable(make_config):
    assert choose({**DEFAULTS, "provider": "openai"}, config(make_config)) is None


def test_the_local_service_never_gets_the_openai_key(make_config, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    voice = {**DEFAULTS, "provider": "local", "url": "http://127.0.0.1:8000/v1/"}
    chosen = choose(voice, config(make_config))
    assert (chosen.provider, chosen.model, chosen.url, chosen.key) == ("local", "whisper-1", "http://127.0.0.1:8000/v1", "")
    assert choose({**voice, "url": ""}, config(make_config)) is None  # 地址还没填：用不了


@pytest.mark.parametrize(
    ("name", "media_type", "expected"),
    [
        ("voice-5.ogg", "audio/ogg", ("voice-5.ogg", "audio/ogg")),
        ("audio-3", "audio/mpeg", ("audio-3.mp3", "audio/mpeg")),
        ("Recording.M4A", "application/octet-stream", ("Recording.M4A", "audio/mp4")),
        ("../../etc/note.wav", "", ("note.wav", "audio/wav")),  # 文件名只留最后一段
        ("memo", "audio/x-wav; codecs=1", ("memo.wav", "audio/wav")),
    ],
)
def test_upload_names_carry_an_extension_the_service_knows(name, media_type, expected):
    assert upload_name(name, media_type) == expected


def test_a_format_it_cannot_take_is_refused_before_sending():
    with pytest.raises(VoiceError, match="audio/amr"):
        upload_name("voice.amr", "audio/amr")


# ---- 发出去转 ---------------------------------------------------------------


def openai_at(server) -> Transcriber:
    return Transcriber("openai", "gpt-transcribe", server.base_url, KEY)


def test_transcribing_sends_what_the_api_expects(fake):
    heard = transcribe(openai_at(fake), OGG, "voice-5.ogg", "audio/ogg")
    assert heard.text == "明天早上八点提醒我开会" and heard.seconds == 7.0
    (sent,) = fake.requests
    assert sent["path"] == "/v1/audio/transcriptions"
    assert sent["headers"]["authorization"] == f"Bearer {KEY}"
    assert sent["fields"] == {"model": "gpt-transcribe", "response_format": "json"}
    assert sent["files"]["file"] == {"filename": "voice-5.ogg", "content_type": "audio/ogg", "data": OGG}


def test_the_local_service_gets_no_authorization_header(local):
    heard = transcribe(Transcriber("local", "whisper-1", local.base_url), OGG, "v.ogg", "audio/ogg")
    assert heard.text == "local heard this" and heard.seconds is None  # 没说秒数：记账时用平台说的时长
    assert "authorization" not in local.requests[0]["headers"]


@pytest.mark.parametrize(
    ("scripted", "words"),
    [
        (error(401, f"Incorrect API key provided: {KEY}", "invalid_api_key"), "API 密钥"),
        (error(404, "The model does not exist"), "找不到模型 gpt-transcribe"),
        (error(413, "too big"), "太大"),
        (error(429, "Rate limit reached", "rate_limit_exceeded"), "太频繁"),
        (error(400, f"Audio file might be corrupted {KEY}"), "Audio file might be corrupted"),
        (error(500, "The server had an error"), "HTTP 500"),
        ({"status": 200, "body": {"nothing": True}}, "没有返回文字"),
    ],
)
def test_errors_are_explained_and_never_show_the_key(fake, scripted, words):
    fake.replies.append(scripted)
    with pytest.raises(VoiceError) as caught:
        transcribe(openai_at(fake), OGG, "v.ogg", "audio/ogg")
    assert words in str(caught.value)
    assert KEY not in str(caught.value)


def test_a_wrong_key_is_rejected(fake):
    with pytest.raises(VoiceError, match="API 密钥"):
        transcribe(Transcriber("openai", "gpt-transcribe", fake.base_url, "sk-wrong-key-123456"), OGG, "v.ogg", "audio/ogg")


def test_an_unreachable_service_is_explained(fake):
    url = fake.base_url
    fake.stop()
    with pytest.raises(VoiceError, match="连不上"):
        transcribe(Transcriber("openai", "gpt-transcribe", url, KEY), OGG, "v.ogg", "audio/ogg", timeout=2)
    fake.start()  # fixture 收尾时还要关它一次


def test_a_plain_http_address_elsewhere_is_never_sent_the_key(fake):
    with pytest.raises(VoiceError, match="不安全"):
        transcribe(Transcriber("openai", "gpt-transcribe", "http://example.com/v1", KEY), OGG, "v.ogg", "audio/ogg")
    assert fake.requests == []


def test_odd_characters_in_the_text_are_cleaned(fake):
    fake.text = "half an emoji \ud83d and more  "
    assert transcribe(openai_at(fake), OGG, "v.ogg", "audio/ogg").text == "half an emoji � and more"


def test_token_usage_has_no_seconds(fake):
    fake.replies.append({"status": 200, "body": {"text": "hi", "usage": {"type": "tokens", "input_tokens": 14, "output_tokens": 45}}})
    assert transcribe(openai_at(fake), OGG, "v.ogg", "audio/ogg").seconds is None


# ---- 价格和账本 -------------------------------------------------------------


def test_prices_are_per_minute_and_local_is_free():
    assert pricing.TRANSCRIBE_PRICES["gpt-transcribe"] == 0.0045
    assert pricing.transcribe_cost("openai", "gpt-transcribe", 120) == 0.009
    assert pricing.transcribe_cost("openai", "gpt-4o-mini-transcribe", 30) == 0.0015
    assert pricing.transcribe_cost("local", "whisper-1", 600) == 0.0


def test_the_ledger_records_a_transcription_as_voice(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = Ledger(path)
    moment = datetime(2026, 11, 22, 9, 30)
    assert ledger.record_audio("openai", "gpt-transcribe", 40, 0.003, moment) == 0.003
    row = json.loads(path.read_text(encoding="utf-8"))
    assert (row["source"], row["model"], row["requests"], row["input"], row["output"], row["seconds"]) == ("voice", "gpt-transcribe", 1, 0, 0, 40.0)
    report = Ledger(path).report(moment.date(), 30)  # 重新读一遍文件：多出来的 seconds 不碍事
    assert report["periods"]["all"]["sources"] == [{"source": "voice", **report["periods"]["all"]["sources"][0], "cost": 0.003}]
    assert report["periods"]["all"]["models"][0]["model"] == "gpt-transcribe"
    assert Ledger(path).day_total(moment.date()) == 0.003


# ---- 聊天里的整个过程（listen） ----------------------------------------------------


def audio(seconds: int = 7, size: int | None = None, fetch=lambda: OGG, name: str = "voice-1.ogg", media_type: str = "audio/ogg") -> InboundAudio:
    return InboundAudio(name=name, media_type=media_type, size=size, duration=seconds, kind="voice", fetch=fetch)


@pytest.fixture
def app(make_config, tmp_path, fake, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    settings = Settings(tmp_path / "settings.json", "anthropic", "claude-opus-5-5")
    return SimpleNamespace(settings=settings, config=make_config(transcribe_api=fake.base_url), ledger=Ledger(tmp_path / "ledger.jsonl"))


def test_listening_transcribes_and_records_the_cost(app, fake):
    heard = listen(app, audio(), clock=lambda: datetime(2026, 11, 22, 9, 30))
    assert heard.text == "明天早上八点提醒我开会" and heard.seconds == 7.0  # 秒数用服务器说的
    assert app.ledger.day_total(datetime(2026, 11, 22).date()) == pricing.transcribe_cost("openai", "gpt-transcribe", 7)


def test_too_long_is_refused_before_downloading(app, fake):
    fetched = []
    with pytest.raises(VoiceError, match=f"最长只转 {VOICE_MAX_SECONDS // 60} 分钟"):
        listen(app, audio(seconds=VOICE_MAX_SECONDS + 1, fetch=lambda: fetched.append(1) or OGG))
    with pytest.raises(VoiceError, match="太大"):
        listen(app, audio(size=30_000_000, fetch=lambda: fetched.append(1) or OGG))
    assert fetched == [] and fake.requests == []
    assert app.ledger.day_total(datetime.now().date()) == 0


def test_not_set_up_says_how_to_turn_it_on(app, fake, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    with pytest.raises(VoiceError, match="设置 → 语音转文字"):
        listen(app, audio())
    app.settings.set_voice({"provider": "off"})
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    with pytest.raises(VoiceError, match="设置 → 语音转文字"):
        listen(app, audio())
    assert fake.requests == []


def test_the_local_service_costs_nothing_and_uses_the_platform_duration(app, local):
    app.settings.set_voice({"provider": "local", "url": local.base_url, "local_model": "large-v3"})
    heard = listen(app, audio(seconds=12))
    assert heard.text == "local heard this" and heard.seconds == 12.0
    assert local.requests[0]["fields"]["model"] == "large-v3"
    assert "authorization" not in local.requests[0]["headers"]  # OPENAI_API_KEY 设着，也不发给它
    row = json.loads(app.ledger.path.read_text(encoding="utf-8"))
    assert (row["provider"], row["model"], row["cost"], row["seconds"], row["source"]) == ("local", "large-v3", 0.0, 12.0, "voice")


def test_a_failed_download_is_explained(app, fake):
    def broken():
        raise OSError("disk on fire")

    with pytest.raises(VoiceError, match="没能下载这段语音"):
        listen(app, audio(fetch=broken))
    assert fake.requests == []


# ---- Telegram ---------------------------------------------------------------


def test_telegram_turns_voice_and_audio_into_inbound_audio(monkeypatch):
    channel = TelegramChannel(test_channels.TOKEN, "http://127.0.0.1:9")
    sender = {"id": 111, "is_bot": False, "first_name": "Akxan", "username": "akxan"}

    def message(**fields):
        return channel._event({"update_id": 1, "message": {"message_id": 5, "from": sender, "chat": {"id": 111, "type": "private"}, **fields}})

    voice = message(voice={"file_id": "v", "duration": 4, "mime_type": "audio/ogg", "file_size": 999})
    assert voice.unsupported == "" and voice.audio is not None
    assert (voice.audio.name, voice.audio.media_type, voice.audio.size, voice.audio.duration, voice.audio.kind) == ("voice-5.ogg", "audio/ogg", 999, 4, "voice")
    song = message(audio={"file_id": "a", "duration": 61, "file_name": "song.mp3", "mime_type": "audio/mpeg"}, caption="听听这个")
    assert (song.text, song.audio.name, song.audio.kind, song.audio.size) == ("听听这个", "song.mp3", "audio", None)
    bare = message(voice={"file_id": "v", "duration": -3})  # 没写类型、时长是负的：按 OGG、0 秒算
    assert (bare.audio.media_type, bare.audio.duration) == ("audio/ogg", 0)
    assert message(voice={"duration": 3}).unsupported == "voice"  # 没有 file_id：下载不了，照旧说处理不了


@pytest.fixture
def bot(make_config, monkeypatch, fake):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    yield from test_channels.rigs(lambda **options: make_config(transcribe_api=fake.base_url, **options), monkeypatch)


def test_a_voice_message_becomes_the_users_words(bot, fake):
    s = bot(reply(text("好的，明天八点提醒你")))
    start = s.say("", voice=(OGG, 7))
    s.wait_reply(start, "（语音转文字）明天早上八点提醒我开会")
    s.wait_reply(start, "好的，明天八点提醒你")
    s.idle()
    sent = json.dumps(s.api.requests[0]["messages"][-1], ensure_ascii=False)
    assert "（语音转文字）明天早上八点提醒我开会" in sent  # 模型知道这是听写出来的
    assert fake.requests[0]["headers"]["authorization"] == f"Bearer {KEY}"
    # 网页上这段会话：你的那条消息是转出来的文字，带着语音有多长
    conversation = s.app.store.load(s.session().conversation.id)
    (user,) = [event for event in conversation.events if event["type"] == "user"]
    assert (user["text"], user["voice"]) == ("明天早上八点提醒我开会", 7.0)
    assert conversation.title == "明天早上八点提醒我开会"
    # 记了一笔账：来源是 voice
    rows = [json.loads(line) for line in s.config.ledger_file.read_text(encoding="utf-8").splitlines()]
    (voice_row,) = [row for row in rows if row["source"] == "voice"]
    assert (voice_row["model"], voice_row["cost"]) == ("gpt-transcribe", pricing.transcribe_cost("openai", "gpt-transcribe", 7))
    # OpenAI 的密钥和机器人的 token 都没有出现在发给浏览器的任何东西里
    assert KEY not in json.dumps(s.browser.events, ensure_ascii=False)


def test_a_caption_on_a_voice_message_is_kept(bot, fake):
    s = bot(reply(text("收到")))
    start = s.say("顺便看看天气", voice=(OGG, 3))
    s.wait_reply(start, "收到")
    s.idle()
    sent = json.dumps(s.api.requests[0]["messages"][-1], ensure_ascii=False)
    assert "（语音转文字）明天早上八点提醒我开会\\n\\n顺便看看天气" in sent


def test_an_audio_file_is_transcribed_too(bot, fake):
    s = bot(reply(text("听到了")))
    start = s.say("", audio=("meeting.m4a", "audio/mp4", b"\x00\x00\x00\x18ftypM4A ", 45))
    s.wait_reply(start, "听到了")
    s.idle()
    assert fake.requests[0]["files"]["file"]["filename"] == "meeting.m4a"


def test_without_a_transcriber_the_chat_says_how_to_enable_it(bot, fake, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    s = bot()
    s.wait_reply(s.say("", voice=(OGG, 3)), "设置 → 语音转文字")
    s.idle()
    assert s.api.requests == [] and fake.requests == []  # 没有开始一轮，也没有发出去


def test_a_voice_message_that_is_too_long_is_not_sent(bot, fake):
    s = bot()
    s.wait_reply(s.say("", voice=(OGG, VOICE_MAX_SECONDS + 5)), "太长了")
    s.idle()
    assert s.api.requests == [] and fake.requests == []


def test_a_failed_transcription_is_explained_in_the_chat(bot, fake):
    fake.replies.append(error(500, "The server had an error"))
    s = bot()
    s.wait_reply(s.say("", voice=(OGG, 3)), "语音转文字的服务出错了")
    s.idle()
    assert s.api.requests == []


def test_silence_is_not_sent_to_the_model(bot, fake):
    fake.text = "   "
    s = bot()
    s.wait_reply(s.say("", voice=(OGG, 3)), "没有听出")
    s.idle()
    assert s.api.requests == []


# ---- 网页上的设置 -------------------------------------------------------------


def voice_state(s) -> dict:
    return latest_state(s.browser)["voice"]


def test_the_state_has_prices_but_never_the_key(serve, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    s = serve()
    voice = voice_state(s)
    assert (voice["provider"], voice["active"], voice["has_key"]) == ("auto", "openai", True)
    assert {item["id"]: item["price"] for item in voice["models"]} == pricing.TRANSCRIBE_PRICES
    assert [item["id"] for item in voice["models"] if item["recommended"]] == ["gpt-transcribe"]
    assert voice["max_minutes"] == VOICE_MAX_SECONDS // 60 and voice["platforms"] == ["telegram"]
    assert KEY not in json.dumps(s.browser.events, ensure_ascii=False)


def test_changing_the_settings_from_the_page(serve):
    s = serve()
    assert s.browser.post("/api/voice/settings", {"provider": "telepathy"})[0] == 400
    assert s.browser.post("/api/voice/settings", {"model": "gpt-99"})[0] == 400
    assert s.browser.post("/api/voice/settings", {})[0] == 400
    status, answer = s.browser.post("/api/voice/settings", {"provider": "local"})
    assert status == 400 and answer["field"] == "url" and "地址" in answer["error"]  # 还没填地址
    status, answer = s.browser.post("/api/voice/settings", {"provider": "local", "url": "http://192.168.1.2:8000/v1"})
    assert status == 400 and answer["field"] == "url" and "只能连这台电脑自己" in answer["error"]
    status, answer = s.browser.post("/api/voice/settings", {"provider": "local", "url": "http://127.0.0.1:8000/v1", "local_model": "large-v3"})
    assert status == 200 and answer["ok"]
    saved = json.loads(s.config.settings_file.read_text(encoding="utf-8"))["voice"]
    assert saved == {"provider": "local", "model": "gpt-transcribe", "url": "http://127.0.0.1:8000/v1", "local_model": "large-v3"}
    until(lambda: voice_state(s)["active"] == "local", what="状态里改成了本机的服务")
    assert s.browser.post("/api/voice/settings", {"model": "whisper-1", "provider": "openai"})[0] == 200
    until(lambda: voice_state(s)["model"] == "whisper-1" and voice_state(s)["active"] == "", what="选了 OpenAI 但没有密钥")
