"""Windows：用任务计划程序（Task Scheduler）把智狐装成后台服务。

Windows 上"登录以后一直开着的程序"有两种做法。真正的"Windows 服务"（services.msc 里的那些）要管理员权限，
而且是在一个看不见的会话里、以系统账号运行的，碰不到你的文件夹和浏览器。任务计划程序的任务可以由你自己的账号
创建、以你的身份在你登录的会话里运行，和 macOS 的 LaunchAgent、Linux 的用户服务是同一个意思，所以用它。

每个任务是一份 XML 写的设置（Task Scheduler Schema）。我们自己留一份在 %LOCALAPPDATA%\\ZhiFox\\service-task.xml，
用 schtasks /Create /XML 交给任务计划程序（它另存一份在 C:\\Windows\\System32\\Tasks 里，那里我们读不了）。
这几项决定了它的样子：

    LogonTrigger + UserId      你（这个账号）登录时开起来。不写 UserId 的话，任何人登录都会去开它
    Principal                  InteractiveToken：用你登录时的身份运行，不存你的密码；
                               LeastPrivilege：普通权限，不要管理员。代价是"只在你登录时运行"，退出登录就停下
    MultipleInstancesPolicy    IgnoreNew：已经在运行时，再开一次什么都不做（不会出现两个智狐）
    DisallowStartIfOnBatteries、StopIfGoingOnBatteries   都是 false：笔记本拔了电源照样开着
                               （任务计划程序默认是"用电池时不开、换成电池就停"，那对一个助手来说太奇怪了）
    ExecutionTimeLimit PT0S    不限时（默认是 3 天后强行结束）
    Priority 5                 普通优先级（默认的 7 是"低于正常"，CPU 和磁盘都排在别人后面，网页会变慢）
    RestartOnFailure           任务没能开起来时，每隔 1 分钟（任务计划程序允许的最短间隔）再试，最多 255 次
                               （这一项最多只能写 255）。注意它只管"没开起来"，不管"开起来以后出错退出"：
                               那个由 supervise.py 管（见那里的说明）
    Hidden                     在"任务计划程序"的列表里默认不显示（查看 → 显示隐藏的任务 才看得见），免得和系统的任务混在一起。
                               它只管列表，不管窗口：不弹窗口靠的是 pythonw.exe
    Exec                       要运行的命令：pythonw.exe -P -m harness.service.supervise … -- python.exe -P -m harness … serve
                               （为什么中间隔着一个 supervise.py，见那个文件）。WorkingDirectory 是工作目录

文件写成 UTF-16（小端、开头带 BOM），开头声明 encoding="UTF-16"：这正是任务计划程序自己导出任务时用的写法。
schtasks 用标准的 XML 解析器读它，靠 BOM 和声明认出编码，所以 UTF-8 其实也行（微软文档里的例子、Git 的
git maintenance 都是不写声明的 UTF-8）；会出错的是"声明的和实际的对不上"，比如用记事本另存成 UTF-8、却还写着 UTF-16。

schtasks 的几条命令（参数用列表交给系统，不经过 shell）：

    /Create /TN 名字 /XML 文件 /F   按这份 XML 建（或者替换）任务
    /Run /TN 名字                   现在就开起来
    /End /TN 名字                   结束（直接结束进程：所以停下时先请它自己退出，见 stop）
    /Delete /TN 名字 /F             删掉任务
    /Query /TN 名字 /FO CSV /V /NH  任务的状态，一行逗号分开的值
    /Query /TN 名字 /XML            任务现在的设置（这里只看它有没有被停用）

读状态时只看数字，不看字：schtasks 的输出跟着 Windows 的语言走（"Running" "正在运行" "En ejecución"……）。
CSV 里的第 7 列是"上次运行结果"，是一个数：正在运行时是 267009（0x41301），从没运行过是 267011（0x41303），
否则是上一次的退出码。列的顺序各种语言都一样，只是标题不一样。
"""

from __future__ import annotations

import codecs
import csv
import getpass
import io
import ntpath
import os
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from .. import instance
from ..config import (
    APP_NAME,
    APP_NAME_LATIN,
    COMMAND,
    SERVICE_POLL_SECONDS,
    SERVICE_STOP_REQUEST,
    SERVICE_STOP_SECONDS,
    SERVICE_STOP_SLACK_SECONDS,
)
from ..i18n import k
from ..safe import private_dir, write_file
from . import logs, supervise
from .base import Result, Runner, State
from .plan import TASK_SCHEDULER, Plan, from_arguments, windowless
from .systemd import DOCUMENTATION

NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
SUPERVISOR = "harness.service.supervise"  # 任务直接运行的那个模块（见 supervise.py）
FILE_NAME = "service-task.xml"
RESTART_INTERVAL = "PT1M"  # RestartOnFailure 的间隔：任务计划程序允许的最短是 1 分钟
RESTART_COUNT = 255  # RestartOnFailure 的次数：这一项是 unsignedByte，最多 255
PRIORITY = 5  # 0 最高、10 最低；4 到 6 是"正常"，默认的 7 是"低于正常"
LAST_RESULT = 6  # schtasks /Query /FO CSV /V 的第 7 列："上次运行结果"
RUNNING = 0x41301  # SCHED_S_TASK_RUNNING：正在运行
NEVER_RAN = (0x41300, 0x41303)  # SCHED_S_TASK_READY、SCHED_S_TASK_HAS_NOT_RUN：还没运行过，没有退出码可说
_BAD_NAME = re.compile(r'[\\/:*?"<>|]')  # 任务的名字里不能有的字符
_ENABLED = re.compile(r"<Settings>.*?<Enabled>(true|false)</Enabled>", re.DOTALL)

# 任务计划程序常见的"上次运行结果"是什么意思（cli.py 的 EXITS 合并了这一张）。supervise.py 自己只会以 0、2 退出，
# 出了错是 1；智狐的退出码 1 不会传到这里（那种情况下 supervise.py 会再开它）
EXITS = {
    1: k("看护它的 supervise.py 自己出错退出了：看日志里最后的错误"),
    0x41306: k("任务计划程序把它结束了（0x41306：schtasks /End，或者在“任务计划程序”里点了“结束”）"),
    0xC000013A: k("Windows 结束了它（0xC000013A：退出登录，或者关机）"),
    0x80070002: k("找不到要运行的程序（0x80070002）：先运行 {repair}，再运行一次 {command} service install"),
    0x8007010B: k("工作目录不在了（0x8007010B）：再运行一次 {command} service install"),
    0x800710E0: k("Windows 拒绝开它（0x800710E0）：多半是你还没有登录"),
}


def task_command(plan: Plan) -> list[str]:
    """任务里的那一条命令，一个参数一项：pythonw 开 supervise.py，supervise.py 再用 python 开智狐（见 supervise.py）。
    环境变量（数据目录、.env 的位置……）由 supervise.py 设好再交给智狐：任务计划程序的任务里设不了环境变量。"""
    settings: list[str] = []
    for name, value in sorted(plan.environment().items()):
        settings += ["--set", f"{name}={value}"]
    return [windowless(plan.python), "-P", "-m", SUPERVISOR, *settings, "--", *plan.arguments(TASK_SCHEDULER)]


def split_command_line(text: str) -> list[str]:
    """把一行 Windows 的命令行拆回一个个参数，规矩和 Python（微软的 C 运行库）拆 sys.argv 的一样，
    正好是 subprocess.list2cmdline 的反过来：

      - 空格、制表符分开参数，双引号里的不算
      - 反斜杠只在双引号前面才特别：2n 个反斜杠加 " 是 n 个反斜杠、引号开或关；2n+1 个是 n 个反斜杠加一个真的 "
      - 引号里连着两个 "" 是一个真的 "
    """
    parts: list[str] = []
    current: list[str] = []
    quoted = started = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            end = index
            while end < len(text) and text[end] == "\\":
                end += 1
            count = end - index
            if end < len(text) and text[end] == '"':
                current.append("\\" * (count // 2))
                if count % 2:
                    current.append('"')
                    end += 1
            else:
                current.append("\\" * count)
            started, index = True, end
            continue
        if char == '"':
            if quoted and text[index + 1 : index + 2] == '"':
                current.append('"')
                index += 2
                continue
            quoted, started = not quoted, True
        elif char in " \t" and not quoted:
            if started:
                parts.append("".join(current))
                current, started = [], False
        else:
            current.append(char)
            started = True
        index += 1
    if started:
        parts.append("".join(current))
    return parts


def parse(data: bytes) -> Plan | None:
    """从任务的 XML 读回 Plan。不是我们写的样子（手改坏了）时返回 None。"""
    try:
        root = ElementTree.fromstring(data)
    except (ElementTree.ParseError, ValueError):
        return None
    action = root.find(f"{{{NAMESPACE}}}Actions/{{{NAMESPACE}}}Exec")
    if action is None:
        return None
    arguments = split_command_line(action.findtext(f"{{{NAMESPACE}}}Arguments") or "")
    if arguments[:3] != ["-P", "-m", SUPERVISOR]:
        return None
    parsed = supervise.parse(arguments[3:])
    if parsed is None:
        return None
    environment, command = parsed
    return from_arguments(command, action.findtext(f"{{{NAMESPACE}}}WorkingDirectory") or "", environment)


def last_result(text: str) -> int | None:
    """schtasks /Query /FO CSV /V /NH 的输出里，"上次运行结果"那一列的数（负数换成对应的正数，和十六进制的写法对得上）。"""
    for row in csv.reader(io.StringIO(text)):
        if len(row) > LAST_RESULT:
            value = row[LAST_RESULT].strip()
            try:
                number = int(value, 16) if value.lower().startswith("0x") else int(value)
            except ValueError:
                return None
            return number & 0xFFFFFFFF
    return None


class TaskScheduler:
    """Windows 的服务管家。runner 运行 schtasks、taskkill（测试换成假的，见 base.py）；sleep、clock 是停下时等它退出用的。"""

    name = TASK_SCHEDULER

    def __init__(
        self, home: Path, runner: Runner, user: str | None = None, domain: str | None = None,
        local_app_data: Path | None = None, system_root: str | None = None,
        sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
        stop_wait: float = SERVICE_STOP_SECONDS + SERVICE_STOP_SLACK_SECONDS,
    ):
        configured = os.environ.get("LOCALAPPDATA", "")
        base = local_app_data or (Path(configured) if configured and ntpath.isabs(configured) else home / "AppData" / "Local")
        self.file = base / APP_NAME_LATIN / FILE_NAME
        self.run = runner
        self.user = user or os.environ.get("USERNAME") or getpass.getuser()
        self.domain = os.environ.get("USERDOMAIN", "") if domain is None else domain
        # 写完整的路径：不让 PATH 决定运行哪个 schtasks（和 launchd.py 写 /bin/launchctl 一样）
        root = system_root or os.environ.get("SystemRoot") or "C:\\Windows"
        self.schtasks = ntpath.join(root, "System32", "schtasks.exe")
        self.taskkill = ntpath.join(root, "System32", "taskkill.exe")
        # 任务的名字放在任务计划程序的最上一层（普通账号在那里就能建任务），带上账号的名字：
        # 一台电脑上的任务名字是大家共用的，两个账号都装智狐时不会撞上
        self.task = "\\" + _BAD_NAME.sub("_", f"{APP_NAME_LATIN}-{self.user}")
        self.sleep = sleep
        self.clock = clock
        self.stop_wait = stop_wait

    @property
    def account(self) -> str:
        """LogonTrigger 里的"谁登录时开"：域名（或者电脑名）\\用户名。"""
        return f"{self.domain}\\{self.user}" if self.domain else self.user

    def _ctl(self, *arguments: str) -> Result:
        return self.run([self.schtasks, *arguments])

    # ---- 文件 --------------------------------------------------------------

    def render(self, plan: Plan) -> bytes:
        """这个任务的 XML（UTF-16，见开头的说明）。同样的设置写出来一字不差，重装时才能看出"没有变"。"""
        command = task_command(plan)
        lines = [
            '<?xml version="1.0" encoding="UTF-16"?>',
            f'<Task version="1.2" xmlns="{NAMESPACE}">',
            "  <RegistrationInfo>",
            f"    <Description>{escape(f'{APP_NAME_LATIN} ({APP_NAME}): {COMMAND} service status')}</Description>",
            f"    <Documentation>{escape(DOCUMENTATION)}</Documentation>",
            "  </RegistrationInfo>",
            "  <Triggers>",
            "    <LogonTrigger>",
            "      <Enabled>true</Enabled>",
            f"      <UserId>{escape(self.account)}</UserId>",
            "    </LogonTrigger>",
            "  </Triggers>",
            "  <Principals>",
            '    <Principal id="Author">',
            "      <LogonType>InteractiveToken</LogonType>",
            "      <RunLevel>LeastPrivilege</RunLevel>",
            "    </Principal>",
            "  </Principals>",
            "  <Settings>",
            "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
            "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
            "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>",
            "    <AllowHardTerminate>true</AllowHardTerminate>",
            "    <StartWhenAvailable>false</StartWhenAvailable>",
            "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>",
            "    <IdleSettings>",
            "      <StopOnIdleEnd>false</StopOnIdleEnd>",
            "      <RestartOnIdle>false</RestartOnIdle>",
            "    </IdleSettings>",
            "    <AllowStartOnDemand>true</AllowStartOnDemand>",
            "    <Enabled>true</Enabled>",
            "    <Hidden>true</Hidden>",
            "    <RunOnlyIfIdle>false</RunOnlyIfIdle>",
            "    <WakeToRun>false</WakeToRun>",
            "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>",
            f"    <Priority>{PRIORITY}</Priority>",
            "    <RestartOnFailure>",
            f"      <Interval>{RESTART_INTERVAL}</Interval>",
            f"      <Count>{RESTART_COUNT}</Count>",
            "    </RestartOnFailure>",
            "  </Settings>",
            '  <Actions Context="Author">',
            "    <Exec>",
            # 程序的路径加上双引号（路径里常有空格；Git 的 git maintenance 也这样写），参数按 Windows 的规矩拼成一行
            f'      <Command>"{escape(command[0])}"</Command>',
            f"      <Arguments>{escape(subprocess.list2cmdline(command[1:]))}</Arguments>",
            f"      <WorkingDirectory>{escape(str(plan.workspace))}</WorkingDirectory>",
            "    </Exec>",
            "  </Actions>",
            "</Task>",
        ]
        return codecs.BOM_UTF16_LE + ("\r\n".join(lines) + "\r\n").encode("utf-16-le")

    def read(self) -> bytes | None:
        try:
            return self.file.read_bytes()
        except OSError:
            return None

    def read_plan(self) -> Plan | None:
        data = self.read()
        return parse(data) if data is not None else None

    def write(self, plan: Plan, content: bytes) -> None:
        """写我们自己留的那一份 XML（里面没有密钥），先建好日志的文件夹（supervise.py 开智狐之前就要打开日志文件）。"""
        private_dir(plan.log_file.parent)
        self.file.parent.mkdir(parents=True, exist_ok=True)
        write_file(self.file, content)

    def delete(self) -> None:
        self.file.unlink(missing_ok=True)

    # ---- 状态 --------------------------------------------------------------

    def state(self) -> State:
        """任务在不在（schtasks 查得到）、在不在运行（上次运行结果是 0x41301）、上一次的退出码。

        pid 是占着数据目录的那个智狐（.lock 里写着是谁，见 instance.py）：schtasks 不告诉我们进程编号，
        而且任务计划程序开的是 supervise.py，真正干活的是它开的那一个。任务在运行、智狐还没开好
        （刚开起来，或者出错退出了、正等着 supervise.py 再开）时，pid 是 None。"""
        installed = self.file.is_file()
        queried = self._ctl("/Query", "/TN", self.task, "/FO", "CSV", "/V", "/NH")
        if not queried.ok:
            return State(installed)
        result = last_result(queried.out)
        if result == RUNNING:
            return State(installed, loaded=True, running=True, pid=self._pid(), enabled=True)
        exit_code = None if result is None or result in NEVER_RAN else result
        return State(installed, loaded=True, exit_code=exit_code, enabled=self._enabled())

    def _pid(self) -> int | None:
        plan = self.read_plan()
        if plan is None or not instance.in_use(plan.data_dir):
            return None
        found = instance.holder(plan.data_dir)
        return found.pid if found is not None and found.kind == "service" else None

    def _enabled(self) -> bool:
        """任务有没有被停用（"任务计划程序"里右键 → 禁用）。查不出来就当作没有。"""
        shown = self._ctl("/Query", "/TN", self.task, "/XML")
        found = _ENABLED.search(shown.out.replace("\x00", "")) if shown.ok else None
        return found is None or found.group(1) == "true"

    def _running(self) -> bool:
        queried = self._ctl("/Query", "/TN", self.task, "/FO", "CSV", "/V", "/NH")
        return queried.ok and last_result(queried.out) == RUNNING

    def stoppable(self, state: State) -> bool:
        return state.running

    # ---- 开、停 ------------------------------------------------------------

    def load(self) -> Result:
        """按我们写的 XML 建（或者替换）任务，并且现在就开起来（任务计划程序建好任务不会自己开）。"""
        created = self._ctl("/Create", "/TN", self.task, "/XML", str(self.file), "/F")
        return self._run_now() if created.ok else created

    def start(self, state: State) -> Result:
        return self._run_now() if state.loaded else self.load()

    def _run_now(self) -> Result:
        plan = self.read_plan()
        if plan is not None:
            (plan.data_dir / SERVICE_STOP_REQUEST).unlink(missing_ok=True)  # 上一次没收走的停下请求：不然一开就停
        return self._ctl("/Run", "/TN", self.task)

    def stop(self) -> Result:
        """好好地停下：在数据目录里放下停下的请求，supervise.py 看到了就请智狐收尾退出（见 supervise.py）。
        等 stop_wait 秒还没停下，才用 schtasks /End 结束任务；智狐还占着数据目录的话，最后用 taskkill 结束它。
        任务还在：下次登录时又会开起来。"""
        plan = self.read_plan()
        if plan is not None:
            request = plan.data_dir / SERVICE_STOP_REQUEST
            try:
                write_file(request, "stop\n")
            except OSError:
                pass  # 放不下请求（数据目录不在了？）：只好直接结束
            else:
                if self._wait(lambda: not instance.in_use(plan.data_dir) and not self._running(), self.stop_wait):
                    request.unlink(missing_ok=True)
                    return Result(0)
        ended = self._ctl("/End", "/TN", self.task)
        if plan is not None:
            # supervise.py 被结束时，智狐的标准输入跟着断开，它会自己收尾退出；等一小会儿还占着，就是卡住了
            if not self._wait(lambda: not instance.in_use(plan.data_dir), SERVICE_STOP_SLACK_SECONDS):
                found = instance.holder(plan.data_dir)
                if found is not None and found.kind == "service":
                    self.run([self.taskkill, "/F", "/T", "/PID", str(found.pid)])
            (plan.data_dir / SERVICE_STOP_REQUEST).unlink(missing_ok=True)
        return ended

    def restart(self, state: State) -> Result:
        if not state.loaded:
            return self.load()
        if state.running:
            stopped = self.stop()
            if not stopped.ok and self._running():
                return stopped
        return self._run_now()

    def remove(self) -> Result:
        """卸载之前：在运行的话先好好地停下，再从任务计划程序里删掉任务。"""
        if self._running():
            self.stop()
        return self._ctl("/Delete", "/TN", self.task, "/F")

    def after_delete(self) -> None:
        """删掉我们那一份 XML 以后不用再做什么（systemd 要 daemon-reload）。"""

    def _wait(self, done: Callable[[], bool], seconds: float) -> bool:
        deadline = self.clock() + seconds
        while not done():
            if self.clock() >= deadline:
                return False
            self.sleep(SERVICE_POLL_SECONDS)
        return True

    # ---- 日志 --------------------------------------------------------------

    def log_place(self, plan: Plan) -> str:
        return str(plan.log_file)

    def show_logs(self, plan: Plan, lines: int, follow: bool, out: IO[str]) -> int:
        return logs.show(plan.log_file, lines, follow, out, sleep=self.sleep)

    def problems(self, plan: Plan, count: int) -> list[str]:
        return logs.recent_problems(plan.log_file, count)
