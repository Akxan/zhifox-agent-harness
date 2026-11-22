"""自检的结果：一项一项的检查（Check），按小节（Section）排好，合成一份报告（Report）。

每一项只有三种状态：
  ok       好
  problem  有问题：fix 里写着怎么修，按这台电脑的系统写（PowerShell 还是 zsh，见 fixes.py）
  unused   没用上：这个功能没配置、没打开，或者这台电脑上本来就没有。不算问题，退出码不受它影响

为什么"没用上"单独是一种：没装后台服务、没接聊天通道的人很多，把它们写成"有问题"，真正的问题就被淹没了；
写成"好"又不对（它确实没在工作）。用户最想先看的是"有问题"的那几行。

给人看的话都是 t() 译好的 Message（界面是哪种语言，就是哪种语言）。机器看的部分在 facts 里：
只放数字、开关、路径和名字，从不放密钥（连最后几位也不放）、口令和带口令的网址——用户会把 --json 的结果贴给别人看。

--json 的格式（format 是这份格式的版本，以后改了格式就加一）：

  {"format": 1, "app": {"version", "commit"}, "system": "mac" | "windows" | "linux", "language": "zh",
   "summary": {"ok": n, "problem": n, "unused": n},
   "sections": [{"id", "title", "checks": [{"id", "label", "status", "detail", "fix": [一行一句], "facts": {…}}]}]}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..i18n import k

OK, PROBLEM, UNUSED = "ok", "problem", "unused"
STATUSES = (OK, PROBLEM, UNUSED)
STATUS_TEXT = {OK: k("好"), PROBLEM: k("有问题"), UNUSED: k("没用上")}
FORMAT = 1


@dataclass
class Check:
    id: str  # 稳定的编号：python、data_dir、key.anthropic……（--json 里给程序认）
    label: str  # 这一项叫什么
    status: str  # OK / PROBLEM / UNUSED
    detail: str = ""  # 现在是什么样子，一句话
    fix: list[str] = field(default_factory=list)  # 有问题时怎么修，一行一句（命令按这台电脑的系统写）
    facts: dict[str, Any] = field(default_factory=dict)  # 给程序看的值：数字、开关、路径、名字。从不放密钥


@dataclass
class Section:
    id: str
    title: str
    checks: list[Check]


@dataclass
class Report:
    sections: list[Section]
    app: dict[str, str]  # 智狐的版本、git 提交
    system: str  # machine.system()
    language: str

    def checks(self) -> list[Check]:
        return [check for section in self.sections for check in section.checks]

    def count(self, status: str) -> int:
        return sum(1 for check in self.checks() if check.status == status)

    @property
    def exit_code(self) -> int:
        """有一项有问题就是 1，都好（或者没用上）是 0。"""
        return 1 if self.count(PROBLEM) else 0

    def to_dict(self) -> dict:
        return {
            "format": FORMAT,
            "app": dict(self.app),
            "system": self.system,
            "language": self.language,
            "summary": {status: self.count(status) for status in STATUSES},
            "sections": [
                {
                    "id": section.id,
                    "title": str(section.title),
                    "checks": [
                        {
                            "id": check.id,
                            "label": str(check.label),
                            "status": check.status,
                            "detail": str(check.detail),
                            "fix": [str(line) for line in check.fix],
                            "facts": check.facts,
                        }
                        for check in section.checks
                    ],
                }
                for section in self.sections
            ],
        }
