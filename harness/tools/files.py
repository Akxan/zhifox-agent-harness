"""读写文件 —— 第二类工具：Anthropic 定义格式，我们负责执行。

这个工具（text editor）的名字和参数格式是 Anthropic 规定的，模型在训练时就学过怎么用，
所以说明书只需要一行声明，不用自己写参数格式。但文件操作发生在你的电脑上，
所以执行部分要我们自己实现：模型发来 view / create / str_replace / insert 四种命令，
这个文件负责真正去读写磁盘。

哪些操作要先问用户，由 permissions.py 决定，不在这里。
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from .base import Tool, ToolError

TOOL_NAME = "str_replace_based_edit_tool"
WRITE_COMMANDS = {"create", "str_replace", "insert"}

MAX_VIEW_CHARS = 60_000  # 一次最多给模型看这么多字符，避免大文件撑爆上下文
MAX_DIR_ENTRIES = 200


class FileEditor:
    def __init__(self, workspace: Path, backup_dir: Path, protected: Iterable[Path] = ()):
        self.workspace = workspace.resolve()
        self.backup_dir = backup_dir
        self.protected = {path.resolve() for path in protected}

    # ---- 路径 -------------------------------------------------------------

    def resolve(self, raw: object) -> Path:
        """把模型给的路径变成绝对路径。相对路径以工作目录为起点。

        模型给的路径不能直接信任：这里把 `..` 和符号链接全部展开，
        之后的检查看到的才是真正会被访问的那个位置。
        """
        if not isinstance(raw, str) or not raw.strip():
            raise ToolError("缺少 path 参数。")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        return path.resolve()

    def in_workspace(self, path: Path) -> bool:
        return path == self.workspace or path.is_relative_to(self.workspace)

    def display(self, path: Path) -> str:
        """给人看的路径：工作目录内的显示相对路径，之外的显示完整路径。"""
        if path != self.workspace and self.in_workspace(path):
            return str(path.relative_to(self.workspace))
        return str(path)

    # ---- 入口 -------------------------------------------------------------

    def run(self, tool_input: dict) -> str:
        if not isinstance(tool_input, dict):
            raise ToolError("参数必须是一个 JSON 对象。")
        command = tool_input.get("command")
        path = self.resolve(tool_input.get("path"))
        if path in self.protected:
            raise ToolError("这个文件保存着密钥，助手不能读取或修改它。")

        if command == "view":
            return self._view(path, tool_input.get("view_range"))
        if command == "create":
            return self._create(path, _text(tool_input, "file_text"))
        if command == "str_replace":
            return self._str_replace(path, _text(tool_input, "old_str"), _text(tool_input, "new_str"))
        if command == "insert":
            # 不同版本的工具对"要插入的文字"用过两个名字，这里都接受
            key = "insert_text" if "insert_text" in tool_input else "new_str"
            return self._insert(path, tool_input.get("insert_line"), _text(tool_input, key))
        raise ToolError(f"不支持的命令 {command!r}。可用的命令：view、create、str_replace、insert。")

    # ---- 四种命令 ---------------------------------------------------------

    def _view(self, path: Path, view_range: object) -> str:
        if path.is_dir():
            entries = sorted(p for p in path.iterdir() if not p.name.startswith("."))
            lines = [f"{p.name}/" if p.is_dir() else p.name for p in entries[:MAX_DIR_ENTRIES]]
            if len(entries) > MAX_DIR_ENTRIES:
                lines.append(f"……还有 {len(entries) - MAX_DIR_ENTRIES} 项没有列出")
            return f"目录 {path} 的内容（不含隐藏项）：\n" + "\n".join(lines or ["（空目录）"])

        lines = self._read(path).split("\n")
        start, end = 1, len(lines)
        if view_range is not None:
            if (
                not isinstance(view_range, list)
                or len(view_range) != 2
                or not all(isinstance(n, int) and not isinstance(n, bool) for n in view_range)
            ):
                raise ToolError("view_range 应该是两个整数：[起始行, 结束行]，结束行填 -1 表示到文件末尾。")
            start = max(1, view_range[0])
            end = len(lines) if view_range[1] == -1 else min(view_range[1], len(lines))
        numbered = "\n".join(f"{n:6}\t{lines[n - 1]}" for n in range(start, end + 1))
        if len(numbered) > MAX_VIEW_CHARS:
            numbered = numbered[:MAX_VIEW_CHARS] + (
                f"\n……内容太长，后面的部分没有显示。文件共 {len(lines)} 行，请用 view_range 分段查看。"
            )
        return numbered

    def _create(self, path: Path, text: str) -> str:
        if path.is_dir():
            raise ToolError(f"{path} 是一个目录，不能当作文件写入。")
        existed = path.exists()
        if existed:
            self._backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return f"已覆盖 {path}（旧版本已备份）。" if existed else f"已创建 {path}。"

    def _str_replace(self, path: Path, old: str, new: str) -> str:
        content = self._read(path)
        count = content.count(old) if old else 0
        if count == 0:
            raise ToolError("没有在文件里找到 old_str。它必须和文件内容逐字一致，包括空格和换行。")
        if count > 1:
            raise ToolError(f"old_str 在文件里出现了 {count} 次，无法确定替换哪一处。请多带一些上下文，让它只出现一次。")
        path.write_text(content.replace(old, new, 1), encoding="utf-8")
        return f"已修改 {path}。"

    def _insert(self, path: Path, line: object, text: str) -> str:
        lines = self._read(path).split("\n")
        if not isinstance(line, int) or isinstance(line, bool) or not 0 <= line <= len(lines):
            raise ToolError(f"insert_line 应该是 0 到 {len(lines)} 之间的整数（0 表示插在文件开头）。")
        lines[line:line] = text.split("\n")
        path.write_text("\n".join(lines), encoding="utf-8")
        return f"已在 {path} 的第 {line} 行之后插入内容。"

    # ---- 辅助 -------------------------------------------------------------

    def _read(self, path: Path) -> str:
        if not path.exists():
            raise ToolError(f"{path} 不存在。")
        if path.is_dir():
            raise ToolError(f"{path} 是一个目录，不是文件。")
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ToolError(f"{path} 不是文本文件，无法读取。") from None

    def _backup(self, path: Path) -> None:
        """整个覆盖一个文件之前，先把旧版本存一份到 data/backups/。"""
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        shutil.copy2(path, self.backup_dir / f"{stamp}-{path.name}")


def _text(tool_input: dict, key: str) -> str:
    value = tool_input.get(key)
    if not isinstance(value, str):
        raise ToolError(f"缺少 {key} 参数，或者它不是字符串。")
    return value


# 其他厂商的模型没有学过 Anthropic 的这个工具，所以要把同样的四种命令用通用的函数格式写出来。
# 执行的代码是同一份（FileEditor.run），只是说明书不同。
PORTABLE = {
    "name": TOOL_NAME,
    "description": (
        "查看、创建和修改文本文件。command 决定做什么："
        "view 查看文件内容（带行号）或列出目录；create 创建文件，或整个覆盖已有文件；"
        "str_replace 把文件里的一段文字替换成另一段（old_str 必须在文件中恰好出现一次）；"
        "insert 在指定行之后插入文字。相对路径以工作目录为起点。"
        "创建和修改文件前会请用户确认。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "enum": ["view", "create", "str_replace", "insert"]},
            "path": {"type": "string", "description": "文件或目录的路径"},
            "view_range": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "view 时可选：[起始行, 结束行]，结束行填 -1 表示到文件末尾",
            },
            "file_text": {"type": "string", "description": "create 时必填：文件的完整内容"},
            "old_str": {"type": "string", "description": "str_replace 时必填：要被替换的原文，逐字一致"},
            "new_str": {"type": "string", "description": "str_replace 时必填：替换成的新内容"},
            "insert_line": {"type": "integer", "description": "insert 时必填：插在第几行之后，0 表示文件开头"},
            "insert_text": {"type": "string", "description": "insert 时必填：要插入的文字"},
        },
        "required": ["command", "path"],
    },
}


def make_file_tool(editor: FileEditor) -> Tool:
    # 给 Claude 的说明书只有这一行：类型和名字。参数格式由 Anthropic 定义，不用（也不能）自己写。
    spec = {"type": "text_editor_20250728", "name": TOOL_NAME}
    return Tool(spec=spec, run=editor.run, portable=PORTABLE)
