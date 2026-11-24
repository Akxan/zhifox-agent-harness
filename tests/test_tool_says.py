"""工具结果里我们自己写的话（zh()，见 harness/i18n.py 开头）：模型拿到中文原文，界面上按界面的语言显示。

守住的几样：
  - 模型看到的、历史里存的，和以前直接写中文一字不差（普通的 str，不是 Message）；
  - 网页的 tool_result 事件带着原文（key、values），页面换语言时照着它重译（页面那一边见 tests/ui/test_ui_tool_says.py）；
  - 终端按界面的语言写；
  - 资料（命令的输出、文件的内容……）从来不翻译，哪怕它恰好和翻译表里的一句中文一样；遮过密钥的结果不带原文；
  - 以前存下的、没有原文的结果照原样重放；
  - 聊天通道的确认卡片按你选的界面语言写，换了语言以后，页面上"在等你批准"的那一句也跟着换。
"""

from __future__ import annotations

import json

import pytest
from fake_api import reply, text, tool_use
from test_channels import bot as bot  # noqa: F401  # 聊天通道（假 Telegram）的 fixture
from test_channels import until

from harness import i18n
from harness.i18n import Message, chinese, joined, retell, t, zh
from harness.tools import ToolRegistry
from harness.tools.base import ToolError, function_tool
from harness.tools.shell import Shell
from harness.ui import TerminalUI
from harness.webui.ui import MAX_RESULT_CHARS, WebUI, said, unsaid

NO_TODO = "没有编号为 {id} 的待办。先用 todo_list 查看现有的待办。"
ANTHROPIC_KEY = "sk-ant-api03-" + "Zx9Qw8Er7Ty6Ui5Op4As3Df2Gh1Jk0Lz-Xc_Vb" + "Nm1Qa2Ws3Ed4Rf5Tg6Yh7Uj8Ik9Ol0P"  # 编造的，形状像真的


def english(text: str, **values: object) -> str:
    """一句话的英文（照着翻译表填，不经过 zh / retell）。"""
    return i18n.fill(i18n.table("en")[text], {name: str(value) for name, value in values.items()})


def tool_results(request: dict) -> list[dict]:
    return [block for block in request["messages"][-1]["content"] if isinstance(block, dict) and block.get("type") == "tool_result"]


def result_text(block: dict) -> str:
    content = block["content"]
    return content if isinstance(content, str) else "".join(part.get("text", "") for part in content)


def results_of(ui: WebUI) -> list[dict]:
    return [event for event in ui.export_events() if event["type"] == "tool_result"]


# ---- zh()、chinese()、retell()、joined() -------------------------------------------


def test_zh_is_the_chinese_original_in_any_language_and_retell_translates_it(monkeypatch):
    monkeypatch.setattr(i18n, "current", "en")
    said_to_model = zh(NO_TODO, id=9)
    assert said_to_model == "没有编号为 9 的待办。先用 todo_list 查看现有的待办。"  # 界面是英文也一样
    assert type(str(said_to_model)) is str and str(said_to_model) == said_to_model
    assert (said_to_model.key, said_to_model.values) == (NO_TODO, {"id": 9})
    assert retell(said_to_model) == english(NO_TODO, id=9)
    monkeypatch.setattr(i18n, "current", "es")
    assert retell(said_to_model) == i18n.fill(i18n.table("es")[NO_TODO], {"id": "9"})
    monkeypatch.setattr(i18n, "current", "zh")
    assert retell(said_to_model) == said_to_model


def test_a_translated_sentence_filled_in_goes_to_the_model_in_chinese(monkeypatch):
    # TaskError、Skill 的问题这些是 t() 写的（页面上也显示）：界面是英文时它们已经译成了英文，交给模型时要换回中文原文
    monkeypatch.setattr(i18n, "current", "en")
    problem = t("参数不正确")
    assert problem != "参数不正确"
    whole = zh("{reason}先用 task_list 查看现有的任务。", reason=problem)
    assert whole == "参数不正确先用 task_list 查看现有的任务。"
    assert retell(whole) == english("{reason}先用 task_list 查看现有的任务。", reason=english("参数不正确"))
    assert chinese(problem) == "参数不正确" and chinese("普通的文字") == "普通的文字"


def test_a_value_called_text_is_fine():
    # zh() 和 t() 的第一个参数只能按位置给：句子里的 {text} 不会撞上它（Skill 读文件的结果就有 {text}）
    said_to_model = zh("{file}（{size}）：\n\n{text}", file="a.md", size="1 KB", text="正文")
    assert said_to_model == "a.md（1 KB）：\n\n正文"
    assert retell(said_to_model) == said_to_model
    assert t("{file}（{size}）：\n\n{text}", file="a.md", size="1 KB", text="正文") == "a.md（1 KB）：\n\n正文"


def test_data_filled_in_is_never_translated_even_when_it_looks_like_one_of_ours(monkeypatch):
    monkeypatch.setattr(i18n, "current", "en")
    # 命令输出了一句和翻译表里一模一样的中文：它是资料，原样；只有后面我们接的那一句译
    result = zh("{output}{code}{sandbox}{note}", output="没有提供要修改的内容。", code=zh("\n[退出码 {code}]", code=1), sandbox="", note="")
    assert result == "没有提供要修改的内容。\n[退出码 1]"
    assert retell(result) == "没有提供要修改的内容。" + english("\n[退出码 {code}]", code=1)
    # 普通的文字（工具直接交回的资料）retell 原样交回，不去查翻译表
    assert retell("没有提供要修改的内容。") == "没有提供要修改的内容。"


def test_joined_keeps_each_part_translatable(monkeypatch):
    assert joined("\n", "a", "", "b") == "a\nb" and not isinstance(joined("\n", "a", "b"), Message)
    monkeypatch.setattr(i18n, "current", "en")
    parts = (zh("没有提供要修改的内容。"), "资料", zh("（空目录）"))
    whole = joined("\n", *parts)
    assert whole == "没有提供要修改的内容。\n资料\n（空目录）"
    assert retell(whole) == "\n".join([english("没有提供要修改的内容。"), "资料", english("（空目录）")])


def test_unsaid_turns_event_fields_back_into_a_sentence_with_its_nested_parts(monkeypatch):
    # 聊天通道的确认卡片、定时任务的进度都靠它：事件里存的原文（连里面填进去的那一句）变回 Message，换了语言照样重译
    inner = t("执行命令：\n\n    $ {command}", command="ls")
    outer = t("{question}\n\n在这个目录里运行：{path}（之前的命令 cd 到了这里）", question=inner, path="/srv/sub")
    fields = said(outer)
    assert fields["values"]["question"] == {"key": inner.key, "values": {"command": "ls"}}
    monkeypatch.setattr(i18n, "current", "es")
    rebuilt = unsaid(fields["text"], fields["key"], fields["values"])
    assert retell(rebuilt) == retell(outer)
    assert "ejecutar un comando" in retell(rebuilt)
    assert unsaid("以前存下的普通文字") == "以前存下的普通文字"


# ---- 一轮对话里：模型、历史、网页的事件 ----------------------------------------------


def test_the_model_gets_chinese_and_the_page_event_carries_the_key(start, monkeypatch):
    h = start(reply(tool_use("toolu_1", "todo_delete", {"id": 9}), stop_reason="tool_use"), reply(text("没有这一条。")), ui=WebUI())
    monkeypatch.setattr(i18n, "current", "en")  # build_app 按设置定了中文；之后换成英文界面
    h.agent.run_turn("删掉 9 号")

    chinese_result = "没有编号为 9 的待办。先用 todo_list 查看现有的待办。"
    (result,) = tool_results(h.api.requests[1])
    assert result["is_error"] is True and result_text(result) == chinese_result  # 发给模型的一字不差
    stored = json.dumps(h.agent.backend.export(), ensure_ascii=False)
    assert chinese_result in stored and english(NO_TODO, id=9) not in stored  # 历史里存的也是中文
    for message in h.agent.backend.export():  # 历史里只有普通的字符串
        for block in message["content"] if isinstance(message["content"], list) else []:
            assert not isinstance(block.get("content"), Message)

    (event,) = results_of(h.ui)
    assert event["key"] == NO_TODO and event["values"] == {"id": 9}
    assert event["text"] == english(NO_TODO, id=9) and event["is_error"] is True


def test_data_results_are_plain_and_our_sentences_carry_their_key(start, monkeypatch):
    h = start(
        reply(
            tool_use("toolu_1", "echo", {"text": "没有提供要修改的内容。"}),  # 资料：和翻译表里的一句一样
            tool_use("toolu_2", "ours", {}),
            tool_use("toolu_3", "fails", {}),
            tool_use("toolu_4", "secret", {}),
            stop_reason="tool_use",
        ),
        reply(text("好。")),
        ui=WebUI(),
    )

    def secret() -> str:
        return zh("{output}{code}{sandbox}{note}", output="key=" + ANTHROPIC_KEY, code=zh("\n[退出码 {code}]", code=1), sandbox="", note="")

    def fails() -> str:
        raise ToolError(zh(NO_TODO, id=3))

    tools = [
        function_tool("echo", "原样交回", {"text": {"type": "string"}}, ["text"], lambda text: text),
        function_tool("ours", "我们的话", {}, [], lambda: zh("（空目录）")),
        function_tool("fails", "出错", {}, [], fails),
        function_tool("secret", "带着密钥", {}, [], secret),
    ]
    h.agent.on_start = None  # 不换成这段对话真正的工具（见 App.fresh_tools），就用上面这几个
    h.agent.use_tools(ToolRegistry(tools))
    monkeypatch.setattr(i18n, "current", "en")
    h.agent.run_turn("试试")

    echo, ours, failed, hidden = results_of(h.ui)
    assert echo["text"] == "没有提供要修改的内容。" and "key" not in echo  # 资料不带原文、不翻译
    assert ours["text"] == english("（空目录）") and ours["key"] == "（空目录）"
    assert failed["text"] == english(NO_TODO, id=3) and failed["key"] == NO_TODO and failed["is_error"] is True
    # 遮过密钥的：交给模型的和 zh() 原来那句不一样了，所以不带原文（原文的值里还是没遮的密钥），显示遮好的那一份
    assert "key" not in hidden and "values" not in hidden and "‹已遮住" in hidden["text"]
    assert ANTHROPIC_KEY not in json.dumps(h.ui.export_events(), ensure_ascii=False)
    sent = [result_text(block) for block in tool_results(h.api.requests[1])]
    assert sent[:3] == ["没有提供要修改的内容。", "（空目录）", "没有编号为 3 的待办。先用 todo_list 查看现有的待办。"]


def test_a_long_result_is_cut_and_shown_as_plain_text(monkeypatch):
    monkeypatch.setattr(i18n, "current", "en")
    ui = WebUI()
    ui.tool_result(zh("{output}{code}{sandbox}{note}", output="x" * (MAX_RESULT_CHARS + 10), code=zh("\n[退出码 {code}]", code=1), sandbox="", note=""), True)
    (event,) = results_of(ui)
    assert "key" not in event and len(event["text"]) < MAX_RESULT_CHARS + 200  # 照着原文重译会把整段又放出来


def test_shell_notes_are_ours_and_the_output_is_not(tmp_path, monkeypatch):
    shell = Shell(tmp_path)
    result = shell.run("exit 3")  # bash 和 PowerShell 都认
    assert result == "（没有输出）\n[退出码 3]"  # 交给模型的和以前一字不差
    monkeypatch.setattr(i18n, "current", "en")
    assert retell(result) == english("（没有输出）") + english("\n[退出码 {code}]", code=3)


def test_an_old_saved_conversation_without_keys_replays_its_text(monkeypatch):
    saved = [
        {"type": "user", "text": "删掉 9 号"},
        {"type": "tool_call", "name": "todo_delete", "summary": "todo_delete 9", "input": {"id": 9}},
        {"type": "tool_result", "text": "没有编号为 9 的待办。先用 todo_list 查看现有的待办。", "is_error": True},
    ]
    monkeypatch.setattr(i18n, "current", "en")
    ui = WebUI()
    ui.load_events(saved)
    (event,) = results_of(ui)
    assert event == saved[2]  # 原样：不猜原文，不翻译（页面照着 text 画）


def test_the_terminal_shows_our_sentences_in_the_interface_language(start, monkeypatch, capsys):
    h = start(reply(tool_use("toolu_1", "todo_delete", {"id": 9}), stop_reason="tool_use"), reply(text("没有这一条。")), ui=TerminalUI())
    monkeypatch.setattr(i18n, "current", "en")
    h.agent.run_turn("删掉 9 号")
    out = capsys.readouterr().out
    assert english(NO_TODO, id=9)[:60] in out and "没有编号为 9 的待办" not in out
    (result,) = tool_results(h.api.requests[1])
    assert result_text(result) == "没有编号为 9 的待办。先用 todo_list 查看现有的待办。"
    (event,) = [event for event in h.ui.log.export_events() if event["type"] == "tool_result"]  # 终端存下的会话：网页打开时照样能重译
    assert event["key"] == NO_TODO


# ---- 聊天通道的确认卡片 -------------------------------------------------------


@pytest.mark.parametrize("language", ["en", "es"])
def test_the_channel_approval_card_is_in_the_interface_language(bot, monkeypatch, language):
    s = bot(
        reply(text("我来看看。"), tool_use("toolu_1", "run_command", {"command": "echo hi"}), stop_reason="tool_use"),
        reply(text("好了。")),
    )
    monkeypatch.setattr(i18n, "current", language)
    start = s.say("看看")
    ask = s.asked(start)
    table = i18n.table(language)
    title = table["助手想要"] + i18n.fill(table["执行命令：\n\n    $ {command}"], {"command": "echo hi"}).split("\n")[0]
    assert ask["plain"].startswith(title + "\n$ echo hi")
    assert "执行命令" not in ask["plain"] and "助手想要" not in ask["plain"]
    assert ask["plain"].endswith(i18n.fill(table["工作目录：{path}"], {"path": str(s.config.workspace)}))
    allow, deny = s.tg.buttons(ask)
    assert (allow["text"], deny["text"]) == (table["允许"], table["拒绝"])
    s.press(ask)
    s.wait_reply(start, "好了")
    s.idle()


def test_the_waiting_approval_on_the_page_follows_a_language_switch(bot, monkeypatch):
    # 确认请求发到聊天里的时候是中文；之后在页面上换成英文，页面上"在等你批准"里的那一句也跟着换（以前是当时的文字，换不了）
    s = bot(
        reply(tool_use("toolu_1", "run_command", {"command": "echo hi"}), stop_reason="tool_use"),
        reply(text("好了。")),
    )
    start = s.say("看看")
    ask = s.asked(start)
    assert ask["plain"].startswith("助手想要执行命令：")
    status, _ = s.browser.post("/api/language", {"language": "en"})
    assert status == 200

    def summary() -> str:
        approvals = s.state()["channels"]["approvals"]
        return approvals[0]["summary"] if approvals else ""

    until(lambda: summary().startswith(english("执行命令：\n\n    $ {command}", command="echo hi").split("\n")[0]), what="换成英文的确认")
    assert "echo hi" in summary()
    s.press(ask)
    s.wait_reply(start, "好了")
    s.idle()
    s.browser.post("/api/language", {"language": "zh"})
