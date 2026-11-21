"""一条回复里连着的几个文字块，要当成同一段显示（providers/claude.py 的 _show）。

Claude 联网搜索后带引用的回答，API 会按引用把它切成一串连着的 text 块：
"Perfect Items (Madrid): es el más similar. Se describe como" | "proveedor B2B líder en España…" | ". Anuncia" | ……
以前每个块结束都告诉界面"这一段写完了"（end_stream）：网页把每一小块当成单独的一段回答去排 Markdown
（一句话断成好几行，标点单独一行，每一小块还有自己的复制按钮），Telegram 把每一小块各发成一条消息。
现在连着的文字块之间不结束；下一个不是文字的块（思考、工具调用）开始时、或者整条回复结束时才结束。
"""

from __future__ import annotations

from conftest import RecordingUI
from fake_api import reply, text, thinking, tool_use


class StreamUI(RecordingUI):
    """把"这一段写完了"也记下来（连着的几次只记一次，和真的界面一样：没有正在写的段落时什么都不做）。"""

    def __init__(self):
        super().__init__()
        self._open = False

    def thinking(self, value: str) -> None:
        super().thinking(value)
        self._open = True

    def text(self, value: str) -> None:
        super().text(value)
        self._open = True

    def end_stream(self) -> None:
        if self._open:
            self.streamed.append(("end", ""))
            self._open = False

    def tool_call(self, name: str, tool_input: object) -> None:
        self.end_stream()
        super().tool_call(name, tool_input)


def test_text_blocks_split_by_citations_stay_one_paragraph(start):
    ui = StreamUI()
    h = start(reply(text("Perfect Items (Madrid): es el más similar. Se describe como "), text("proveedor B2B líder"), text(". Anuncia más de 1.200 empresas")), ui=ui)
    h.agent.run_turn("busca información")
    assert ui.streamed == [
        ("text", "Perfect Items (Madrid): es el más similar. Se describe como "),
        ("text", "proveedor B2B líder"),
        ("text", ". Anuncia más de 1.200 empresas"),
        ("end", ""),
    ]


def test_thinking_and_tool_calls_still_end_the_paragraph(start):
    ui = StreamUI()
    h = start(
        reply(thinking("先看看待办"), text("我看一下"), text("你的待办。"), tool_use("toolu_1", "todo_list", {}), stop_reason="tool_use"),
        reply(text("没有待办。")),
        ui=ui,
    )
    h.agent.run_turn("我有什么待办")
    assert ui.streamed == [
        ("thinking", "先看看待办"),
        ("end", ""),  # 思考和回答是两块不同的东西，中间要分开
        ("text", "我看一下"),
        ("text", "你的待办。"),
        ("end", ""),  # 工具调用之前结束
        ("text", "没有待办。"),
        ("end", ""),
    ]
