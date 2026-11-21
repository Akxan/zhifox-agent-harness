"""文件工具看图片：view 一张图片时交回图片本身（harness/tools/files.py 的 _picture）。

用户在 Telegram 里让智狐截屏，截图存好了，可文件工具只会读文字，报"不是文本文件"，模型就看不到图。
这里测：认得出图片（看内容，不看扩展名）、太大的说清楚、Claude 在工具结果里拿到图片、GPT 拿到图片、
DeepSeek（看不了图片）拿到一句说明。
"""

from __future__ import annotations

import base64

import pytest
from fake_api import reply, text, tool_use
from fake_mcp import PNG
from fake_openai import call as openai_call
from fake_openai import chat
from sample_files import JPEG_BYTES

from harness.tools import files
from harness.tools.base import ToolError, ToolOutput
from harness.tools.files import FileEditor

FILE_TOOL = "str_replace_based_edit_tool"
PNG_BYTES = base64.b64decode(PNG)


@pytest.fixture
def editor(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    return FileEditor(workspace, tmp_path / "data" / "backups", protected=[workspace / ".env"])


def test_viewing_an_image_returns_the_image_itself(editor):
    (editor.workspace / "shot.png").write_bytes(PNG_BYTES)
    out = editor.run({"command": "view", "path": "shot.png"})
    assert isinstance(out, ToolOutput) and not out.is_error
    assert out.text.startswith(f"图片 {editor.workspace / 'shot.png'}（PNG，") and "附在这条结果里" in out.text
    (image,) = out.images
    assert image.media_type == "image/png" and base64.b64decode(image.data) == PNG_BYTES


def test_what_counts_as_an_image_is_decided_by_the_content(editor):
    # 扩展名说是 txt、内容是 JPEG：当图片；扩展名说是 png、内容是文字：照文字读
    (editor.workspace / "photo.txt").write_bytes(JPEG_BYTES)
    out = editor.run({"command": "view", "path": "photo.txt"})
    assert isinstance(out, ToolOutput) and out.images[0].media_type == "image/jpeg"
    (editor.workspace / "fake.png").write_text("其实是一段文字", encoding="utf-8")
    assert "其实是一段文字" in editor.run({"command": "view", "path": "fake.png"})
    # 别的二进制文件照旧说读不了
    (editor.workspace / "blob.bin").write_bytes(b"\x00\x01\x02\xff\xfe")
    with pytest.raises(ToolError, match="不是文本文件"):
        editor.run({"command": "view", "path": "blob.bin"})


def test_an_image_that_is_too_big_says_how_to_shrink_it(editor, monkeypatch):
    monkeypatch.setattr(files, "MAX_IMAGE_BYTES", 10)
    (editor.workspace / "big.png").write_bytes(PNG_BYTES)
    with pytest.raises(ToolError, match="太大了") as caught:
        editor.run({"command": "view", "path": "big.png"})
    assert "sips -Z" in str(caught.value)


def test_protected_files_stay_closed_even_if_they_look_like_images(editor):
    (editor.workspace / ".env").write_bytes(PNG_BYTES)
    with pytest.raises(ToolError, match="密钥"):
        editor.run({"command": "view", "path": ".env"})


def test_claude_sees_the_image_inside_the_tool_result(start):
    h = start(
        reply(tool_use("toolu_1", FILE_TOOL, {"command": "view", "path": "shot.png"}), stop_reason="tool_use"),
        reply(text("截图里是一个小点。")),
    )
    (h.config.workspace / "shot.png").write_bytes(PNG_BYTES)
    h.agent.run_turn("看看这张截图")
    result = h.api.requests[1]["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and "is_error" not in result
    assert result["content"][0]["type"] == "text" and result["content"][0]["text"].startswith("图片 ")
    assert result["content"][1] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}}


def test_gpt_gets_the_image_and_deepseek_gets_a_note(start_openai):
    view = openai_call("call_1", FILE_TOOL, '{"command": "view", "path": "shot.png"}')
    h = start_openai(chat(calls=[view]), chat("好的。"))
    (h.config.workspace / "shot.png").write_bytes(PNG_BYTES)
    h.agent.run_turn("看看这张截图")  # DeepSeek 看不了图片：结果里换成一句说明，不发图片
    messages = h.api.requests[1]["messages"]
    assert messages[-1]["role"] == "tool" and "看不了图片" in messages[-1]["content"]

    h = start_openai(chat(calls=[view]), chat("看到了。"))
    h.app.select_model("openai", "gpt-test")  # 看得懂图片的一家
    h.agent.run_turn("看看这张截图")
    messages = h.api.requests[1]["messages"]
    assert messages[-2]["role"] == "tool" and messages[-2]["content"].startswith("图片 ")
    assert messages[-1]["content"][1] == {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}"}}
