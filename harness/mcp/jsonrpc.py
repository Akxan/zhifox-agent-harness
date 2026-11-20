"""JSON-RPC 2.0：MCP 的"信封"。

MCP 的每一条消息都是一个 JSON-RPC 2.0 的对象，只有三种：

  请求    {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {...}}    要对方回答
  回答    {"jsonrpc": "2.0", "id": 7, "result": {...}}                            或者 "error": {"code", "message"}
  通知    {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {...}} 没有 id，对方不回答

回答靠 id 和请求对上号。消息走的是同一条通道（stdio 时是一根管道），先发的请求不一定先回答，
所以每发出一个请求，就留一个"座位"（Slot）等它的回答；读消息的那一边收到回答，按 id 找到座位放进去。

这个文件只管信封和"等回答"这件事，和用哪种方式传输（stdio.py、http.py）、信封里装的是什么（client.py）都无关。
"""

from __future__ import annotations

import threading
import time
from typing import Any

from ..i18n import t
from ..safe import clean_text

JSONRPC = "2.0"
MAX_ERROR_CHARS = 500  # 服务器给的出错说明最多显示这么多字：它是别人的程序写的，长度没有保证

# 错误码。前面五个是 JSON-RPC 本身定的，后面三个是 MCP 2026-07-28 新定的（只有"新一代"的服务器才会用）
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
HEADER_MISMATCH = -32020  # HTTP 请求头和请求体对不上
MISSING_CAPABILITY = -32021  # 处理这个请求需要客户端声明某项能力，而我们没有声明
UNSUPPORTED_VERSION = -32022  # 不支持我们请求的协议版本；data.supported 里列着它支持哪些
# 见到这几个错误码，就知道对面是"新一代"（不用握手）的服务器，见 client.py 的说明
MODERN_ERRORS = frozenset({HEADER_MISMATCH, MISSING_CAPABILITY, UNSUPPORTED_VERSION})


class McpError(Exception):
    """和 MCP 服务器打交道失败：启动不了、连不上、超时、连接断了、回答看不懂。错误信息可以直接给用户看。"""


class RpcError(McpError):
    """服务器按 JSON-RPC 的格式回答了一个错误。"""

    def __init__(self, code: object, message: object, data: object = None, status: int | None = None):
        self.code = code if isinstance(code, int) and not isinstance(code, bool) else INTERNAL_ERROR
        self.text = brief(message)
        self.data = data
        self.status = status  # 走 HTTP 时的状态码（400、404……），stdio 时是 None
        super().__init__(t("MCP 服务器返回了错误（{code}）：{message}", code=self.code, message=self.text))


class TimedOut(McpError):
    """等了太久，没有等到回答。"""


class Cancelled(McpError):
    """用户点了"停止"（或者按了 Ctrl-C），不再等这个回答了。"""


def brief(message: object) -> str:
    """把对方给的一段说明整理成一行能读的话：洗掉编码不了的字符，太长的截断。"""
    text = " ".join(clean_text(str(message if message is not None else "")).split())
    return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 1] + "…"


def request(id: int, method: str, params: dict | None = None) -> dict:
    message: dict[str, Any] = {"jsonrpc": JSONRPC, "id": id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def notification(method: str, params: dict | None = None) -> dict:
    message: dict[str, Any] = {"jsonrpc": JSONRPC, "method": method}
    if params is not None:
        message["params"] = params
    return message


def result(id: object, value: dict) -> dict:
    return {"jsonrpc": JSONRPC, "id": id, "result": value}


def error(id: object, code: int, message: str) -> dict:
    return {"jsonrpc": JSONRPC, "id": id, "error": {"code": code, "message": message}}


def kind(message: object) -> str:
    """一条收到的消息是什么："response"、"request"、"notification"，看不懂的是 ""。"""
    if not isinstance(message, dict):
        return ""
    if "method" in message and isinstance(message["method"], str):
        return "request" if message.get("id") is not None else "notification"
    if "id" in message and ("result" in message or "error" in message):
        return "response"
    return ""


def unwrap(message: dict, status: int | None = None) -> dict:
    """从一条回答里取出结果。回答的是错误就抛出 RpcError。"""
    if "error" in message:
        problem = message["error"] if isinstance(message["error"], dict) else {}
        raise RpcError(problem.get("code"), problem.get("message"), problem.get("data"), status)
    value = message.get("result")
    if not isinstance(value, dict):
        raise McpError(t("MCP 服务器的回答格式不对：result 不是一个对象。"))
    return value


class Slot:
    """一个发出去了、还在等回答的请求。

    回答由另一个线程（读消息的那个）放进来，等回答的线程靠 done 这个事件知道它到了。
    """

    def __init__(self, id: object, method: str):
        self.id = id
        self.method = method
        self.done = threading.Event()
        self.message: dict | None = None  # 收到的回答（整条消息）
        self.status: int | None = None  # 走 HTTP 时回答的状态码
        self.failure: BaseException | None = None  # 没等到回答的原因（连接断了……）

    def settle(self, message: dict, status: int | None = None) -> None:
        if not self.done.is_set():
            self.message, self.status = message, status
            self.done.set()

    def fail(self, failure: BaseException) -> None:
        if not self.done.is_set():
            self.failure = failure
            self.done.set()

    def outcome(self) -> dict:
        """等到之后，取出结果（或者抛出没等到的原因）。"""
        if self.failure is not None:
            raise self.failure
        assert self.message is not None
        return unwrap(self.message, self.status)


def wait(slot: Slot, timeout: float, stop: threading.Event | None = None) -> dict:
    """等一个请求的回答，最多等 timeout 秒；stop 被立起来（用户点了"停止"）就不等了。

    每次只等一小会儿再回头看看，原因有两个：一是要及时发现 stop；二是终端版里按 Ctrl-C 时，
    Python 只在两次等待之间才处理这个按键，一直死等的话 Ctrl-C 要很久才有反应。
    """
    deadline = time.monotonic() + timeout
    while not slot.done.wait(0.1):
        if stop is not None and stop.is_set():
            raise Cancelled(t("已经取消。"))
        if time.monotonic() >= deadline:
            raise TimedOut(t("等了 {seconds} 秒，MCP 服务器还没有回答（{method}），不再等了。", seconds=round(timeout), method=slot.method))
    return slot.outcome()
