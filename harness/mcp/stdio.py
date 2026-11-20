"""stdio 传输：把 MCP 服务器当成一个子进程启动，消息走它的标准输入和标准输出。

这是本地 MCP 服务器最常见的用法（Claude Desktop 配置里的 "command" 就是这种）：

    智狐 ──写一行 JSON──► 服务器的 stdin
    智狐 ◄──读一行 JSON── 服务器的 stdout
                          服务器的 stderr：它自己的日志，我们只留着给人看（见 log.py）

每条消息占一行（JSON 里的换行都被转义成 \\n 了，所以一条消息里不会有真正的换行）。

几条安全上的规矩：
  - 命令用参数列表交给系统（["npx", "-y", "某个包"]），不经过 shell：参数里有什么字符都不会被当成命令的一部分。
  - 子进程拿不到各家模型的 API 密钥（和 run_command 一样，见 tools/shell.py），只拿到你给这个服务器配的环境变量。
  - 子进程放在单独的进程组里。关掉它时整组一起结束：npx 这类启动器会再启动真正的服务器进程，只结束外面那一层的话，
    里面那个会一直留在后台。
"""

from __future__ import annotations

import itertools
import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ..i18n import t
from ..safe import clean, tame, to_json
from ..tools.shell import SECRET_ENV_VARS
from . import jsonrpc
from .jsonrpc import McpError, RpcError, Slot
from .log import ServerLog

MAX_MESSAGE_BYTES = 64_000_000  # 一行最长这么多字节（工具结果里可能有几张图片）。更长的丢掉，免得一个出错的服务器把内存吃光
CLOSE_WAIT_SECONDS = 2  # 关掉 stdin 之后，等它自己退出的时间；过了就强制结束


class StdioTransport:
    name = "stdio"

    def __init__(self, command: str, args: list[str], env: dict[str, str], cwd: str | None, log: ServerLog):
        self.command, self.args, self.env, self.cwd = command, list(args), dict(env), cwd
        self.log = log
        self.modern = False  # 由 client.py 在弄清对面是哪一代之后设置；stdio 这一层用不上，只是和 http.py 保持一样
        self.protocol_version = ""
        # 三个回调由 client.py 设置。它们在读消息的线程里被调用，所以不能在里面等别的回答（会把自己卡死）
        self.on_notification: Callable[[dict], None] = lambda message: None
        self.on_request: Callable[[dict], dict] = lambda message: {}
        self.on_closed: Callable[[str], None] = lambda reason: None
        self._process: subprocess.Popen | None = None
        self._ids = itertools.count(1)
        self._pending: dict[object, Slot] = {}  # 还在等回答的请求，按 id
        self._lock = threading.Lock()  # 保护 _pending，并保证两条消息不会交错着写进 stdin
        self._closing = False

    # ---- 启动和关闭 ---------------------------------------------------------

    def start(self) -> None:
        if self.cwd and not Path(self.cwd).expanduser().is_dir():
            raise McpError(t("工作目录不存在：{path}", path=self.cwd))
        # 你的环境变量照常给它（PATH、HOME 这些服务器多半要用），只是去掉各家模型的密钥；再加上给这个服务器配的
        env = {name: value for name, value in os.environ.items() if name not in SECRET_ENV_VARS}
        env.update(self.env)
        try:
            self._process = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=Path(self.cwd).expanduser() if self.cwd else None,
                start_new_session=True,  # 单独的进程组，见文件开头的说明
            )
        except FileNotFoundError as exc:
            raise McpError(
                t("找不到命令“{command}”。检查一下拼写，或者写出完整的路径（在终端里运行 which {command} 可以查到）。", command=self.command)
            ) from exc
        except PermissionError as exc:
            raise McpError(t("没有权限运行“{command}”。", command=self.command)) from exc
        except OSError as exc:
            raise McpError(t("启动“{command}”失败：{error}", command=self.command, error=exc)) from exc
        self.log.note(t("已启动：{command}（进程 {pid}）", command=" ".join([self.command, *self.args]), pid=self._process.pid))
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None and not self._closing

    def close(self) -> None:
        """关掉服务器：先关上它的 stdin（规范里约定的"请退出"），等一会儿；还不退出就强制结束整个进程组。"""
        process = self._process
        if process is None or self._closing:
            return
        self._closing = True
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(CLOSE_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        # 主进程退出了，它启动的子进程也可能还在（npx 启动的 node 就是这样），所以不管怎样都对整组收一次尾
        _end_group(process.pid)
        try:
            process.wait(1)
        except subprocess.TimeoutExpired:
            pass
        self._fail_pending(McpError(t("连接已经关闭。")))

    # ---- 发消息 -------------------------------------------------------------

    def send(self, method: str, params: dict | None = None) -> Slot:
        """发出一个请求，返回等它回答的座位（不等）。"""
        with self._lock:
            id = next(self._ids)
            slot = Slot(id, method)
            self._pending[id] = slot
            self._write(jsonrpc.request(id, method, params))
        return slot

    def request(self, method: str, params: dict | None, timeout: float, stop: threading.Event | None = None, headers: dict | None = None) -> dict:
        """发出一个请求并等它的回答。headers 只有 HTTP 用得上，这里忽略。"""
        slot = self.send(method, params)
        try:
            return jsonrpc.wait(slot, timeout, stop)
        except BaseException:  # 超时、用户点了停止、按了 Ctrl-C：告诉服务器这个请求不用做了
            self.forget(slot, cancel=True)
            raise

    def forget(self, slot: Slot, cancel: bool) -> None:
        """不再等这个请求。cancel 为真时通知服务器：stdio 上只有这一个办法，没有"挂断某一个请求"这回事。"""
        with self._lock:
            waiting = self._pending.pop(slot.id, None) is not None
        if cancel and waiting and self.alive:
            try:
                self.notify("notifications/cancelled", {"requestId": slot.id, "reason": "cancelled by the user or timed out"})
            except McpError:
                pass

    def notify(self, method: str, params: dict | None = None) -> None:
        with self._lock:
            self._write(jsonrpc.notification(method, params))

    def listen(self, params: dict) -> None:
        """开一个长期的订阅（新一代协议的 subscriptions/listen），之后的变化通知会从 stdout 陆续到来。

        这个请求要一直开着，服务器只在它关掉订阅时才回答，所以只发不等。
        """
        self.send("subscriptions/listen", params)

    def _write(self, message: dict) -> None:
        """调用的人要先拿到 self._lock，两条消息才不会交错着写进去。"""
        process = self._process
        if process is None or process.stdin is None or self._closing:
            raise McpError(t("连接已经关闭。"))
        try:
            process.stdin.write(to_json(message).encode() + b"\n")  # to_json 只输出 ASCII，不会有真正的换行
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise McpError(t("和服务器的连接断了：{error}", error=exc)) from exc

    # ---- 收消息 -------------------------------------------------------------

    def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        stream, skipping = self._process.stdout, False
        while True:
            try:
                raw = stream.readline(MAX_MESSAGE_BYTES)
            except (OSError, ValueError):
                break
            if not raw:
                break
            if len(raw) >= MAX_MESSAGE_BYTES and not raw.endswith(b"\n"):
                if not skipping:
                    self.log.note(t("服务器发来一条超过 {mb} MB 的消息，没有处理。", mb=MAX_MESSAGE_BYTES // 1_000_000))
                skipping = True
                continue
            if skipping:  # 那条太长的消息的最后一截
                skipping = False
                continue
            self._handle_line(raw)
        self._ended()

    def _handle_line(self, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return
        try:
            # 先 tame 再 clean：服务器的输出里可能有 NaN（Python 写的服务器默认会这样输出），也可能有半个表情
            message = clean(tame(json.loads(text)))
        except (ValueError, RecursionError):
            # 规范说 stdout 上只能有 MCP 消息，但总有服务器往里打印别的东西。不当成错误，记进日志
            self.log.add(text, source="stdout")
            return
        for one in message if isinstance(message, list) else [message]:  # 老版本的协议允许把几条消息装在一个数组里
            self._dispatch(one)

    def _dispatch(self, message: object) -> None:
        kind = jsonrpc.kind(message)
        assert isinstance(message, dict) or not kind  # 给类型检查看的：kind 不是空字符串，message 就一定是字典
        if kind == "response":
            with self._lock:
                slot = self._pending.pop(message["id"], None)
            if slot is not None:
                slot.settle(message)
        elif kind == "request":
            # 老一代的服务器会反过来问我们（ping、要列出根目录、要我们替它调用模型……）。回答很快，就在这里答
            try:
                answer = jsonrpc.result(message["id"], self.on_request(message))
            except RpcError as exc:
                answer = jsonrpc.error(message["id"], exc.code, exc.text)
            try:
                self.notify_raw(answer)
            except McpError:
                pass
        elif kind == "notification":
            try:
                self.on_notification(message)
            except Exception as exc:  # noqa: BLE001 —— 处理一条通知时的 bug 不能让读消息的线程停下
                self.log.note(t("处理通知时出错：{error}", error=exc))

    def notify_raw(self, message: dict) -> None:
        """原样写一条消息（回答服务器的请求时用）。"""
        with self._lock:
            self._write(message)

    def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        for raw in iter(self._process.stderr.readline, b""):
            self.log.add(raw.decode("utf-8", errors="replace"))

    def _ended(self) -> None:
        """stdout 读到头了：服务器进程退出了。"""
        process = self._process
        assert process is not None
        try:
            code = process.wait(2)
        except subprocess.TimeoutExpired:
            code = None  # 关掉了 stdout 却还没退出。按断开处理
        reason = t("服务器进程退出了（退出码 {code}）。", code=code if code is not None else "?")
        self.log.note(reason)
        self._fail_pending(McpError(reason))
        if not self._closing:
            self.on_closed(reason)

    def _fail_pending(self, failure: McpError) -> None:
        with self._lock:
            waiting, self._pending = list(self._pending.values()), {}
        for slot in waiting:
            slot.fail(failure)


def _end_group(pid: int) -> None:
    """结束整个进程组：先客气地请它退出（SIGTERM），一秒之内还有没退出的，再强制结束（SIGKILL）。"""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            return  # 组里已经没有进程了
        for _ in range(10):
            time.sleep(0.1)
            try:
                os.killpg(pid, 0)  # 0 号信号不做任何事，只用来问"组里还有没有进程"
            except (ProcessLookupError, PermissionError):
                return
