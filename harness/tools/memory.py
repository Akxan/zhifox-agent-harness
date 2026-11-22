"""长期记忆 —— 同样是第二类工具：Anthropic 定义格式，我们负责执行。

模型本身没有记忆，每次对话都从零开始。记忆工具给它一个目录（它看到的是 /memories），
它可以在里面创建、查看、修改文件；下次启动时文件还在，它读一遍就"想起来"了。

本地怎么存由我们决定。这里直接用 SDK 自带的实现：存成 data/memories/ 下的普通文件，
你可以随时打开查看或手动修改。Windows 上补了两样：路径的花招交给 SDK 之前先挡下（_windows_trick），
SDK 写文件时不让 Windows 改换行（_binary_writes）。
"""

from __future__ import annotations

import importlib
import os
import threading
from pathlib import Path

from .. import pathnames
from .base import Tool, ToolError

MEMORY_ROOT = "/memories"  # 模型看到的记忆目录（SDK 规定的），路径都以它开头

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


def _windows_trick(tool_input: dict) -> str | None:
    r"""Windows 上，模型给的记忆路径用没用花招（见 pathnames.py）。SDK 会把路径展开、确认它还在 /memories 里面，
    可是展开这一步本身就会去碰那个位置：/memories/\\某台主机\共享 光是展开一下就会去连那台主机（还会把登录凭据发过去），
    CON、COM1 是设备。所以交给 SDK 之前先看一眼，用了就不认。/memories 后面的部分按相对路径看。"""
    for key in ("path", "old_path", "new_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.startswith(MEMORY_ROOT):
            reason = pathnames.trick(value[len(MEMORY_ROOT) :].lstrip("/"))
            if reason:
                return f"{reason}。为了安全，这种写法不认，请换成普通的路径（比如 /memories/notes.md）。"
    return None


# ---- Windows：SDK 写记忆文件时会改掉换行 ------------------------------------------
# 有意的破例：改动 SDK 模块里的一个名字。SDK 写记忆文件用的是 os.open + os.write，没有带 O_BINARY。
# Windows 上 os.open 默认是"文本模式"，写下去的每个 \n 都变成 \r\n；它读的时候却是按字节读的，
# 于是每改一次，换行前面就多一个 \r（\r\r\n……），记忆页面上的字节数也和内容对不上。
# SDK 我们改不了，只好把它那两个模块里的 os 换成一个替身（_BinaryOs）：open 总带着 O_BINARY，别的都还是 os。
# 边界：只在 Windows 上换（别的系统没有这个旗子，也不需要）；只换下面这两个模块；SDK 改了写法、模块里不再有 os
# 的话就什么都不做（Windows 上 tests/test_pathnames.py 的记忆测试会发现换行又不对了）
_SDK_MODULES = ("anthropic.lib.tools._beta_builtin_memory_tool", "anthropic.lib.tools._file_store")


class _BinaryOs:
    """和 os 模块一样，只是 open 总带着 O_BINARY（按字节读写，不改换行）。"""

    def __getattr__(self, name: str) -> object:
        return getattr(os, name)

    @staticmethod
    def open(path: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        return os.open(path, flags | pathnames.BINARY, mode, dir_fd=dir_fd)


def _binary_writes() -> None:
    if not pathnames.BINARY:
        return
    for name in _SDK_MODULES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        if getattr(module, "os", None) is os:
            module.os = _BinaryOs()


def make_memory_tool(data_dir: Path) -> Tool:
    # SDK 用到时才导入：anthropic 要半秒多才导入完，而 tools 这个包会被 harness service、hooks、sandbox 这些
    # 用不到模型的小命令一路带进来（它们只要 tools/shell.py 里的一张名单）。真正组装工具时才付这半秒。
    # Windows 上换 os 的那一步（_binary_writes）自己会导入那两个 SDK 模块再换；它排在建 SDK 的工具之前，
    # 所以 SDK 第一次写记忆文件之前一定已经换好了（tests/test_pathnames.py 的 test_the_sdk_os_is_swapped_before_the_memory_tool_writes）
    _binary_writes()
    from anthropic.tools import BetaLocalFilesystemMemoryTool
    from anthropic.tools import ToolError as SdkToolError

    backend = BetaLocalFilesystemMemoryTool(base_path=str(data_dir))  # 实际位置：data_dir/memories/

    def run(tool_input: dict) -> str:
        reason = _windows_trick(tool_input) if isinstance(tool_input, dict) else None
        if reason:
            raise ToolError(reason)
        try:
            with _LOCK:
                return str(backend.call(tool_input))
        except SdkToolError as exc:
            raise ToolError(str(exc)) from exc

    # 给 Claude 的说明书由 SDK 生成：{"type": "memory_20250818", "name": "memory"}
    return Tool(spec=dict(backend.to_dict()), run=run, portable=PORTABLE)
