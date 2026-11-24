from __future__ import annotations

from pathlib import Path

from .commands import Places, Unclear, classify
from .desktop.session import Facts as DesktopFacts
from .permissions_browser import _always_ask
from .permissions_command import _command_dir, _escapes
from .permissions_types import PermissionsPart
from .tools import files, shell
from .tools.base import ToolError
from .tools.browser import BrowserTool
from .tools.computer import DesktopTool

# 没问就放行时，为什么（见 Permissions.allowance）。界面上这几种写得更具体（"自动允许（只读）"）
WHY_READONLY = "readonly"  # 内置的只读命令、读工作目录之外的文件
WHY_LISTED = "listed"  # 你自己加的"不用问的命令"
WHY_TRUSTED = "trusted"  # 操作整台电脑：你在这段对话的确认卡片上选了"这段对话里不再问"


class PermissionsAllowance(PermissionsPart):
    def auto_allows(self, tool_name: str, tool_input: object, tool: object = None) -> bool:
        """一次需要确认的操作，在现在的模式下是不是可以不问就做。tool 是这次要用的那个工具（见 review）。"""
        return self.allowance(tool_name, tool_input, tool) is not None

    def allowance(self, tool_name: str, tool_input: object, tool: object = None) -> str | None:
        """和 auto_allows 一样，只是说出为什么可以不问：返回 None 表示要问；
        WHY_READONLY、WHY_LISTED 是 readonly 那一档放行的，WHY_TRUSTED 是这段对话里说过不再问的（界面上写得更具体），
        别的情况返回 "mode"。"""
        if isinstance(tool, DesktopTool):
            return self._computer_allowance(tool.look(tool_input) if isinstance(tool_input, dict) else None)
        if _escapes(tool_name, tool_input, tool):
            return None  # 在沙箱之外执行一条命令：在任何模式下都要问（见文件开头）
        if isinstance(tool, BrowserTool) and isinstance(tool_input, dict) and _always_ask(tool.look(tool_input)):
            return None  # 往密码框、银行卡里输入，打开不是网页的地址：在任何模式下都要问（见文件开头）
        if self.mode == "all":
            return "mode"
        found = self._mcp_of(tool_name, tool)
        if found is not None:
            # 你单独放开了这个 MCP 工具（见文件开头的说明）。认不出是哪个服务器的，一律问
            trusted = self.trust_mcp and found[0] is not None and self.mcp is not None and self.mcp.trusted(*found)
            return "mode" if trusted else None
        if self.mode == "edits":
            return "mode" if self._edits_workspace_file(tool_name, tool_input) else None
        if self.mode == "readonly":
            return self._reads_only(tool_name, tool_input, tool)
        return None

    def _computer_allowance(self, facts: DesktopFacts | None) -> str | None:
        """操作整台电脑的一步能不能不问就做（规矩见文件开头）。只有两种情况不问：这段对话里说过这一类不再问，
        或者这一档是"全部自动允许"。危险的组合键怎么都要问；"只读的命令""改文件"那两档、MCP 的"不用问"都不算数。"""
        if facts is None or facts.danger:
            return None
        if facts.trusted and facts.attended:
            return WHY_TRUSTED
        return "mode" if self.mode == "all" else None

    def places(self, cwd: Path | None = None) -> Places:
        """命令在哪个目录里运行（不给是工作目录）、哪些地方不能不问就读（给 commands.classify 用）。"""
        protected = [*self.editor.protected, *([self.commands.path] if self.commands is not None else [])]
        return Places(cwd or self.editor.workspace, protected=protected, private=self.private, workspace=self.editor.workspace)

    def _reads_only(self, tool_name: str, tool_input: object, tool: object = None) -> str | None:
        """readonly 那一档：这一步是不是只是看。只读的命令、你自己加的命令、读工作目录之外的文件算；
        碰到密钥文件、助手自己的数据目录、~/.ssh 这类地方的不算（见 commands/paths.py）。
        命令里的相对路径从这条命令真正运行的目录算（上一条命令 cd 到的地方，见 _command_dir）。"""
        if self.editor is None or not isinstance(tool_input, dict):
            return None
        if tool_name == shell.TOOL_NAME:
            listed = self.commands.words() if self.commands is not None else []
            here = _command_dir(tool_input, tool)
            places = self.places(here)
            if here is not None and here != self.editor.workspace:
                # 不在工作目录里运行（之前的命令 cd 走了）：这个目录本身也要看一眼。不带路径的 ls、git status 看的就是它，
                # 命令里却一个路径都没有；cd 进了数据目录、~/.ssh 这类地方，那里的什么命令都不算只读。
                # 在工作目录里运行时不看（和以前一样）：工作目录的路径里碰巧有 secret 这样的词，也不该让每条命令都要问
                try:
                    places.check(".")
                except Unclear:
                    return None
            verdict = classify(tool_input.get("command"), places, listed)
            if not verdict.readonly:
                return None
            return WHY_LISTED if verdict.source == "listed" else WHY_READONLY
        if tool_name == files.TOOL_NAME and tool_input.get("command") == "view":
            try:
                path = self.editor.resolve(tool_input.get("path"))
                if self._is_private(path):
                    return None  # 助手自己的数据：除了"全部自动允许"，读它一律先问（见 __init__ 的说明）
                self.places().check(str(path))
            except (ToolError, Unclear):
                return None
            return WHY_READONLY
        return None

    def _edits_workspace_file(self, tool_name: str, tool_input: object) -> bool:
        """是不是"创建或修改工作目录里的文件"（不算助手自己的数据目录）。"""
        if tool_name != files.TOOL_NAME or not isinstance(tool_input, dict):
            return False
        if tool_input.get("command") not in files.WRITE_COMMANDS:
            return False
        try:
            path = self.editor.resolve(tool_input.get("path"))
        except ToolError:
            return False
        return self.editor.in_workspace(path) and not self._is_private(path)
