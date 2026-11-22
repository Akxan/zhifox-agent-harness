"""Windows 上后台服务的"看护"：任务计划程序开的是它，它再开真正的智狐（harness serve），
智狐出错退出了隔一会儿再开，有人请它停下时让智狐好好地收尾退出。

macOS 的 launchd、Linux 的 systemd 自己就会做这几件事，任务计划程序不会，所以 Windows 上多了这一层：

  1. 出错退出了再开。任务计划程序有一个"如果任务失败，按以下频率重新启动"（任务里的 RestartOnFailure），
     但它说的"失败"是"任务没能开起来"（比如找不到程序）；程序开起来以后再以退出码 1 退出，它不管——
     任务计划程序根本不看退出码。所以这里自己等智狐退出：退出码不是 0 就隔 SERVICE_RESTART_SECONDS 秒再开
     （和 launchd 的 KeepAlive、systemd 的 Restart=on-failure 一样）；是 2（设置有问题，比如 channels.json 坏了）
     就不再开，免得对着坏了的设置一直重启（和 systemd 的 RestartPreventExitStatus=2 一样）；是 0（好好地退出了）
     也不再开。RestartOnFailure 仍然留在任务里，管的是这个看护自己没能开起来的情况。

  2. 好好地停下。Windows 没有"服务管家发一个 SIGTERM、程序收尾退出"这回事：schtasks /End 是直接结束进程
     （TerminateProcess），正在进行的一轮来不及存盘。所以 harness service stop 在数据目录里放下一个
     .service-stop 文件，这里看到了，就关上智狐的标准输入；智狐那边有一个线程一直等着它（serve.watch_input），
     等到了就像收到 SIGTERM 一样收尾、退出。到 SERVICE_STOP_SECONDS 秒还没退出，才直接结束它。
     为什么用文件、而不用 Windows 的"命名事件"：文件在 macOS、Linux 上一样能测，看一眼就知道有没有人请它停下；
     晚半秒看到也不要紧。为什么用标准输入、而不让智狐自己也去看那个文件：这个看护要是被结束了
     （schtasks /End、在"任务计划程序"里点了"结束"、退出登录），管道的另一头跟着断开，智狐照样知道该收尾了，
     不会剩下一个没人管的智狐占着数据目录。

  3. 不弹窗口。任务计划程序开的是 pythonw.exe（没有控制台）。这里开智狐用的是 python.exe 加上 CREATE_NO_WINDOW：
     智狐有一个看不见的控制台，它再执行的命令（cmd、git、npx……）都用这个看不见的。要是智狐也用 pythonw，
     它每执行一条命令，就会弹出一个黑窗口一闪而过。

  4. 日志。pythonw 没有标准输出，任务计划程序也不像 launchd 那样把输出接到文件上。这里把智狐的标准输出和
     标准错误接到 data\\logs\\service.log（智狐启动时就出了错，Traceback 也进得去），之后智狐自己管这个文件、
     写满了换新的（log.ServiceLog）。这里自己的几行（开、退出、重启、停下）也写进同一个文件，格式一样，
     harness service status 才认得出其中的警告和错误。每写一行就关上：Windows 上一个文件开着就改不了名，
     这里要是一直开着它，智狐的日志就永远换不了新的。

命令行（windows.py 写进任务里，不用自己敲）：

    pythonw.exe -P -m harness.service.supervise --set HARNESS_DATA_DIR=… --set HARNESS_ENV_FILE=… --set PYTHONUTF8=1
                -- python.exe -P -m harness --workspace … serve --web --service taskscheduler

任务计划程序的任务里设不了环境变量，所以数据目录、.env 的位置由这里设好了再交给智狐。--set 只认 plan.WRITTEN
里的那几个名字，都是路径和开关，没有密钥（密钥留在 .env 里，智狐启动时自己读）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import traceback
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import IO, Protocol

from .. import i18n
from ..config import (
    APP_NAME,
    EXIT_CONFIG,
    EXIT_OK,
    SERVICE_LOG,
    SERVICE_POLL_SECONDS,
    SERVICE_RESTART_SECONDS,
    SERVICE_STOP_REQUEST,
    SERVICE_STOP_SECONDS,
    SETTINGS_FILE,
)
from ..i18n import t
from .plan import WRITTEN

EXIT_CRASHED = 1  # 这个看护自己出了错（见 main）。智狐的退出码 1 不会传出去：那种情况下这里会再开它
# 开智狐的时候不给它控制台窗口，但给它一个看不见的控制台（见开头的第 3 条）。只有 Windows 上有这个标志
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Child(Protocol):
    """开起来的智狐（subprocess.Popen 有的这几样）。测试换成假的。"""

    stdin: IO[bytes] | None

    def poll(self) -> int | None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


Spawn = Callable[[Sequence[str], dict[str, str], Path], Child]


def spawn(command: Sequence[str], env: dict[str, str], log_file: Path) -> Child:
    """开智狐：标准输入是一根只有这里能写的管道（关上它就是请智狐停下），标准输出、标准错误接到日志文件上。

    日志文件在这里只开一下：子进程拿到了自己的那一份，这里的马上关掉（见开头的第 4 条）。"""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "ab") as output:
        return subprocess.Popen(
            list(command), env=env, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
        )


class Supervisor:
    """开着智狐、出错退出了再开、有人请它停下时让它收尾退出（见开头的说明）。等待和计时都可以换成假的（测试用）。"""

    def __init__(
        self, command: Sequence[str], env: dict[str, str], data_dir: Path, spawn: Spawn = spawn,
        sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
        restart_seconds: float = SERVICE_RESTART_SECONDS, stop_seconds: float = SERVICE_STOP_SECONDS,
        poll_seconds: float = SERVICE_POLL_SECONDS,
    ):
        self.command = list(command)
        self.env = env
        self.log_file = data_dir / SERVICE_LOG
        self.request = data_dir / SERVICE_STOP_REQUEST
        self.spawn = spawn
        self.sleep = sleep
        self.clock = clock
        self.restart_seconds = restart_seconds
        self.stop_seconds = stop_seconds
        self.poll_seconds = poll_seconds

    def run(self) -> int:
        """一直开着智狐，直到有人请它停下（返回 0），或者它的设置有问题（返回 2）。"""
        # 上一次留下的停下请求（harness service stop 停到一半被打断了）：这一次是刚开起来的，不算数，不然一开就停
        self.request.unlink(missing_ok=True)
        self.note("info", t("{name}的看护开起来了（进程 {pid}）。", name=APP_NAME, pid=os.getpid()))
        while True:
            try:
                child = self.spawn(self.command, self.env, self.log_file)
            except OSError as exc:  # 找不到 python.exe、日志文件夹建不了……：和出错退出一样，隔一会儿再试
                self.note("error", t("开不起{name}：{error}。{seconds} 秒后再试。", name=APP_NAME, error=exc, seconds=self.restart_seconds))
            else:
                code = self.watch(child)
                if code is None:
                    self.note("info", t("已经停下了。"))
                    return EXIT_OK
                if code == EXIT_OK:
                    self.note("info", t("{name}自己好好地退出了，不再开它。", name=APP_NAME))
                    return EXIT_OK
                if code == EXIT_CONFIG:
                    self.note("error", t("{name}的设置有问题（退出码 2），重启也没用，不再开它。看上面的错误，修好以后运行 uv run harness service restart。", name=APP_NAME))
                    return EXIT_CONFIG
                self.note("warning", t("{name}退出了（退出码 {code}），{seconds} 秒后重新开起来。", name=APP_NAME, code=code, seconds=self.restart_seconds))
            if self.pause():
                self.note("info", t("已经停下了。"))
                return EXIT_OK

    def watch(self, child: Child) -> int | None:
        """等着智狐退出，返回它的退出码；有人请它停下时，让它收尾退出，返回 None。"""
        while True:
            code = child.poll()
            if code is not None:
                return code
            if self.requested():
                self.stop(child)
                return None
            self.sleep(self.poll_seconds)

    def stop(self, child: Child) -> None:
        """关上智狐的标准输入（它就开始收尾），等它退出；SERVICE_STOP_SECONDS 秒还没退出，直接结束它。"""
        self.note("info", t("收到停下的请求（harness service stop）：请{name}收尾……", name=APP_NAME))
        try:
            if child.stdin is not None:
                child.stdin.close()
        except OSError:
            pass  # 管道已经断了：它多半正在退出
        deadline = self.clock() + self.stop_seconds
        while child.poll() is None:
            if self.clock() >= deadline:
                self.note("warning", t("{name}过了 {seconds} 秒还没退出，直接结束它。", name=APP_NAME, seconds=self.stop_seconds))
                child.kill()
                try:
                    child.wait(timeout=self.stop_seconds)
                except subprocess.TimeoutExpired:
                    pass
                return
            self.sleep(self.poll_seconds)

    def pause(self) -> bool:
        """出错退出以后，隔 restart_seconds 秒再开。这期间有人请它停下的话返回 True（不再开了）。"""
        deadline = self.clock() + self.restart_seconds
        while self.clock() < deadline:
            if self.requested():
                return True
            self.sleep(self.poll_seconds)
        return self.requested()

    def requested(self) -> bool:
        return self.request.exists()

    def note(self, level: str, text: str) -> None:
        """往日志里写一行，格式和智狐自己写的一样（log.py 的 _Formatter：日期、时间、级别、话）。写完就关上（见开头的第 4 条）。"""
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {level} {text}\n"
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_file, "a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            pass  # 写不进日志不影响看着智狐


def parse(arguments: Sequence[str]) -> tuple[dict[str, str], list[str]] | None:
    """读命令行：--set 名字=值（只认 plan.WRITTEN 里的名字，而且要有 HARNESS_DATA_DIR），然后是 --，后面是智狐的命令。
    看不懂的返回 None。windows.py 读回任务的设置时也用它。"""
    arguments = list(arguments)
    if "--" not in arguments:
        return None
    split = arguments.index("--")
    options, command = arguments[:split], arguments[split + 1 :]
    if len(options) % 2:
        return None
    values: dict[str, str] = {}
    for flag, item in zip(options[::2], options[1::2], strict=True):
        name, equals, value = item.partition("=")
        if flag != "--set" or not equals or name not in WRITTEN:
            return None
        values[name] = value
    if not command or not values.get("HARNESS_DATA_DIR"):
        return None
    return values, command


def main(arguments: Sequence[str] | None = None) -> int:
    parsed = parse(sys.argv[1:] if arguments is None else arguments)
    if parsed is None:
        # pythonw 下 sys.stderr 是 None，print 什么都不做；这条命令本来就只由 harness service install 写进任务里
        print(t("用法：python -m harness.service.supervise --set HARNESS_DATA_DIR=… [--set 名字=值 …] -- 要运行的命令"), file=sys.stderr)
        return EXIT_CONFIG
    values, command = parsed
    data_dir = Path(values["HARNESS_DATA_DIR"])
    _choose_language(data_dir)
    supervisor = Supervisor(command, {**os.environ, **values}, data_dir)
    try:
        return supervisor.run()
    except Exception:  # noqa: BLE001 —— 这里自己出了错：写进日志（pythonw 没有别的地方可写），再退出
        supervisor.note("error", t("{name}的看护出错了：", name=APP_NAME) + traceback.format_exc().rstrip().replace("\n", " | "))
        return EXIT_CRASHED


def _choose_language(data_dir: Path) -> None:
    """日志里的话用你在设置里选的界面语言（和智狐自己写的一样）。"""
    from ..settings import Settings  # 用到时才导入

    i18n.choose(Settings(data_dir / SETTINGS_FILE, "", "").language)


if __name__ == "__main__":
    sys.exit(main())
