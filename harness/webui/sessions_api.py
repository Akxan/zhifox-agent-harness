"""几段对话同时进行：WebApp 这一侧怎么新建一段、换着看、停下、收尾（名单本身和锁的规矩见 sessions.py）。

  POST /api/stop   停下一段。带 conversation 的话停那一段（会话栏上后台那一段的"停止"），不带是你正看着的这一段

SessionActions 混进 WebApp（见 webapp.py）：只有方法，用到的状态都在 WebApp.__init__ 里。
  _fresh_session   新建一段：app.new_chat 加上它自己的事件日志（编号和别的几段从同一个数往下排，见 ui.EventIds）
  _arm             给一段接上"停止"要停的东西（只是它自己的）、用量变了刷新状态、开始等你确认时刷新会话栏
  _holding         试着拿一段的锁（不等），拿到了还要看它还在不在名单里。只关系到这一段的短操作用它
  _viewed_idle     拿 _switch 和你正看着的那一段的锁（换模型、填密钥、改提示词、改自定义接口、撤回文件）
  _take            发消息、重新生成时拿正看着的那一段的锁，一直拿到这一轮收尾
  _too_many、_busy_everywhere   到了同时几段的上限、全局的操作碰上有几段在跑时，说清楚为什么
  _show            换成看名单里的另一段：信息栏、MCP、"现在用的模型"跟着它，事件流换一份日志读（见 server.py 的 _send_events）
  _left_behind     换走以后，原来那一段怎么办：没在跑的丢掉（_discard），在跑的留在后台接着跑
  _settle          一轮结束：记下跑完了（在后台的那一段标上"跑完了""出错了"，存好了盘的从名单里拿掉）、发状态、告诉页面、放开锁
  answer_confirm   确认卡片的编号是全局唯一的，按编号找到是哪一段在等，只答到那一段
  stop、stop_all、wait_idle、save_all   停下一段 / 退出时停下全部、等它们收尾、再存一次没存上的

route_* 由 server.py 的 Handler._route 调用（写法和约定见 server.py 开头）。
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import TYPE_CHECKING

from ..i18n import t
from .sessions import DONE, ERROR, Session, take_switch
from .ui import WebUI
from .webapp_types import WebAppPart

if TYPE_CHECKING:
    from .server import Handler
    from .webapp import WebApp


class SessionActions(WebAppPart):
    """几段对话：新建、换着看、停下、收尾。"""

    # ---- 新建一段、接上该接的 ----------------------------------------------------

    def _fresh_session(self) -> Session:
        """新建一段（还没放进名单）：此刻设置里选的模型、一段空的会话、它自己的事件日志。"""
        ui = WebUI(ids=self._ids)
        session = Session(self.app.new_chat(ui), ui)
        self._arm(session)
        return session

    def _arm(self, session: Session) -> None:
        """"停止"要停的东西都只是这一段自己的：它的命令、它名下的 MCP 调用、它的浏览器和电脑的动作、它正在跑的钩子。
        浏览器和电脑的 session 换一段会话就换一个，所以每次现找（chat 本身不换）。"""
        chat, ui = session.chat, session.ui
        ui.on_stop(chat.shell.kill_running)
        ui.on_stop(lambda: self.app.mcp.cancel_running(chat.owner))
        ui.on_stop(lambda: chat.browser.stop())
        ui.on_stop(lambda: chat.desktop.stop())
        ui.on_stop(lambda: chat.agent.hooks.stop())
        chat.agent.on_usage = self.publish_state  # 每调用完一次模型，就刷新一次用量和费用（后台那一段花的钱也算在今天里）
        ui.on_waiting = lambda: self._background_changed(session)

    def _background_changed(self, session: Session) -> None:
        """后台的一段开始等你确认、或者等完了：会话栏上的标记跟着变。你正看着的那一段不用（确认卡片就在眼前）。"""
        if session is not self.sessions.viewed:
            self.publish_state()

    # ---- 拿一段的锁 --------------------------------------------------------------

    @contextlib.contextmanager
    def _holding(self, session: Session) -> Iterator[bool]:
        """试着拿 session 的锁（不等）。拿到了、它也还在名单里，才是 True；用完放开。
        拿着 _turn（AllIdle）的线程本来就拿着每一段的锁，算拿到了，也不替它放开。"""
        if self._turn.owns(session):
            yield True
            return
        if not session.lock.acquire(blocking=False):
            yield False
            return
        try:
            yield self.sessions.alive(session)
        finally:
            self.sessions.unlock(session)

    @contextlib.contextmanager
    def _viewed_idle(self) -> Iterator[bool]:
        """拿着 _switch 和你正看着的那一段的锁（都不等）：True 是它没在进行一轮、也没人在改它，这时谁也换不走"正看着哪一段"，
        self.app.agent、self.ui 这些就是它的。换模型、填密钥、改提示词、改自定义接口用它。"""
        if not take_switch(self._switch):
            yield False
            return
        try:
            with self._holding(self.sessions.viewed) as free:
                yield free
        finally:
            self._switch.release()

    def _take(self, session: Session) -> bool:
        """拿 session 的锁，一直拿着（这一轮跑完时由 _settle 放开）。拿不到、或者它已经不是你正看着的那一段了，返回 False。"""
        if not session.lock.acquire(blocking=False):
            return False
        if self.sessions.alive(session) and session is self.sessions.viewed:
            return True
        self.sessions.unlock(session)
        return False

    def _busy_everywhere(self) -> str:
        """_turn（"没有一段在跑"）拿不到的原因：说清楚是哪几段在跑。"""
        running = [session.chat.conversation.title or t("新对话") for session in self.sessions.all() if session.running]
        if len(running) > 1:
            return t("「{title}」等 {n} 段对话正在进行。等它们结束，或者在会话栏上停下它们，再试。", title=running[0], n=len(running))
        if running:
            return t("「{title}」正在进行一轮对话。等它结束，或者先停下它，再试。", title=running[0])
        return self._busy()

    def _too_many(self) -> str:
        return t("已经有 {n} 段对话在同时进行，这是上限。等其中一段结束，或者先在会话栏上停下一段，再发。", n=self._parallel_limit())

    def _parallel_limit(self) -> int:
        """你的对话最多同时进行几段（见 config.py 的 MAX_PARALLEL_CHATS）。"""
        return max(1, self.app.config.max_parallel_chats)

    # ---- 换着看 ------------------------------------------------------------------

    def _show(self, session: Session) -> Session:
        """换成看 session（它已经在名单里）。返回原来正看着的那一段。调用的人拿着 _switch。

        "正看着哪一段"在 _publishing 里换：同一时刻算出来的状态快照不会一半是原来那一段的、一半是这一段的。"""
        with self._publishing:
            previous = self.sessions.view(session)
            self.app.view(session.chat)
        return previous

    def _show_live(self, conversation_id: str) -> tuple[Session | None, Session]:
        """这段会话正活着的话换过去看它（见 Sessions.view_live）。返回（它，原来正看着的那一段）。调用的人拿着 _switch。"""
        with self._publishing:
            found, previous = self.sessions.view_live(conversation_id)
            if found is not None and found is not previous:
                self.app.view(found.chat)
        return found, previous

    def _left_behind(self, previous: Session, idle: bool) -> None:
        """换着看以后，原来那一段（previous）：idle 是调用的人拿着它的锁、它没在跑（这时丢掉它：内容已经在磁盘上，
        或者你已经确认过放弃，见 _before_leaving）；在跑的留在名单里接着跑。它的日志里那张旧的状态清掉，
        正在读它的事件流叫醒一下，好换过去读新的这一段。"""
        previous.ui.forget_states()
        previous.ui.wake()
        if idle:
            self._discard(previous)

    def _discard(self, session: Session) -> None:
        """丢掉一段（调用的人拿着它的锁）：从名单里拿掉，关掉它的浏览器标签页，屏幕让出来。"""
        self.sessions.remove(session)
        if not self.sessions.alive(session):
            self.app.close_chat(session.chat)

    # ---- 一轮结束 ----------------------------------------------------------------

    def _settle(self, session: Session) -> None:
        """一轮的收尾做完了（存了盘、让出了屏幕、放开了会话的认领），最后这几步（见 chat_api._run_turn）：

        先记下它跑完了（在后台的话标上"跑完了""出错了"，存好了盘的从名单里拿掉），发一次状态（会话栏的标记、"同时在跑几段"
        都已经是跑完以后的样子），再告诉页面"这一轮结束了"，紧接着放开这一段的锁。
        "结束了"和放开锁之间不做别的：页面一收到它就可能发下一条消息，不能撞上还没放开的锁。
        认领要在拿掉之前放开：拿掉以后马上就可能有人从磁盘再打开它、开始一轮，那时这段会话要是还被这边认领着，
        那边的认领就会被这边随后的 release 一起放掉。从名单里拿掉了的，最后关掉它的浏览器标签页。"""
        dropped = self.sessions.finish(session, self._outcome(session))
        try:
            self.publish_state()
        finally:
            session.ui.end_turn()
            self.sessions.unlock(session)
        if dropped:
            self.app.close_chat(session.chat)

    def _outcome(self, session: Session) -> str:
        """这一轮结束时，要是它在后台，会话栏上标什么：出了错（或者没存上盘）是 ERROR，好好跑完的是 DONE，你自己停下的不标。"""
        if session.ui.stop_requested():
            return ""
        _, events = session.ui.last_turn_events()
        failed = session.unsaved or any(event.get("type") == "notice" and event.get("level") == "error" for event in events)
        return ERROR if failed else DONE

    # ---- 确认 --------------------------------------------------------------------

    def answer_confirm(self, confirm_id: int, allowed: bool, feedback: str) -> bool:
        """答复一张确认卡片。编号是全局唯一的（见 ui.EventIds），只有在等它的那一段会收下。返回 False 表示没有谁在等它了。"""
        return any(session.ui.answer_confirm(confirm_id, allowed, feedback) for session in self.sessions.all())

    def session_waiting_on(self, confirm_id: object) -> Session | None:
        """哪一段正在等这张卡片的答复（/api/computer/trust 用：放开的只是那一段的电脑操作）。"""
        if not isinstance(confirm_id, int) or isinstance(confirm_id, bool):
            return None
        return next((session for session in self.sessions.all() if session.ui.waiting_on(confirm_id)), None)

    # ---- 停下、退出 --------------------------------------------------------------

    def stop(self, conversation_id: str | None = None) -> bool:
        """对话里的"停止"：只停这一段的这一轮（连同它正在运行的命令）。不给编号是你正看着的这一段。
        返回 False 表示这段会话现在没有在进行（不在名单里）。

        定时任务的运行各有各的界面和 shell，这里碰不到它们；停任务走 /api/task/stop。
        """
        session = self.sessions.viewed if conversation_id is None else self.sessions.find(conversation_id)
        if session is None:
            return False
        session.ui.request_stop()
        return True

    def stop_all(self) -> None:
        """退出前：每一段都停下（连同它们正在运行的命令）。"""
        for session in self.sessions.all():
            session.ui.request_stop()
            session.chat.shell.kill_running()

    def wait_idle(self, timeout: float) -> bool:
        """等每一段正在进行的一轮都结束（包括它收尾时的存盘）。等到了返回 True，超时返回 False。"""
        return self.sessions.wait(self.sessions.idle, timeout)

    def save_all(self) -> None:
        """退出前：上一次没存上盘的那几段再存一次（内容只在内存里，这是最后的机会）。"""
        for session in self.sessions.all():
            if session.unsaved and not session.running:
                self._try_save(session)


def route_stop(handler: Handler, web: WebApp, body: dict) -> None:
    conversation_id = body.get("conversation")
    if conversation_id is not None and not isinstance(conversation_id, str):
        handler._send_json(400, {"error": t("参数不正确")})
    elif web.stop(conversation_id):
        handler._send_json(200, {"ok": True})
    else:
        handler._send_json(409, {"error": t("这段会话现在没有在进行。")})
