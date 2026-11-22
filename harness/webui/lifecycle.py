"""一直在后台开着的东西：定时任务的调度线程、聊天通道。什么时候开，什么时候关。

它们只在智狐作为一个长期运行的程序开着的时候工作，有两种开法，用的是这同一处：

  harness --web     网页界面（server.py 的 _serve）
  harness serve     服务器上的服务，没有浏览器（../serve.py）；加 --web 的话顺便开着网页界面

终端界面（harness）不开它们：终端在你打字时一直等着输入，没法同时显示任务的进度、聊天里的确认请求。

关的时候顺序有讲究：先不让新的开始（通道不再收消息、调度器不再排新的任务），再让正在进行的停下，
最后等它们收尾存盘。停下只是"通知一声"，收尾是在它们自己的线程里做的；不等的话，程序一退出那些线程就没了，
助手已经做过的事（执行过的命令、改过的文件）在保存的会话里不留一点记录。模型一直没有输出时等不到它停下，
所以一共只等有限的几秒。
"""

from __future__ import annotations

import time
import weakref
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .server import WebApp


# 调用过 start、还没 stop 的 WebApp。从备份恢复以后换上一个新的 WebApp 时（见 restore_api.py），照着它决定要不要也开起来：
# 测试里的 WebApp 从来不 start（它们直接调用 tick），换上的新的也不该自己开起调度线程
_started: weakref.WeakSet[WebApp] = weakref.WeakSet()


def start(web: WebApp) -> None:
    """开始运行定时任务和聊天通道。刚启动时调度器会先看一眼：程序关着的时候错过的任务，补跑一次。"""
    _started.add(web)
    web.tasks.start()
    web.channels.start()


def started(web: WebApp) -> bool:
    return web in _started


def stop(web: WebApp, wait: float, holding_turn: bool = False) -> None:
    """退出前的收尾：网页上正在进行的一轮、正在运行的定时任务、聊天里正在进行的对话，都停下来、存好盘。

    holding_turn：调用的人自己拿着对话的锁（web._turn，从备份恢复时，见 restore_api.py），网页上的对话不会在跑，
    不用叫它停、也不用等它（等的话要等满 wait 秒：锁就在调用的人手里）。"""
    _started.discard(web)
    app = web.app
    web.tasks.close()  # 排队的不跑了，正在跑的各自停下（连同它们自己的命令）
    if not holding_turn:
        web.stop()
    app.shell.kill_running()
    app.mcp.cancel_all()  # 正在等的 MCP 调用，不管是对话的、任务的还是聊天里的，都取消
    deadline = time.monotonic() + wait
    # 聊天通道：不再收消息；聊天里正在进行的停下，等它们存好盘（最多 CHANNEL_SHUTDOWN_SECONDS 秒，和下面的一起算）
    web.channels.close()
    if not holding_turn:
        web.wait_idle(max(0.0, deadline - time.monotonic()))
    web.tasks.wait_idle(max(0.0, deadline - time.monotonic()))
    app.close()  # 关掉 MCP 服务器的进程
    web.restore.close()  # 上传了、还没确认的备份（在系统的临时文件夹里）删掉
