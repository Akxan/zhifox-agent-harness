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

## Windows

Windows 上没有"第 3、4 号描述符"可以交给子进程，Chrome 为此另有一个参数：--remote-debugging-io-pipes=读,写，
两个数是管道的句柄（Windows 的 HANDLE，十进制写），Chrome 用 _open_osfhandle 把它们接过去（见 Chromium 的
content/browser/devtools/devtools_agent_host_impl.cc）。所以 Windows 上的做法是：
  1. os.pipe() 开两根管道（Python 开的默认不让子进程继承）；
  2. 只把给 Chrome 的那两头设成可以继承，写进参数；
  3. 用 subprocess 启动，STARTUPINFO 的 lpAttributeList 里 handle_list 只列这两个句柄（加上标准输入输出）：
     Windows 只让子进程继承列表里的这几个，我们开着的别的文件、套接字一个都不会漏给 Chrome。
这样 Windows 上也只走管道、不开端口：这台电脑上别的程序（包括你自己账号下的别的程序）没有办法连上它、替它点网页。
不用 --remote-debugging-port 的理由和 macOS、Linux 上一样（见 cdp.py 开头）：端口谁都连得上。

Windows 也没有进程组和信号：关的时候先礼貌地请它退出（Browser.close），不走就用 taskkill /T 结束它和它的子进程。
"""

from __future__ import annotations

import contextlib
import os
import platform
import shutil
import signal
import subprocess
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
# Windows 上常见的几个位置：哪个文件夹（环境变量的名字）下面的哪几层。Chrome（装给所有人的、只装给自己的）排前面；
# Edge 是每台 Windows 10、11 都有的，同一个内核，所以没装 Chrome 也能用
WINDOWS_APPS = [
    ("ProgramFiles", ("Google", "Chrome", "Application", "chrome.exe")),
    ("ProgramFiles(x86)", ("Google", "Chrome", "Application", "chrome.exe")),
    ("LOCALAPPDATA", ("Google", "Chrome", "Application", "chrome.exe")),
    ("LOCALAPPDATA", ("Chromium", "Application", "chrome.exe")),
    ("ProgramFiles", ("Google", "Chrome Beta", "Application", "chrome.exe")),
    ("ProgramFiles", ("Google", "Chrome Dev", "Application", "chrome.exe")),
    ("LOCALAPPDATA", ("Google", "Chrome SxS", "Application", "chrome.exe")),
    ("ProgramFiles(x86)", ("Microsoft", "Edge", "Application", "msedge.exe")),
    ("ProgramFiles", ("Microsoft", "Edge", "Application", "msedge.exe")),
    ("ProgramFiles", ("BraveSoftware", "Brave-Browser", "Application", "brave.exe")),
    ("LOCALAPPDATA", ("BraveSoftware", "Brave-Browser", "Application", "brave.exe")),
]
WINDOWS = os.name == "nt"

# 环境变量：在 settings 里没填路径时，可以用它指定（服务器上常用）
PATH_ENV = "HARNESS_CHROME"


def find_chrome(override: str = "") -> Path | None:
    """这台电脑上的 Chrome 在哪儿。override 是用户在设置里填的路径（优先），其次是环境变量 HARNESS_CHROME。

    填了却找不到的，返回 None，不悄悄换成别的：用户既然指定了，就该知道它不对。
    """
    chosen = override.strip() or os.environ.get(PATH_ENV, "").strip()
    if chosen:
        path = Path(chosen).expanduser()
        return path if can_run(path) else None
    candidates: list[Path] = []
    if WINDOWS:
        candidates += [Path(os.environ[folder]).joinpath(*rest) for folder, rest in WINDOWS_APPS if os.environ.get(folder)]
    elif platform.system() == "Darwin":
        for base in (Path("/Applications"), Path.home() / "Applications"):
            candidates += [base / app for app in MAC_APPS]
    else:
        candidates += [Path(found) for name in LINUX_COMMANDS if (found := shutil.which(name))]
        candidates += [Path(path) for path in LINUX_PATHS]
    return next((path for path in candidates if can_run(path)), None)


def can_run(path: Path) -> bool:
    """这个文件能不能当成程序运行。macOS、Linux 上看"可执行"的权限位；Windows 上没有这一位
    （os.access 对任何文件都说能），看的是扩展名在不在 PATHEXT 里（.exe、.cmd……）。"""
    if not path.is_file():
        return False
    if WINDOWS:
        known = os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
        return path.suffix.lower() in {suffix.strip().lower() for suffix in known.split(";") if suffix.strip()}
    return os.access(path, os.X_OK)


def needs_headless() -> bool:
    """这台电脑能不能显示窗口。Linux 上没有图形界面（服务器、SSH 进来的）时只能无界面运行。"""
    if WINDOWS or platform.system() == "Darwin":
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
    """一个正在运行的 Chrome：进程号、和它说话的连接。process 只在 Windows 上有（subprocess 开的，靠它知道退出了没有）。"""

    def __init__(self, pid: int, connection: Connection, executable: Path, headless: bool, process: subprocess.Popen | None = None):
        self.pid = pid
        self.connection = connection
        self.executable = executable
        self.headless = headless
        self.started = time.time()
        self._process = process

    def alive(self) -> bool:
        """进程还在不在。顺便把已经退出的收掉（不然它会变成僵尸进程）。"""
        if self._process is not None:
            return self._process.poll() is None
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
            self._signal(force=False)
            self._wait(wait / 2)
        self.discard()

    def discard(self) -> None:
        """结束整个进程组、收掉主进程（不留僵尸进程），关掉我们这一头的管道。已经退出了也可以调用。"""
        self._signal(force=True)
        self._wait(1.0)
        self.connection.close()

    def kill(self) -> None:
        """立刻结束整个进程组，不等（程序退出时的最后一道保险：atexit 里不能慢）。"""
        self._signal(force=True)
        if self._process is not None:
            self._process.poll()
            return
        with contextlib.suppress(ChildProcessError, OSError):
            os.waitpid(self.pid, os.WNOHANG)

    def _signal(self, force: bool) -> None:
        """请整个进程组退出（SIGTERM），或者立刻结束它（force：SIGKILL）。"""
        if self._process is not None:
            # Windows：没有信号。"请退出"是不带 /F 的 taskkill（给它的窗口发"关闭"），"立刻结束"是带 /F 的；/T 连同子进程。
            # 已经退出了的不再去结束：Popen 一直拿着它的句柄，进程号不会被别的程序用上，但也没有必要再打扰谁。
            # 它的子进程（渲染进程这些）在主进程没了以后会自己退出
            if self.alive():
                _taskkill(self.pid, force=force)
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            # 进程组的编号就是主进程的编号（启动时 setpgroup=0）。SIGKILL 只有 macOS、Linux 有，所以在这里才取
            os.killpg(self.pid, signal.SIGKILL if force else signal.SIGTERM)

    def _wait(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not self.alive():
                return True
            time.sleep(0.05)
        return not self.alive()


def _taskkill(pid: int, force: bool) -> None:
    from ..tools.shell import system32

    subprocess.run(
        [system32("taskkill.exe"), "/T", *(["/F"] if force else []), "/PID", str(pid)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _high(fd: int) -> int:
    """把描述符挪到 10 号以上，并且不让子进程继承。

    后面要在子进程里把它放到 3、4 号上（dup2）。要是它本来就是 3 号，dup2(3, 3) 什么都不做，
    "不继承"的标记还在，Chrome 就拿不到它了。挪开了就没有这个问题。
    """
    import fcntl  # 只有 macOS、Linux 有；Windows 走 _launch_windows

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
    if WINDOWS:
        return _launch_windows(executable, profile, headless, on_close, extra_args)
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


def _launch_windows(
    executable: Path, profile: Path, headless: bool, on_close: Callable[[], None] | None, extra_args: list[str] | None,
) -> Chrome:
    """Windows 上的启动：管道的两个句柄写进 --remote-debugging-io-pipes，只让 Chrome 继承这两个（见文件开头）。"""
    import msvcrt

    from ..tools.shell import SECRET_ENV_VARS

    to_chrome_read, to_chrome_write = os.pipe()
    from_chrome_read, from_chrome_write = os.pipe()
    given = [msvcrt.get_osfhandle(to_chrome_read), msvcrt.get_osfhandle(from_chrome_write)]  # Chrome 读的、Chrome 写的
    for handle in given:
        os.set_handle_inheritable(handle, True)
    args = arguments(executable, profile, headless)
    args.insert(2, f"--remote-debugging-io-pipes={given[0]},{given[1]}")  # 紧跟在 --remote-debugging-pipe 后面
    args += extra_args or []
    startup = subprocess.STARTUPINFO()
    startup.lpAttributeList = {"handle_list": given}  # 只继承这两个（subprocess 会再加上下面的三个标准输入输出）
    env = {key: value for key, value in os.environ.items() if key not in SECRET_ENV_VARS}
    try:
        process = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,  # Chrome 往终端里写的日志很多，扔掉
            env=env, startupinfo=startup, close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except BaseException:
        os.close(to_chrome_write)
        os.close(from_chrome_read)
        raise
    finally:
        # 给 Chrome 的那两头，我们自己不用：不关掉的话，Chrome 退出时我们读不到结尾
        os.close(to_chrome_read)
        os.close(from_chrome_write)
    connection = Connection(from_chrome_read, to_chrome_write, on_close=on_close)
    return Chrome(process.pid, connection, executable, headless, process=process)
