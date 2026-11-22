"""助手程序所在的这台电脑是哪种系统：mac、windows，还是 linux。

只是一个名字，给"要按系统说不同的话"的地方用。现在用到它的是网页：状态里的 platform（见 webui/webapp.py 的 publish_state），
页面照着它说"在访达中显示"（mac）、"在文件资源管理器中显示"（windows）、"在文件管理器中显示"（linux）。

为什么由助手程序告诉页面，而不让页面自己看浏览器：文件管理器开在助手程序所在的这台电脑上，不一定是打开页面的那一台
（你可能在 Mac 上通过 SSH 隧道打开 Linux 服务器上的智狐）。快捷键（⌘ 还是 Ctrl）正相反，看的是浏览器那台，
由页面自己判断（见 webui/page/scripts/platform.js）。

linux 这一档也包括别的类 Unix 系统（FreeBSD……）：它们和 Linux 一样，文件管理器是桌面环境自带的那个，没有统一的名字。
"""

from __future__ import annotations

import sys

MAC, WINDOWS, LINUX = "mac", "windows", "linux"


def system() -> str:
    """这台电脑的系统：MAC、WINDOWS 或 LINUX。"""
    if sys.platform == "darwin":
        return MAC
    if sys.platform == "win32":
        return WINDOWS
    return LINUX
