"""测试用的假沙箱后端：不建真的沙箱，只把"本该在沙箱里运行的命令"记下来，命令照原样运行。

conftest 默认把 sandbox.backend.detect 换成它（能用）。要测"这台电脑上用不了"的，把 problem 设成一句话；
真的沙箱在 tests/test_sandbox_macos.py（这台 Mac 上的 sandbox-exec）和 tests/test_sandbox_linux.py（CI 里装的 bwrap）里测。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from harness.i18n import Message, t
from harness.sandbox.backend import Backend
from harness.sandbox.policy import Policy


@dataclass
class FakeSandbox:
    problem: str = ""  # 不是空的：这台"电脑"上沙箱用不了，原因是这句话
    calls: list[tuple[Policy, list[str]]] = field(default_factory=list)
    symptoms: tuple[str, ...] = ("Operation not permitted",)

    def backend(self) -> Backend:
        def problem() -> Message | None:
            return t(self.problem) if self.problem else None

        def command(policy: Policy, argv: list[str]) -> list[str]:
            self.calls.append((policy, list(argv)))
            return list(argv)

        return Backend("fake", "假沙箱", "", problem, command, self.symptoms)


def install(monkeypatch, problem: str = "") -> FakeSandbox:
    from harness.sandbox import backend as sandbox_backend

    fake = FakeSandbox(problem=problem)
    monkeypatch.setattr(sandbox_backend, "detect", fake.backend)
    return fake
