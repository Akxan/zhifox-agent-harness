"""HandlerPart：混进 Handler 的每一组方法（HandlerGate、HandlerPage、HandlerEvents）能用到的东西，写给类型检查（mypy）看。

为什么要有这个文件，和 ../app_types.py、webapp_types.py 是同一个道理：Handler 由几组方法拼成（见 server.py 开头），
每一组写在自己的 server_xxx.py 里，是一个只有方法的类。这些方法里用到 self.headers、self.wfile、self.server.web、self._send_json(...)：
前几样是标准库 BaseHTTPRequestHandler 的，后几样在 server.py 的 Handler 里。单看 HandlerGate 这个类，mypy 不知道 self 上有这些。

办法：HandlerGate 这些类都继承 HandlerPart。它是一个 Protocol，列出这几组要用的东西的类型：
  - BaseHTTPRequestHandler 的那几样（headers、command、rfile、wfile、send_response……）照着标准库的类型（typeshed）写。
    Protocol 不能继承一个普通的类，所以只能照抄这几行；抄错了，mypy 会报 "Definition of … in base class HandlerPart is
    incompatible with definition in base class StreamRequestHandler"，不会悄悄和标准库对不上。
  - server.py 的 Handler 里的方法只写签名，函数体是 ...；Handler 那边的签名改了、这里没跟着改，mypy 会报 [override]。
  - 混进来的方法会整个换掉的状态（_send_events 里的 self._responded = True）要写一个值，原因见 ../app_types.py 开头。
  - 和 App 不一样的一点：Handler 不是我们自己建的（ThreadingHTTPServer 拿着这个类，每个请求建一个），所以 mypy 那条
    "写了没实现会报 [abstract]"在这里用不上：这里列了、server.py 里却没实现的，要等测试真的发请求才会发现。所以只列真用到的几样。
  - 这些只在类型检查时存在：运行时 HandlerPart 是下面那个空的类，Handler 的行为、MRO 里找到的方法和不拆开的时候一模一样。

新加一组方法时：class HandlerXxx(HandlerPart)，加进 Handler 的基类（放在 BaseHTTPRequestHandler 前面）；
用到了 Handler 或者 BaseHTTPRequestHandler 的东西、mypy 报"没有这个属性"时，照着它的定义在下面补一行。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from email.message import Message
    from io import BufferedIOBase
    from typing import Protocol

    from .server import Server

    class HandlerPart(Protocol):
        server: Server
        headers: Message
        command: str
        rfile: BufferedIOBase
        wfile: BufferedIOBase

        _responded: bool = False

        def send_response(self, code: int, message: str | None = None) -> None: ...
        def send_header(self, keyword: str, value: str) -> None: ...
        def end_headers(self) -> None: ...

        def _send_json(self, status: int, payload: dict) -> None: ...
        def _send_text(self, status: int, text: str) -> None: ...
        def _send(self, status: int, content_type: str, body: bytes, headers: dict | None = None) -> None: ...

else:

    class HandlerPart:
        """运行时什么都没有：上面的那些只是写给 mypy 看的类型。"""
