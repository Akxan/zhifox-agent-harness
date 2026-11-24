"""权限 · 要不要问、问的话卡片上写什么（Permissions 的一组方法，见 permissions.py 开头的表）。

  review               入口：按工具分给各自的那一个。MCP 的工具在这里把参数整段给你看；浏览器、读网页、操作整台电脑、执行命令
                       分别在 permissions_browser.py、permissions_fetch.py、permissions_computer.py、permissions_command.py
  _review_send         把一个文件发到聊天里（每次都问；数据目录里的带一句提醒）
  _review_task_add、_review_task_delete   添加、删除让助手去做的定时任务（只是提醒的不用问）
  _review_file         创建、修改、读取文件

返回 None 是不用问，返回一段话是要问（这段话显示在卡片上）。这一档能不能不问是另一回事，在 permissions_allowance.py。
新加一种会改动电脑的工具，要在 review 里给它加一条（见 CLAUDE.md）。
"""

from __future__ import annotations

import json

from .display import file_size, short
from .i18n import t
from .permissions_browser import review_browser
from .permissions_cards import _preview
from .permissions_command import _review_command
from .permissions_computer import review_computer
from .permissions_fetch import review_fetch
from .permissions_types import PermissionsPart
from .tasks import TaskError, check_task, describe
from .tools import files, shell
from .tools import tasks as task_tools
from .tools.base import ToolError
from .tools.browser import BrowserTool
from .tools.computer import DesktopTool
from .tools.fetch import FetchTool
from .tools.send import SendFacts, SendTool


class PermissionsReview(PermissionsPart):
    def review(self, tool_name: str, tool_input: object, tool: object = None) -> str | None:
        """审查一次工具调用。tool 是这次要用的那个工具（agent.py 会给；不给的话只按名字判断）。

        返回 None     —— 可以直接执行。
        返回一段文字  —— 必须先问用户。这段文字会显示给用户，说明助手想做什么。
        """
        if not isinstance(tool_input, dict):
            return None  # 参数本身不合法，工具会直接报错，什么都不会执行（MCP 的工具也是：还没联系服务器就退回去了）
        found = self._mcp_of(tool_name, tool)
        if found is not None:
            # 参数整段给你看：MCP 的工具会把它们发给另一个程序（可能在网上），你要知道发出去的是什么
            shown = json.dumps(tool_input, ensure_ascii=False, indent=2, default=str)
            return t("调用 MCP 服务器“{server}”的工具 {tool}，参数是：\n\n{args}", server=found[0] or "?", tool=found[1], args=_preview(shown, 40))
        if isinstance(tool, BrowserTool):
            return review_browser(tool.look(tool_input))
        if isinstance(tool, FetchTool):
            return review_fetch(tool.look(tool_input))
        if isinstance(tool, DesktopTool):
            return review_computer(tool.look(tool_input))
        if isinstance(tool, SendTool):
            return self._review_send(tool.look(tool_input))
        if tool_name == shell.TOOL_NAME:
            return _review_command(tool_input, tool)
        if tool_name == files.TOOL_NAME:
            return self._review_file(tool_input)
        if tool_name == task_tools.ADD:
            return self._review_task_add(tool_input)
        if tool_name == task_tools.DELETE:
            return self._review_task_delete(tool_input)
        return None

    def _review_send(self, facts: SendFacts) -> str | None:
        """把一个文件发到聊天里：每次都问（"全部自动允许"除外，auto_allows 照常按模式算）。发不了的（不在聊天里、
        保存密钥的文件、太大……）不问：工具会直接把原因告诉模型，什么都不会发出去。"""
        if facts.refusal or facts.path is None:
            return None
        kind = t("图片，会直接显示在聊天里") if facts.kind == "photo" else t("作为文件发送")
        caption = t("\n  说明：{caption}", caption=short(facts.caption, 300)) if facts.caption else ""
        private = t("\n\n⚠ 这是助手自己保存的数据（以前的会话、设置、待办都在这个目录里）。发出去以后它就离开了这台电脑。") if self._is_private(facts.path) else ""
        return t(
            "把一个文件发到 {platform}（和 {person} 的私聊）：\n\n  文件：{path}\n  大小：{size}（{kind}）{caption}{private}",
            platform=t(facts.label),
            person=facts.person or "?",
            path=facts.shown,
            size=file_size(facts.size),
            kind=kind,
            caption=caption,
            private=private,
        )

    def _review_task_add(self, tool_input: dict) -> str | None:
        if "approval" in tool_input:
            # 工具没有这个参数（模型添加的任务一律"每次都问"，见 tools/tasks.py），会直接报错、什么都不会添加：
            # 不用为一次注定失败的调用去打扰用户
            return None
        try:
            task = check_task({**tool_input, "enabled": True})
        except TaskError:
            return None  # 参数不对，工具会直接报错，什么都不会添加
        if task["kind"] != "agent":
            return None  # 只是提醒：不花钱，也不会自己做任何事
        return t(
            "添加定时任务“{title}”（{when}）：到时间后，助手会在没人看着的时候自己运行下面这条指令，每次运行都要花 API 费用。\n\n{prompt}",
            title=task["title"],
            when=describe(task["schedule"]),
            prompt=_preview(task["prompt"]),
        )

    def _review_task_delete(self, tool_input: dict) -> str | None:
        task = self.tasks.get(tool_input.get("id")) if self.tasks is not None else None  # 模型没给 id 时 get 照样回答 None
        if task is None or task["kind"] != "agent":
            return None  # 不存在（工具会报错）或者只是提醒：不用问
        return t("删除定时任务“{title}”（{when}），以后它不会再自动运行。", title=task["title"], when=describe(task["schedule"]))

    def _review_file(self, tool_input: dict) -> str | None:
        command = tool_input.get("command")
        try:
            path = self.editor.resolve(tool_input.get("path"))
        except ToolError:
            return None  # 路径无效，工具会直接报错，什么都不会执行
        shown = self.editor.display(path)

        # 这些说明会接在"助手想要"后面显示（"助手想要执行命令：…"），所以都写成"做什么"的样子
        if command == "create":
            content = _preview(tool_input.get("file_text"))
            if path.exists():
                return t("覆盖已有文件：{path}\n\n{content}", path=shown, content=content)
            return t("创建文件：{path}\n\n{content}", path=shown, content=content)
        if command == "str_replace":
            old, new = _preview(tool_input.get("old_str")), _preview(tool_input.get("new_str"))
            return t("修改文件：{path}\n\n  把这段：\n{old}\n  改成：\n{new}", path=shown, old=old, new=new)
        if command == "insert":
            text = tool_input.get("insert_text", tool_input.get("new_str"))
            line = tool_input.get("insert_line")
            return t("在文件 {path} 的第 {line} 行之后插入：\n\n{content}", path=shown, line=line, content=_preview(text))

        # 剩下的是读取（view）
        if self._is_private(path):
            return t("读取助手自己保存的数据（以前的会话、设置、待办都在这个目录里）：\n\n    {path}", path=path)
        if self.editor.in_workspace(path):
            return None
        return t("读取工作目录之外的内容：\n\n    {path}", path=path)
