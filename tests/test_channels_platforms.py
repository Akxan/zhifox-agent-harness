"""几个聊天平台共用的那部分：平台的名单和要填的东西（platforms.py、guides.py）、凭据怎么存（store.py）、
连接时"没重填的接着用"（hub.merge）、官方 SDK 没装时怎么说、命令行的 setup 和 status，以及 /new 某个 Agent。

每个平台自己的协议在 test_channels_<平台>.py 里测；这里测的是加一个平台时要守的那几条规矩，
免得以后加第七个平台时漏掉一处（比如密钥的环境变量没加进 tools/shell.SECRET_ENV_VARS）。
"""

from __future__ import annotations

import io
import json
import logging
import sys

import pytest
from fake_api import reply, text
from fake_discord import TOKEN as DISCORD_TOKEN
from fake_discord import FakeDiscord
from fake_telegram import TOKEN
from test_agents_web import agent_file
from test_channels import rigs, stop_later

from harness import cli, log
from harness.channels import guides, hub, platforms
from harness.channels.base import ChannelError
from harness.channels.store import ChannelStore, handle, pair_link
from harness.i18n import fill
from harness.tools.shell import SECRET_ENV_VARS

# ---- 平台的名单 ------------------------------------------------------------------


def test_every_ready_platform_is_wired_up_everywhere():
    ready = {item["id"] for item in platforms.PLATFORMS if item["stage"] == "ready"}
    assert ready == {"telegram", "discord", "slack", "feishu", "dingtalk", "qq"}
    assert set(hub.FACTORIES) == set(hub.CHECKS) == set(hub.CLASSES) == ready  # 加一个平台，三张表都要登记
    assert platforms.ORDER == [item["id"] for item in platforms.PLATFORMS if item["stage"] == "ready"]
    for item in platforms.PLATFORMS:
        if item["stage"] != "ready":
            assert "fields" not in item  # 还不能用的平台没有要填的东西
            continue
        assert item["fields"] and item["step"] and "{bot}" in item["pair"], item["id"]
        assert any(field["secret"] for field in item["fields"]), item["id"]  # 至少有一样是密钥
        assert guides.GUIDES[item["id"]], item["id"]  # 向导第一步有步骤可看
        for step in guides.GUIDES[item["id"]]:
            assert ("{code}" in step["text"]) == bool(step.get("code")), step  # 有代码就一定有地方放，反过来也一样
            assert not step.get("link") or step["link"].startswith("https://")
            assert bool(step.get("link")) == bool(step.get("link_label")) and bool(step.get("copy")) == bool(step.get("copy_label"))


def test_every_secret_environment_variable_is_kept_from_commands_and_logs(monkeypatch):
    """密钥可以放在环境变量里（harness channels setup 会去读）：助手运行的命令拿不到它们，日志里也不会出现。"""
    names = platforms.secret_env_vars()
    assert names == {"TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "FEISHU_APP_SECRET", "DINGTALK_CLIENT_SECRET", "QQ_APP_SECRET"}
    assert names <= SECRET_ENV_VARS
    # 不是密钥的（应用的编号）不用藏
    assert not {"FEISHU_APP_ID", "DINGTALK_CLIENT_ID", "QQ_APP_ID"} & SECRET_ENV_VARS
    for name in names:
        monkeypatch.setenv(name, f"secret-value-of-{name.lower()}")
    stream = io.StringIO()
    root = logging.getLogger("harness")
    handlers, level, propagate = list(root.handlers), root.level, root.propagate
    try:
        log.setup(stream)
        for name in names:
            logging.getLogger("harness.test").warning("出错了：%s", f"secret-value-of-{name.lower()}")
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in handlers:
            root.addHandler(handler)
        root.setLevel(level)
        root.propagate = propagate
    written = stream.getvalue()
    assert "secret-value-of" not in written and written.count(log.HIDDEN) == len(names)


def test_the_catalog_is_translated_and_says_how_to_install_an_sdk(monkeypatch):
    monkeypatch.setattr(platforms, "sdk_ready", lambda platform: platform != "feishu")
    catalog = {item["id"]: item for item in platforms.catalog()}
    assert catalog["telegram"]["guide"][1]["code"] == "/newbot" and "{code}" in catalog["telegram"]["guide"][1]["text"]
    assert "sdk" not in catalog["telegram"] and catalog["dingtalk"]["sdk"]["ready"] is True
    feishu = catalog["feishu"]["sdk"]
    assert feishu["ready"] is False and feishu["install"] == "uv sync --extra feishu"
    assert "uv sync --extra feishu" in feishu["message"] and "lark-oapi" in feishu["message"]
    assert catalog["slack"]["guide"][0]["copy"] == guides.SLACK_MANIFEST
    manifest = json.loads(guides.SLACK_MANIFEST)
    assert manifest["settings"]["socket_mode_enabled"] is True and manifest["features"]["slash_commands"][0]["command"] == guides.SLACK_COMMAND
    assert manifest["settings"]["event_subscriptions"]["bot_events"] == ["message.im"]


def test_sdk_ready_only_looks_for_the_package():
    assert platforms.sdk_ready("telegram") is True  # 不要 SDK 的平台
    assert platforms.missing_sdk("telegram") == ""


# ---- 凭据怎么存 ------------------------------------------------------------------


def test_a_file_from_the_first_version_keeps_its_token(tmp_path):
    """第一版只有 Telegram，channels.json 里存的是一个 token 字段：读进来换成 credentials，什么都不用重填。"""
    path = tmp_path / "channels.json"
    path.write_text(json.dumps({"version": 1, "platforms": {"telegram": {"token": TOKEN, "bot": {"id": "1", "username": "old_bot", "name": "Old"}}}}), encoding="utf-8")
    store = ChannelStore(path)
    assert store.credentials("telegram") == {"token": TOKEN} and store.configured("telegram")
    assert store.get("telegram")["bot"]["at"] is True and handle(store.get("telegram")["bot"]) == "@old_bot"
    store.set_approval("telegram", "edits")  # 改一处就按新的样子写回去
    saved = json.loads(path.read_text(encoding="utf-8"))["platforms"]["telegram"]
    assert saved["credentials"] == {"token": TOKEN} and "token" not in saved


def test_hand_edited_credentials_of_the_wrong_type_are_dropped(tmp_path):
    path = tmp_path / "channels.json"
    path.write_text(json.dumps({"platforms": {"slack": {
        "credentials": {"bot_token": "xoxb-1", "app_token": 5, "": "x", "extra": ["y"]},
        "bot": {"id": "U1", "username": "zhifox", "link": "javascript:alert(1)", "install": "http://example.com"},
    }}}), encoding="utf-8")
    store = ChannelStore(path)
    assert store.credentials("slack") == {"bot_token": "xoxb-1"}
    assert not store.configured("slack")  # 少了 app_token：不算接好了，hub 不会去连
    assert store.get("slack")["bot"]["link"] == "" and store.get("slack")["bot"]["install"] == ""  # 只认 https:// 的链接


def test_the_summary_shows_ids_but_only_the_end_of_secrets(tmp_path):
    store = ChannelStore(tmp_path / "channels.json")
    store.connect("feishu", {"app_id": "cli_a1b2c3", "app_secret": "very-secret-value-1234"}, {"id": "ou_1", "username": "智狐助手", "name": "智狐助手", "at": False,
                                                                                               "link": "https://applink.feishu.cn/client/bot/open?appId=cli_a1b2c3"})
    summary = store.summary("feishu")
    assert summary["hints"] == {"app_id": "cli_a1b2c3", "app_secret": "…1234"} and summary["token_hint"] == "…1234"
    assert "very-secret-value" not in json.dumps(summary, ensure_ascii=False)
    assert summary["bot"]["handle"] == "智狐助手"  # 飞书的机器人没有 @用户名
    code = store.start_pairing("feishu")
    assert store.summary("feishu")["pairing"]["link"] == "https://applink.feishu.cn/client/bot/open?appId=cli_a1b2c3"  # 只能打开聊天，配对码要自己发
    assert pair_link("telegram", {"username": "zhifox_bot"}, code["code"]) == f"https://t.me/zhifox_bot?start={code['code']}"
    assert pair_link("qq", None, code["code"]) == ""


def test_connecting_again_keeps_what_was_not_filled_in_again(tmp_path):
    store = ChannelStore(tmp_path / "channels.json")
    store.connect("slack", {"bot_token": "xoxb-old", "app_token": "xapp-old"}, {"id": "U1", "username": "zhifox"})
    assert hub.merge(store, "slack", {"app_token": " xapp-new ", "bot_token": ""}) == {"bot_token": "xoxb-old", "app_token": "xapp-new"}
    assert hub.merge(store, "slack", {"unknown": "x"}) == {"bot_token": "xoxb-old", "app_token": "xapp-old"}  # 不认识的字段不收
    with pytest.raises(ChannelError, match="还没有填：App Secret"):
        hub.merge(ChannelStore(tmp_path / "other.json"), "feishu", {"app_id": "cli_x"})


def test_connect_refuses_a_platform_whose_sdk_is_missing(tmp_path, make_config, monkeypatch):
    monkeypatch.setattr(platforms, "sdk_ready", lambda platform: False)
    store = ChannelStore(tmp_path / "channels.json")
    checked = []
    with pytest.raises(ChannelError, match="uv sync --extra dingtalk"):
        hub.connect(store, make_config(), "dingtalk", {"client_id": "dingabc", "client_secret": "x" * 20}, lambda config, given: checked.append(given) or {})
    assert checked == [] and not store.configured("dingtalk")  # 没去检查，也没存


def test_connect_hides_the_new_secrets_from_the_logs(tmp_path, make_config):
    store = ChannelStore(tmp_path / "channels.json")
    bot = hub.connect(store, make_config(), "qq", {"app_id": "102000", "app_secret": "qq-secret-for-the-log-test"}, lambda config, given: {"id": "1", "username": "智狐", "at": False})
    assert bot["handle"] == "智狐"
    assert log.redact("错误：qq-secret-for-the-log-test") == f"错误：{log.HIDDEN}"


# ---- 命令行 ----------------------------------------------------------------------


@pytest.fixture
def discord_cli(tmp_path, monkeypatch, capsys):
    fake = FakeDiscord().start()
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "cli-data"))
    monkeypatch.setenv("DISCORD_API_BASE", fake.base_url + "/api/v10")
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)

    def run_cli(*argv: str) -> tuple[int, str, str]:
        capsys.readouterr()
        code = cli.main(list(argv))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    run_cli.store = lambda: ChannelStore(tmp_path / "cli-data" / "channels.json")
    run_cli.fake = fake
    yield run_cli
    stop_later(fake.stop)


def test_setup_prints_the_platform_guide_and_reads_the_token_from_the_environment(discord_cli, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", DISCORD_TOKEN)
    code, out, err = discord_cli("channels", "setup", "discord")
    assert code == 0, err
    assert "1. 打开 Discord 的开发者后台，点 New Application" in out and "https://discord.com/developers/applications" in out  # {code} 换成了菜单的名字
    assert "已连接机器人 @zhifox_test（密钥 …" in out and DISCORD_TOKEN not in out + err
    assert "integration_type=1" in out  # Discord 要先把机器人加到你的账号
    assert "下一步：运行 harness channels pair discord，把配对码发给 @zhifox_test。" in out
    assert discord_cli.store().credentials("discord") == {"token": DISCORD_TOKEN}


def test_setup_reads_several_piped_values_in_order(discord_cli, monkeypatch, make_config):
    """要填两项的平台（这里用 QQ 的 AppID 和 AppSecret）：管道里一行一项，按向导里的顺序。"""
    given = []

    def fake_check(config, credentials):
        given.append(credentials)
        return {"id": "1", "username": "智狐", "name": "智狐", "at": False}

    monkeypatch.setitem(hub.CHECKS, "qq", fake_check)
    monkeypatch.setattr(sys, "stdin", io.StringIO("102000123\nqq-app-secret-value\n"))
    code, out, err = discord_cli("channels", "setup", "qq")
    assert code == 0, err
    assert given == [{"app_id": "102000123", "app_secret": "qq-app-secret-value"}]
    assert "已连接机器人 智狐（密钥 …alue）" in out and "qq-app-secret-value" not in out


def test_setup_explains_how_to_install_a_missing_sdk(discord_cli, monkeypatch):
    monkeypatch.setattr(platforms, "sdk_ready", lambda platform: False)
    code, out, err = discord_cli("channels", "setup", "feishu")
    assert code == 1 and "uv sync --extra feishu" in err and out == ""


def test_status_lists_every_platform_in_order(discord_cli):
    code, out, err = discord_cli("channels", "status")
    assert code == 0, err
    order = [out.index(name) for name in ("Telegram", "Discord", "Slack", "飞书", "钉钉", "QQ")]
    assert order == sorted(order)
    assert "还没有设置。运行 harness channels setup slack。" in out


def test_the_guide_fills_in_every_code():
    for platform, steps in guides.GUIDES.items():
        for step in steps:
            assert "{code}" not in fill(step["text"], {"code": step.get("code", "")}), platform


# ---- /new 某个 Agent ------------------------------------------------------------


@pytest.fixture
def bot(make_config, monkeypatch):
    yield from rigs(make_config, monkeypatch)


def test_new_with_an_agent_name_starts_a_conversation_with_that_persona(bot):
    s = bot(reply(text("我是研究员。")), reply(text("普通的回答")))
    s.config.agents_dir.mkdir(parents=True, exist_ok=True)
    (s.config.agents_dir / "researcher.md").write_text(agent_file("researcher", body="你是一个只查资料的研究员。", tools="Read"), encoding="utf-8")

    s.wait_reply(s.say("/new 不存在的"), "没法用「不存在的」开始")
    s.wait_reply(s.say("/new researcher"), "由「researcher」来回答")
    assert "角色：researcher" in s.wait_reply(s.say("/status"), "现在：")["plain"]
    s.wait_reply(s.say("你是谁"), "我是研究员")
    s.idle()
    request = s.api.requests[0]
    assert request["system"][0]["text"].startswith("你是一个只查资料的研究员")  # 提示词换成了它的设定
    names = {tool["name"] for tool in request["tools"]}
    assert names and "run_command" not in names  # 工具只有它放开的（它只要了 Read）
    conversation = s.session().conversation
    assert conversation.agent == "researcher" and s.app.store.load(conversation.id).agent == "researcher"

    # 再 /new（不带名字）：回到普通的对话
    s.wait_reply(s.say("/new"), "好，开始一段新对话。")
    s.wait_reply(s.say("再问"), "普通的回答")
    s.idle()
    assert not s.api.requests[1]["system"][0]["text"].startswith("你是一个只查资料的研究员")
    assert s.session().conversation.agent == ""


# ---- 浏览器：每个聊天自己的标签页 ----------------------------------------------------


def test_each_chat_turn_closes_its_own_browser_tabs(bot, monkeypatch):
    """一轮结束就关掉这个聊天自己开的浏览器标签页（和定时任务的一次运行一样）；下一轮要用时换一个新的。"""
    from harness.app import App

    closed = []
    real = App.channel_agent

    def spy(self, *args, **kwargs):
        made = real(self, *args, **kwargs)
        original = made.close
        made.close = lambda: (closed.append(True), original())
        return made

    monkeypatch.setattr(App, "channel_agent", spy)
    s = bot(reply(text("第一轮")), reply(text("第二轮")))
    s.wait_reply(s.say("一"), "第一轮")
    s.idle()
    assert closed == [True]
    s.wait_reply(s.say("二"), "第二轮")
    s.idle()
    assert closed == [True, True]  # 同一个助手（工具说明书不变），每一轮各关一次


def test_closing_a_run_hands_the_next_turn_a_fresh_browser_session(make_config, monkeypatch):
    """关掉的 BrowserSession 会被浏览器管理器忘掉（之后它开的标签页没人认领）：所以 close 之后换一个新的、登记过的。"""
    from harness.app import build_app
    from harness.webui.ui import WebUI

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    app = build_app(make_config(), WebUI(), client_factory=lambda provider: None)
    before = set(app.browser._sessions)
    run = app.channel_agent(WebUI(), "ask", "anthropic", "claude-opus-5-5")
    first = set(app.browser._sessions) - before
    assert len(first) == 1
    run.close()
    second = set(app.browser._sessions) - before
    assert len(second) == 1 and second != first  # 旧的忘掉了，新的登记上了
    run.stop()  # 停的是新的那一个：不出错
