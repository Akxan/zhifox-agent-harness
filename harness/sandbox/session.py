"""一次运行的沙箱：这次运行用哪一档、能不能请人放行一条命令、在哪儿放 HOME 和临时文件。

和浏览器、操作整台电脑一样，"一次运行"是：你正在看的这段对话（网页和终端）、定时任务的每一次运行、聊天通道里的每个聊天。
每次运行的 Shell 带着自己的一个 Sandbox（见 tools/shell.py），叫出来的子 Agent 用同一个：子 Agent 只会更少，不会更多。

一条命令怎么走：
  check（tools/shell.py 的 check → refusal）
      沙箱开着、这台电脑上却用不了（没装 bwrap、Windows……）：直接拒绝，告诉模型原因和怎么办，不弹确认卡片。
      绝不悄悄在沙箱之外运行——你以为它在沙箱里，它其实不在，比没有沙箱更糟。
      要在沙箱之外运行（unsandboxed），这次运行却不许请人放行：同样直接拒绝。
  review（permissions.py）
      照常问不问你，卡片上多一行"沙箱：只在工作目录里（……）"。要在沙箱之外运行的，在任何一档下都问（见下面）。
  run（enter）
      把命令包进沙箱（macOS 的 sandbox-exec、Linux 的 bwrap），HOME 和临时文件夹换成这次运行自己的。
      命令失败了、输出里像是被沙箱挡住的（"Operation not permitted"……），在结果后面补一段说明给模型看（explain）。

在沙箱之外运行一条命令（模型给 run_command 加上 unsandboxed: true）。规矩是用户定的方向，具体这样做：
  - 只有人能放行：这一条在任何一档下都要问你，包括"全部自动允许"，卡片上不给"不想每次都问？"（permissions.always）。
    没有"这段对话里不再问"：每一条都单独看。
  - 你正看着的对话（网页、终端）：可以请你放行。
  - 定时任务、聊天通道（没人看着的运行）：看它们自己的那一档。选了"全部自动允许"的，你说过"别问我"，
    多半也没人会来回答——请你放行要么让它一直等着，要么让你习惯不看就点；所以直接拒绝，沙箱照样挡着。
    选了别的几档的（每次都问、只读的命令不用问、改文件不用问），本来就会问你，就照样问你这一条。
  - 沙箱关着的时候，unsandboxed 没有作用，命令照常按"执行命令"来问。

为什么这样定：沙箱是第二道关卡，最要紧的是"被网页里的一句话带偏的模型，自己出不了沙箱"。
放行只能由人在卡片上点，而且是看得清清楚楚的一条命令；在你说过"别问我"的运行里，干脆没有这扇门。
"""

from __future__ import annotations

import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..i18n import Message, fill, k, t
from . import backend as backends
from .policy import (
    DEFAULT,
    LEVEL_SHORT,
    LEVELS,
    Policy,
    build,
    inside,
    program_paths,
    real,
    resolve,
)

# 一次运行是哪一种（卡片上、给模型的话里怎么称呼它）
SCOPES = {"chat": k("这段对话"), "task": k("这个定时任务"), "channel": k("这个聊天通道")}
_SCOPES_ZH = {"chat": "这段对话", "task": "这个定时任务", "channel": "这个聊天通道"}
# 写给模型看的、每一档在沙箱里是什么样（中文，不进翻译表）
_INSIDE = {
    "workspace": "只在工作目录里：能读写工作目录，能读运行程序要用的系统目录；读不到工作目录之外的个人文件（家目录里别的东西、别的硬盘、/tmp），"
    "碰不到智狐自己的数据目录和 .env；不能联网，连 127.0.0.1 上的服务也连不上；HOME 和 TMPDIR 是这次运行自己的临时文件夹",
    "network": "工作目录 + 联网：能读写工作目录，能读运行程序要用的系统目录，能联网；读不到工作目录之外的个人文件（家目录里别的东西、别的硬盘、/tmp），"
    "碰不到智狐自己的数据目录和 .env；连不上 Unix 套接字（Docker、ssh-agent）；HOME 和 TMPDIR 是这次运行自己的临时文件夹",
}


@dataclass(frozen=True)
class Layout:
    """和沙箱有关的几个地方，App 组装时给一次，每次运行共用。extra 和 default 每条命令执行前读一次：设置改了马上算数。"""

    workspace: Path
    private: tuple[Path, ...]  # 智狐自己的数据目录
    protected: tuple[Path, ...]  # 文件工具保护的文件（.env、mcp.json、channels.json……）
    extra: Callable[[], list[dict]]  # 设置里"额外允许的路径"
    default: Callable[[], str]  # 设置里选的那一档
    home: Callable[[], Path] = Path.home  # 你真正的家目录（测试里换成临时目录）


@dataclass(frozen=True)
class Facts:
    """一次调用在沙箱这件事上是什么情况（问你之前看一眼，permissions.py 照着它写卡片）。"""

    level: str  # 这次运行实际用的那一档：off / workspace / network
    default: str  # 设置里的那一档
    scope: str  # chat / task / channel
    unsandboxed: bool = False  # 模型要这一条在沙箱之外运行（而且沙箱开着）
    escape: bool = False  # 这次运行能不能请人放行
    problem: Message | None = None  # 沙箱开着、这台电脑上却用不了（那样的话命令会被拒绝）

    @property
    def on(self) -> bool:
        return self.level != "off"

    @property
    def escapes(self) -> bool:
        """这一条要在沙箱之外运行、而且允许请人放行：在任何一档下都要问。"""
        return self.on and self.unsandboxed and self.escape


@dataclass
class Jail:
    """一条命令进了沙箱以后：用什么命令行、什么环境变量运行，被挡住时怎么跟模型说。"""

    argv: list[str]
    env: dict[str, str]
    level: str
    symptoms: tuple[str, ...]
    advice: str = field(default="")  # 被挡住以后能怎么办（写给模型看的）

    def explain(self, output: str) -> str:
        """命令失败了：输出里像是被沙箱挡住的，返回要补在结果后面的那段说明；不像的返回空字符串。"""
        if not any(symptom in output for symptom in self.symptoms):
            return ""
        return f"\n[这条命令是在沙箱里运行的（{_INSIDE[self.level]}）。上面的错误看起来是沙箱挡住的。{self.advice}]"


def _zh(message: object) -> str:
    """一句 t() 给的话的中文原文（连里面填进去的那几句一起）：写给模型看的话用中文。"""
    if not isinstance(message, Message):
        return str(message)
    values = {name: _zh(message.nested[name]) if name in message.nested else value for name, value in message.values.items()}
    return fill(message.key, values)


class Sandbox:
    """一次运行的沙箱。own 是这次运行自己选的那一档（定时任务、通道的；对话是 DEFAULT，跟着设置），每次都重新读：
    你在页面上改了，下一条命令就按新的来。escape 说这次运行现在能不能请人放行（见文件开头）。"""

    def __init__(self, layout: Layout, own: Callable[[], str] = lambda: DEFAULT, escape: Callable[[], bool] = lambda: True, scope: str = "chat"):
        self.layout = layout
        self.own = own
        self.can_escape = escape
        self.scope = scope if scope in SCOPES else "chat"
        self._lock = threading.Lock()
        self._scratch: tempfile.TemporaryDirectory | None = None  # 这次运行的 HOME 和临时文件夹（用完自动删掉）

    def level(self) -> str:
        own = self.own()
        default = self.layout.default()
        if own == "off" or (own == DEFAULT and default == "off"):
            return "off"
        return resolve(own, default)

    def facts(self, unsandboxed: bool = False) -> Facts:
        level = self.level()
        on = level != "off"
        return Facts(
            level=level,
            default=self.layout.default() if self.layout.default() in LEVELS else "workspace",
            scope=self.scope,
            unsandboxed=on and unsandboxed,
            escape=on and self.can_escape(),
            problem=backends.detect().problem() if on else None,
        )

    def refusal(self, unsandboxed: bool = False) -> str | None:
        """这条命令根本不会去运行的原因（写给模型看的中文）；会照常运行（问过你以后）返回 None。"""
        facts = self.facts(unsandboxed)
        if not facts.on:
            return None
        who, level = _SCOPES_ZH[facts.scope], _zh(t(LEVELS[facts.level]))
        if facts.unsandboxed:
            if facts.escape:
                return None
            return (
                f"命令没有执行：{who}不能在沙箱之外运行命令。它是在没人看着的时候自己运行的，选的是“全部自动允许”，"
                f"没有人会来回答“要不要放行”，所以沙箱（{level}）照样挡着。请换一个在沙箱里就能做的办法；"
                "确实做不到的话，把原因告诉用户，请他在这个定时任务（或者聊天通道）的设置里把沙箱改成“工作目录 + 联网”或者“关”。"
            )
        if facts.problem is not None:
            escape = (
                "如果这一条确实必须现在运行，可以把 unsandboxed 设为 true 再试一次，用户会被单独问一次要不要在沙箱之外运行。"
                if facts.escape else ""
            )
            return (
                f"命令没有执行：{who}的沙箱是“{level}”，可是这台电脑上用不了沙箱（{_zh(facts.problem)}）。"
                "智狐不会悄悄在沙箱之外运行命令。请把原因告诉用户：要么照着上面说的把沙箱装好、修好，"
                f"要么在设置（定时任务、聊天通道是它们自己的设置）里把沙箱改成“关”。{escape}"
            )
        return None

    def enter(self, argv: list[str], env: dict[str, str], unsandboxed: bool = False, cwd: Path | None = None) -> Jail | None:
        """把一条命令包进沙箱：返回包好的命令行和环境变量；沙箱关着、或者这一条已经请你放行在沙箱之外运行，返回 None。
        不能运行的（见 refusal）抛出 ToolError，什么都不会执行。cwd：命令从哪个目录开始（不给是工作目录，见 tools/cwd.py）。"""
        # 在这里才导入：导入 tools 会先运行 tools/__init__.py，它又要导入 tools/shell.py，而 shell.py 要用这个文件
        from ..tools.base import ToolError

        refusal = self.refusal(unsandboxed)
        if refusal:
            raise ToolError(refusal)
        facts = self.facts(unsandboxed)
        if not facts.on or facts.escapes:
            return None
        policy = self.policy(facts.level, cwd)
        found = backends.detect()
        advice = (
            "可以换一个只用工作目录里的文件的办法；如果确实需要碰工作目录之外的东西（或者联网），可以把 unsandboxed 设为 true 再执行一次，"
            "用户会被单独问一次要不要在沙箱之外运行这一条。"
            if facts.escape
            else f"{_SCOPES_ZH[facts.scope]}不能请用户放行（它选的是“全部自动允许”，没人会来回答）：请换一个在沙箱里就能做的办法，"
            "确实做不到的话，把原因告诉用户，请他在设置里改这个任务（或者通道）的沙箱。"
        )
        return Jail(argv=found.command(policy, argv), env=policy.env(env), level=facts.level, symptoms=found.symptoms, advice=advice)

    def policy(self, level: str, cwd: Path | None = None) -> Policy:
        home, temp = self._dirs()
        layout = self.layout
        return build(
            level=level,
            workspace=layout.workspace,
            extra=layout.extra(),
            private=layout.private,
            protected=layout.protected,
            home=home,
            temp=temp,
            real_home=layout.home(),
            program=program_paths(),
            cwd=cwd,
        )

    def reaches(self, path: Path) -> bool:
        """沙箱开着时，一条命令能不能从 path 这个目录开始：它得在沙箱里你给的地方（能读写的：工作目录、这次运行的 HOME 和
        临时文件夹、额外允许的路径；只读的额外路径也算，在那里只是写不了），而且不是永远碰不到的地方。沙箱关着时哪儿都行。

        为什么要问：上一条命令可能是在沙箱之外运行的（你放行的那一条，或者沙箱是后来才打开的），cd 到了沙箱里看不到的地方。
        bwrap 根本进不去那个目录，命令一条都运行不了（连 cd 回来都不行）；sandbox-exec 进得去，可是什么都读不了。
        所以那时回到工作目录（见 tools/cwd.py）。系统目录（/usr……）不算在内：只看你给的地方，规矩简单、两个系统一样。"""
        level = self.level()
        if level == "off":
            return True
        policy = self.policy(level)
        place = real(path)
        return inside(place, (*policy.write, *policy.read)) and not inside(place, policy.deny)

    def _dirs(self) -> tuple[Path, Path]:
        """这次运行自己的 HOME 和临时文件夹：第一条在沙箱里的命令要用时才建，这次运行里的命令共用。
        TemporaryDirectory 在这个 Sandbox 不再被用到（或者智狐退出）时把它整个删掉。"""
        with self._lock:
            if self._scratch is None or not Path(self._scratch.name).is_dir():
                self._scratch = tempfile.TemporaryDirectory(prefix="zhifox-sandbox-", ignore_cleanup_errors=True)
            root = Path(self._scratch.name)
        home, temp = root / "home", root / "tmp"
        for place in (home, temp):
            place.mkdir(mode=0o700, exist_ok=True)
        return home, temp


# ---- 卡片上的话（permissions.py 用）---------------------------------------------


def question(facts: Facts, command: object) -> str:
    """执行一条命令时卡片上的话：命令，加上一行沙箱的情况。沙箱从来没打开过的人看到的和以前一模一样。"""
    if facts.on:
        return t(
            "执行命令：\n\n    $ {command}\n\n沙箱：{level}（{short}）",
            command=command, level=t(LEVELS[facts.level]), short=t(LEVEL_SHORT[facts.level]),
        )
    if facts.default != "off":
        return t("执行命令：\n\n    $ {command}\n\n沙箱：关（{who}没有用沙箱，命令直接在这台电脑上运行）", command=command, who=t(SCOPES[facts.scope]))
    return t("执行命令：\n\n    $ {command}", command=command)


def escape_question(facts: Facts, command: object) -> str:
    """要在沙箱之外运行一条命令时，卡片上的话（permissions.py 会把它标成"在任何一档下都问"）。"""
    return t(
        "在沙箱之外执行命令（每次都会问）：\n\n    $ {command}\n\n"
        "⚠ 这一条不在沙箱里运行：它能读写你能读写的一切（家目录、密钥、别的硬盘），也能联网。{who}平时用的是“{level}”。"
        "看懂了这条命令、确定需要时才允许。",
        command=command,
        who=t(SCOPES[facts.scope]),
        level=t(LEVELS[facts.level]),
    )
