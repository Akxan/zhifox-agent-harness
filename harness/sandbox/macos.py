"""macOS 的沙箱：sandbox-exec，加上一份现写的配置（SBPL，Sandbox Profile Language）。

macOS 自己就有一套沙箱（Seatbelt），App Store 里的程序、Safari、系统里的很多服务都关在里面。
/System/Library/Sandbox/Profiles 里的 .sb 文件就是系统服务用的配置，我们的写法照着它们学
（开头的 (import "system.sb") 就是借用那里面"一个普通程序起码要能做的事"）。
sandbox-exec 是命令行上用它的办法：sandbox-exec -p 配置 程序 参数……，那个程序和它开的所有子进程都在沙箱里，出不来。

苹果在 sandbox-exec 的说明（man sandbox-exec）里写了它已经"过时"（deprecated），不建议新程序用，但它在现在的 macOS 上
照样能用，系统自己的服务也还在用同一套机制。将来哪一版 macOS 真的去掉了它，这里会说"用不了"（见 problem），
命令会被拒绝执行，而不是悄悄在沙箱之外运行。

配置怎么写（见 profile）：
  (deny default)           先什么都不许，再一样一样放开。没写到的（打开别的程序、发 Apple Events 控制别的程序、
                           读钥匙串、连 Unix 套接字……）都做不了
  (import "system.sb")     一个程序起码要能做的：加载系统的库、读 /dev/null、查时区……
  file-read-metadata       看一眼文件在不在、多大，不读内容：展开路径、找程序都要用
  file-read*               能读的：系统目录、你加的只读的路径
  file-read* file-write*   能读写的：工作目录、这次运行的 HOME 和临时文件夹、你加的可读写的路径
  (deny file-write* …)     在能读写的地方里也只能读的（智狐自己的程序……）
  (deny file-read* …)      永远碰不到的（data/、.env、~/.ssh……）。写在最后：同一个操作有好几条规则时，后面的说了算
  network*                 只有"工作目录 + 联网"那一档才有：能连 IP 地址（包括 127.0.0.1）、能查域名；Unix 套接字还是不行

路径不写进配置的文字里，而是用参数（-D 名字=路径，配置里写 (param "名字")）：路径里有引号、括号、反斜杠、换行都不会
把配置弄坏，也就没有"转义没写对，一个奇怪的文件名就能改掉规则"这种事。

挡住了会怎样：被挡住的操作得到 EPERM（"Operation not permitted"），程序照常报错；不能联网时是"Could not resolve host"、
"Couldn't connect to server"。sudo、ps 这种带特殊权限（setuid）的程序在沙箱里运行不了。
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from ..config import SANDBOX_PROBE_SECONDS, SANDBOX_PROBE_TTL
from ..i18n import Message, k, t
from .policy import Policy

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
LABEL = k("macOS 的 sandbox-exec")
NOTE = k("苹果已经把 sandbox-exec 标成“过时”，但它在现在的 macOS 上照样能用；哪天用不了了，命令会被拒绝执行，不会悄悄在沙箱之外运行。")
# 运行程序要用的系统目录（只读）。/usr 里包括 /usr/local（Intel Mac 上的 Homebrew）；/opt/homebrew 是 Apple 芯片上的 Homebrew，
# /opt/local 是 MacPorts，/nix 是 Nix。/Library 和 /Applications 是整台电脑共用的（不是你自己的 ~/Library）：
# Xcode 的命令行工具（git、clang）在 /Library/Developer 下面。/private/etc 是 /etc，/private/var/select 里是 /bin/sh 指向谁
SYSTEM_READ = (
    "/System", "/usr", "/bin", "/sbin", "/Library", "/Applications", "/private/etc", "/private/var/db", "/private/var/select",
    "/opt/homebrew", "/opt/local", "/nix", "/dev",
)
# 在沙箱里被挡住时，命令的输出里常见的几句话（见 Sandbox.explain）
SYMPTOMS = (
    "Operation not permitted", "Could not resolve host", "nodename nor servname", "Network is unreachable",
    "Couldn't connect to server", "Could not connect to server", "Failed to connect",  # curl 新旧版本的两种说法
)

_BASE = """\
(version 1)
(deny default)
(import "system.sb")
(allow process-fork)
(allow process-exec)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))
(allow file-read-metadata)
(allow file-write-data (literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty"))
"""
_NETWORK = """\
(system-network)
(allow network-outbound (literal "/private/var/run/mDNSResponder"))
(allow network-outbound (remote ip))
(allow network-inbound (local ip))
(allow network-bind (local ip))
"""


def profile(policy: Policy) -> tuple[str, dict[str, str]]:
    """policy 写成 SBPL 配置：返回配置的文字和要用 -D 交给 sandbox-exec 的参数（名字 → 真实路径）。

    一组路径一条规则。一组里一个路径都没有时，不写这条规则：(allow file-read*) 后面什么都不跟，意思是"哪儿都能读"。"""
    params: dict[str, str] = {}

    def group(prefix: str, paths: tuple[Path, ...] | list[Path]) -> str:
        names = []
        for index, path in enumerate(paths):
            name = f"{prefix}{index}"
            params[name] = str(path)
            names.append(f'(subpath (param "{name}"))')
        return " ".join(names)

    lines = [_BASE]
    system = [Path(path) for path in SYSTEM_READ if Path(path).exists()]
    for operations, prefix, paths in (
        ("allow file-read* file-map-executable", "SYSTEM_", system),
        ("allow file-read* file-map-executable", "READ_", policy.read),
        ("allow file-read* file-write* file-map-executable", "WRITE_", policy.write),
        ("deny file-write*", "READONLY_", policy.readonly),
        ("deny file-read* file-write*", "DENY_", policy.deny),
    ):
        filters = group(prefix, paths)
        if filters:
            lines.append(f"({operations} {filters})\n")
    if policy.network:
        lines.append(_NETWORK)
    return "".join(lines), params


def command(policy: Policy, argv: list[str]) -> list[str]:
    """在沙箱里运行 argv 的完整命令（参数列表，不经过 shell）。"""
    text, params = profile(policy)
    return [SANDBOX_EXEC, "-p", text, *(f"-D{name}={value}" for name, value in params.items()), *argv]


_probe_lock = threading.Lock()
_probe: tuple[float, Message | None] | None = None


def problem() -> Message | None:
    """这台 Mac 上 sandbox-exec 能不能用：能用返回 None，不能用返回原因（给人看的，也会写进给模型的拒绝里）。

    不只看文件在不在：真的建一个什么都不做的沙箱试一下。智狐自己已经在一个沙箱里运行时（比如从别的沙箱里启动），
    再建沙箱会失败（"sandbox_apply: Operation not permitted"）。结果记一会儿（见 config.SANDBOX_PROBE_TTL）。"""
    global _probe
    with _probe_lock:
        if _probe is not None and time.monotonic() - _probe[0] < SANDBOX_PROBE_TTL:
            return _probe[1]
        found = _try()
        _probe = (time.monotonic(), found)
        return found


def _try() -> Message | None:
    if not Path(SANDBOX_EXEC).is_file():
        return t("这台 Mac 上找不到 {path}（可能是 macOS 去掉了它）。", path=SANDBOX_EXEC)
    try:
        done = subprocess.run(
            [SANDBOX_EXEC, "-p", "(version 1)\n(allow default)\n", "/usr/bin/true"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", timeout=SANDBOX_PROBE_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return t("没能运行 sandbox-exec：{error}", error=str(exc) or type(exc).__name__)
    if done.returncode != 0:
        error = (done.stderr or done.stdout).strip()[:300] or t("退出码 {code}", code=done.returncode)
        return t("sandbox-exec 建不了沙箱：{error}（智狐自己是不是已经在别的沙箱里运行？）", error=error)
    return None


def forget() -> None:
    """忘掉上一次试的结果（测试用）。"""
    global _probe
    with _probe_lock:
        _probe = None
