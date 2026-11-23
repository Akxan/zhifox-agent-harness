"""命令行里的 `harness service …`：装上、卸下、开、停、看状态、看日志、打开网页。

每条命令都先问一下这台电脑的服务管家（launchd / systemd / 任务计划程序，见 launchd.py、systemd.py、windows.py）
现在是什么样子，再决定做什么，
所以重复运行是安全的：装过的再装一次，设置没变就什么都不动，变了就停下旧的、换上新的；没在运行的 stop 一下只说一声。

和数据目录的锁（instance.py）配合：同一个数据目录只能有一个智狐。装、开、重启之前先看一眼有没有别人占着
（比如你在终端里、或者 Claude Code 里开着的 harness --web）：有的话不装、不开，说清楚是谁、怎么关掉它。
不这样的话，服务一开起来就因为"已经有一个在运行"退出，launchd 隔几秒又开一次，没完没了。

"开起来了没有"不只看服务管家说的"在运行"：还要看数据目录的锁是不是在它（同一个进程编号）手里、网页的地址写出来没有。
这时 harness service open 才打得开。
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import re
import shutil
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from .. import instance
from ..config import (
    APP_NAME,
    COMMAND,
    FROM_SOURCE,
    REPAIR,
    SERVICE_RESTART_SECONDS,
    SERVICE_START_SECONDS,
    SERVICE_STOP_SECONDS,
    WEB_ADDRESS,
    Config,
)
from ..i18n import k, t
from ..webui.token import forget_token
from . import base, windows
from .base import State
from .launchd import Launchd
from .plan import Plan, make_plan, problem, protected_folders, shell_only
from .systemd import Systemd
from .windows import TaskScheduler

Manager = Launchd | Systemd | TaskScheduler
ADDRESS = re.compile(r"http://127\.0\.0\.1:(\d{1,5})/\?token=[A-Za-z0-9_-]+")  # serve.py 写的地址就是这个样子，别的一概不开

# 占着数据目录的是哪一种（instance.py 的 kind），给人看的说法
WHO = {
    "web": k("在终端里开着的网页界面（harness --web）"),
    "serve": k("在终端里或者 systemd 里开着的 harness serve"),
    "service": k("另一个后台服务"),
    "channels": k("harness channels pair（正在等配对码）"),
    "restore": k("harness restore（正在从备份恢复）"),
    "terminal": k("终端界面（harness）"),
}
# 几个常见的退出码是什么意思（1、2 见 serve.py；78 是 launchd 自己没能开起它）。
# Windows 上任务计划程序记下的是 supervise.py 的退出码和它自己的几个结果码，另有一张（windows.EXITS），见 _why
EXITS = {
    1: k("数据目录被别人占着，或者端口被占用"),
    2: k("设置有问题（比如 channels.json 坏了），重启也没用，先看日志"),
    78: k("launchd 没能开起它：工作目录或者日志文件夹不在了？再运行一次 {command} service install"),
}
TASK_SCHEDULER_TITLE = k("任务计划程序")  # 给人看的名字（--service 里写的是 taskscheduler）
MAC_FOLDERS = k(
    "{folders} 在 macOS 保护的文件夹里（文稿、桌面、下载、iCloud 云盘）。后台服务要读这些文件夹时，macOS 可能会弹窗问你"
    "（点“好”），也可能直接拦下（日志里会有 Operation not permitted）。最省事的办法是把项目和工作目录移到这些文件夹外面"
    "（比如 ~/zhifox），在那里重新运行 uv sync 和 {command} service install。"
    "不想移的话，可以在“系统设置 → 隐私与安全性 → 完整磁盘访问权限”里加上 {python}，再运行 {command} service restart；"
    "注意这等于允许用这个 Python 运行的所有程序读你全部的文件。"
)
# 装成工具时（uv tool install）程序不在这些文件夹里，要挪的只有工作目录（见 install.py）
MAC_FOLDERS_TOOL = k(
    "{folders} 在 macOS 保护的文件夹里（文稿、桌面、下载、iCloud 云盘）。后台服务要读这些文件夹时，macOS 可能会弹窗问你"
    "（点“好”），也可能直接拦下（日志里会有 Operation not permitted）。最省事的办法是换一个在这些文件夹外面的工作目录"
    "（比如 ~/zhifox），再运行 {command} service install --workspace 那个文件夹。"
    "不想换的话，可以在“系统设置 → 隐私与安全性 → 完整磁盘访问权限”里加上 {python}，再运行 {command} service restart；"
    "注意这等于允许用这个 Python 运行的所有程序读你全部的文件。"
)


def _mac_folders(folders: str, plan: Plan) -> str:
    """macOS 保护的文件夹的那段提醒。从仓库里运行和装成工具时，要挪的东西不一样。"""
    return t(MAC_FOLDERS if FROM_SOURCE else MAC_FOLDERS_TOOL, folders=folders, python=os.path.realpath(plan.python), command=COMMAND)


def _open_browser(url: str) -> bool:
    """在默认浏览器里打开。浏览器（在 Linux 上可能是这个进程的子进程）用不着各家的密钥：这条命令马上就退出，先把它们从环境变量里拿掉。"""
    import webbrowser

    from ..tools.shell import SECRET_ENV_VARS

    for name in SECRET_ENV_VARS:
        os.environ.pop(name, None)
    return webbrowser.open(url)


@dataclass
class System:
    """这台电脑：用哪种服务管家，往哪里打印、怎么等、怎么打开浏览器。正式运行时用 detect() 得到，测试换成假的。"""

    manager: Manager | None
    unsupported: str = ""  # manager 是 None 时：为什么用不了
    out: IO[str] = field(default_factory=lambda: sys.stdout)
    err: IO[str] = field(default_factory=lambda: sys.stderr)
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    opener: Callable[[str], bool] = lambda url: _open_browser(url)  # 每次现找：测试把 _open_browser 换成了会报错的假函数
    home: Path = field(default_factory=Path.home)
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    start_wait: float = SERVICE_START_SECONDS
    stop_wait: float = SERVICE_STOP_SECONDS

    def say(self, text: str = "") -> None:
        print(text, file=self.out)

    def warn(self, text: str) -> None:
        print(text, file=self.err)


def detect() -> System:
    """这台电脑用哪种服务管家。运行系统命令的函数在这时才从 base 里取（测试把它们换成了会报错的假函数）。"""
    home = Path.home()
    if sys.platform == "darwin":
        return System(Launchd(home, base.run_command))
    if sys.platform.startswith("linux"):
        systemctl = shutil.which("systemctl")
        if systemctl:
            return System(Systemd(home, base.run_command, base.run_attached, systemctl, shutil.which("journalctl") or "journalctl"))
        return System(None, t("这台机器上没有 systemd（找不到 systemctl），装不了后台服务。可以用别的进程管理工具（supervisord、tmux……）运行 {command} serve --web。", command=COMMAND))
    if sys.platform == "win32":
        return System(TaskScheduler(home, base.run_command))
    return System(None, t("这个系统还不支持后台服务（支持 macOS、Linux 和 Windows）。"))


def add_parser(commands: argparse._SubParsersAction) -> None:
    """在 harness 的命令行里加上 service 这一组命令。"""
    parser = commands.add_parser("service", help=t("后台服务：登录时自动启动、出错退出后自己重启（macOS 用 launchd，Linux 用 systemd，Windows 用任务计划程序）"))
    actions = parser.add_subparsers(dest="service_action", required=True, metavar="ACTION")
    install = actions.add_parser("install", help=t("装上并启动后台服务（装过的话，换成这次的设置）"))
    install.add_argument("--web", dest="service_web", action=argparse.BooleanOptionalAction, default=True, help=t("开着网页界面（默认开着；--no-web 只运行定时任务和聊天通道）"))
    install.add_argument("--port", dest="service_port", type=int, default=None, metavar="PORT", help=t("网页界面使用的端口（默认 8765，被占用时自动换一个）"))
    install.add_argument("--workspace", dest="service_workspace", type=Path, default=None, metavar="DIR", help=t("工作目录：助手读写文件、执行命令的地方（默认是现在所在的目录）"))
    actions.add_parser("uninstall", help=t("停下后台服务，以后也不再自动启动（数据和日志都留着）"))
    actions.add_parser("start", help=t("现在就开起来"))
    actions.add_parser("stop", help=t("停下（下次登录时还会自动启动）"))
    actions.add_parser("restart", help=t("重启（改了 .env、升级了智狐之后）"))
    actions.add_parser("status", help=t("在不在运行、网页的端口、日志在哪儿、最近的错误"))
    logs = actions.add_parser("logs", help=t("看后台服务的日志"))
    logs.add_argument("-n", "--lines", type=int, default=50, metavar="N", help=t("显示最后多少行（默认 50）"))
    logs.add_argument("-f", "--follow", action="store_true", help=t("一直跟着看新写的日志（Ctrl-C 结束）"))
    opener = actions.add_parser("open", help=t("在浏览器里打开它的网页（地址里的口令每次启动都换）"))
    opener.add_argument("--print", dest="print_only", action="store_true", help=t("只打印带口令的地址，不打开浏览器"))


def run(args: argparse.Namespace, config: Config, system: System | None = None) -> int:
    system = system or detect()
    if system.manager is None:
        system.warn(system.unsupported)
        return 2
    action = args.service_action
    if action == "install":
        return install(args, config, system)
    if action == "open":
        return open_page(config, system, print_only=args.print_only)
    if action == "logs":
        plan = system.manager.read_plan() or make_plan(config, environ=system.environ)
        return system.manager.show_logs(plan, max(0, args.lines), args.follow, system.out)
    return {"uninstall": uninstall, "start": start, "stop": stop, "restart": restart, "status": status}[action](config, system)


# ---- 小帮手 ------------------------------------------------------------------


def _headline(manager: Manager, text: str) -> str:
    """"智狐的后台服务（launchd）：正在运行……"这样的第一行。"""
    title = t(TASK_SCHEDULER_TITLE) if isinstance(manager, TaskScheduler) else manager.name
    return t("{title}：{state}", title=t("{name}的后台服务（{manager}）", name=APP_NAME, manager=title), state=text)


def _blocker(data_dir: Path, state: State) -> tuple[bool, instance.Holder | None]:
    """数据目录是不是被别人（不是这个服务自己）占着。返回（是不是，是谁：旧版本的智狐不说是谁，那时是 None）。"""
    if not instance.in_use(data_dir):
        return False, None
    found = instance.holder(data_dir)
    if found is not None and found.kind == "service" and state.running and found.pid == state.pid:
        return False, None
    return True, found


def _refuse(system: System, data_dir: Path, found: instance.Holder | None, again: str) -> int:
    system.warn(t("数据目录正被另一个{name}用着：{path}", name=APP_NAME, path=data_dir))
    if found is not None:
        system.warn("  " + t("是{who}，进程 {pid}。", who=t(WHO[found.kind]), pid=found.pid))
    if found is not None and found.kind == "terminal":
        # 终端界面里 Ctrl-C 只是打断这一轮、清掉这一行，不退出（见 cli.py）：退出它要 /exit 或者 Ctrl-D
        system.warn(t("两个一起运行会互相覆盖对方保存的会话，所以先把它关掉：在那个终端里输入 /exit（或者按 Ctrl-D），再运行一次 {command}。", command=again))
    elif isinstance(system.manager, TaskScheduler):
        # Windows 上没有 kill -INT：别的程序没法替你在那个终端里按 Ctrl-C（任务管理器里"结束任务"是直接结束，来不及存盘）
        system.warn(t("两个一起运行会互相覆盖对方保存的会话，所以先把它关掉：在开它的终端里按 Ctrl-C（在 Claude Code 里开的，结束那个会话），再运行一次 {command}。", command=again))
    else:
        system.warn(t("两个一起运行会互相覆盖对方保存的会话，所以先把它关掉：在开它的终端里按 Ctrl-C（在 Claude Code 里开的，结束那个会话，或者运行 kill -INT 加上它的进程编号，和按 Ctrl-C 一样），再运行一次 {command}。", command=again))
    return 1


def _up(plan: Plan, state: State) -> bool:
    """真的开起来了：在运行，数据目录的锁在它手里，要开网页的话网页的地址也写出来了。"""
    if not state.running:
        return False
    found = instance.holder(plan.data_dir)
    if found is None or found.kind != "service" or found.pid != state.pid or not instance.in_use(plan.data_dir):
        return False
    return not plan.web or plan.web_address_file.is_file()


def _wait_free(system: System, data_dir: Path) -> bool:
    """等旧的那个退出、放开数据目录。"""
    deadline = system.clock() + system.stop_wait
    while instance.in_use(data_dir):
        if system.clock() >= deadline:
            return False
        system.sleep(0.5)
    return True


def _why(state: State, manager: Manager) -> str:
    """上一次为什么退出，给人看的一句。"""
    if state.signal is not None:
        return t("被信号 {signal} 结束", signal=state.signal)
    if state.exit_code is None:
        return t("没有记录")
    meanings = {**EXITS, **windows.EXITS} if isinstance(manager, TaskScheduler) else EXITS
    if state.exit_code in meanings:
        return t("退出码 {code}：{meaning}", code=state.exit_code, meaning=t(meanings[state.exit_code], command=COMMAND, repair=REPAIR))
    return t("退出码 {code}", code=state.exit_code)


def _report_started(plan: Plan, system: System, previous: int | None = None) -> int:
    """等它开起来（最多 start_wait 秒），说一声结果。没开起来的话把日志里最近的错误给你看。"""
    manager = system.manager
    assert manager is not None
    deadline = system.clock() + system.start_wait
    while True:
        state = manager.state()
        if _up(plan, state) and state.pid != previous:
            if plan.web:
                system.say(t("已经开起来了（进程 {pid}）。打开网页：{command} service open", command=COMMAND, pid=state.pid))
            else:
                system.say(t("已经开起来了（进程 {pid}）。没有开网页界面（装的时候用了 --no-web）。", pid=state.pid))
            return 0
        if system.clock() >= deadline:
            break
        system.sleep(0.5)
    system.warn(t("等了 {seconds} 秒还没看到它开起来（{state}）。", seconds=round(system.start_wait), state=_describe(state, manager)))
    _print_problems(plan, state, system, to_err=True)
    system.warn(t("过一会儿用 {command} service status 再看看；日志在：{place}", command=COMMAND, place=manager.log_place(plan)))
    return 1


def _print_problems(plan: Plan, state: State, system: System, to_err: bool = False) -> None:
    manager = system.manager
    assert manager is not None
    write = system.warn if to_err else system.say
    found = manager.problems(plan, 5)
    if found:
        write(t("最近的警告和错误："))
        for line in found:
            write("  " + line)
    if isinstance(manager, Launchd):
        folders = protected_folders(plan, system.home)
        denied = any("Operation not permitted" in line for line in found)
        if denied or (folders and not state.running):
            write(_mac_folders(t("、").join(str(folder) for folder in folders) or str(plan.workspace), plan))


def _describe(state: State, manager: Manager) -> str:
    """服务现在的样子，一句话。"""
    if state.running and state.pid is None:
        # 只有 Windows 上会这样：任务在运行（supervise.py 开着），智狐还没占住数据目录——刚开起来，或者出错退出了、正等着被重新开
        return t("正在运行，{name}还没开好（刚开起来，或者出错退出了、过几秒会重新开；最近的错误见下面）", name=APP_NAME)
    if state.running:
        return t("正在运行（进程 {pid}）", pid=state.pid)
    if state.restarting:
        return t("出错退出了，等着被重新开起来（上次：{why}）", why=_why(state, manager))
    if not state.installed:
        return t("还没有装上")
    if isinstance(manager, Launchd):
        if not state.loaded:
            return t("已停下。下次登录时会自动启动")
        if state.exit_code not in (None, 0) or state.signal is not None:
            return t("没在运行（上次：{why}）。launchd 每隔 {seconds} 秒会再试一次", why=_why(state, manager), seconds=SERVICE_RESTART_SECONDS)
        return t("已停下（上次是正常退出的）")
    if isinstance(manager, TaskScheduler) and not state.loaded:
        return t("任务计划程序里没有这个任务了（被删掉了？）。再运行一次 {command} service install 装回来", command=COMMAND)
    if state.exit_code not in (None, 0) or state.signal is not None:
        return t("出错停下了（{why}）", why=_why(state, manager))
    return t("已停下。下次登录时会自动启动") if state.enabled else t("已停下，也不会自动启动")


def _summary(plan: Plan, manager: Manager, system: System) -> None:
    """装好、换好以后，说清楚装在哪儿、数据在哪儿、怎么管它。"""
    system.say("  " + t("登录时自动启动，出错退出后自己重启；关掉终端、结束 Claude Code 的会话都不影响它。"))
    system.say("  " + t("工作目录：{path}", path=plan.workspace))
    system.say("  " + t("数据目录：{path}", path=plan.data_dir))
    system.say("  " + t("服务的设置：{path}", path=manager.file))
    if isinstance(manager, TaskScheduler):
        system.say("  " + _task_line(manager))
    system.say("  " + t("日志：{place}", place=manager.log_place(plan)))
    system.say("  " + t("管它：{command} service status | logs -f | stop | restart | uninstall", command=COMMAND))


def _task_line(manager: TaskScheduler) -> str:
    return t("在“任务计划程序”里叫 {task}（是隐藏的任务：在“查看”菜单里勾上“显示隐藏的任务”才看得见）", task=manager.task)


def _warnings(plan: Plan, config: Config, system: System) -> None:
    """装之前的提醒：服务读不到的设置、还没填的密钥、macOS 保护的文件夹、Linux 上退出登录就停。"""
    manager = system.manager
    names = shell_only(plan, system.environ)
    if names:
        system.warn(t("注意：{names} 只在终端里设置了，{env_file} 里没有。后台服务不会继承终端里的环境变量；需要的话，把它们写进 {env_file}。", names=", ".join(names), env_file=plan.env_file))
    from ..providers import PROVIDERS  # 用到时才导入
    from ..settings import Settings

    provider = PROVIDERS.get(Settings(config.settings_file, config.default_provider, config.default_model).provider)
    if provider is not None and not system.environ.get(provider.key_env):
        system.warn(t("注意：还没有设置 {provider} 的 API 密钥。服务照样会开起来，打开网页（{command} service open）在“设置”里填就行。", command=COMMAND, provider=provider.label))
    if isinstance(manager, Launchd):
        folders = protected_folders(plan, system.home)
        if folders:
            system.warn(t("注意：") + _mac_folders(t("、").join(str(folder) for folder in folders), plan))
    if isinstance(manager, Systemd) and not manager.lingering():
        system.warn(t("注意：退出登录以后，systemd 会把你这个账号的服务一起停掉。要让它没人登录时也开着（服务器、开机就运行）：sudo loginctl enable-linger {user}", user=manager.user))


# ---- 每条命令 ------------------------------------------------------------------


def install(args: argparse.Namespace, config: Config, system: System) -> int:
    manager = system.manager
    assert manager is not None
    if args.service_workspace is not None:
        config = dataclasses.replace(config, workspace=args.service_workspace.expanduser().resolve())
    plan = make_plan(config, web=args.service_web, port=args.service_port, environ=system.environ, manager=manager.name)
    reason = problem(plan, manager.name)
    if reason:
        system.warn(reason)
        return 2
    content = manager.render(plan)
    state = manager.state()
    same = manager.read() == content
    # pid 是 None：Windows 上任务在运行、智狐还没开好（可能正因为数据目录被别人占着一直起不来），那就照常往下走
    if same and state.running and state.pid is not None:
        system.say(_headline(manager, t("已经装好了，设置没有变，正在运行（进程 {pid}）。", pid=state.pid)))
        if plan.web:
            system.say("  " + t("打开网页：{command} service open", command=COMMAND))
        return 0
    blocked, found = _blocker(plan.data_dir, state)
    if blocked:
        return _refuse(system, plan.data_dir, found, f"{COMMAND} service install")
    if isinstance(manager, Systemd):
        reachable = manager.precheck()
        if not reachable.ok:
            system.warn(t("连不上你这个账号的 systemd（systemctl --user）：{error}", error=reachable.message()))
            system.warn(t("要在登录的会话里运行（SSH 登录也行），不要用 su 或者 sudo 切过来的账号。"))
            return 1
    _warnings(plan, config, system)
    replacing = state.installed or state.loaded
    if manager.stoppable(state):
        manager.stop()
        if not _wait_free(system, plan.data_dir):
            system.warn(t("旧的那个还没退出；新的会等它放开数据目录再开起来。"))
    manager.write(plan, content)  # type: ignore[arg-type]  # content 是这个 manager 自己 render 的（launchd、任务计划程序是 bytes，systemd 是 str），mypy 对不上 union 里谁和谁配对
    loaded = manager.load()
    if not loaded.ok:
        system.warn(t("服务的设置写好了，但没能启动它：{error}", error=loaded.message()))
        system.warn(t("文件在：{path}。修好以后再运行一次 {command} service install。", command=COMMAND, path=manager.file))
        return 1
    headline = t("设置没有变，重新开起来了。") if replacing and same else t("换成了这次的设置，重新开起来了。") if replacing else t("已经装好了。")
    system.say(_headline(manager, headline))
    _summary(plan, manager, system)
    return _report_started(plan, system)


def uninstall(config: Config, system: System) -> int:
    manager = system.manager
    assert manager is not None
    state = manager.state()
    if not state.installed and not state.loaded:
        system.say(t("没有装后台服务，不用卸载。"))
        return 0
    plan = manager.read_plan()
    # systemd 读不出状态的时候也照样 disable 一次：不然删了文件，"登录时启动"的那个链接还留着
    if state.loaded or (state.installed and isinstance(manager, Systemd)):
        removed = manager.remove()
        after = manager.state() if not removed.ok else None
        if after is not None and manager.stoppable(after):
            system.warn(t("没能停下它：{error}", error=removed.message()))
            return 1
        if after is not None and isinstance(manager, TaskScheduler) and after.loaded:
            # 任务还在任务计划程序里：要是照样删掉我们那一份 XML、说"卸载了"，下次登录它又会开起来
            system.warn(t("没能从任务计划程序里删掉 {task}：{error}", task=manager.task, error=removed.message()))
            return 1
    manager.delete()
    manager.after_delete()
    if plan is not None:
        if _wait_free(system, plan.data_dir):
            plan.web_address_file.unlink(missing_ok=True)  # 它要是没来得及删（被强行结束了），口令也不留着
            forget_token(plan.data_dir)  # 存着的口令也删掉：下次再装上换一个新的（见 webui/token.py）
        else:
            system.warn(t("它还没完全退出；过一会儿就好。"))
    system.say(t("已经卸载了{name}的后台服务：它停下了，以后登录时也不会再自动启动。", name=APP_NAME))
    if plan is not None:
        system.say("  " + t("数据和日志都还在：{path}", path=plan.data_dir))
    system.say("  " + t("在终端里用：{command} --web；想再装上：{command} service install", command=COMMAND))
    return 0


def _installed(system: System) -> tuple[Manager, State, Plan | None] | None:
    """装好的服务：（服务管家，现在的状态，装的时候的 Plan）。没装的话说一声，返回 None。
    把查过不是 None 的 manager 一起交回去，调用的地方就不用再查一遍。"""
    manager = system.manager
    assert manager is not None
    state = manager.state()
    if not state.installed:
        system.warn(t("还没有装后台服务。装上它：{command} service install", command=COMMAND))
        return None
    return manager, state, manager.read_plan()


def start(config: Config, system: System) -> int:
    found = _installed(system)
    if found is None:
        return 1
    manager, state, plan = found
    plan = plan or make_plan(config, environ=system.environ)
    if state.running:
        if state.pid is None:  # Windows：任务在运行，智狐还没开好
            system.say(_headline(manager, _describe(state, manager)))
        else:
            system.say(t("已经在运行了（进程 {pid}）。", pid=state.pid))
        return 0
    blocked, holder = _blocker(plan.data_dir, state)
    if blocked:
        return _refuse(system, plan.data_dir, holder, f"{COMMAND} service start")
    result = manager.start(state)
    if not result.ok:
        system.warn(t("没能启动：{error}", error=result.message()))
        return 1
    return _report_started(plan, system)


def stop(config: Config, system: System) -> int:
    manager = system.manager
    assert manager is not None
    state = manager.state()
    if not state.installed and not state.loaded:
        system.say(t("没有装后台服务。"))
        return 0
    if not manager.stoppable(state):
        system.say(t("本来就没有在运行。"))
        return 0
    result = manager.stop()
    if not result.ok and manager.stoppable(manager.state()):
        system.warn(t("没能停下它：{error}", error=result.message()))
        return 1
    plan = manager.read_plan()
    if plan is not None and not _wait_free(system, plan.data_dir):
        system.warn(t("它还没完全退出；过一会儿就好。"))
    system.say(t("已停下。下次登录时会自动启动；现在就开：{command} service start。不想再自动启动：{command} service uninstall。", command=COMMAND))
    return 0


def restart(config: Config, system: System) -> int:
    found = _installed(system)
    if found is None:
        return 1
    manager, state, plan = found
    plan = plan or make_plan(config, environ=system.environ)
    blocked, holder = _blocker(plan.data_dir, state)
    if blocked:
        return _refuse(system, plan.data_dir, holder, f"{COMMAND} service restart")
    result = manager.restart(state)
    if not result.ok:
        system.warn(t("没能重启：{error}", error=result.message()))
        return 1
    return _report_started(plan, system, previous=state.pid)


def status(config: Config, system: System) -> int:
    """在不在运行、网页的端口、工作目录和数据目录、日志在哪儿、最近的错误。在运行时退出码是 0，不在运行时是 3（和 systemctl status 一样）。"""
    manager = system.manager
    assert manager is not None
    state = manager.state()
    plan = manager.read_plan()
    system.say(_headline(manager, _describe(state, manager)))
    if not state.installed and not state.loaded:
        system.say("  " + t("装上它：{command} service install（登录时自动启动，出错退出后自己重启）", command=COMMAND))
        if instance.in_use(config.data_dir):
            found = instance.holder(config.data_dir)
            who = t(WHO[found.kind]) if found is not None else APP_NAME
            system.say("  " + t("现在开着的是{who}（数据目录 {path}）。", who=who, path=config.data_dir))
        return 3
    if plan is None and not state.installed:
        # launchd 里还读着它，文件却被删掉了
        system.say("  " + t("服务的设置文件不见了：{path}。再运行一次 {command} service install 装回来，或者 {command} service uninstall 把它卸干净。", command=COMMAND, path=manager.file))
        return 0 if state.running else 3
    if plan is None:
        system.say("  " + t("服务的设置看不懂（被手改过？）：{path}。再运行一次 {command} service install 就会换成新写的。", command=COMMAND, path=manager.file))
        return 0 if state.running else 3
    if not plan.web:
        system.say("  " + t("网页：没开（装的时候用了 --no-web）"))
    elif _up(plan, state):
        match = ADDRESS.fullmatch(_read_address(plan.web_address_file))
        port = match.group(1) if match else "?"
        system.say("  " + t("网页：http://127.0.0.1:{port}（打开：{command} service open）", command=COMMAND, port=port))
    elif state.running:
        system.say("  " + t("网页：正在开……"))
    system.say("  " + t("工作目录：{path}", path=plan.workspace))
    system.say("  " + t("数据目录：{path}", path=plan.data_dir))
    system.say("  " + t("服务的设置：{path}", path=manager.file))
    if isinstance(manager, TaskScheduler):
        system.say("  " + _task_line(manager))
    system.say("  " + t("日志：{place}（跟着看：{command} service logs -f）", command=COMMAND, place=manager.log_place(plan)))
    if isinstance(manager, Systemd):
        system.say("  " + (t("没人登录时也开着（已经开了 linger）。") if manager.lingering() else t("退出登录以后会跟着停下。要一直开着：sudo loginctl enable-linger {user}", user=manager.user)))
    if isinstance(manager, TaskScheduler):
        system.say("  " + t("你登录时开起来，退出登录时跟着停下（这样任务计划程序不用存你的密码，也不用管理员权限）。"))
    blocked, found = _blocker(plan.data_dir, state)
    if blocked:
        who = t("{who}，进程 {pid}", who=t(WHO[found.kind]), pid=found.pid) if found is not None else APP_NAME
        system.say("  " + t("数据目录正被别的程序占着（{who}）：它关掉之前，服务开不起来。", who=who))
    _print_problems(plan, state, system)
    return 0 if state.running else 3


def _read_address(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def open_page(config: Config, system: System, print_only: bool = False) -> int:
    """读 data/.web-address（serve --web 没有终端时写的，见 serve.py），在浏览器里打开。"""
    manager = system.manager
    plan = manager.read_plan() if manager is not None else None
    places = [plan.data_dir] if plan is not None else []
    if config.data_dir not in places:
        places.append(config.data_dir)
    running = [place for place in places if instance.serving(place)]  # 终端界面开着不算：它没有网页
    url = ""
    for place in running:
        url = _read_address(place / WEB_ADDRESS)
        if url:
            break
    if not running:
        if plan is not None:
            system.warn(t("{name}没有在运行。开起来：{command} service start", command=COMMAND, name=APP_NAME))
        else:
            system.warn(t("{name}没有在运行。装成后台服务：{command} service install；或者在终端里开：{command} --web", command=COMMAND, name=APP_NAME))
        return 1
    if not url:
        if plan is not None and not plan.web and plan.data_dir in running:
            system.warn(t("后台服务没有开网页界面（装的时候用了 --no-web）。要开：{command} service install --web", command=COMMAND))
        else:
            system.warn(t("{name}在运行，但还没找到网页的地址。刚开起来的话等几秒再试；在终端里开的 harness --web，地址印在那个终端里。", name=APP_NAME))
        return 1
    match = ADDRESS.fullmatch(url)
    if not match:
        system.warn(t("网页地址的文件里写的不像是智狐的地址，没有打开：{path}", path=running[0] / WEB_ADDRESS))
        return 1
    if print_only:
        system.say(url)
        system.warn(t("地址里带着这次运行的口令，有了它就能在这台电脑上执行命令：不要发给别人。"))
        return 0
    if system.opener(url):
        system.say(t("已经在浏览器里打开了：http://127.0.0.1:{port}（口令在地址里，这里不打印）", port=match.group(1)))
        return 0
    system.say(t("没能打开浏览器。把这个地址复制到浏览器里（里面带着口令，不要发给别人）："))
    system.say(url)
    return 0
