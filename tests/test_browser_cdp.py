"""cdp.Connection 的测试：一来一回怎么对上、事件怎么送到、超时、取消、读不懂的消息、对面走了。

不用 Chrome，也不起子进程：两根 os.pipe() 加一个线程扮演 Chrome 的那一头（Peer），
Connection 用的是和真的 Chrome 一模一样的描述符读写，所以走的是真实的代码路径。
Peer 要么按 handler 自动回答，要么把收到的命令交给测试，由测试决定什么时候、按什么顺序、发什么字节回去——
"先发事件再回答""一条消息拆成三段""半个表情"这些情况，只有这样才造得出来。
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from harness.browser import cdp
from harness.browser.cdp import Cancelled, CDPClosed, CDPError, CDPTimeout, Connection


def wait_until(check, timeout: float = 3, what: str = "条件") -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, f"等不到{what}"
        time.sleep(0.01)


class Peer:
    """扮演 Chrome 的另一头：我们写给它的一根管道，它写给我们的一根。

    handler(命令) 返回要发回去的消息（在 Peer 的线程里调用）。不给 handler 时只把命令放进 received，
    由测试自己用 send / send_raw 回答。reader=False 时干脆不读（测"对面不再读了"）。
    """

    def __init__(self, handler=None, reader: bool = True):
        self.handler = handler
        self.to_peer_read, to_peer_write = os.pipe()
        from_peer_read, self.from_peer_write = os.pipe()
        self.received: queue.Queue[dict] = queue.Queue()
        self.saw_end = threading.Event()  # 对面读到了结尾（我们关掉了自己那一头）
        self.closes = 0
        self._open = {self.to_peer_read, self.from_peer_write}
        self._write_lock = threading.Lock()
        self.connection = Connection(from_peer_read, to_peer_write, on_close=self._closed)
        self._thread = threading.Thread(target=self._loop, daemon=True, name="fake-peer") if reader else None
        if self._thread is not None:
            self._thread.start()

    def _closed(self) -> None:
        self.closes += 1

    def _loop(self) -> None:
        buffer = b""
        while True:
            try:
                chunk = os.read(self.to_peer_read, 1 << 16)
            except OSError:
                return
            if not chunk:
                self.saw_end.set()
                return
            buffer += chunk
            while b"\0" in buffer:
                raw, _, buffer = buffer.partition(b"\0")
                message = json.loads(raw)  # 我们发出去的每一条都必须是完整的 JSON：两条搅在一起这里就会出错
                self.received.put(message)
                if self.handler is not None:
                    self.send(*self.handler(message))

    def send(self, *messages: dict) -> None:
        """几条消息一次写出去（读线程多半在同一块里读到它们）。"""
        self.send_raw(b"".join(json.dumps(message).encode() + b"\0" for message in messages))

    def send_raw(self, data: bytes) -> None:
        with self._write_lock:
            if self.from_peer_write not in self._open:
                return  # 这一头已经关了：号码可能已经是下一个测试的管道，不能再往上写
            view = memoryview(data)
            while view:
                view = view[os.write(self.from_peer_write, view) :]

    def next(self, timeout: float = 10) -> dict:
        # 等对面收到下一条。只有等不到（测试要失败）时才真的等这么久；GitHub 的 Windows 机器忙的时候，
        # 几个线程同时发的请求 3 秒都没到齐过一次，所以放宽到 10 秒。要断言"没有发"的地方自己给短的 timeout
        return self.received.get(timeout=timeout)

    def hang_up(self) -> None:
        """Chrome 那一头没了：它写给我们的管道关上，读线程会读到结尾。"""
        self._close(self.from_peer_write)

    def stop_reading(self) -> None:
        self._close(self.to_peer_read)

    def _close(self, fd: int) -> None:
        with self._write_lock:  # 和 send_raw 同一把锁：不会一边关、一边往上写
            if fd in self._open:
                self._open.discard(fd)
                os.close(fd)

    def shutdown(self) -> None:
        # 两边的读线程都先读到结尾、自己停下，再关它们读的那一头：读线程还在用的号码被关掉，马上就会被下一个测试的管道用上，
        # 它下一圈读到的、回答的就是下一个测试的东西（CI 的 Windows 上真的串过：上一个测试的 -32000 落到了下一个测试里）
        self.hang_up()  # 我们的读线程读到结尾，自己关掉它那一头（见 Connection._read_loop）
        self.connection._reader.join(5)
        self.connection.close()  # 关掉写给对面的那一头：对面的读线程读到结尾
        if self._thread is not None:
            self._thread.join(5)
        self.stop_reading()


@pytest.fixture
def peer():
    made: list[Peer] = []

    def make(handler=None, reader: bool = True) -> Peer:
        made.append(Peer(handler, reader))
        return made[-1]

    yield make
    for each in made:
        each.shutdown()


def answer(result: dict | None = None):
    """一个 handler：每个命令都回答 result。"""
    return lambda message: [{"id": message["id"], "result": result or {}}]


# ---- 一来一回 -----------------------------------------------------------------


def test_replies_are_matched_by_id_even_out_of_order_with_events_in_between(peer):
    fake = peer()
    seen = []
    fake.connection.on("Page.loadEventFired", lambda params, session: seen.append(params["n"]))
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(fake.connection.call, "Page.navigate", {"url": "https://a.example"}, timeout=5)
        a = fake.next()
        second = pool.submit(fake.connection.call, "Runtime.evaluate", {"expression": "1"}, timeout=5)
        b = fake.next()
        # 后来的先回答，中间夹着事件
        fake.send(
            {"method": "Page.loadEventFired", "params": {"n": 1}},
            {"id": b["id"], "result": {"value": "second"}},
            {"method": "Page.loadEventFired", "params": {"n": 2}},
            {"id": a["id"], "result": {"value": "first"}},
        )
        assert first.result(2) == {"value": "first"}
        assert second.result(2) == {"value": "second"}
    assert a["id"] != b["id"]
    assert (a["method"], a["params"]) == ("Page.navigate", {"url": "https://a.example"})
    assert seen == [1, 2]  # 读线程按顺序处理：回答 a 送到时，两个事件都已经送到了
    assert fake.connection._pending == {}


def test_a_reply_without_result_is_an_empty_dict_and_params_default_to_empty(peer):
    fake = peer(lambda message: [{"id": message["id"]}])
    assert fake.connection.call("Page.enable", timeout=2) == {}
    assert fake.next()["params"] == {}


def test_session_id_goes_out_only_when_given_and_comes_back_with_events(peer):
    fake = peer(answer())
    fake.connection.call("Page.enable", session="S1", timeout=2)
    fake.connection.call("Browser.getVersion", timeout=2)
    first, second = fake.next(), fake.next()
    assert first["sessionId"] == "S1"
    assert "sessionId" not in second  # 对着整个浏览器的命令不带 sessionId

    got = []
    fake.connection.on("Page.frameStoppedLoading", lambda params, session: got.append((params["frameId"], session)))
    only_s2 = fake.connection.expect("Page.frameStoppedLoading", lambda params, session: session == "S2")
    fake.send(
        {"method": "Page.frameStoppedLoading", "params": {"frameId": "F"}, "sessionId": "S1"},
        {"method": "Page.frameStoppedLoading", "params": {"frameId": "G"}, "sessionId": "S2"},
        {"method": "Page.frameStoppedLoading", "params": {"frameId": "H"}},
    )
    assert only_s2.wait(2) == {"frameId": "G"}
    wait_until(lambda: len(got) == 3, what="三个事件")
    assert got == [("F", "S1"), ("G", "S2"), ("H", "")]


def test_chrome_saying_no_becomes_a_cdp_error_with_its_code(peer):
    fake = peer(lambda message: [{"id": message["id"], "error": {"code": -32000, "message": "No target with given id found"}}])
    with pytest.raises(CDPError) as caught:
        fake.connection.call("Target.attachToTarget", {"targetId": "nope"}, timeout=2)
    error = caught.value
    assert type(error) is CDPError  # 不是超时，也不是断开
    assert (error.method, error.code, error.message) == ("Target.attachToTarget", -32000, "No target with given id found")
    assert "Target.attachToTarget" in str(error)
    assert not fake.connection.closed  # 一个命令没做成，连接照常
    assert fake.connection._pending == {}


def test_an_error_without_a_code_has_code_zero(peer):
    fake = peer(lambda message: [{"id": message["id"], "error": {"message": "坏了"}}])
    with pytest.raises(CDPError) as caught:
        fake.connection.call("Page.reload", timeout=2)
    assert caught.value.code == 0 and caught.value.message == "坏了"


def test_a_slow_chrome_times_out_and_the_late_reply_is_ignored(peer):
    fake = peer()  # 不回答
    started = time.monotonic()
    with pytest.raises(CDPTimeout) as caught:
        fake.connection.call("Page.navigate", {"url": "https://slow.example"}, timeout=0.1)
    assert time.monotonic() - started < 1
    assert caught.value.method == "Page.navigate"
    assert isinstance(caught.value, CDPError)  # 调用的地方可以统一按 CDPError 处理
    assert fake.connection._pending == {}  # 等的人走了，登记也删掉了，不会越攒越多
    late = fake.next()
    fake.handler = answer({"ok": True})
    fake.send({"id": late["id"], "result": {"late": True}})  # 晚到的回答：没人等了，丢掉
    assert fake.connection.call("Browser.getVersion", timeout=2) == {"ok": True}
    assert not fake.connection.closed


def test_calls_from_many_threads_do_not_mix_their_bytes(peer):
    # 一条 100 KB 的命令一次写不完（管道只保证不超过 PIPE_BUF——几百字节到 4 KB——的一次写入不被打断）：
    # 没有写锁的话，几个线程的字节会搅在一起，Peer 那边解析 JSON 就会出错
    fake = peer(lambda message: [{"id": message["id"], "result": {"head": message["params"]["blob"][:4], "size": len(message["params"]["blob"])}}])

    def one(n: int) -> dict:
        return fake.connection.call("Echo", {"blob": f"{n:04d}" + "x" * 100_000}, timeout=10)

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(one, range(24)))
    assert results == [{"head": f"{n:04d}", "size": 100_004} for n in range(24)]
    ids = [fake.next()["id"] for _ in range(24)]
    assert len(set(ids)) == 24


def test_a_big_reply_split_across_many_reads_arrives_whole(peer):
    blob = "x" * 300_000  # 比读线程一次读的 64 KB 大得多（一张截图的 base64 就是这样）
    fake = peer(lambda message: [{"id": message["id"], "result": {"data": blob}}])
    assert fake.connection.call("Page.captureScreenshot", timeout=5)["data"] == blob


# ---- 事件 ---------------------------------------------------------------------


def test_expect_before_the_command_catches_an_event_that_beats_the_reply(peer):
    # Chrome 常常先发事件、再回答触发它的命令。先登记再发命令，事件就不会溜走
    fake = peer(lambda message: [{"method": "Page.frameStoppedLoading", "params": {"frameId": "F1"}}, {"id": message["id"], "result": {}}])
    loaded = fake.connection.expect("Page.frameStoppedLoading")
    fake.connection.call("Page.navigate", timeout=2)
    assert loaded.wait(1) == {"frameId": "F1"}
    # 反过来：回答到了才开始等，事件已经过去了，只能等到超时
    fake.connection.call("Page.navigate", timeout=2)
    assert fake.connection.wait_for("Page.frameStoppedLoading", timeout=0.1) is None
    assert fake.connection._waiters == []  # 等完的、超时的都不留登记


def test_expect_only_takes_the_event_that_matches(peer):
    fake = peer()

    def main_frame(params, session):
        return params.get("frameId") == "MAIN"

    only_iframe = fake.connection.expect("Page.frameStoppedLoading", main_frame)
    fake.send({"method": "Page.frameStoppedLoading", "params": {"frameId": "IFRAME"}})  # 页面里嵌的框加载完了：不算
    assert only_iframe.wait(0.1) is None
    wanted = fake.connection.expect("Page.frameStoppedLoading", main_frame)
    fake.send(
        {"method": "Page.loadEventFired", "params": {"frameId": "MAIN"}},  # 名字不对：不算
        {"method": "Page.frameStoppedLoading", "params": {"frameId": "IFRAME"}},
        {"method": "Page.frameStoppedLoading", "params": {"frameId": "MAIN"}},
    )
    assert wanted.wait(1) == {"frameId": "MAIN"}
    dropped = fake.connection.expect("Page.frameStoppedLoading", main_frame)
    dropped.cancel()  # 不等了：登记删掉
    assert fake.connection._waiters == []


def test_a_failing_listener_does_not_stop_the_reader(peer):
    fake = peer(lambda message: [{"method": "Network.dataReceived", "params": {"n": message["id"]}}, {"id": message["id"], "result": {"ok": True}}])
    got = []

    def broken(params, session):
        raise RuntimeError("订阅者自己出错了")

    fake.connection.on("Network.dataReceived", broken)
    fake.connection.on("Network.dataReceived", lambda params, session: got.append(params["n"]))
    assert fake.connection.call("A", timeout=2) == {"ok": True}
    assert fake.connection.call("B", timeout=2) == {"ok": True}
    assert len(got) == 2  # 出错的那个排在前面，后面的照样收到
    assert not fake.connection.closed


def test_on_returns_a_function_that_unsubscribes(peer):
    fake = peer(lambda message: [{"method": "Log.entryAdded", "params": {}}, {"id": message["id"], "result": {}}])
    got = []
    forget = fake.connection.on("Log.entryAdded", lambda params, session: got.append(1))
    fake.connection.call("A", timeout=2)
    forget()
    forget()  # 取消两次也没关系
    fake.connection.call("B", timeout=2)
    assert got == [1]


# ---- 取消 ---------------------------------------------------------------------


def test_cancel_only_wakes_that_owners_calls_and_waits(peer):
    fake = peer()
    mine, theirs = object(), object()
    with ThreadPoolExecutor(5) as pool:
        my_call = pool.submit(fake.connection.call, "Page.navigate", timeout=5, owner=mine)
        their_call = pool.submit(fake.connection.call, "Runtime.evaluate", timeout=5, owner=theirs)
        nobodys_call = pool.submit(fake.connection.call, "Browser.getVersion", timeout=5)
        requests = {message["method"]: message for message in (fake.next(), fake.next(), fake.next())}
        my_wait = pool.submit(fake.connection.wait_for, "Page.loadEventFired", timeout=5, owner=mine)
        their_wait = pool.submit(fake.connection.wait_for, "Page.loadEventFired", timeout=5, owner=theirs)
        wait_until(lambda: len(fake.connection._waiters) == 2, what="两个等事件的")

        fake.connection.cancel(mine)
        with pytest.raises(Cancelled):
            my_call.result(1)
        with pytest.raises(Cancelled):
            my_wait.result(1)
        fake.connection.cancel(None)  # 没有主人的请求不会被"没有主人"一起取消
        time.sleep(0.05)
        assert not their_call.done() and not their_wait.done() and not nobodys_call.done()

        fake.send(
            {"method": "Page.loadEventFired", "params": {"t": 1}},
            {"id": requests["Runtime.evaluate"]["id"], "result": {"v": 2}},
            {"id": requests["Browser.getVersion"]["id"], "result": {"v": 3}},
            {"id": requests["Page.navigate"]["id"], "result": {"too": "late"}},  # 被取消的那个：回答来了也没人要
        )
        assert their_wait.result(2) == {"t": 1}
        assert their_call.result(2) == {"v": 2}
        assert nobodys_call.result(2) == {"v": 3}
    assert fake.connection._pending == {} and fake.connection._waiters == []
    assert not fake.connection.closed


def test_cancel_with_nothing_pending_does_nothing(peer):
    fake = peer(answer({"ok": True}))
    fake.connection.cancel(object())
    assert fake.connection.call("A", timeout=2) == {"ok": True}


# ---- 读不懂的消息 -----------------------------------------------------------------


def test_unreadable_messages_are_dropped_and_later_ones_still_work(peer):
    fake = peer()
    got = []
    fake.connection.on("Log.entryAdded", lambda params, session: got.append(params))
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(fake.connection.call, "Browser.getVersion", timeout=3)
        request = fake.next()
        fake.send_raw(b"this is not json\0")
        fake.send_raw(b"[1, 2, 3]\0")  # 不是对象
        fake.send_raw(b"\0")  # 空的一条
        fake.send_raw(b'{"method": 5, "params": {}}\0')  # method 不是文字
        fake.send_raw(b'{"method": "Log.entryAdded", "params": [1]}\0')  # params 不是对象
        fake.send_raw(b"\xff\xfe\0")  # 不是 UTF-8
        fake.send_raw(b'{"id": 99999, "result": {}}\0')  # 没人在等的编号
        fake.send_raw(b'{"method": "Log.entryAdded", "params": {"text": "a\\ud800b"}}\0')  # 半个表情：进门时就洗掉
        # 一条回答拆成三段，分开发过来
        reply = json.dumps({"id": request["id"], "result": {"product": "Fake"}}).encode() + b"\0"
        for piece in (reply[:5], reply[5:20], reply[20:]):
            fake.send_raw(piece)
            time.sleep(0.02)
        assert future.result(2) == {"product": "Fake"}
    assert got == [{"text": "a�b"}]
    assert not fake.connection.closed


def test_two_messages_in_one_write_are_both_delivered(peer):
    fake = peer()
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(fake.connection.call, "A", timeout=3)
        a = fake.next()
        second = pool.submit(fake.connection.call, "B", timeout=3)
        b = fake.next()
        fake.send_raw(json.dumps({"id": a["id"], "result": {"n": 1}}).encode() + b"\0" + json.dumps({"id": b["id"], "result": {"n": 2}}).encode() + b"\0")
        assert first.result(2) == {"n": 1} and second.result(2) == {"n": 2}


@pytest.mark.parametrize(
    "strange",
    [b'{"id": [1], "result": {}}', b'{"id": 1, "error": {"code": "oops", "message": "m"}}'],
    ids=["unhashable-id", "non-numeric-code"],
)
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")  # 读线程停掉时的那段报错就是这个 bug
def test_a_strange_reply_does_not_kill_the_reader(peer, strange):
    fake = peer()
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(fake.connection.call, "Browser.getVersion", timeout=0.5)
        request = fake.next()
        assert request["id"] == 1
        fake.send_raw(strange + b"\0")
        fake.send({"id": request["id"], "result": {"ok": True}})
        try:
            outcome = future.result(2)
        except CDPTimeout:
            outcome = "读线程停掉了，一直等到超时"
        except CDPError:
            outcome = "这一条算作 Chrome 的错误"  # 非数字的 code 当作一个错误也可以；要紧的是读线程还活着
    assert outcome in ({"ok": True}, "这一条算作 Chrome 的错误")
    fake.handler = answer({"next": True})
    assert fake.connection.call("Next", timeout=1) == {"next": True}


def test_a_message_that_never_ends_closes_the_connection(peer, monkeypatch):
    monkeypatch.setattr(cdp, "MAX_MESSAGE_BYTES", 1000)
    fake = peer()
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(fake.connection.call, "Page.captureScreenshot", timeout=5)
        fake.next()
        fake.send_raw(b'{"id": 1, "result": "' + b"x" * 5000)  # 一直没有结尾的 \0：当作 Chrome 出了问题
        with pytest.raises(CDPClosed):
            future.result(2)
    assert fake.connection.closed
    assert fake.closes == 1


# ---- 断开 ---------------------------------------------------------------------


def test_when_chrome_goes_away_everyone_waiting_is_told_and_on_close_runs_once(peer):
    fake = peer()
    with ThreadPoolExecutor(2) as pool:
        call = pool.submit(fake.connection.call, "Page.navigate", timeout=5)
        fake.next()
        loaded = fake.connection.expect("Page.loadEventFired")
        waiting = pool.submit(loaded.wait, 5)
        fake.hang_up()
        with pytest.raises(CDPClosed):
            call.result(2)
        with pytest.raises(CDPClosed):
            waiting.result(2)
    assert fake.connection.closed
    assert fake.closes == 1
    started = time.monotonic()
    with pytest.raises(CDPClosed):
        fake.connection.call("Browser.getVersion", timeout=5)  # 断了以后的请求立刻失败，不用等到超时
    assert time.monotonic() - started < 0.5
    with pytest.raises(CDPClosed):
        fake.connection.expect("Page.loadEventFired")
    fake.connection.close()  # 再关一次：不会再调用 on_close
    assert fake.closes == 1


def test_closing_our_end_wakes_the_waiters_and_the_other_side_sees_the_end(peer):
    fake = peer()
    with ThreadPoolExecutor(1) as pool:
        call = pool.submit(fake.connection.call, "Page.navigate", timeout=5)
        fake.next()
        fake.connection.close()
        with pytest.raises(CDPClosed):
            call.result(2)
    assert fake.closes == 1
    # 对面读到了结尾：真的 Chrome 就是靠这个，在智狐退出（哪怕是被强行杀掉）以后自己退出的
    assert fake.saw_end.wait(2)
    fake.hang_up()
    fake.connection._reader.join(2)
    assert not fake.connection._reader.is_alive()
    assert fake.closes == 1


def test_writing_to_a_chrome_that_stopped_reading_raises_closed(peer):
    fake = peer(reader=False)
    fake.stop_reading()  # 对面那个进程没了，没有人再读这根管道
    with pytest.raises(CDPClosed):
        fake.connection.call("Browser.getVersion", timeout=1)
    assert fake.connection.closed
    assert fake.closes == 1
    assert fake.connection._pending == {}


# ---- 关掉以后不再碰那两个描述符的号码 ---------------------------------------------------------
# 描述符关掉以后，号码马上会被下一根管道用上（比如再开一个 Chrome）。还拿着旧号码去读、去写的话，读到的、写进去的
# 就是另一根管道的东西：一个 Chrome 的回答落到另一个连接上（CI 的 Windows 上，测试之间真的串过一次）


def test_after_close_nothing_more_is_written_to_the_old_number(peer, monkeypatch):
    fake = peer(answer())
    fake.connection.close()
    writes = []
    monkeypatch.setattr(cdp.os, "write", lambda fd, data: writes.append(fd) or len(data))
    with pytest.raises(CDPClosed):
        fake.connection._write({"id": 99, "method": "Page.reload"})  # call() 查过"没关"以后、真的写之前，连接被关了
    assert writes == []  # 一个字节都没往那个号码上写


def test_the_reader_closes_its_own_end_after_its_last_read(peer):
    # close() 不替还在读的读线程关它那一头：读线程读完最后一次再自己关。不然它下一圈 os.read 用的号码可能已经是别人的管道了。
    # Windows 上 close() 用 CancelIoEx 叫停那一次读（见 cdp._cancel_read），读线程马上退出、自己关掉；
    # 别的系统上读线程要等对面（Chrome）关掉它那一头才读到结尾，在那之前它那一头一直开着，号码不会让出去
    fake = peer(answer())
    read_fd = fake.connection._read_fd
    fake.connection.close()
    if os.name != "nt":
        os.fstat(read_fd)  # 对面还开着：读线程还在等，它那一头还开着
        assert fake.connection._reader.is_alive()
    fake.hang_up()  # Chrome 退出了：读线程读到结尾（Windows 上它已经停了），自己关掉、退出
    fake.connection._reader.join(5)
    assert not fake.connection._reader.is_alive()
    with pytest.raises(OSError):
        os.fstat(read_fd)
