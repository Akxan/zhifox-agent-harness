"""在系统的文件管理器里显示一个文件或文件夹：macOS 的访达、Windows 的文件资源管理器、Linux 的 xdg-open。

整个程序里"页面点一下，电脑上就启动一个程序"的地方只有这里（reveal_in_file_manager），按系统分开写的也只在这里
（CLAUDE.md 第 9 条）。别的文件要显示什么，一律写 reveal.reveal_in_file_manager(...)、reveal.has_file_manager()，
调用的时候现找：测试把这个模块上的两个名字换掉（免得真的打开访达），所有调用的地方就都换到了。
所以别处不要 from .reveal import 它们，也不要另写一份（tests/test_webui.py 会查）。

  has_file_manager         这台电脑上能不能打开文件管理器（服务器上通常不能：回答里的 opened 为假，页面改成告诉你文件在哪儿）
  reveal_in_file_manager   显示。路径只能是程序自己算出来的，命令用参数列表、不经过 shell
  POST /api/reveal         显示助手的一个数据文件（任务、待办、提示词、聊天通道）：页面只说是哪一个（RevealActions.reveal_data）

route_reveal 由 server.py 的 Handler._route 调用（写法和约定见 server.py 开头）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ..i18n import t
from ..safe import private_dir
from ..tools.shell import SECRET_ENV_VARS
from .webapp_types import WebAppPart

if TYPE_CHECKING:
    from .server import Handler
    from .webapp import WebApp


def has_file_manager() -> bool:
    """这台电脑上能不能打开文件管理器。服务器上通常没有图形界面（没有 DISPLAY），也没有 xdg-open。
    macOS 和 Windows 总是有（访达、文件资源管理器）。"""
    if sys.platform in ("darwin", "win32"):
        return True
    return bool(shutil.which("xdg-open")) and bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def reveal_in_file_manager(path: Path) -> None:
    """在系统的文件管理器里显示一个文件或文件夹（macOS 上是访达）。失败时抛出 OSError。
    没有文件管理器的电脑（服务器）上什么都不做：回答里的 opened 是 false（各个 route_* 照 has_file_manager 填），页面改成告诉你文件在哪儿。

    这是整个服务器里唯一一处"页面点一下，电脑上就启动一个程序"的地方，所以守两条规矩：
    路径只能是程序自己算出来的（见 conversations_api.py 的 reveal_conversation、memory_api.py 的 reveal_memories 和 show_memory、
    media_api.py 的 reveal_media、mcp_api.py 的 mcp_reveal、这个文件里的 reveal_data，和别的 *_api.py 的 reveal：浏览器只能给会话编号、记忆文件夹里的一个相对路径（逐层检查过，出不了那个文件夹）、
    附件的编号（查过格式，文件名由内容的 SHA-256 定）、一个 Skill 的名字（只能是 Skill 文件夹里的一个子文件夹），
    或者什么都不给（commands_api.py 的 reveal：只显示 data/commands.json））；命令用参数列表交给系统，不经过 shell，文件名里有什么字符都不会被当成命令的一部分。
    """
    if not has_file_manager():
        return
    if sys.platform == "win32":
        _reveal_in_explorer(path)
        return
    if sys.platform == "darwin":
        command = ["open", "-R", str(path)] if path.is_file() else ["open", str(path)]  # -R：在访达里选中这个文件，而不是打开它
    else:
        # Linux 上没有通用的"选中某个文件"的办法，退一步，打开它所在的文件夹
        command = ["xdg-open", str(path if path.is_dir() else path.parent)]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # 有的系统上文件管理器会作为我们的子进程启动、一直开着。不让它带着各家的 API 密钥
        # （从它里面打开的终端都能看到环境变量），也不让它因为我们退出而被一起关掉
        env={name: value for name, value in os.environ.items() if name not in SECRET_ENV_VARS},
        start_new_session=True,
    )
    try:
        if process.wait(timeout=5) != 0:
            raise OSError(t("{command} 没有成功（退出码 {code}）", command=command[0], code=process.returncode))
    except subprocess.TimeoutExpired:
        pass  # 有的文件管理器会一直开着不退出，那也算打开了，不用等它


def windows_reveal_command(path: Path, is_file: bool) -> str:
    """Windows 上打开文件资源管理器的那一行命令：文件是 explorer /select,"路径"（打开它所在的文件夹并选中它），文件夹是直接打开。

    为什么是一整行字符串、不是参数列表：explorer 不按通常的规则解析命令行，/select, 和路径必须连在一起、路径单独加引号；
    交给 subprocess 一个列表的话，它会把 "/select,带空格的路径" 整个括进一对引号，explorer 就不认了。
    字符串在 Windows 上也不经过 shell（CreateProcess 直接运行），路径是程序自己算出来的，Windows 的文件名里也不可能有引号。
    """
    explorer = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "explorer.exe")  # 完整路径：PATH 里同名的程序冒充不了
    return f'"{explorer}" /select,"{path}"' if is_file else f'"{explorer}" "{path}"'


def _reveal_in_explorer(path: Path) -> None:
    if '"' in str(path):
        raise OSError(t("路径里有引号，没法交给文件资源管理器：{path}", path=path))
    # explorer 把事情交给已经开着的那个文件资源管理器就退出了，而且成功了也常常返回 1：不看退出码
    subprocess.Popen(  # 见 windows_reveal_command：不经过 shell，路径由程序算出来
        windows_reveal_command(path, path.is_file()),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={name: value for name, value in os.environ.items() if name not in SECRET_ENV_VARS},
    )


class RevealActions(WebAppPart):
    """显示助手的一个数据文件。"""

    def reveal_data(self, what: str) -> tuple[str, str]:
        """在文件管理器里显示一个数据文件。返回（失败原因，显示的路径）。

        和 reveal_conversation 一样：页面只能说是哪一个（下面这几个名字之一），路径是程序自己定的。
        文件还不存在（还没建过任务、提示词用的是内置的），就打开它将来所在的文件夹，并说一声
        """
        config = self.app.config
        target = {"tasks": config.tasks_file, "todos": config.todo_file, "prompt": config.prompt_file, "channels": config.channels_file}[what]
        shown = target if target.exists() else target.parent
        try:
            private_dir(config.data_dir)
            reveal_in_file_manager(shown)
        except OSError as exc:
            return t("没能打开文件管理器（{error}）。文件在：{path}", error=exc, path=target), ""
        return "", str(shown)


def route_reveal(handler: Handler, web: WebApp, body: dict) -> None:
    what = body.get("what")
    if what not in ("tasks", "todos", "prompt", "channels"):
        handler._send_json(400, {"error": t("参数不正确")})
    else:
        error, shown = web.reveal_data(what)
        if error:
            handler._send_json(409, {"error": error})
        else:
            handler._send_json(200, {"ok": True, "path": shown, "opened": has_file_manager()})
