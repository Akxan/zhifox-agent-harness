"""Windows：暂时没有沙箱。这里说清楚为什么，页面、命令行、拒绝执行时给模型的话都用这一句。

想过、试着评估过的几种办法（都不加新的依赖，只用 ctypes 调系统的接口）：

  作业对象（Job Object）     能限制进程数、内存、CPU 时间，能保证"结束时连孙进程一起"（winjob.py 已经在用），
                            但它管不了能读写哪些文件、能不能联网。
  受限令牌（Restricted Token）和低完整性级别（Low Integrity）
                            低完整性的进程写不了你的大部分文件，但照样能读（~/.ssh 也能读），照样能联网；
                            要做到"只能读写工作目录"，得像 Chromium 的沙箱那样配一整套令牌和桌面，远不是几十行的事。
  AppContainer              最接近我们要的：默认读不了你的文件、不能联网（不给 internetClient 就连不上，也连不上本机）。
                            可是它要"读写工作目录"，就得改工作目录的权限（ACL），给这个 AppContainer 加一条——
                            那会真的改动你磁盘上文件的权限，而且要逐个文件夹地加、用完还得改回来；
                            PowerShell 在 AppContainer 里很多东西用不了（模块、配置、.NET 的临时目录），
                            Python 的 subprocess 也没法直接在 AppContainer 里启动进程，要整个改用 ctypes 调 CreateProcess。
                            做出来也很脆，而且只能在 CI 上远远地看，没法在真的 Windows 桌面上验证。
  Windows 沙盒（Windows Sandbox）
                            是一台小虚拟机，只有专业版、企业版有，要你自己在"Windows 功能"里打开，启动要好几秒，
                            看不到工作目录，除非把它映射进去。不适合"每条命令一个沙箱"。

所以这一版在 Windows 上明说"用不了"：设置里选了"只在工作目录里"或者"工作目录 + 联网"，命令一律拒绝执行，
告诉你原因和怎么改（把沙箱改成"关"，或者在 WSL 里运行智狐：WSL 是 Linux，可以装 bubblewrap）。绝不悄悄在沙箱之外运行。
"""

from __future__ import annotations

from ..i18n import Message, k, t

LABEL = k("Windows")
NOTE = k("想在 Windows 上用沙箱，可以在 WSL（Windows 里的 Linux）里运行智狐，再装上 bubblewrap。")
REASON = k(
    "Windows 上暂时没有可靠的沙箱：系统自带的办法里，能挡住读文件和联网的（AppContainer）要改动你文件夹的权限，"
    "PowerShell 在里面也很多东西用不了；别的办法（受限令牌、低完整性级别、作业对象）挡不住读你的文件和联网。"
)
SYMPTOMS: tuple[str, ...] = ()


def problem() -> Message | None:
    return t(REASON)


def forget() -> None:
    pass
