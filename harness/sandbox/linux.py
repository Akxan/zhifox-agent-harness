"""Linux 的沙箱：bubblewrap（命令叫 bwrap）。

Linux 内核有"命名空间"（namespace）：一个进程可以有自己的一套挂载点（它看到的文件系统）、自己的进程编号、自己的网络。
容器（Docker、Flatpak）就是这么做的。bwrap 是 Flatpak 用的那个小程序，它不需要 root：
从一个空的文件系统开始，只把我们点名的目录"挂"进去（--ro-bind 只读、--bind 能读写），别的地方在沙箱里根本不存在。

参数怎么写（见 arguments）：
  --unshare-pid           自己的进程编号：看不到、也杀不掉沙箱外面的进程（包括智狐自己）
  --unshare-net           只有"只在工作目录里"那一档才有：自己的一套网络，里面只有一个空的 localhost，
                          所以连外网连不上，这台电脑上监听 127.0.0.1 的服务（数据库、Docker、智狐自己）也连不上；
                          沙箱里自己开的服务，沙箱里的命令照样能连
  --die-with-parent       智狐退出了，沙箱里的东西一起结束
  --proc /proc、--dev /dev 沙箱自己的 /proc（只有沙箱里的进程）和一个最小的 /dev
  --tmpfs /tmp            一个空的 /tmp，命令结束就没了
  --ro-bind 系统目录       /usr、/etc、/opt……只读；/bin、/lib 这些在新的系统上是指向 /usr 的链接，照样做成链接
  --bind 工作目录          能读写的；--ro-bind-try 你加的只读的路径（不存在的跳过）
  --ro-bind 同一个路径      在能读写的地方里也只能读的（智狐自己的程序……）：同一个地方再挂一次，只读的盖在上面
  --tmpfs / /dev/null 盖住  永远碰不到的（data/、.env、~/.ssh）：文件夹盖上一个空的、只读的，文件换成 /dev/null。
                          后挂的盖住先挂的，所以这几条写在最后。一个在另一个里面的已经由 policy.build 去掉了
                          （只读的 tmpfs 里建不了挂载点）；不存在的只盖 policy.guard 里的那几个（见下面 arguments）

在沙箱里，看不到的地方是"不存在"（No such file or directory），能读不能写的地方是"Read-only file system"。

没装 bwrap 的话，沙箱"用不了"（见 problem），命令被拒绝执行，给出各个发行版的安装命令。我们不替你装。
装了却建不了沙箱（有的系统关掉了不用 root 建用户命名空间，比如 Ubuntu 24.04 默认用 AppArmor 限制它），也一样说清楚原因。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from ..config import SANDBOX_PROBE_SECONDS, SANDBOX_PROBE_TTL
from ..i18n import Message, k, t
from .policy import Policy, inside

LABEL = k("Linux 的 bubblewrap（bwrap）")
NOTE = k("沙箱里看不到的地方是“不存在”；有自己的一个空的 localhost：里面开的服务能连，这台电脑上的连不上。")
INSTALL = k("装上 bubblewrap：Ubuntu、Debian 上是 sudo apt install bubblewrap，Fedora 上是 sudo dnf install bubblewrap，Arch 上是 sudo pacman -S bubblewrap。装好以后不用重启智狐。")
# 运行程序要用的系统目录（只读）。/home/linuxbrew/.linuxbrew 是 Linux 上的 Homebrew
SYSTEM_READ = ("/usr", "/etc", "/opt", "/nix", "/snap", "/home/linuxbrew/.linuxbrew")
# 新的发行版上是指向 /usr 里面的链接（merged /usr），旧的是真的目录
SYSTEM_LINKS = ("/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
SYMPTOMS = (
    "Read-only file system", "No such file or directory", "Permission denied", "Operation not permitted",
    "Could not resolve host", "Temporary failure in name resolution", "Name or service not known", "Network is unreachable",
    "Couldn't connect to server", "Could not connect to server", "Failed to connect",  # curl 新旧版本的两种说法
    "Connection refused",  # 沙箱有自己的一个空的 localhost：连本机的服务是"拒绝连接"
)


def bwrap() -> str | None:
    return shutil.which("bwrap")


def arguments(policy: Policy) -> list[str]:
    """policy 写成 bwrap 的参数（不含程序本身和最后的 --）。"""
    args = ["--die-with-parent", "--unshare-pid", "--unshare-ipc"]
    if not policy.network:
        args.append("--unshare-net")
    args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for name in SYSTEM_LINKS:
        place = Path(name)
        if place.is_symlink():
            args += ["--symlink", os.readlink(place), name]
        elif place.is_dir():
            args += ["--ro-bind", name, name]
    for name in SYSTEM_READ:
        if Path(name).exists():
            args += ["--ro-bind", name, name]
    if policy.network:
        # /etc/resolv.conf 常常是指向 /run 里的链接（systemd-resolved），/run 不挂进来，那个文件要单独挂上，不然查不了域名
        resolv = Path(os.path.realpath("/etc/resolv.conf"))
        if resolv.is_file() and not inside(resolv, [Path("/etc"), Path("/usr")]):
            args += ["--ro-bind", str(resolv), str(resolv)]
    for path in policy.read:
        args += ["--ro-bind-try", str(path), str(path)]
    for path in policy.write:
        args += ["--bind" if path == policy.workspace else "--bind-try", str(path), str(path)]
    writable = [*policy.write]
    visible = [*writable, *policy.read]
    for path in policy.readonly:
        if path.exists() and inside(path, writable):
            args += ["--ro-bind", str(path), str(path)]
    for path in policy.deny:
        if not inside(path, visible):
            continue  # 本来就看不到
        if path.is_dir():
            args += ["--tmpfs", str(path), "--remount-ro", str(path)]
        elif path.exists():
            args += ["--ro-bind", "/dev/null", str(path)]
        elif path in policy.guard and inside(path.parent, writable):
            # 不存在、却在能写的地方（比如还没有 .env）：照样盖上，不然命令可以新建一个 .env，智狐下次启动时会读它。
            # bwrap 会在那个位置留下一个空的挂载点：文件夹（数据目录、~/.ssh）留一个空文件夹，别的留一个空文件
            # （空的 .env 和没有 .env 是一回事）。拿文件占文件夹的位置的话，以后就建不了那个文件夹了。
            # 只有 policy.guard 里的这样做：家目录里别的放密钥的地方不存在时不盖，里面本来就没有东西可读，
            # 盖上会在你真正的家目录里留下一个只读的空文件（以后 npm login 写不了 .npmrc，空的 settings.xml 让 Maven 报错，
            # 见 secret_places.py 开头）。代价：家目录整个能写时，命令能新建它们（和上面 policy.readonly 里开终端时运行的文件一样：
            # 不存在的也不盖）
            if path in policy.folders:
                args += ["--tmpfs", str(path), "--remount-ro", str(path)]
            else:
                args += ["--ro-bind", "/dev/null", str(path)]
    # 从哪个目录开始：上一条命令 cd 到的地方（见 tools/cwd.py）。沙箱里看不到的目录到不了这里（Sandbox.reaches 先挡下了），
    # 不然 bwrap 进不去它，命令一条都运行不了
    args += ["--chdir", str(policy.start)]
    return args


def command(policy: Policy, argv: list[str]) -> list[str]:
    program = bwrap() or "bwrap"
    return [program, *arguments(policy), "--", *argv]


_probe_lock = threading.Lock()
_probe: tuple[float, Message | None] | None = None


def problem() -> Message | None:
    """这台电脑上 bwrap 能不能用：没装、装了建不了沙箱，都返回原因；能用返回 None。结果记一会儿（config.SANDBOX_PROBE_TTL）。"""
    global _probe
    with _probe_lock:
        if _probe is not None and time.monotonic() - _probe[0] < SANDBOX_PROBE_TTL:
            return _probe[1]
        found = _try()
        _probe = (time.monotonic(), found)
        return found


def _try() -> Message | None:
    program = bwrap()
    if program is None:
        return t("这台电脑上没有装 bubblewrap（bwrap）。{install}", install=t(INSTALL))
    # 用和真正运行时一样的几样（自己的进程编号、自己的网络、最小的 /dev 和 /proc）试一下
    probe = [program, "--die-with-parent", "--unshare-pid", "--unshare-net", "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "true"]
    try:
        done = subprocess.run(probe, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", timeout=SANDBOX_PROBE_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return t("没能运行 bwrap：{error}", error=str(exc) or type(exc).__name__)
    if done.returncode != 0:
        error = (done.stderr or done.stdout).strip()[:300] or t("退出码 {code}", code=done.returncode)
        return t(
            "装了 bubblewrap，但这台电脑不让它建沙箱：{error}。常见的原因是系统不许普通用户建“用户命名空间”"
            "（Ubuntu 24.04 起由 AppArmor 限制；Debian 上看 kernel.unprivileged_userns_clone）。",
            error=error,
        )
    return None


def forget() -> None:
    global _probe
    with _probe_lock:
        _probe = None
