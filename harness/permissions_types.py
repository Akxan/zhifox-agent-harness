"""PermissionsPart：混进 Permissions 的两组方法（PermissionsAllowance、PermissionsReview）能用到的东西，写给类型检查（mypy）看。

为什么要有这个文件，和 app_types.py 是同一个道理：Permissions 由几组方法拼成（见 permissions.py 开头），每一组写在自己的
permissions_xxx.py 里，是一个只有方法的类。这些方法里用到 self.editor、self.mode、self._is_private(...)：状态在 permissions.py 的
__init__ 里，_is_private、_mcp_of 也在那里。单看 PermissionsReview 这个类，mypy 不知道 self 上有这些。

办法：两组都继承 PermissionsPart。它是一个 Protocol：
  - 状态只写类型，值在 Permissions.__init__ 里。两组方法对它们都只读不改，所以不用像 app_types.py 说的那样写一个值。
  - 方法只写签名，实现在 permissions.py；那边的签名改了、这里没跟着改，mypy 会报 [override]。
  - 这些只在类型检查时存在：运行时 PermissionsPart 是下面那个空的类，Permissions 的行为、MRO 里找到的方法和不拆开的时候一模一样。

新加一组方法时：class PermissionsXxx(PermissionsPart)，加进 Permissions 的基类；mypy 报"没有这个属性"时，照着它的定义在下面补一行。
"""

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
