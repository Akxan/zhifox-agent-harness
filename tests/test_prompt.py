"""系统提示词：内置的版本、用户改过的版本、占位符、什么时候生效（harness/prompt.py、App.set_prompt）。"""

from __future__ import annotations

import pytest
from fake_api import reply, text
from fake_openai import chat
from modes import has_mode

from harness.config import MAX_PROMPT_CHARS
from harness.prompt import (
    DEFAULT_TEMPLATE,
    PromptError,
    build_system_prompt,
    load_template,
    render,
    reset_template,
    save_template,
)


def system_text(request: dict) -> str:
    """Claude 请求里的系统提示词。"""
    return request["system"][0]["text"]


def test_on_windows_the_system_line_says_commands_are_powershell(make_config, monkeypatch):
    """Windows 上"操作系统"那一行写明命令用的是 PowerShell：模型最容易默认写 bash。别的系统上只是系统的名字和版本。"""
    from harness import prompt
    from harness.tools import shell

    monkeypatch.setattr(shell, "WINDOWS", True)
    monkeypatch.setattr(shell.shutil, "which", lambda name: None)
    assert "Windows PowerShell 5.1" in prompt.placeholder_values(make_config())["system"]
    monkeypatch.setattr(shell, "WINDOWS", False)
    assert "PowerShell" not in prompt.placeholder_values(make_config())["system"]


def test_placeholders_are_filled_and_other_braces_kept(make_config):
    config = make_config()
    filled = render('我叫{name}（{latin}），在 {workspace} 里工作。示例：{"a": 1} 和 {不认识的}', config)
    assert filled == f'我叫智狐（ZhiFox），在 {config.workspace} 里工作。示例：{{"a": 1}} 和 {{不认识的}}'
    # 联网那一行：能联网的那一家才写
    assert "web_search" in render("{web_line}", config, web=True)
    assert render("{web_line}", config, web=False) == ""
    assert "web_search" not in build_system_prompt(make_config(web=False))


def test_the_built_in_prompt_is_used_until_the_user_saves_one(make_config):
    config = make_config()
    assert load_template(config) == DEFAULT_TEMPLATE and not config.prompt_file.exists()
    save_template(config, "你是{name}。只说英语。\r\n")
    assert load_template(config) == "你是{name}。只说英语。\n"  # Windows 的换行统一成 \n
    assert has_mode(config.prompt_file, 0o600)  # 和别的数据文件一样只有自己能读
    assert build_system_prompt(config) == "你是智狐。只说英语。\n"
    # 文件是空的、读不了：用内置的
    config.prompt_file.write_text("  \n", encoding="utf-8")
    assert load_template(config) == DEFAULT_TEMPLATE
    config.prompt_file.write_bytes(b"\xff\xfe bad")
    assert load_template(config) == DEFAULT_TEMPLATE
    # 存的和内置的一模一样，就当作恢复默认：文件删掉，以后跟着程序升级
    save_template(config, DEFAULT_TEMPLATE)
    assert not config.prompt_file.exists()
    save_template(config, "改过的")
    reset_template(config)
    assert not config.prompt_file.exists()
    reset_template(config)  # 已经是默认的，再恢复一次也没事


def test_prompts_that_cannot_be_saved_say_why(make_config):
    config = make_config()
    with pytest.raises(PromptError, match="不能是空的"):
        save_template(config, " \n ")
    with pytest.raises(PromptError, match=f"最多 {MAX_PROMPT_CHARS} 个字符"):
        save_template(config, "字" * (MAX_PROMPT_CHARS + 1))
    with pytest.raises(PromptError):
        save_template(config, None)
    assert not config.prompt_file.exists()


def test_a_new_prompt_starts_with_the_next_conversation(start):
    h = start(reply(text("好。")), reply(text("还是原来的。")), reply(text("新的。")))
    h.agent.run_turn("你好")
    old = system_text(h.api.requests[0])
    # 已经聊起来了：这一段继续用原来的提示词（一段对话里提示词不能变）
    assert h.app.set_prompt("你是{name}。回答只用一个字。") is False
    h.agent.run_turn("再说一句")
    assert system_text(h.api.requests[1]) == old
    # 下一段新对话用新的
    h.app.new_conversation()
    h.agent.run_turn("你好")
    assert system_text(h.api.requests[2]) == "你是智狐。回答只用一个字。"


def test_an_empty_conversation_switches_to_the_new_prompt_right_away(start):
    h = start(reply(text("好。")), reply(text("又换回来了。")))
    assert h.app.set_prompt("你是{name}。") is True  # 还没聊：马上用新的
    h.agent.run_turn("你好")
    assert system_text(h.api.requests[0]) == "你是智狐。"
    # 恢复默认之后，打开一段旧会话也用回内置的
    conversation_id = h.app.conversation.id
    h.app.save_conversation([{"type": "user", "text": "你好"}])
    assert h.app.set_prompt(None) is False
    h.app.new_conversation()
    h.app.open_conversation(conversation_id)
    h.agent.run_turn("接着聊")
    assert system_text(h.api.requests[1]) == build_system_prompt(h.config) and "web_search" in system_text(h.api.requests[1])


def test_providers_without_web_search_are_not_told_about_it(start_openai):
    h = start_openai(chat("好。"))
    h.agent.run_turn("你好")
    system = h.api.requests[0]["messages"][0]
    assert system["role"] == "system" and "智狐" in system["content"]
    # DeepSeek 拿不到联网工具，提示词里也就不说能联网，不然它会去调用一个不存在的工具
    assert "web_search" not in system["content"]


def test_the_built_in_prompt_explains_what_the_program_adds(make_config):
    # 程序会往对话里放几样东西，模型得知道它们是什么：时间说明、定时任务的说明、附件、压缩后的摘要
    prompt_text = build_system_prompt(make_config())
    for marker in ("<context>", "<task>", '<attachment name="文件名">', "摘要"):
        assert marker in prompt_text, marker
    assert "以文件里现在的内容为准" in prompt_text  # 用户可以在界面里改、删记忆
    assert "不用问" in prompt_text  # 用户可能设成了自动允许，不是每次都会问
