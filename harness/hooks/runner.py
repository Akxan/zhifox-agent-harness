"""运行一个钩子的命令：启动、把 JSON 写进它的标准输入、收它的输出、到时间还没完就结束掉。

几条规矩（和 stdio 的 MCP 服务器一样，见 mcp/stdio.py）：
  - 命令用参数列表交给系统（["python3", "check.py"]），不经过 shell：参数里有什么字符都不会被当成命令的一部分；
  - 在工作目录里运行（和 run_command 一样），相对路径的程序（./hooks/check.sh）也按工作目录找；
  - 环境里去掉各家模型的密钥和聊天机器人的 token（tools/shell.py 的 SECRET_ENV_VARS），另外加上几个告诉它
    "这是哪个钩子、什么事件"的变量（HARNESS_HOOK_EVENT、HARNESS_HOOK_NAME、HARNESS_PROJECT_DIR）；
  - 放在单独的进程组里：超时、你点了"停止"时整组一起结束，它启动的子进程不会留在后台；
  - 标准输入、输出、错误各用一个线程读写：一个不读标准输入、或者一口气打印很多的钩子，不会把我们卡住。
    输出各最多收 config.HOOK_OUTPUT_BYTES，再多的读出来扔掉。

和系统有关的只有两处（怎么放进单独的进程组、怎么整组结束），写在 _group 和 _kill_tree 里。
命令真正启动的地方只有 spawn 一个函数：以后 run_command 的沙箱合进来，钩子的命令也从这里交给它。
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import HOOK_OUTPUT_BYTES
from ..i18n import t
from ..safe import clean_text
from ..tools.shell import SECRET_ENV_VARS

DRAIN_WAIT_SECONDS = 1.0  # 程序退出以后，再等这么久把输出读完（它启动的后台进程可能还拿着管道，不能一直等）


@dataclass
class Ran:
    """一次运行的经过。exit 是退出码；没有自己退出（启动不了、超时、被停下）的是 None。"""

    exit: int | None
    stdout: str
    stderr: str
    seconds: float
    failure: str = ""  # "" 正常退出了 / start 启动不了 / timeout 超时 / stopped 你点了停止
    problem: str = ""  # 启动不了的原因（t() 译好的，给人看）


def hook_env(event: str, name: str, workspace: Path) -> dict[str, str]:
    """钩子拿到的环境变量：你的环境（PATH、HOME 这些它多半要用），去掉智狐自己的密钥，再加上几个说明。"""
    env = {key: value for key, value in os.environ.items() if key.upper() not in SECRET_ENV_VARS}
    env.update(HARNESS_HOOK_EVENT=event, HARNESS_HOOK_NAME=name, HARNESS_PROJECT_DIR=str(workspace))
    return env


def argv_of(command: str, args: list[str], workspace: Path) -> list[str]:
    """要交给系统的参数列表。~ 开头的程序换成家目录；写成相对路径的（带 / 的）按工作目录找。光写名字的（python3）按 PATH 找。"""
    program = os.path.expanduser(command)
    if not os.path.isabs(program) and ("/" in program or os.sep in program):
        program = str(workspace / program)
    return [program, *args]


def _group() -> dict:
    """新进程单独放进一个进程组的写法（系统相关）。POSIX 是新的会话；Windows 是 CREATE_NEW_PROCESS_GROUP。"""
    if hasattr(os, "killpg"):
        return {"start_new_session": True}
    return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}


def _kill_tree(process: subprocess.Popen) -> None:
    """连同它启动的子进程一起结束（系统相关）。"""
    try:
        if hasattr(os, "killpg"):
            os.killpg(process.pid, signal.SIGKILL)
        else:  # Windows：taskkill /T 连子进程一起结束；参数列表、不经过 shell
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True, timeout=10, check=False)
            process.kill()
    except (ProcessLookupError, PermissionError, OSError, subprocess.SubprocessError):
        pass


def spawn(argv: list[str], cwd: Path, env: dict[str, str]) -> subprocess.Popen:
    """启动钩子的命令。只有这一处真的启动它（见文件开头）。"""
    return subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **_group(),
    )


class _Drain:
    """在一个线程里把一个管道读完：前 limit 个字节留下，后面的读出来扔掉（不读的话程序写满管道就卡住了）。"""

    def __init__(self, pipe, limit: int = HOOK_OUTPUT_BYTES):
        self._pipe = pipe
        self._limit = limit
        self._chunks: list[bytes] = []
        self._kept = 0
        self.more = False  # 有没有扔掉的
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        try:
            while chunk := self._pipe.read1(65536):
                room = self._limit - self._kept
                if room > 0:
                    self._chunks.append(chunk[:room])
                    self._kept += min(room, len(chunk))
                if len(chunk) > max(room, 0):
                    self.more = True
        except (OSError, ValueError):
            pass

    def text(self, wait: float) -> str:
        self._thread.join(wait)
        data = b"".join(self._chunks)
        return clean_text(data.decode("utf-8", errors="replace"))


def _feed(pipe, payload: bytes) -> None:
    """把 JSON 写进它的标准输入，然后关上（它读到结尾就知道写完了）。它不读、提前退出了也没关系。"""
    try:
        pipe.write(payload)
    except (OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()
        except (OSError, ValueError):
            pass


class Launcher:
    """一次运行（你的对话、每一次定时任务、每个聊天）各有一个：记着正在运行的那个钩子，"停止"只结束它（和 Shell 一样）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._running: subprocess.Popen | None = None
        self._stopped = False

    def run(self, argv: list[str], payload: bytes, cwd: Path, env: dict[str, str], timeout: float) -> Ran:
        began = time.monotonic()
        try:
            process = spawn(argv, cwd, env)
        except FileNotFoundError:
            return Ran(None, "", "", time.monotonic() - began, "start", t("找不到命令“{command}”。检查一下拼写，或者写出完整的路径。", command=argv[0]))
        except PermissionError:
            return Ran(None, "", "", time.monotonic() - began, "start", t("没有权限运行“{command}”。", command=argv[0]))
        except OSError as exc:
            return Ran(None, "", "", time.monotonic() - began, "start", t("启动“{command}”失败：{error}", command=argv[0], error=exc))
        with self._lock:
            self._running, self._stopped = process, False
        out, err = _Drain(process.stdout), _Drain(process.stderr)
        threading.Thread(target=_feed, args=(process.stdin, payload), daemon=True).start()
        failure = ""
        try:
            process.wait(timeout)
        except subprocess.TimeoutExpired:
            failure = "timeout"
            _kill_tree(process)
            process.wait()
        except KeyboardInterrupt:  # 终端里按了 Ctrl-C：结束它，照常往上抛（和 run_command 一样）
            _kill_tree(process)
            process.wait()
            raise
        finally:
            with self._lock:
                self._running = None
                stopped = self._stopped
        seconds = time.monotonic() - began
        if stopped:
            failure = "stopped"
        stdout, stderr = out.text(DRAIN_WAIT_SECONDS), err.text(DRAIN_WAIT_SECONDS)
        return Ran(None if failure else process.returncode, stdout, stderr, seconds, failure)

    def stop(self) -> None:
        """结束正在运行的钩子（如果有）。网页上的"停止"、定时任务和聊天通道的停止都会调用它。"""
        with self._lock:
            process = self._running
            if process is not None:
                self._stopped = True
        if process is not None:
            _kill_tree(process)
