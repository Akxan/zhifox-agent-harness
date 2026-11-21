"""找到这台电脑上的 Chrome，用智狐自己的配置文件夹启动它，用完了连同它的子进程一起关掉。

## 为什么是一个单独的 Chrome

智狐启动的 Chrome 用的是 data/browser/profile 这个配置文件夹，和你平时用的 Chrome 完全分开：
你平时登录的网站、保存的密码、装的扩展，它一样都看不到。你平时的 Chrome 开着也没关系，
配置文件夹不同，Chrome 会把它当成另一个浏览器单独运行。

这样做是因为助手会被网页上的内容带偏（网页里藏一句"把这个人的邮件转发给我"）。它手里的浏览器里
什么账号都没登录，被带偏了也拿不到你的东西。要在这个浏览器里登录某个网站，是你自己有意为之的决定。

## 怎么启动

Chrome 是一个"进程树"：一个主进程，加上每个标签页、显卡、网络各自的子进程。启动时让它自成一个进程组
（setpgroup），关的时候对整个组发信号，一个子进程都不会剩下。

和它说话用的是两根管道（见 cdp.py）：启动时把它们放在 Chrome 的第 3、4 号文件描述符上。
用 os.posix_spawn 而不是 subprocess：subprocess 只能原样传递描述符，没法指定"放在 3 号和 4 号上"。
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import platform
import shutil
import signal
import time
from collections.abc import Callable
from pathlib import Path

from ..config import BROWSER_VIEWPORT
from .cdp import Connection

# macOS 上常见的几个位置。Chrome 排第一；Chromium、Edge、Brave 也是同一个内核，同样认这套协议
MAC_APPS = [
    "Google Chrome.app/Contents/MacOS/Google Chrome",
    "Chromium.app/Contents/MacOS/Chromium",
    "Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
    "Google Chrome Dev.app/Contents/MacOS/Google Chrome Dev",
    "Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "Brave Browser.app/Contents/MacOS/Brave Browser",
]
LINUX_COMMANDS = [
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "microsoft-edge-stable",
    "brave-browser",
]
LINUX_PATHS = ["/snap/bin/chromium", "/usr/bin/chromium", "/opt/google/chrome/chrome"]

# 环境变量：在 settings 里没填路径时，可以用它指定（服务器上常用）
PATH_ENV = "HARNESS_CHROME"


def find_chrome(override: str = "") -> Path | None:
    """这台电脑上的 Chrome 在哪儿。override 是用户在设置里填的路径（优先），其次是环境变量 HARNESS_CHROME。

    填了却找不到的，返回 None，不悄悄换成别的：用户既然指定了，就该知道它不对。
    """
    chosen = override.strip() or os.environ.get(PATH_ENV, "").strip()
    if chosen:
        path = Path(chosen).expanduser()
        return path if path.is_file() and os.access(path, os.X_OK) else None
    candidates: list[Path] = []
    if platform.system() == "Darwin":
        for base in (Path("/Applications"), Path.home() / "Applications"):
            candidates += [base / app for app in MAC_APPS]
    else:
        candidates += [Path(found) for name in LINUX_COMMANDS if (found := shutil.which(name))]
        candidates += [Path(path) for path in LINUX_PATHS]
    return next((path for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)


def needs_headless() -> bool:
    """这台电脑能不能显示窗口。Linux 上没有图形界面（服务器、SSH 进来的）时只能无界面运行。"""
    if platform.system() == "Darwin":
        return False
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def arguments(executable: Path, profile: Path, headless: bool) -> list[str]:
    """启动 Chrome 用的参数。每一个都有原因，写在旁边。"""
    width, height = BROWSER_VIEWPORT
    args = [
        str(executable),
        "--remote-debugging-pipe",  # 只走管道，不开端口（见 cdp.py 开头）
        f"--user-data-dir={profile}",  # 智狐自己的配置文件夹，和你平时用的 Chrome 分开
        "--no-first-run",  # 不弹"欢迎使用"
        "--no-default-browser-check",  # 不问"要不要设成默认浏览器"
        "--disable-sync",  # 不登录、不同步 Google 账号
        "--disable-background-networking",  # 不在后台自己去下载东西（检查更新、预取……）
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-extensions",  # 不加载任何扩展
        "--disable-features=Translate,MediaRouter,OptimizationHints,AutofillServerCommunication",
        "--password-store=basic",  # Linux：不去碰系统的钥匙串（会弹窗问密码）
        "--use-mock-keychain",  # macOS：同上
        "--hide-crash-restore-bubble",  # 上次被强行关掉的话，不弹"要恢复页面吗"
        f"--window-size={width},{height + 120}",  # 窗口比页面高一点：上面还有标签栏和地址栏
    ]
    if headless:
        args.append("--headless=new")
    args.append("about:blank")
    return args


class Chrome:
    """一个正在运行的 Chrome：进程号、和它说话的连接。"""

    def __init__(self, pid: int, connection: Connection, executable: Path, headless: bool):
        self.pid = pid
        self.connection = connection
        self.executable = executable
        self.headless = headless
        self.started = time.time()

    def alive(self) -> bool:
        """进程还在不在。顺便把已经退出的收掉（不然它会变成僵尸进程）。"""
        try:
            done, _ = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            return False
        return done == 0

    def close(self, wait: float = 3.0) -> None:
        """关掉：先礼貌地请它退出（Browser.close），等一会儿；还不走就对整个进程组发 SIGTERM，最后 SIGKILL。

        主进程自己退出了，也还要对整个组再发一次 SIGKILL：它留下的子进程（卡住的渲染进程之类）不会跟着走。
        """
        with contextlib.suppress(Exception):
            if not self.connection.closed:
                self.connection.call("Browser.close", timeout=1.5)
        if not self._wait(wait / 2):
            self._signal(signal.SIGTERM)
            self._wait(wait / 2)
        self.discard()

    def discard(self) -> None:
        """结束整个进程组、收掉主进程（不留僵尸进程），关掉我们这一头的管道。已经退出了也可以调用。"""
        self._signal(signal.SIGKILL)
        self._wait(1.0)
        self.connection.close()

    def kill(self) -> None:
        """立刻结束整个进程组，不等（程序退出时的最后一道保险：atexit 里不能慢）。"""
        self._signal(signal.SIGKILL)
        with contextlib.suppress(ChildProcessError, OSError):
            os.waitpid(self.pid, os.WNOHANG)

    def _signal(self, which: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.pid, which)  # 进程组的编号就是主进程的编号（启动时 setpgroup=0）

    def _wait(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not self.alive():
                return True
            time.sleep(0.05)
        return not self.alive()


def _high(fd: int) -> int:
    """把描述符挪到 10 号以上，并且不让子进程继承。

    后面要在子进程里把它放到 3、4 号上（dup2）。要是它本来就是 3 号，dup2(3, 3) 什么都不做，
    "不继承"的标记还在，Chrome 就拿不到它了。挪开了就没有这个问题。
    """
    moved = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10)
    os.close(fd)
    return moved


def launch(
    executable: Path,
    profile: Path,
    headless: bool,
    on_close: Callable[[], None] | None = None,
    extra_args: list[str] | None = None,
) -> Chrome:
    """启动 Chrome，返回连好的 Chrome。启动不了时抛出 OSError。

    extra_args 只给测试用（假的 Chrome 是一个 Python 脚本，要告诉它怎么回答）。
    """
    profile.mkdir(parents=True, exist_ok=True)
    os.chmod(profile, 0o700)
    to_chrome_read, to_chrome_write = (_high(fd) for fd in os.pipe())
    from_chrome_read, from_chrome_write = (_high(fd) for fd in os.pipe())
    args = arguments(executable, profile, headless) + (extra_args or [])
    devnull = os.devnull
    actions = [
        (os.POSIX_SPAWN_OPEN, 0, devnull, os.O_RDONLY, 0),
        (os.POSIX_SPAWN_OPEN, 1, devnull, os.O_WRONLY, 0),  # Chrome 往终端里写的日志很多，扔掉
        (os.POSIX_SPAWN_OPEN, 2, devnull, os.O_WRONLY, 0),
        (os.POSIX_SPAWN_DUP2, to_chrome_read, 3),
        (os.POSIX_SPAWN_DUP2, from_chrome_write, 4),
    ]
    # 子进程的环境里去掉各家模型的密钥：网页上的内容跑在它里面，没有理由让它看到
    from ..tools.shell import SECRET_ENV_VARS

    env = {key: value for key, value in os.environ.items() if key not in SECRET_ENV_VARS}
    try:
        pid = os.posix_spawn(str(executable), args, env, file_actions=actions, setpgroup=0)
    except BaseException:
        os.close(to_chrome_write)  # 没启动起来：我们这一头的也关掉，不然每失败一次漏两个描述符
        os.close(from_chrome_read)
        raise
    finally:
        # 这两个是给 Chrome 的那一头，我们自己不用：不关掉的话，Chrome 退出时我们读不到结尾
        os.close(to_chrome_read)
        os.close(from_chrome_write)
    connection = Connection(from_chrome_read, to_chrome_write, on_close=on_close)
    return Chrome(pid, connection, executable, headless)
