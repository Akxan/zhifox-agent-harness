"""停下正在进行的那一次模型请求：网页上的"停止"、终端里的 Ctrl-C、定时任务和聊天通道的停止、退出。

以前的"停止"为什么不马上生效
  "停止"是在另一个线程里立一面旗子（webui/ui.py 的 request_stop）。循环所在的线程这时多半卡在 SDK 里等网络：
  读回复的下一段（socket 的 recv）。它要等服务器再发来一点东西才回到我们的代码里，界面的 text()、thinking()
  才看得到旗子、抛出 KeyboardInterrupt。可有好几种时候服务器一个字都不发：模型在想（思考的摘要隔几秒才来一段，
  GPT 想的时候什么都不给）、在写一个很长的工具参数（这一段不经过界面）、请求还在排队（连回答的头都没到）。
  这时点"停止"，要一直等到它再开口。终端里的 Ctrl-C 在 macOS、Linux 上没有这个问题：信号会打断主线程正在等的
  系统调用；Windows 上一样有（Ctrl-C 打断不了正在等网络的 select，见 console.on_ctrl_c）。

现在的做法
  停下的那一刻，就在发出"停止"的那个线程里把这次请求的连接断开（_cut：socket.shutdown）。等着的那个线程马上醒来
  （读到连接断了），抛出 Aborted——它是 KeyboardInterrupt 的一种，所以循环照"被中断"收尾（agent.py 的 _finish_turn），
  和以前在界面上看到旗子时一模一样：历史从末尾撤回、每个工具调用都有结果、页面上写"已中断"。服务器看到连接断了，
  也就不再接着生成、不再计费。

  只断连接，不替那个线程关回复（response.close）：它正在 httpcore 里读，两个线程一起去收拾同一个连接池会出错。
  它醒来以后自己按出错的路收拾（关回复、把连接从池子里拿掉），和网络真的断了时一样。

  Aborted 不是 Exception，这一点也是有意的：SDK 只把 Exception 当成"网络出错"去重试（anthropic、openai 都是），
  循环只在 Unavailable（一种 Exception）上试备用模型。所以停下以后不会再发一次请求、也不会换个模型接着做。

怎么找到"这次请求的连接"（用的都是 httpx、httpcore 公开的东西）
  - 回答的头到了以后：回复上的 network_stream 扩展（response.extensions["network_stream"]），get_extra_info("socket")
    就是那条连接。不管客户端是谁建的，适配层拿到 SDK 的流以后都交给 Call.claim。
  - 头还没到（还在连、在发请求、在等）：要靠客户端上装的钩子（event_hooks，见下面的 Wire）。智狐自己建的客户端都装了
    （providers.make_client、endpoints/client.py）。请求的钩子给每个请求挂上 httpcore 的 trace 扩展：新连上的连接
    在连上的那一刻（connect_tcp、start_tls 的 complete）就知道是它。复用一条空闲的连接时 trace 不说是哪一条：
    这时把这个客户端上没有别的请求认领的连接都断开（Wire.unclaimed）。智狐里一个适配层一个客户端、同一时间只发
    一个请求，所以那正好就是它在用的那一条，别的只是空闲的（池子下次用之前会发现它断了，换一条新的）。
    没装钩子的客户端（测试里自己建的）头没到之前找不到连接：等到头来了马上断开（claim 看到已经停下了）。
  - 钩子还做两件事：已经停下了的请求不再连、不再发（请求的钩子、trace 的 started 直接抛 Aborted）；
    断开以后读到的错误（trace 的 failed）换成 Aborted，SDK 也就不会把它当成网络出错去重试、白等一会儿。

谁来叫停（见 agent.py 的 _request）
  循环每一步把 backend.step 包在 calling() 里，并在这一步的时间里把 Call.abort 登记到界面的 on_stop 上。
  网页、定时任务、聊天通道的界面（WebUI 和它的子类）登记时已经点过"停止"了的话会马上调用它：这样停止落在
  "这一步刚要开始"的那一下也不会漏掉。子 Agent 的界面把登记交给叫它的那一方。终端在 Windows 上靠 console.on_ctrl_c。

钱：被停下的那一步，API 在回复的最后才报告用量，这时已经收不到了，所以不记账（和以前在界面上停下时一样）。
Anthropic 在开头会说输入有多少 token，但输出了多少要到最后才说；只记一半是猜，宁可不记。
"""

from __future__ import annotations

import contextlib
import contextvars
import socket
import sys
import threading
import weakref
from collections.abc import Callable, Iterator
from typing import Any

# 这些时刻还没开始碰网络：已经停下了的请求在这里就不往下做了（不连、不发）
_STARTS = frozenset(
    {
        "connection.connect_tcp.started",
        "connection.connect_unix_socket.started",
        "connection.start_tls.started",
        "http11.send_request_headers.started",
        "http2.send_request_headers.started",
    }
)


class Aborted(KeyboardInterrupt):
    """这次请求被停下了。是 KeyboardInterrupt 的一种：循环、子 Agent、各个界面处理"被中断"的代码都认它，不用改。
    它不是 Exception：SDK 不会把它当成网络出错去重试，循环也不会因为它去试备用模型（见文件开头）。"""


class Call:
    """循环里的一步（一次 backend.step，OpenAI 格式那几家在它前面写摘要的那次请求也算在里面）。

    abort 可以从任何线程调用，只做两件事：立起 aborted，把这一步正在用的连接断开。别的（关回复、撤回历史）
    都由正在等的那个线程自己做。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.aborted = False
        self._streams: list[Any] = []  # 知道是这一步在用的连接（httpcore 的 NetworkStream）
        self._wire: Wire | None = None  # 这一步的请求走的那个客户端（装了钩子的才有）
        self._sending = False  # 正有一个 HTTP 请求在进行（见 request）

    def abort(self) -> None:
        with self._lock:
            if self.aborted:
                return
            self.aborted = True
            streams, wire, sending = list(self._streams), self._wire, self._sending
        if sending and not streams and wire is not None:
            streams = wire.unclaimed(self)  # 复用了一条空闲的连接、回答的头还没到（见文件开头）
        for stream in streams:
            _cut(stream)

    def check(self) -> None:
        """已经停下了的话抛出 Aborted。"""
        if self.aborted:
            raise Aborted

    @contextlib.contextmanager
    def request(self) -> Iterator[None]:
        """适配层把一次 HTTP 请求（连同读完它的回复）包在这里面。

        进去之前已经停下了的，不发。里面出了错、而这时已经停下了的，换成 Aborted：断开连接以后读到的
        "连接断了"（ReadError 这类、SDK 包成的 APIConnectionError）不是真的网络问题，不能报成出错、不能重试、
        不能换备用模型。正常读完以后这条连接回到池子里，就不再算这一步的：之后再停下，不去断它。"""
        self.check()
        with self._lock:
            self._sending = True
        try:
            yield
        except Exception as exc:
            if self.aborted:
                raise Aborted from exc
            raise
        finally:
            with self._lock:
                self._sending = False
                streams, self._streams = self._streams, []
            if self._wire is not None:
                self._wire.release(streams)

    def claim(self, stream: Any) -> None:
        """这条连接是这一步在用的（连上的那一刻、回答的头到了的那一刻）。已经停下了的话马上断开。"""
        if not _is_stream(stream):
            return
        with self._lock:
            if stream not in self._streams:
                self._streams.append(stream)
            aborted, wire = self.aborted, self._wire
        if wire is not None:
            wire.claim(stream, self)
        if aborted:
            _cut(stream)

    def on(self, wire: Wire) -> None:
        with self._lock:
            self._wire = wire

    def traced(self, name: str, info: dict) -> None:
        """httpcore 的 trace 扩展在每个时刻调用它（在发请求的那个线程里，见 Wire.on_request）。"""
        if name.endswith(".complete"):
            self.claim(info.get("return_value"))  # connect_tcp、start_tls：新连上的连接
        if not self.aborted or "response_closed" in name:
            return  # 收尾的那几步（response_closed）照常做完，不然 httpcore 来不及把连接从池子里拿掉
        # 已经停下了：还没开始的不开始；读到的错误（连接断了）换成 Aborted。不是 Exception 的不换：
        # GeneratorExit 是回复正在被关掉（读回复的那个生成器被收起来），KeyboardInterrupt 本来就是中断，换掉它们反而出错
        if name in _STARTS or (name.endswith(".failed") and isinstance(info.get("exception"), Exception)):
            raise Aborted


_current: contextvars.ContextVar[Call | None] = contextvars.ContextVar("harness_inflight_call", default=None)


@contextlib.contextmanager
def calling() -> Iterator[Call]:
    """循环里的一步（见 agent.py 的 _request）：这个线程接下来的请求都算这一步的，用完放回去（子 Agent 的一步嵌在里面也对）。"""
    call = Call()
    token = _current.set(call)
    try:
        yield call
    finally:
        _current.reset(token)


def current() -> Call:
    """这个线程正在进行的那一步。不在 calling 里面（测试里直接调用 backend.step）的话，给一个没人能停的。"""
    return _current.get() or Call()


class Wire:
    """一个 httpx 客户端上的钩子，和它见过的连接：哪一条正被哪一步认领着（空闲的是 None）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 连接 → 正在用它的那一步。用弱引用：连接被池子扔掉以后，这里自己就忘了
        self._owners: weakref.WeakKeyDictionary[Any, Call | None] = weakref.WeakKeyDictionary()

    def hooks(self) -> dict[str, list[Callable[[Any], None]]]:
        return {"request": [self.on_request], "response": [self.on_response]}

    def on_request(self, request: Any) -> None:
        call = _current.get()
        if call is not None:
            call.on(self)
            call.check()  # 停下以后 SDK 想重试、或者写完摘要接着发真正的请求：不发
        earlier = request.extensions.get("trace")

        def trace(name: str, info: dict) -> None:
            if name.endswith(".complete"):
                self.seen(info.get("return_value"))  # 不在哪一步里的请求（同步模型列表）连上的，也记下来：以后会被复用
            if call is not None:
                call.traced(name, info)
            if earlier is not None:
                earlier(name, info)

        request.extensions["trace"] = trace

    def on_response(self, response: Any) -> None:
        call = _current.get()
        if call is None:
            return
        call.claim(network_stream(response))
        call.check()  # 头刚到就已经停下了：httpx 会关掉这个回复（连接跟着断开）

    def seen(self, stream: Any) -> None:
        if _is_stream(stream):
            with self._lock, contextlib.suppress(TypeError):
                self._owners.setdefault(stream, None)

    def claim(self, stream: Any, call: Call) -> None:
        with self._lock, contextlib.suppress(TypeError):
            self._owners[stream] = call

    def release(self, streams: list[Any]) -> None:
        with self._lock:
            for stream in streams:
                with contextlib.suppress(TypeError, KeyError):
                    if stream in self._owners:
                        self._owners[stream] = None

    def unclaimed(self, call: Call) -> list[Any]:
        """这个客户端上没有别的哪一步认领的连接（见文件开头"复用一条空闲的连接"）。"""
        with self._lock:
            return [stream for stream, owner in list(self._owners.items()) if owner is None or owner is call]


def event_hooks() -> dict[str, list[Callable[[Any], None]]]:
    """给一个新的 httpx 客户端装的钩子（交给 SDK 的 DefaultHttpxClient，见 providers.make_client、endpoints/client.py）。
    每个客户端一份：它见过的连接只记在它自己这里。"""
    return Wire().hooks()


def network_stream(response: Any) -> Any:
    """httpx 的回复底下的那条连接（httpcore 的 network_stream 扩展）。拿不到（不是 httpx 的回复）是 None。"""
    extensions = getattr(response, "extensions", None)
    return extensions.get("network_stream") if isinstance(extensions, dict) else None


def _is_stream(value: Any) -> bool:
    return value is not None and callable(getattr(value, "get_extra_info", None))


def _cut(stream: Any) -> None:
    """断开这条连接，让正在读它的那个线程马上醒来。

    shutdown 用的是 socket.socket 自己的那一个：加密的连接是 SSLSocket，它的 shutdown 还会顺手清掉自己的状态，
    而那个线程这时可能正在它里面读。这里只要底下的 TCP 连接断开就行。macOS、Linux 上 shutdown 就会叫醒它
    （光 close 不行：Linux 上正在等这个 socket 的线程不会醒，连接也不会真的断）。Windows 上再 close 一次：
    那里能确定叫醒正在等的 select 的是 closesocket（这部分只在 CI 的 Windows 上验证过）。
    连接早就断了、socket 已经交给了加密的那一层（fileno 是 -1）的，什么都不做。"""
    try:
        sock = stream.get_extra_info("socket")
    except Exception:  # noqa: BLE001 —— 拿不到就算了：最多是这一次停得没那么快
        return
    if not isinstance(sock, socket.socket):
        return
    with contextlib.suppress(OSError):
        socket.socket.shutdown(sock, socket.SHUT_RDWR)
    if sys.platform == "win32":
        with contextlib.suppress(OSError):
            sock.close()
