"""哪些信号的意思是"请收好尾退出"（网页界面 server.py 和无界面模式 serve.py 都接它们）。

  SIGTERM   macOS、Linux 上普通的 kill、systemctl stop、launchctl、关机注销时发的就是它。
            Windows 上也有这个名字，但系统自己从来不发（任务管理器、taskkill /F 是直接结束进程，没有机会收尾）。
  SIGBREAK  只有 Windows 有：在终端里按 Ctrl+Break。和 Ctrl-C（SIGINT，Python 自己变成 KeyboardInterrupt）一样当作"请退出"。

Ctrl-C（SIGINT）不在这里：两处各有自己的接法（网页界面靠 KeyboardInterrupt，serve.py 自己接）。
"""

from __future__ import annotations

import signal

EXIT_SIGNALS: tuple[int, ...] = (signal.SIGTERM, *((signal.SIGBREAK,) if hasattr(signal, "SIGBREAK") else ()))
