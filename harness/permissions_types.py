from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any, Protocol

    from .commands.allowlist import CommandList
    from .tasks import TaskStore
    from .tools.files import FileEditor

    class PermissionsPart(Protocol):
        editor: FileEditor
        private: list[Path]
        tasks: TaskStore | None
        mcp: Any
        commands: CommandList | None
        mode: str
        trust_mcp: bool

        def _is_private(self, path: Path) -> bool: ...
        def _mcp_of(self, tool_name: str, tool: object) -> tuple[str | None, str] | None: ...

else:

    class PermissionsPart:
        """运行时什么都没有：上面的那些只是写给 mypy 看的类型。"""
