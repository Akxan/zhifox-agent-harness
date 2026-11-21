"""Windows 上的后台服务（任务计划程序，见 harness/service/windows.py 和 supervise.py）的测试。

这台测试机多半不是 Windows，就算是，也绝不能真的建一个任务：所以 schtasks.exe、taskkill.exe 全是假的
（FakeSchtasks：记下每一条命令，按测试设好的样子回答），路径（C:\\…、%LOCALAPPDATA%、账号）都由测试给。
"服务开起来了"和 test_service.py 一样是假装的：在测试自己的进程里占住数据目录的锁（kind 写成 service）、
写好 .web-address，再让假的 schtasks 报出"正在运行"（上次运行结果 0x41301）。

只有几个测试开了真的进程，它们在 macOS、Linux、Windows 上都跑：supervise.py 看着一个小小的子进程
（出错退出、请它停下、它自己被结束了），以及 supervise.py 加上真的 harness serve --web --service taskscheduler
——和任务计划程序开的是同一条命令，只是不经过任务计划程序。
"""

from __future__ import annotations

import codecs
import csv
import dataclasses
import io
import ntpath
import os
import random
import subprocess
import sys
import threading
from pathlib import Path
from xml.etree import ElementTree

import pytest
from modes import has_mode
from test_channels import until
from test_service import (
    OTHER_PID,
    PATH,
    TOKEN,
    URL,
    Clock,
    Opener,
    args_for,
    hold,
    make_system,
    said,
)

from harness import cli, instance
from harness.config import (
    EXIT_CONFIG,
    SERVICE_LOG,
    SERVICE_RESTART_SECONDS,
    SERVICE_STOP_REQUEST,
    SERVICE_STOP_SECONDS,
    SERVICE_STOP_SLACK_SECONDS,
)
from harness.serve import SERVICES, watch_input
from harness.service import base, logs, supervise, windows
from harness.service import cli as service_cli
from harness.service.base import Result
from harness.service.plan import Plan, make_plan, problem, windowless
from harness.service.supervise import Supervisor
from harness.service.windows import (
    NAMESPACE,
    RUNNING,
    TaskScheduler,
    last_result,
    split_command_line,
    task_command,
)

ROOT = Path(__file__).resolve().parent.parent
TASK = "\\ZhiFox-tester"
TERMINATED = 0x41306
NEVER_RAN = 0x41303
NS = {"t": NAMESPACE}


# ---- 假的 schtasks ------------------------------------------------------------------


class FakeSchtasks:
    """假的 schtasks.exe 和 taskkill.exe：记下每一条命令（完整的参数列表），按现在的"样子"回答。

    registered 是任务在不在任务计划程序里；running / result 是"状态"和"上次运行结果"。
    starts 为真时，/Run 顺便"开起来"一个智狐（占住数据目录、写好网页的地址），/End、taskkill 让它退出。
    obeys 为真时，"服务"看得见 harness service stop 放下的停下请求：每次假的 sleep 之后（tick）就好好地退出。
    hangs 为真时，supervise.py 被 /End 结束以后智狐也不退出（卡住了），只有 taskkill 结束得了它。
    回答里的文字故意用中文（"正在运行""就绪"）：读状态只能看数，不能看字（见 windows.py 开头的说明）。
    """

    def __init__(self, data_dir: Path, starts: bool = False):
        self.data_dir = data_dir
        self.starts = starts
        self.obeys = True
        self.hangs = False
        self.calls: list[list[str]] = []
        self.registered = False
        self.created = b""  # /Create 时读到的 XML
        self.running = False
        self.result = NEVER_RAN
        self.enabled = True
        self.lock = None
        self.pid: int | None = None
        self.runs = 0
        self.fail: dict[str, Result] = {}

    def __call__(self, args: list[str]) -> Result:
        self.calls.append(list(args))
        program = ntpath.basename(args[0]).lower()
        if program == "taskkill.exe":
            assert args[1:4] == ["/F", "/T", "/PID"], args
            if self.pid is not None and args[4] == str(self.pid):
                self.down()
                return Result(0, f"成功: 已终止 PID {args[4]} 的进程。\r\n")
            return Result(128, "", f"错误: 没有找到进程 \"{args[4]}\"。\r\n")
        assert args[0] == "C:\\Windows\\System32\\schtasks.exe", args
        verb = args[1]
        assert args[2:4] == ["/TN", TASK], args
        if verb in self.fail:
            return self.fail[verb]
        if verb == "/Query":
            if not self.registered:
                return Result(1, "", "错误: 系统找不到指定的文件。\r\n")
            if "/XML" in args:
                text = '<?xml version="1.0" encoding="UTF-16"?>\r\n<Task><Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>'
                text += f"<Settings><Enabled>{'true' if self.enabled else 'false'}</Enabled></Settings></Task>\r\n"
                return Result(0, text)
            assert args[4:] == ["/FO", "CSV", "/V", "/NH"], args
            out = io.StringIO()
            csv.writer(out, quoting=csv.QUOTE_ALL, lineterminator="\r\n").writerow([
                "DESKTOP-1", TASK, "暂缺", "正在运行" if self.running else "就绪", "仅交互方式", "2026/11/21 9:00:00",
                str(RUNNING if self.running else self.result), "DESKTOP-1\\tester", "pythonw.exe …", "C:\\work", "暂缺",
                "已启用" if self.enabled else "已禁用",
            ])
            return Result(0, "\r\n" + out.getvalue())
        if verb == "/Create":
            assert args[4] == "/XML" and args[6:] == ["/F"], args
            self.created = Path(args[5]).read_bytes()
            self.registered = True
            return Result(0, f"成功: 成功创建计划任务 \"{TASK}\"。\r\n")
        if verb == "/Run":
            if not self.registered:
                return Result(1, "", "错误: 系统找不到指定的文件。\r\n")
            if not self.running:
                self.running = True
                self.up()
            return Result(0, f"成功: 尝试运行 \"{TASK}\"。\r\n")
        if verb == "/End":
            if self.running:
                self.running, self.result = False, TERMINATED
                if not self.hangs:
                    self.down()  # supervise.py 被结束了，管道断开，智狐自己收尾退出
            return Result(0, f"成功: 计划任务 \"{TASK}\" 已成功终止。\r\n")
        if verb == "/Delete":
            self.registered = False
            return Result(0, f"成功: 计划的任务 \"{TASK}\" 已被成功删除。\r\n")
        raise AssertionError(args)

    def tick(self) -> None:
        """每次假的 sleep 之后：服务看一眼有没有人请它停下（supervise.py 每半秒看一次）。"""
        if self.running and self.obeys and (self.data_dir / SERVICE_STOP_REQUEST).exists():
            self.down()
            self.running, self.result = False, 0

    def up(self) -> None:
        if not self.starts:
            return
        self.down()
        self.runs += 1  # 第一次开起来是这个测试进程自己的编号；以后每次都换一个（真的也是这样）
        self.pid = os.getpid() if self.runs == 1 else 600_000 + self.runs
        self.lock = hold(self.data_dir, self.pid)
        (self.data_dir / ".web-address").write_text(URL + "\n", encoding="utf-8")

    def down(self) -> None:
        if self.lock is not None:
            self.lock.close()
            self.lock = None
        self.pid = None
        (self.data_dir / ".web-address").unlink(missing_ok=True)

    def verbs(self) -> list[list[str]]:
        """除了查状态的 /Query 以外的命令（schtasks 的去掉程序名；taskkill 的保留程序名）。"""
        found = []
        for call in self.calls:
            if call[0].lower().endswith("taskkill.exe"):
                found.append(["taskkill", *call[1:]])
            elif call[1] != "/Query":
                found.append(call[1:])
        return found


class Ticking(Clock):
    """假的钟，每次 sleep 之后让假的服务看一眼停下的请求。"""

    def __init__(self):
        super().__init__()
        self.hooks: list = []

    def sleep(self, seconds: float) -> None:
        super().sleep(seconds)
        for hook in self.hooks:
            hook()


def fake_venv(folder: Path) -> Path:
    """一个假的虚拟环境：Scripts\\python.exe 和 pythonw.exe（只要在、能执行就行，没人真的运行它们）。"""
    scripts = folder / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    for name in ("python.exe", "pythonw.exe"):
        (scripts / name).write_text("", encoding="utf-8")
        (scripts / name).chmod(0o755)
    return scripts / "python.exe"


@pytest.fixture
def win(tmp_path, make_config, monkeypatch):
    """一台假的 Windows：假的虚拟环境（sys.executable 指向它的 python.exe）、临时的家目录和 %LOCALAPPDATA%、
    假的 schtasks（装上就"开起来"）、假的钟。"""
    python = fake_venv(tmp_path / "zhifox 项目")
    monkeypatch.setattr(sys, "executable", str(python))
    config = make_config()
    home = tmp_path / "home"
    home.mkdir()
    fake = FakeSchtasks(config.data_dir, starts=True)
    clock = Ticking()
    clock.hooks.append(fake.tick)
    manager = TaskScheduler(
        home, fake, user="tester", domain="DESKTOP-1", local_app_data=tmp_path / "LocalAppData", system_root="C:\\Windows",
        sleep=clock.sleep, clock=clock,
    )
    system = make_system(manager, home, clock=clock)
    yield type("Win", (), {"config": config, "home": home, "fake": fake, "manager": manager, "system": system, "clock": clock, "python": python})
    fake.down()


@pytest.fixture
def held():
    handles: list = []
    yield handles
    for handle in handles:
        handle.close()


def windows_plan(**changes) -> Plan:
    """一份 Windows 上的样子：路径里有反斜杠、空格、中文、&，结尾还有一个反斜杠（命令行里最容易出错的地方）。"""
    return Plan(**{
        "python": "C:\\Users\\张三\\My Projects\\zhifox\\.venv\\Scripts\\python.exe",
        "workspace": Path("C:\\Users\\张三\\工作 & 目录\\"),
        "data_dir": Path("C:\\Users\\张三\\My Projects\\zhifox\\data"),
        "env_file": Path("C:\\Users\\张三\\My Projects\\zhifox\\.env"),
        **changes,
    })


def manager_for(tmp_path: Path, runner=None, **options) -> TaskScheduler:
    return TaskScheduler(tmp_path, runner or FakeSchtasks(tmp_path / "data"), user="tester", domain="DESKTOP-1",
                         local_app_data=tmp_path / "LocalAppData", system_root="C:\\Windows", **options)


def task_xml(data: bytes) -> ElementTree.Element:
    return ElementTree.fromstring(data)


# ---- 任务的 XML ---------------------------------------------------------------------


def test_the_task_xml_has_exactly_what_task_scheduler_needs(tmp_path):
    manager = manager_for(tmp_path)
    plan = windows_plan()
    data = manager.render(plan)
    # UTF-16 小端、开头带 BOM，声明的也是 UTF-16：任务计划程序自己导出任务时就是这样写的
    assert data.startswith(codecs.BOM_UTF16_LE)
    text = data[2:].decode("utf-16-le")
    assert text.startswith('<?xml version="1.0" encoding="UTF-16"?>\r\n')
    root = task_xml(data)
    assert root.tag == f"{{{NAMESPACE}}}Task" and root.get("version") == "1.2"

    def value(path: str) -> str | None:
        return root.findtext(path, namespaces=NS)

    # 你（这个账号）登录时开起来；用你登录的身份、普通权限运行，不存密码
    assert value("t:Triggers/t:LogonTrigger/t:Enabled") == "true"
    assert value("t:Triggers/t:LogonTrigger/t:UserId") == "DESKTOP-1\\tester"
    assert len(root.findall("t:Triggers/*", NS)) == 1
    assert value("t:Principals/t:Principal/t:LogonType") == "InteractiveToken"
    assert value("t:Principals/t:Principal/t:RunLevel") == "LeastPrivilege"
    assert root.find("t:Principals/t:Principal/t:UserId", NS) is None  # 不写：就是建任务的这个账号（Git 的 git maintenance 也这样）
    settings = {child.tag.split("}")[1]: (child.text or "").strip() for child in root.find("t:Settings", NS)}
    assert settings["MultipleInstancesPolicy"] == "IgnoreNew"  # 已经在运行时不会再开第二个
    assert settings["DisallowStartIfOnBatteries"] == settings["StopIfGoingOnBatteries"] == "false"  # 拔了电源照样开着
    assert settings["ExecutionTimeLimit"] == "PT0S"  # 不限时（默认 3 天后强行结束）
    assert settings["Hidden"] == "true" and settings["Enabled"] == "true" and settings["AllowStartOnDemand"] == "true"
    assert settings["Priority"] == "5"  # 普通优先级（默认的 7 是"低于正常"）
    # RestartOnFailure：间隔最短 1 分钟，次数是 unsignedByte，最多 255（写 999 会被当成坏的 XML）
    assert value("t:Settings/t:RestartOnFailure/t:Interval") == "PT1M"
    assert value("t:Settings/t:RestartOnFailure/t:Count") == "255"
    # 要运行的命令：pythonw（不弹窗口）开 supervise.py，supervise.py 再开智狐
    command = value("t:Actions/t:Exec/t:Command")
    assert command == '"C:\\Users\\张三\\My Projects\\zhifox\\.venv\\Scripts\\pythonw.exe"'
    assert split_command_line(value("t:Actions/t:Exec/t:Arguments")) == [
        "-P", "-m", "harness.service.supervise",
        "--set", "HARNESS_DATA_DIR=C:\\Users\\张三\\My Projects\\zhifox\\data",
        "--set", "HARNESS_ENV_FILE=C:\\Users\\张三\\My Projects\\zhifox\\.env",
        "--set", "PYTHONUTF8=1",
        "--", "C:\\Users\\张三\\My Projects\\zhifox\\.venv\\Scripts\\python.exe", "-P", "-m", "harness",
        "--workspace", "C:\\Users\\张三\\工作 & 目录\\", "serve", "--web", "--service", "taskscheduler",
    ]
    assert value("t:Actions/t:Exec/t:WorkingDirectory") == "C:\\Users\\张三\\工作 & 目录\\"
    assert "&amp;" in text  # & 在 XML 里要写成 &amp;
    assert manager.render(plan) == data  # 同样的设置写出来一字不差：重装时才看得出"没有变"


def test_the_task_reads_back_to_the_same_plan(tmp_path, make_config):
    manager = manager_for(tmp_path)
    plans = [
        windows_plan(),
        windows_plan(web=False, port=9000),
        windows_plan(workspace=Path('C:\\a\\"quoted" \\\\server\\share\\'), python="C:\\zf\\Scripts\\python.exe"),
        # macOS、Linux 上的路径也一样读得回来（测试机上用的就是这种）
        make_plan(make_config(workspace=tmp_path), environ={"PATH": PATH}, manager="taskscheduler"),
    ]
    for plan in plans:
        assert windows.parse(manager.render(plan)) == plan
    assert windows.parse(b"not xml") is None
    assert windows.parse(b'<?xml version="1.0"?><Task xmlns="x"/>') is None
    other = manager.render(windows_plan()).replace("harness.service.supervise".encode("utf-16-le"), "evil.module.supervise".encode("utf-16-le"))
    assert windows.parse(other) is None  # 不是我们写的样子


def test_the_task_carries_no_path_lang_or_secrets(win):
    """Windows 上不抄 PATH 和 LANG（任务计划程序给的就是你账号的那一套），密钥更不会进任务的设置。"""
    win.system.environ = {
        "PATH": "C:\\Windows;C:\\very\\long\\path", "LANG": "zh_CN.UTF-8", "ANTHROPIC_API_KEY": "sk-ant-api03-SECRETSECRET",
        "TELEGRAM_BOT_TOKEN": "123456:SECRETBOT", "HTTPS_PROXY": "http://me:proxy-password@proxy.local:3128",
    }
    assert service_cli.run(args_for("install"), win.config, win.system) == 0
    text = win.manager.file.read_bytes()[2:].decode("utf-16-le")
    for secret in ("SECRETSECRET", "SECRETBOT", "proxy-password", "very\\long", "LANG=", "PATH="):
        assert secret not in text
    assert "token=" not in text.lower() and TOKEN not in text  # 网页的口令也不在（InteractiveToken 是登录方式，不是口令）
    out, err = said(win.system)
    assert "proxy-password" not in out + err and "SECRET" not in out + err
    assert "HTTPS_PROXY" in err  # 只说名字：它只在终端里设置了，服务读不到
    assert make_plan(win.config, environ={"PATH": "x", "LANG": "y"}, manager="taskscheduler").environment() == {
        "HARNESS_DATA_DIR": str(win.config.data_dir), "HARNESS_ENV_FILE": str(win.config.env_file), "PYTHONUTF8": "1",
    }


@pytest.mark.parametrize("arguments", [
    ["C:\\a b\\", 'x"y', "", "中文 路径", "--set", "A=C:\\p q\\"],
    ["\\\\server\\share\\", 'a\\"b', "tab\there", "a\\\\b", '"', "\\", "\\\\", 'end\\"'],
    ["plain", "two  spaces", "trailing ", " leading"],
])
def test_split_command_line_is_the_inverse_of_list2cmdline(arguments):
    assert split_command_line(subprocess.list2cmdline(arguments)) == arguments


def test_split_command_line_survives_random_strings():
    generator = random.Random(20261121)
    alphabet = ['\\', '"', " ", "\t", "a", "狐", "&", "%"]
    for _ in range(2000):
        arguments = ["".join(generator.choice(alphabet) for _ in range(generator.randint(0, 6))) for _ in range(generator.randint(1, 4))]
        assert split_command_line(subprocess.list2cmdline(arguments)) == arguments, arguments


def test_split_command_line_follows_the_c_runtime_rules():
    # 微软文档"Parsing C command-line arguments"里的几个例子
    assert split_command_line('"abc" d e') == ["abc", "d", "e"]
    assert split_command_line('a\\\\\\b d"e f"g h') == ["a\\\\\\b", "de fg", "h"]
    assert split_command_line('a\\\\\\"b c d') == ['a\\"b', "c", "d"]
    assert split_command_line('a\\\\\\\\"b c" d e') == ["a\\\\b c", "d", "e"]
    assert split_command_line('a"b"" c d') == ['ab" c d']


# ---- 装之前的检查 ---------------------------------------------------------------------


def test_windows_paths_are_checked_by_windows_rules(tmp_path):
    python = fake_venv(tmp_path / "venv")
    workspace = tmp_path / "w"
    workspace.mkdir()
    good = Plan(python=str(python), workspace=workspace, data_dir=Path("C:\\Users\\me\\zf\\data"), env_file=Path("C:\\Users\\me\\zf\\.env"))
    assert problem(good, "taskscheduler") is None  # 反斜杠是 Windows 路径的分隔符，当然可以
    assert "反斜杠" in problem(good, "launchd") and "反斜杠" in problem(good)  # macOS、Linux 上照旧不行（没有放宽）
    assert "%" in problem(dataclasses.replace(good, data_dir=Path("C:\\50%off%\\data")), "taskscheduler")  # 任务计划程序会展开 %NAME%
    assert "%" in problem(dataclasses.replace(good, env_file=Path('C:\\a"b\\.env')), "taskscheduler")
    assert "换行" in problem(dataclasses.replace(good, data_dir=Path("C:\\a\nb")), "taskscheduler")
    (python.parent / "pythonw.exe").unlink()
    assert "pythonw.exe" in problem(good, "taskscheduler")  # 没有不弹窗口的那个 Python：先 uv sync
    posix = dataclasses.replace(good, data_dir=tmp_path / "data", env_file=tmp_path / ".env")
    assert problem(posix, "launchd") is None  # 只有 Windows 上才要 pythonw.exe


def test_windowless_finds_pythonw_next_to_python():
    assert windowless("C:\\zf\\.venv\\Scripts\\python.exe") == "C:\\zf\\.venv\\Scripts\\pythonw.exe"
    assert windowless("/tmp/zf/.venv/Scripts/python.exe") == "/tmp/zf/.venv/Scripts/pythonw.exe"


def test_make_plan_keeps_path_and_lang_for_the_others(make_config):
    config = make_config()
    environ = {"PATH": "/usr/bin", "LANG": "C.UTF-8"}
    assert make_plan(config, environ=environ).path == "/usr/bin"
    assert make_plan(config, environ=environ, manager="launchd").lang == "C.UTF-8"
    windows_plan_ = make_plan(config, environ=environ, manager="taskscheduler")
    assert (windows_plan_.path, windows_plan_.lang) == ("", "")


def test_the_task_name_is_per_account_and_paths_follow_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", "D:\\Profiles\\me\\AppData\\Local")
    monkeypatch.setenv("SystemRoot", "D:\\WINNT")
    monkeypatch.setenv("USERNAME", "Zhang San")
    monkeypatch.setenv("USERDOMAIN", "OFFICE")
    manager = TaskScheduler(tmp_path, FakeSchtasks(tmp_path))
    assert manager.task == "\\ZhiFox-Zhang San" and manager.account == "OFFICE\\Zhang San"
    assert str(manager.file) == str(Path("D:\\Profiles\\me\\AppData\\Local") / "ZhiFox" / "service-task.xml")
    assert manager.schtasks == "D:\\WINNT\\System32\\schtasks.exe" and manager.taskkill == "D:\\WINNT\\System32\\taskkill.exe"
    monkeypatch.delenv("LOCALAPPDATA")
    assert TaskScheduler(tmp_path, FakeSchtasks(tmp_path)).file == tmp_path / "AppData" / "Local" / "ZhiFox" / "service-task.xml"
    assert TaskScheduler(tmp_path, FakeSchtasks(tmp_path), user="a:b*c").task == "\\ZhiFox-a_b_c"  # 任务的名字里不能有的字符
    assert TaskScheduler(tmp_path, FakeSchtasks(tmp_path), user="me", domain="").account == "me"


# ---- 发给 schtasks 的命令 -----------------------------------------------------------


def test_install_creates_the_task_and_runs_it(win):
    assert service_cli.run(args_for("install"), win.config, win.system) == 0
    task_file = str(win.manager.file)
    assert win.fake.calls[0] == ["C:\\Windows\\System32\\schtasks.exe", "/Query", "/TN", TASK, "/FO", "CSV", "/V", "/NH"]
    assert win.fake.verbs() == [["/Create", "/TN", TASK, "/XML", task_file, "/F"], ["/Run", "/TN", TASK]]
    assert win.fake.created == win.manager.file.read_bytes() and win.fake.created.startswith(codecs.BOM_UTF16_LE)
    assert windows.parse(win.fake.created).arguments("taskscheduler")[0] == str(win.python)  # 智狐本身用有控制台的 python.exe
    out, err = said(win.system)
    assert "智狐的后台服务（任务计划程序）：已经装好了" in out and task_file in out
    assert f"在“任务计划程序”里叫 {TASK}" in out and str(win.config.data_dir / "logs" / "service.log") in out
    assert f"已经开起来了（进程 {os.getpid()}）" in out and "uv run harness service open" in out
    assert TOKEN not in out + err
    assert (win.config.data_dir / "logs").is_dir()


def test_installing_again_with_the_same_settings_changes_nothing(win):
    service_cli.run(args_for("install"), win.config, win.system)
    written = win.manager.file.stat().st_mtime_ns
    win.fake.calls.clear()
    system = make_system(win.manager, win.home, clock=win.clock)
    assert service_cli.run(args_for("install"), win.config, system) == 0
    assert win.fake.verbs() == [] and win.manager.file.stat().st_mtime_ns == written
    assert "已经装好了，设置没有变，正在运行" in said(system)[0]


def test_installing_with_new_settings_stops_gracefully_and_replaces_the_task(win):
    service_cli.run(args_for("install"), win.config, win.system)
    win.fake.calls.clear()
    system = make_system(win.manager, win.home, clock=win.clock)
    assert service_cli.run(args_for("install", "--no-web", "--port", "9001"), win.config, system) == 0
    # 先好好地停下旧的（放下停下的请求，等它退出；不用 /End），再换上新的、开起来
    assert win.fake.verbs() == [["/Create", "/TN", TASK, "/XML", str(win.manager.file), "/F"], ["/Run", "/TN", TASK]]
    assert not (win.config.data_dir / SERVICE_STOP_REQUEST).exists()  # 请求收走了，不然新的一开就停
    arguments = windows.parse(win.manager.file.read_bytes()).arguments("taskscheduler")
    assert "--web" not in arguments and arguments[arguments.index("--port") + 1] == "9001"
    out, _ = said(system)
    assert "换成了这次的设置，重新开起来了" in out and "没有开网页界面" in out


def test_stop_asks_the_service_to_finish_its_turn_first(win):
    """schtasks /End 是直接结束进程，正在进行的一轮来不及存盘。所以先放下停下的请求，等它自己收尾退出。"""
    service_cli.run(args_for("install"), win.config, win.system)
    win.fake.calls.clear()
    before = win.clock.now
    assert service_cli.run(args_for("stop"), win.config, win.system) == 0
    assert win.fake.verbs() == []  # 没有用到 /End、taskkill
    assert win.clock.now - before < 2  # 服务半秒之内就看到了请求
    assert not instance.in_use(win.config.data_dir) and not (win.config.data_dir / SERVICE_STOP_REQUEST).exists()
    assert win.fake.result == 0 and win.fake.registered  # 任务还在：下次登录时又会开起来
    assert "下次登录时会自动启动" in said(win.system)[0]
    assert service_cli.run(args_for("stop"), win.config, win.system) == 0
    assert "本来就没有在运行" in said(win.system)[0]


def test_stop_falls_back_to_end_when_the_service_does_not_listen(win):
    service_cli.run(args_for("install"), win.config, win.system)
    win.fake.obeys = False  # 比如是旧版本、或者卡住了
    win.fake.calls.clear()
    before = win.clock.now
    assert service_cli.run(args_for("stop"), win.config, win.system) == 0
    assert win.fake.verbs() == [["/End", "/TN", TASK]]
    assert win.clock.now - before >= SERVICE_STOP_SECONDS + SERVICE_STOP_SLACK_SECONDS  # 先等够了它自己退出的时间
    assert not instance.in_use(win.config.data_dir) and not (win.config.data_dir / SERVICE_STOP_REQUEST).exists()


def test_stop_ends_a_stuck_zhifox_with_taskkill_as_the_last_resort(win):
    service_cli.run(args_for("install"), win.config, win.system)
    win.fake.obeys, win.fake.hangs = False, True  # supervise.py 被结束以后，智狐还占着数据目录
    pid = win.fake.pid
    win.fake.calls.clear()
    assert service_cli.run(args_for("stop"), win.config, win.system) == 0
    assert win.fake.verbs() == [["/End", "/TN", TASK], ["taskkill", "/F", "/T", "/PID", str(pid)]]
    assert not instance.in_use(win.config.data_dir)


def test_start_and_restart(win):
    service_cli.run(args_for("install"), win.config, win.system)
    service_cli.run(args_for("stop"), win.config, win.system)
    win.fake.calls.clear()
    assert service_cli.run(args_for("start"), win.config, win.system) == 0
    assert win.fake.verbs() == [["/Run", "/TN", TASK]]
    win.fake.calls.clear()
    assert service_cli.run(args_for("start"), win.config, win.system) == 0
    assert win.fake.verbs() == [] and "已经在运行了" in said(win.system)[0]
    old = win.fake.pid
    win.fake.calls.clear()
    assert service_cli.run(args_for("restart"), win.config, win.system) == 0
    assert win.fake.verbs() == [["/Run", "/TN", TASK]]  # 先好好地停下（没有 /End），再开
    assert win.fake.pid != old and f"已经开起来了（进程 {win.fake.pid}）" in said(win.system)[0]  # 等到的是新的那个


def test_a_stale_stop_request_does_not_stop_the_next_start(win):
    service_cli.run(args_for("install"), win.config, win.system)
    service_cli.run(args_for("stop"), win.config, win.system)
    (win.config.data_dir / SERVICE_STOP_REQUEST).write_text("stop\n", encoding="utf-8")  # 上一次停到一半被打断了
    assert service_cli.run(args_for("start"), win.config, win.system) == 0
    assert not (win.config.data_dir / SERVICE_STOP_REQUEST).exists() and win.fake.running


def test_start_puts_back_a_task_deleted_from_task_scheduler(win):
    service_cli.run(args_for("install"), win.config, win.system)
    service_cli.run(args_for("stop"), win.config, win.system)
    win.fake.registered = False  # 有人在"任务计划程序"里把它删了
    system = make_system(win.manager, win.home, clock=win.clock)
    service_cli.run(args_for("status"), win.config, system)
    assert "任务计划程序里没有这个任务了" in said(system)[0]
    win.fake.calls.clear()
    assert service_cli.run(args_for("start"), win.config, win.system) == 0
    assert [verb[0] for verb in win.fake.verbs()] == ["/Create", "/Run"]


def test_a_failed_create_is_reported(win):
    win.fake.fail["/Create"] = Result(1, "", "错误: 拒绝访问。\r\n")
    assert service_cli.run(args_for("install"), win.config, win.system) == 1
    _, err = said(win.system)
    assert "没能启动它：错误: 拒绝访问。" in err and str(win.manager.file) in err


def test_uninstall_stops_gracefully_and_deletes_the_task(win):
    service_cli.run(args_for("install"), win.config, win.system)
    win.fake.calls.clear()
    assert service_cli.run(args_for("uninstall"), win.config, win.system) == 0
    assert win.fake.verbs() == [["/Delete", "/TN", TASK, "/F"]]
    assert not win.manager.file.exists() and not win.fake.registered
    assert not (win.config.data_dir / ".web-address").exists() and not instance.in_use(win.config.data_dir)
    out, _ = said(win.system)
    assert "已经卸载了智狐的后台服务" in out and str(win.config.data_dir) in out
    assert service_cli.run(args_for("uninstall"), win.config, win.system) == 0
    assert "没有装后台服务，不用卸载" in said(win.system)[0]


def test_uninstall_keeps_everything_when_the_task_cannot_be_deleted(win):
    """删不掉任务（比如没有权限）：要是照样删掉我们那一份 XML、说"卸载了"，下次登录它又会开起来。"""
    service_cli.run(args_for("install"), win.config, win.system)
    win.fake.fail["/Delete"] = Result(1, "", "错误: 拒绝访问。\r\n")
    assert service_cli.run(args_for("uninstall"), win.config, win.system) == 1
    assert win.manager.file.exists() and win.fake.registered
    assert f"没能从任务计划程序里删掉 {TASK}：错误: 拒绝访问。" in said(win.system)[1]


# ---- 数据目录被别人占着 ------------------------------------------------------------


def test_install_refuses_while_another_zhifox_uses_the_data_folder(win, held):
    held.append(instance.claim(win.config.data_dir, "web"))
    assert service_cli.run(args_for("install"), win.config, win.system) == 1
    _, err = said(win.system)
    assert "数据目录正被另一个智狐用着" in err and "harness --web" in err and f"进程 {os.getpid()}" in err
    assert "Ctrl-C" in err and "kill -INT" not in err  # Windows 上没有 kill -INT
    assert not win.manager.file.exists() and win.fake.verbs() == []


def test_status_says_when_the_service_cannot_come_up_because_of_someone_else(win, held):
    service_cli.run(args_for("install"), win.config, win.system)
    win.fake.lock.close()  # 智狐退出了（比如你在终端里开了 harness --web），supervise.py 还开着、过一会儿再试
    win.fake.lock = None
    held.append(hold(win.config.data_dir, pid=OTHER_PID, kind="web"))
    system = make_system(win.manager, win.home, clock=win.clock)
    assert service_cli.run(args_for("status"), win.config, system) == 0
    out = said(system)[0]
    assert "正在运行，智狐还没开好" in out and "网页：正在开" in out and "8765" not in out
    assert f"数据目录正被别的程序占着（在终端里开着的网页界面（harness --web），进程 {OTHER_PID}）" in out
    system = make_system(win.manager, win.home, clock=win.clock)
    assert service_cli.run(args_for("start"), win.config, system) == 0
    assert "还没开好" in said(system)[0]  # 不说"进程 None"


# ---- 读状态 -----------------------------------------------------------------------


@pytest.mark.parametrize(("row", "expected"), [
    ('"PC","\\ZhiFox-me","N/A","Running","Interactive only","11/21/2026 9:00:00 AM","267009","PC\\me"', RUNNING),
    ('"PC","\\ZhiFox-me","不适用","就绪","仅交互方式","2026/11/21 9:00:00","0","PC\\me"', 0),
    ('"PC","\\ZhiFox-me","N/D","Listo","Solo interactivo","21/11/2026 9:00:00","2","PC\\me"', 2),
    ('"PC","\\ZhiFox-me","N/A","Ready","Interactive only","N/A","-2147024894","PC\\me"', 0x80070002),  # 负数是 HRESULT
    ('"PC","\\ZhiFox-me","N/A","Ready","Interactive only","N/A","0x41306","PC\\me"', TERMINATED),
    ('\r\n"PC","\\ZhiFox-me, with a comma","N/A","Ready","x","y","1","z"\r\n', 1),
    ('"PC","\\ZhiFox-me","N/A","Ready"', None),
    ('"PC","\\ZhiFox-me","N/A","Ready","x","y","n/a","z"', None),
])
def test_last_result_reads_the_number_not_the_words(row, expected):
    assert last_result(row) == expected


def test_state_reads_what_schtasks_says(win):
    fake, manager = win.fake, win.manager
    assert manager.state() == manager.state().__class__(False)  # 没有任务
    service_cli.run(args_for("install"), win.config, win.system)
    state = manager.state()
    assert (state.installed, state.loaded, state.running, state.pid, state.exit_code) == (True, True, True, os.getpid(), None)
    fake.down()  # supervise.py 还开着，智狐没占着数据目录
    assert manager.state().running and manager.state().pid is None
    fake.running, fake.result = False, NEVER_RAN
    state = manager.state()
    assert (state.running, state.exit_code, state.enabled) == (False, None, True)  # 还没运行过：没有退出码可说
    fake.result, fake.enabled = TERMINATED, False
    state = manager.state()
    assert (state.exit_code, state.enabled) == (TERMINATED, False)  # 被结束了；在"任务计划程序"里被禁用了
    fake.fail["/Query"] = Result(1, "", "错误: 拒绝访问。")
    assert manager.state() == manager.state().__class__(True)


def test_status_when_running(win):
    service_cli.run(args_for("install"), win.config, win.system)
    system = make_system(win.manager, win.home, clock=win.clock)
    assert service_cli.run(args_for("status"), win.config, system) == 0
    out, err = said(system)
    assert out.startswith(f"智狐的后台服务（任务计划程序）：正在运行（进程 {os.getpid()}）")
    assert "网页：http://127.0.0.1:8765（打开：uv run harness service open）" in out
    assert f"在“任务计划程序”里叫 {TASK}" in out and "显示隐藏的任务" in out
    assert str(win.config.data_dir / "logs" / "service.log") in out
    assert "退出登录时跟着停下" in out and "linger" not in out
    assert TOKEN not in out + err


@pytest.mark.parametrize(("result", "enabled", "phrase"), [
    (0, True, "已停下。下次登录时会自动启动"),
    (0, False, "已停下，也不会自动启动"),
    (2, True, "出错停下了（退出码 2：设置有问题（比如 channels.json 坏了），重启也没用，先看日志）"),
    (1, True, "supervise.py 自己出错退出了"),
    (TERMINATED, True, f"出错停下了（退出码 {TERMINATED}：任务计划程序把它结束了（0x41306"),
    (0x80070002, True, "找不到要运行的程序（0x80070002）"),
    (NEVER_RAN, True, "已停下。下次登录时会自动启动"),
])
def test_status_explains_why_it_stopped(win, result, enabled, phrase):
    service_cli.run(args_for("install"), win.config, win.system)
    win.fake.down()
    win.fake.running, win.fake.result, win.fake.enabled = False, result, enabled
    system = make_system(win.manager, win.home, clock=win.clock)
    assert service_cli.run(args_for("status"), win.config, system) == 3
    assert phrase in said(system)[0]


def test_status_lists_the_supervisors_warnings(win):
    service_cli.run(args_for("install"), win.config, win.system)
    log_file = win.config.data_dir / SERVICE_LOG
    supervisor = Supervisor(["x"], {}, win.config.data_dir)
    supervisor.note("warning", "智狐退出了（退出码 1），10 秒后重新开起来。")
    system = make_system(win.manager, win.home, clock=win.clock)
    service_cli.run(args_for("status"), win.config, system)
    assert "warning 智狐退出了（退出码 1），10 秒后重新开起来。" in said(system)[0]
    assert log_file.read_text(encoding="utf-8").count("\n") == 1


# ---- 日志 --------------------------------------------------------------------


def test_logs_on_windows_need_no_tail(win, monkeypatch):
    system = make_system(win.manager, win.home, clock=win.clock)
    assert service_cli.run(args_for("logs"), win.config, system) == 0
    assert "还没有日志" in said(system)[0]
    log_file = win.config.data_dir / SERVICE_LOG
    log_file.parent.mkdir(parents=True)
    log_file.write_text("".join(f"第 {n} 行\n" for n in range(10)), encoding="utf-8")
    system = make_system(win.manager, win.home, clock=win.clock)
    assert service_cli.run(args_for("logs", "-n", "2"), win.config, system) == 0
    assert said(system)[0] == "第 8 行\n第 9 行\n"
    # -f：接着往下看，Ctrl-C 结束（这里让第二次等待时"按下 Ctrl-C"）
    rounds = []

    def sleep(_seconds):
        rounds.append(1)
        if len(rounds) == 1:
            with open(log_file, "a", encoding="utf-8") as handle:
                handle.write("新的一行\n")
        else:
            raise KeyboardInterrupt

    follower = TaskScheduler(win.home, win.fake, user="tester", domain="DESKTOP-1", local_app_data=win.manager.file.parent.parent,
                             system_root="C:\\Windows", sleep=sleep)
    system = make_system(follower, win.home, clock=win.clock)
    assert service_cli.run(args_for("logs", "-f", "-n", "1"), win.config, system) == 0
    assert said(system)[0] == "第 9 行\n新的一行\n"


# ---- supervise.py：出错退出了再开、好好地停下 --------------------------------------------


class FakeChild:
    """假的智狐进程。codes 是它每次 poll 的回答（None 是还在运行），用完了就一直是最后一个。
    stdin 关上以后，stops_on_eof 为真的话下一次 poll 就退出（退出码 0），像真的智狐那样收尾。"""

    def __init__(self, codes: list[int | None], stops_on_eof: bool = True):
        self.codes = list(codes)
        self.stops_on_eof = stops_on_eof
        self.stdin = io.BytesIO()
        self.killed = False
        self.code: int | None = None

    def poll(self) -> int | None:
        if self.code is not None:
            return self.code
        if self.killed:
            self.code = 1
        elif self.stdin.closed and self.stops_on_eof:
            self.code = 0
        elif self.codes:
            self.code = self.codes.pop(0) if len(self.codes) > 1 else self.codes[0]
        return self.code

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        return self.poll() or 0


class Spawner:
    """假的"开智狐"：按顺序交出准备好的 FakeChild，记下每次开的时间（假的钟）。"""

    def __init__(self, clock: Clock, children: list):
        self.clock = clock
        self.children = list(children)
        self.started: list[float] = []
        self.seen: list[tuple[list[str], dict[str, str], Path]] = []

    def __call__(self, command, env, log_file):
        self.started.append(self.clock.now)
        self.seen.append((list(command), dict(env), log_file))
        child = self.children.pop(0)
        if isinstance(child, Exception):
            raise child
        return child


def supervisor_for(tmp_path: Path, children: list, **options) -> tuple[Supervisor, Spawner, Clock]:
    clock = Ticking()
    spawner = Spawner(clock, children)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return Supervisor(["python.exe", "-m", "harness"], {"HARNESS_DATA_DIR": str(data)}, data, spawn=spawner, sleep=clock.sleep, clock=clock, **options), spawner, clock


def log_lines(tmp_path: Path) -> list[str]:
    return (tmp_path / "data" / SERVICE_LOG).read_text(encoding="utf-8").splitlines()


def test_the_supervisor_restarts_after_a_crash_and_stops_on_a_config_error(tmp_path):
    supervisor, spawner, _ = supervisor_for(tmp_path, [FakeChild([None, None, 1]), FakeChild([None, 3]), FakeChild([EXIT_CONFIG])])
    assert supervisor.run() == EXIT_CONFIG  # 设置有问题：不再开，免得一直重启
    assert len(spawner.started) == 3
    # 每次出错退出以后都隔 SERVICE_RESTART_SECONDS 秒才再开（和 launchd 的 ThrottleInterval、systemd 的 RestartSec 一样）
    assert spawner.started[1] - spawner.started[0] >= SERVICE_RESTART_SECONDS
    assert spawner.started[2] - spawner.started[1] >= SERVICE_RESTART_SECONDS
    lines = log_lines(tmp_path)
    assert any("warning 智狐退出了（退出码 1），10 秒后重新开起来。" in line for line in lines)
    assert any("warning 智狐退出了（退出码 3）" in line for line in lines)
    assert "error 智狐的设置有问题（退出码 2）" in lines[-1]
    # 这几行和智狐自己写的格式一样，status 认得出其中的警告和错误
    assert [line.split(" ", 3)[2] for line in logs.problems(lines, 10)] == ["warning", "warning", "error"]


def test_the_supervisor_does_not_restart_after_a_clean_exit(tmp_path):
    supervisor, spawner, _ = supervisor_for(tmp_path, [FakeChild([None, 0])])
    assert supervisor.run() == 0 and len(spawner.started) == 1
    assert "好好地退出了" in log_lines(tmp_path)[-1]


def test_a_stop_request_closes_zhifoxs_input_and_waits_for_it(tmp_path):
    child = FakeChild([None])
    supervisor, spawner, clock = supervisor_for(tmp_path, [child])
    clock.at.append((3.0, lambda: (tmp_path / "data" / SERVICE_STOP_REQUEST).write_text("stop\n", encoding="utf-8")))
    assert supervisor.run() == 0
    assert child.stdin.closed and not child.killed and len(spawner.started) == 1
    assert 3.0 <= clock.now < 5  # 半秒之内看到了请求，智狐马上收好了尾
    lines = log_lines(tmp_path)
    assert "收到停下的请求" in lines[-2] and lines[-1].endswith("info 已经停下了。")


def test_zhifox_that_ignores_the_request_is_ended_after_the_stop_timeout(tmp_path):
    child = FakeChild([None], stops_on_eof=False)
    supervisor, _, clock = supervisor_for(tmp_path, [child])
    clock.at.append((0.1, lambda: (tmp_path / "data" / SERVICE_STOP_REQUEST).write_text("stop\n", encoding="utf-8")))
    assert supervisor.run() == 0
    assert child.stdin.closed and child.killed
    assert clock.now >= SERVICE_STOP_SECONDS
    assert any(f"过了 {SERVICE_STOP_SECONDS} 秒还没退出，直接结束它" in line for line in log_lines(tmp_path))


def test_a_stop_request_during_the_restart_pause_ends_the_supervisor(tmp_path):
    supervisor, spawner, clock = supervisor_for(tmp_path, [FakeChild([1]), FakeChild([None])])
    clock.at.append((2.0, lambda: (tmp_path / "data" / SERVICE_STOP_REQUEST).write_text("stop\n", encoding="utf-8")))
    assert supervisor.run() == 0
    assert len(spawner.started) == 1  # 没有再开


def test_a_stale_request_is_ignored_and_a_failed_spawn_is_retried(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / SERVICE_STOP_REQUEST).write_text("stop\n", encoding="utf-8")  # 上一次留下的
    supervisor, spawner, _ = supervisor_for(tmp_path, [OSError("找不到 python.exe"), FakeChild([None, 0])])
    assert supervisor.run() == 0
    assert len(spawner.started) == 2 and spawner.started[1] >= SERVICE_RESTART_SECONDS
    assert any("error 开不起智狐：找不到 python.exe。10 秒后再试。" in line for line in log_lines(tmp_path))


def test_the_supervisor_command_line_is_checked(tmp_path):
    data = str(tmp_path / "数据 目录")
    assert supervise.parse(["--set", f"HARNESS_DATA_DIR={data}", "--set", "PYTHONUTF8=1", "--", "python.exe", "-m", "harness"]) == (
        {"HARNESS_DATA_DIR": data, "PYTHONUTF8": "1"}, ["python.exe", "-m", "harness"],
    )
    for bad in (
        ["--set", f"HARNESS_DATA_DIR={data}", "python.exe"],  # 没有 --
        ["--set", "ANTHROPIC_API_KEY=sk-x", "--set", f"HARNESS_DATA_DIR={data}", "--", "x"],  # 只认那几个名字
        ["--set", "PYTHONUTF8=1", "--", "x"],  # 没有数据目录
        ["--set", f"HARNESS_DATA_DIR={data}", "--"],  # 没有命令
        ["--env", f"HARNESS_DATA_DIR={data}", "--", "x"],
        ["--set", "HARNESS_DATA_DIR", "--", "x"],
        ["--set", f"HARNESS_DATA_DIR={data}", "--set", "--", "x"],
    ):
        assert supervise.parse(bad) is None, bad
    assert supervise.main(["--", "x"]) == EXIT_CONFIG


def test_the_supervisor_gives_zhifox_the_settings_from_the_task(tmp_path):
    supervisor, spawner, _ = supervisor_for(tmp_path, [FakeChild([0])])
    supervisor.run()
    command, env, log_file = spawner.seen[0]
    assert command == ["python.exe", "-m", "harness"] and env["HARNESS_DATA_DIR"] == str(tmp_path / "data")
    assert log_file == tmp_path / "data" / SERVICE_LOG


def test_watch_input_turns_the_end_of_input_into_a_stop():
    read, write = os.pipe()
    stop = threading.Event()
    with open(read, "rb") as stream:
        thread = watch_input(stream, stop)
        assert not stop.wait(0.2)
        os.close(write)  # supervise.py 关上了它（或者 supervise.py 被结束了）
        thread.join(5)
    assert stop.is_set()
    assert watch_input(None, stop) is None  # pythonw 没有标准输入：什么都不做


# ---- 真的进程 -------------------------------------------------------------------


def _clean_env(tmp_path: Path) -> dict[str, str]:
    env = {name: value for name, value in os.environ.items() if not name.startswith("HARNESS_") and name != "PYTHONPATH"}
    env.update({"HOME": str(tmp_path / "home"), "NO_PROXY": "*", "no_proxy": "*"})
    env.pop("JOURNAL_STREAM", None)
    return env


CHILD = """
import sys, pathlib
marker = pathlib.Path(sys.argv[1])
if sys.argv[2] == "crash-once" and not marker.with_suffix(".crashed").exists():
    marker.with_suffix(".crashed").write_text("1")
    sys.exit(3)
if sys.argv[2] == "config":
    sys.exit(2)
print("child started", flush=True)
sys.stdin.buffer.read()  # 等着 supervise.py 关上标准输入
marker.write_text("got eof")
"""


def _run_supervisor(tmp_path: Path, mode: str) -> tuple[subprocess.Popen, Path, Path]:
    data = tmp_path / "data 数据"
    data.mkdir()
    marker = tmp_path / "marker.txt"
    command = [sys.executable, "-P", "-m", windows.SUPERVISOR, "--set", f"HARNESS_DATA_DIR={data}", "--", sys.executable, "-P", "-c", CHILD, str(marker), mode]
    process = subprocess.Popen(command, cwd=ROOT, env=_clean_env(tmp_path), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return process, data, marker


def test_the_real_supervisor_stops_its_child_through_its_input(tmp_path):
    process, data, marker = _run_supervisor(tmp_path, "wait")
    try:
        until(lambda: "child started" in (data / SERVICE_LOG).read_text(encoding="utf-8") if (data / SERVICE_LOG).exists() else False, timeout=30, what="子进程开起来")
        (data / SERVICE_STOP_REQUEST).write_text("stop\n", encoding="utf-8")  # harness service stop 做的就是这个
        assert process.wait(30) == 0, process.stderr.read()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert marker.read_text(encoding="utf-8") == "got eof"  # 子进程等到了"输入结束"，自己退出的（不是被结束的）
    text = (data / SERVICE_LOG).read_text(encoding="utf-8")
    assert "收到停下的请求" in text and text.rstrip().endswith("已经停下了。")


def test_the_real_supervisor_does_not_loop_on_a_config_error(tmp_path):
    process, data, _ = _run_supervisor(tmp_path, "config")
    try:
        assert process.wait(30) == EXIT_CONFIG
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert "退出码 2" in (data / SERVICE_LOG).read_text(encoding="utf-8")


def test_when_the_supervisor_is_ended_its_child_still_finishes_cleanly(tmp_path):
    """schtasks /End（或者"任务计划程序"里的"结束"）直接结束的是 supervise.py。它一没，管道就断了，智狐照样收尾退出。"""
    process, data, marker = _run_supervisor(tmp_path, "wait")
    try:
        until(lambda: "child started" in (data / SERVICE_LOG).read_text(encoding="utf-8") if (data / SERVICE_LOG).exists() else False, timeout=30, what="子进程开起来")
        process.kill()
        process.wait(30)
        # 等写完，不只是等文件出现（write_text 先建文件、再写内容，中间那一下读到的是空的）
        until(lambda: marker.exists() and marker.read_text(encoding="utf-8") == "got eof", timeout=30, what="子进程看到输入结束、自己收尾")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_the_task_command_really_runs_zhifox_under_the_supervisor(tmp_path, make_config):
    """任务计划程序开的那条命令（不经过任务计划程序）：supervise.py 开真的 harness serve --web --service taskscheduler，
    harness service stop 放下请求以后，智狐收尾退出、删掉网页的地址，supervise.py 以 0 退出。

    在 Windows 上用的是虚拟环境里真的 pythonw.exe（没有标准输出，正好测到 log.py 里没有 1、2 号文件描述符的情况）；
    别的系统上没有 pythonw，用同一个 python。工作目录里埋了一个 json.py：-P 让两个进程都不会导入它。"""
    workspace = tmp_path / "work dir 工作"
    workspace.mkdir()
    (workspace / "json.py").write_text("raise SystemExit('导入了工作目录里埋的 json.py')\n", encoding="utf-8")
    data = tmp_path / "data"
    config = make_config(workspace=workspace, data_dir=data, env_file=tmp_path / "no-such.env")
    plan = make_plan(config, port=0, environ={}, manager="taskscheduler")
    command = task_command(plan)
    if not os.path.isfile(command[0]):
        command[0] = sys.executable
    process = subprocess.Popen(command, cwd=workspace, env=_clean_env(tmp_path), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    address = data / ".web-address"
    try:
        url = until(lambda: process.poll() is None and address.is_file() and address.read_text(encoding="utf-8").strip(), timeout=60, what="网页的地址")
        found = instance.holder(data)
        assert found is not None and found.kind == "service" and found.pid != process.pid  # 占着数据目录的是 supervise.py 开的智狐
        (data / SERVICE_STOP_REQUEST).write_text("stop\n", encoding="utf-8")
        assert process.wait(60) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    until(lambda: not instance.in_use(data), timeout=30, what="智狐退出、放开数据目录")
    text = (data / SERVICE_LOG).read_text(encoding="utf-8")
    assert "埋的 json.py" not in text and url.split("token=", 1)[1] not in text  # 口令不进日志
    assert "看护开起来了" in text and "已作为服务启动" in text and "再见。" in text and text.rstrip().endswith("已经停下了。")
    assert not address.exists()


def test_the_service_log_survives_rotation_without_stdout(tmp_path):
    """像 pythonw.exe 那样没有 1、2 号文件描述符时，own_stdio 照样能用；写满了能换新文件（Windows 上 1、2 号开着
    这个文件时改不了名，所以换之前先放开它们）；没接住的异常也写进了日志。"""
    script = """
import logging, os, sys
from pathlib import Path
from harness import log
log.SERVICE_LOG_BYTES = 400
os.close(1)
os.close(2)
sys.stdout = sys.stderr = None
log.setup(file=Path(sys.argv[1]), own_stdio=True)
logger = logging.getLogger("harness.test")
for n in range(60):
    logger.info("第 %d 行：写满了就换一个新文件", n)
print("print 写到标准输出")
os.write(2, "直接写到 2 号\\n".encode())
raise RuntimeError("最后没接住的异常")
"""
    path = tmp_path / "logs" / "service.log"
    done = subprocess.run([sys.executable, "-P", "-c", script, str(path)], cwd=ROOT, env=_clean_env(tmp_path), capture_output=True, timeout=60, check=False)
    assert done.returncode == 1
    names = sorted(item.name for item in path.parent.iterdir())
    assert names == ["service.log", "service.log.1", "service.log.2", "service.log.3"], names
    text = path.read_text(encoding="utf-8")
    assert "第 59 行" in text and "print 写到标准输出" in text and "直接写到 2 号" in text
    assert "RuntimeError: 最后没接住的异常" in text
    assert "第 59 行" not in (path.parent / "service.log.1").read_text(encoding="utf-8")


# ---- 命令行、其它 ---------------------------------------------------------------


def test_serve_accepts_taskscheduler(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "cli-data"))
    seen = {}
    monkeypatch.setattr("harness.serve.run_serve", lambda config, **options: seen.update(options) or 0)
    assert cli.main(["serve", "--web", "--service", "taskscheduler"]) == 0
    assert seen == {"web": True, "port": None, "service": "taskscheduler"}
    assert "taskscheduler" in SERVICES


def test_command_output_is_decoded_whatever_the_code_page():
    assert base.decode("成功: 已创建".encode()) == "成功: 已创建"
    text = base.decode("错误".encode("gbk"))  # 中文 Windows 的 OEM 代码页：不是 UTF-8 也不会出错
    assert isinstance(text, str) and text


def test_main_dispatches_to_task_scheduler(win, monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "cli-data"))
    monkeypatch.setattr(service_cli, "detect", lambda: win.system)
    assert cli.main(["service", "status"]) == 3
    assert "智狐的后台服务（任务计划程序）：还没有装上" in said(win.system)[0]


def test_open_works_the_same_on_windows(win):
    service_cli.run(args_for("install"), win.config, win.system)
    opener = Opener()
    system = make_system(win.manager, win.home, clock=win.clock, opener=opener)
    assert service_cli.run(args_for("open"), win.config, system) == 0
    assert opener.urls == [URL] and TOKEN not in "".join(said(system))


@pytest.mark.skipif(os.name == "nt", reason="Windows 上的文件权限不是这样表示的")
def test_the_task_file_is_private(win):
    service_cli.run(args_for("install"), win.config, win.system)
    assert has_mode(win.manager.file, 0o600)
