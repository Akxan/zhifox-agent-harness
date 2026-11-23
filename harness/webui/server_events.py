from __future__ import annotations

from ..safe import to_json
from .server_types import HandlerPart
from .ui import latest_state_only

KEEPALIVE_SECONDS = 15


class HandlerEvents(HandlerPart):
    def _send_events(self) -> None:
        """事件流（Server-Sent Events）：连接不关闭，有新事件就写过去。

        刚连上时，先把日志里已有的事件补发一遍，然后发一个名叫 synced 的标记，意思是
        "旧的补完了，后面的都是此刻正在发生的"。页面靠它区分两种内容：补发的旧内容直接画出来，
        实时的新内容才播放入场动画。标记不带编号、也不进日志，所以不影响断线重连时
        "从第几号接着发"的计算。

        读的是你正看着的那一段的日志（见 sessions.py）。换着看另一段时（打开一段正在后台跑的会话、
        你正看着的那一段在跑时开了新对话），先写一个不带编号的 reset（页面清空，"reset 到下一个状态之间算重放"），
        再把那一段的日志从头补发：它可能正在一轮中间，确认卡片、写了一半的回复都在里面，之后的事件接着实时发过来。
        那个 reset 不带编号：浏览器记着的还是最后一个真实事件的编号。

        断线重连时浏览器报上来它收到的最后一个编号（Last-Event-ID）。几段的编号从同一个数往下排（见 ui.EventIds），
        这一段从 since 那一号起才成了正看着的：报上来的比 since 小，说明浏览器还没收到过这一段的日志（或者不知道收全了没有），
        从头补发一遍（前面加上 reset）；不小于 since，说明它收到的就是这一段的，接着发后面的。
        换过去的那一刻（Sessions.view）和这里读日志（Sessions.watching）拿的是同一把锁，所以换过去以前读到的事件，
        编号一定都比 since 小：浏览器不会拿着上一段的编号，被当成"这一段已经收到这里了"。
        """
        try:
            # 浏览器断线重连时，会用这个请求头告诉我们它上次收到了第几号事件
            cursor = int(self.headers.get("Last-Event-ID") or 0)
        except ValueError:
            cursor = 0
        self._responded = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        caught_up = False  # 已有的事件补发完了没有
        watched = None  # 正在读的是哪一段的日志
        try:
            while True:
                # 第一次（浏览器报上来的编号不是这一段的），或者换着看了另一段（restart）：页面清空，从头补发这一段的日志
                # 每次现找 server.web：从备份恢复以后换上了一个新的 WebApp（见 restore_api.py），接着读它的（还是同一个 WebUI）
                ui, restart, events, wakes = self.server.web.sessions.watching(cursor, watched)
                watched = ui
                if restart:
                    if not events or events[0]["type"] != "reset":  # 这一段的日志不是从 reset 开始的：替它补一个
                        self.wfile.write(f"data: {to_json({'type': 'reset'})}\n\n".encode())
                    events = latest_state_only(events)
                if not events:
                    if not caught_up:
                        caught_up = True
                        self.wfile.write(b"event: synced\ndata: {}\n\n")
                    elif not ui.wait_newer(cursor, KEEPALIVE_SECONDS, wakes):
                        self.wfile.write(b": keepalive\n\n")  # 没有新事件时也定期写一点，才能发现对方已经断开
                    continue
                cursor = events[-1]["id"]
                if not caught_up:
                    events = latest_state_only(events)  # 补发的这一批里，过时的状态不用发
                for event in events:
                    # to_json 保证编码不会失败（见 safe.py）。要是在这里抛了异常，浏览器重连后会从同一个事件
                    # 接着要、我们再抛一次，这段会话的页面就再也连不上了
                    self.wfile.write(f"id: {event['id']}\ndata: {to_json(event)}\n\n".encode())
                if not caught_up:
                    caught_up = True
                    self.wfile.write(b"event: synced\ndata: {}\n\n")
        except OSError:
            pass  # 浏览器关掉了页面
