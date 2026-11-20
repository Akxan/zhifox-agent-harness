"""长期记忆 —— 同样是第二类工具：Anthropic 定义格式，我们负责执行。

模型本身没有记忆，每次对话都从零开始。记忆工具给它一个目录（它看到的是 /memories），
它可以在里面创建、查看、修改文件；下次启动时文件还在，它读一遍就"想起来"了。

本地怎么存由我们决定。这里直接用 SDK 自带的实现：存成 data/memories/ 下的普通文件，
你可以随时打开查看或手动修改。
"""

from __future__ import annotations

import threading
from pathlib import Path

from anthropic.tools import BetaLocalFilesystemMemoryTool
from anthropic.tools import ToolError as SdkToolError

from .base import Tool, ToolError

# 给其他厂商模型用的说明书。Claude 学过这个工具，知道每次对话开始时要先看一眼记忆目录；
# 别家的模型没学过，所以这条规矩要在说明里写出来。
PORTABLE = {
    "name": "memory",
    "description": (
        "长期记忆。/memories 目录里的文件在程序重启后仍然存在，是跨会话记住事情的唯一方式。"
        "每段对话开始时，先用 view 查看 /memories 目录，了解已经记住了什么。"
        "command 决定做什么：view 查看目录或文件；create 新建文件；str_replace 替换文件里的一段文字；"
        "insert 在指定行之后插入文字；delete 删除文件或目录；rename 重命名或移动。"
        "所有路径都必须以 /memories 开头。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "enum": ["view", "create", "str_replace", "insert", "delete", "rename"]},
            "path": {"type": "string", "description": "除 rename 之外都必填，例如 /memories/user.md"},
            "view_range": {"type": "array", "items": {"type": "integer"}, "description": "view 时可选：[起始行, 结束行]"},
            "file_text": {"type": "string", "description": "create 时必填：文件内容"},
            "old_str": {"type": "string", "description": "str_replace 时必填：要被替换的原文"},
            "new_str": {"type": "string", "description": "str_replace 时必填：新内容"},
            "insert_line": {"type": "integer", "description": "insert 时必填：插在第几行之后"},
            "insert_text": {"type": "string", "description": "insert 时必填：要插入的文字"},
            "old_path": {"type": "string", "description": "rename 时必填：原路径"},
            "new_path": {"type": "string", "description": "rename 时必填：新路径"},
        },
        "required": ["command"],
    },
}


# 记忆的操作一个一个来。你的对话和几个定时任务可能同时在改记忆：SDK 的实现写文件是原子的（不会写出半个文件），
# 但 str_replace、insert 是"读出来、改一处、整个写回去"，两边同时改同一个文件，后写的会把先写的那一处冲掉。
# 整个程序共用这一把锁（而不是每个工具一把）：不管谁建了几份记忆工具，改的都是同一个文件夹
_LOCK = threading.Lock()


def make_memory_tool(data_dir: Path) -> Tool:
    backend = BetaLocalFilesystemMemoryTool(base_path=str(data_dir))  # 实际位置：data_dir/memories/

    def run(tool_input: dict) -> str:
        try:
            with _LOCK:
                return str(backend.call(tool_input))
        except SdkToolError as exc:
            raise ToolError(str(exc)) from exc

    # 给 Claude 的说明书由 SDK 生成：{"type": "memory_20250818", "name": "memory"}
    return Tool(spec=dict(backend.to_dict()), run=run, portable=PORTABLE)
