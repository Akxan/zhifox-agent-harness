"""倒回最后一轮：编辑最后一条消息、重新生成最后一个回答（规矩见 ../rewind.py 开头）。

  POST /api/turn/edit    编辑最后一条消息（text、keep：留下第几个附件）
  POST /api/turn/retry   重新生成最后一个回答
  两个都带 conversation 和 turn（页面看到的那一轮的编号），对不上就回答 409；confirmed、undo_files、undo_force 见 rewind

RewindActions 混进 WebApp（见 webapp.py）：只有方法，用到的状态都在 WebApp.__init__ 里。
  _rewind_state    状态里的 rewind：最后一轮能不能倒、为什么不能、倒之前要不要问你（页面的按钮只照着它画）
  _rewind_problem  为什么倒不回去
  rewind           真的倒回去：和发消息一样拿这一段的锁、认领会话，截断以后马上存盘，新的一轮交给 _run_turn（见 chat_api.py）。
                   只倒你正看着的那一段（页面带来的会话编号对不上就 409）：拿着它的锁、它又没在跑，这时谁也换不走"正看着哪一段"，
                   所以这里用 self.ui、self.app.agent 这些就是它的；新的一轮在后台线程里只用它自己的（Session）
  _undo_with_rewind  倒回去之前先撤回这一轮改的文件（确认卡片上勾了"同时撤回"）

route_* 由 server.py 的 Handler._route 调用（写法和约定见 server.py 开头）。
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from .. import media, rewind
from ..attachments import MAX_ATTACHMENTS, Attachment
from ..i18n import t
from ..providers import has_key
from ..undo.restore import sets_in as rewind_sets
from .changes_api import issues
from .chat_api import MAX_MESSAGE_CHARS
from .ui import said
from .webapp_types import WebAppPart

if TYPE_CHECKING:
    # 类里的注解写 rewind.Turn 不行：在 RewindActions 的类体里，rewind 是它的方法 rewind，不是这个模块
    from ..rewind import Turn
    from .server import Handler
    from .webapp import WebApp


class RewindActions(WebAppPart):
    """倒回最后一轮：能不能倒、真的倒回去。"""

    def _rewind_state(self) -> dict | None:
        """状态里的 rewind：最后一轮能不能倒回去，不能的话为什么，倒回去之前要不要先问。一条用户消息都没有时是 None。

          turn     这一轮的 user 事件的编号。页面发请求时带回来（连同会话的编号），这边对一下，免得倒回的是别的一轮
          mark     这一轮在历史里的位置（见 rewind.py）。只是给人看、给测试看的，页面不用
          ok       能不能编辑、重新生成（正在进行一轮时页面自己把按钮收起来，这里不管）
          reason   不能的原因（换语言时页面照着原文重译）
          effects  这一轮做过的、可能改动了东西的事（每件一行）。不是空的，倒回去之前在原地问你一次
          more     太多了没列出来的还有几项（"……还有几项"那一句由页面自己写）
          operated 这些事里有没有操作过整台电脑（"computer"）、在浏览器里点过输入过（"browser"）：页面为它们单独写一句"撤不回来"
          files    这一轮文件工具改的文件里还有几个没撤回（见 undo/）。不是 0 的话，确认卡片上可以勾"同时撤回这一轮改的文件"
        """
        start, events = self.ui.last_turn_events()
        turn = rewind.last_turn(events, start)
        if turn is None:
            return None
        problem = self._rewind_problem(turn)
        effects, more = rewind.listed(rewind.effects(turn.events))
        return {
            "turn": turn.id,
            "mark": turn.mark,
            "ok": not problem,
            "reason": said(problem) if problem else None,
            "effects": effects,
            "more": more,
            "operated": rewind.operated(turn.events),
            "files": self.app.changes.applied(self.app.changes.last_set(turn.events), turn.events),
        }

    def _rewind_problem(self, turn: rewind.Turn) -> str:
        """最后一轮为什么倒不回去；能倒回去返回空字符串。"""
        conversation, backend = self.app.conversation, self.app.agent.backend
        if turn.mark is None:
            return rewind.problem(turn, backend, conversation.task)  # 没记下位置（旧版本、定时任务自己跑的）
        platform = self.channels.holder(conversation.id)
        if platform:
            return t("这段会话正在 {platform} 上进行。等那边这一轮结束再试。", platform=platform)
        running = self.tasks.running(conversation.task)
        if running:
            return t("定时任务“{title}”正在运行。等它跑完再编辑或重新生成。", title=running)
        return rewind.problem(turn, backend)  # 还在不在历史里（终端也照着它说，见 rewind.problem）

    def rewind(
        self, action: str, target: tuple[str, int], text: str = "", keep: list[int] | None = None, confirmed: bool = False, undo_files: bool = False, undo_force: bool = False
    ) -> tuple[str, dict]:
        """编辑最后一条消息（action 是 edit，text 是改过的文字，keep 是留下第几个附件，None 是全部）、
        重新生成最后一个回答（retry）。返回（不能做的原因，回答里另外带的东西）；做成了返回空字符串，新的一轮在后台开始。

        target 是页面看到的那一轮：（会话的编号，它的 user 事件的编号）。和现在的最后一轮对不上（另一个标签页刚倒回过、
        已经换了一段会话、聊天通道刚聊过一轮），就不做。
        这一轮做过可能改动了东西的事、而你还没有确认（confirmed），也不做，回答里带上 effects，页面在原地问你。

        undo_files：倒回去之前先撤回这一轮文件工具改的文件（确认卡片上勾了"同时撤回这一轮改的文件"，见 undo/）。
        有要你确认的（这一轮之后文件又被改过……）、又没有 undo_force，就先不做，回答里带上 undo（asks、blocked），页面在卡片上问你。
        一起撤回的不记 file_undo 事件、不告诉模型：这一轮马上就从对话里去掉了，它改文件的记录也跟着去掉。

        和发消息一样拿着对话的锁、认领这段会话（见 claims.py），新的一轮跑完才放开。倒回去以后马上存一次盘：
        会话文件里的 messages 和 events 一起截断，搜索和多媒体的索引跟着更新（ConversationStore.save → _remember），
        这时候程序退出，再打开看到的也是倒回去以后的样子。
        """
        provider = self.app.provider
        if not has_key(provider):
            return t("还没有设置 {provider} 的 API 密钥，请先点右上角的“设置”填写。", provider=provider.label), {}
        session = self.sessions.viewed
        if not self._take(session):
            return self._busy(t("上一轮对话还没有结束")), {}
        started = claimed = counted = False
        try:
            elsewhere = self._claim(session)
            if elsewhere:
                return elsewhere, {}
            claimed = True
            start, events = self.ui.last_turn_events()
            turn = rewind.last_turn(events, start)
            if turn is None or turn.mark is None or (self.app.conversation.id, turn.id) != target:
                self.publish_state()  # 页面上看到的已经过时了：先把最新的发过去
                return t("对话刚刚变过了，最后一轮已经不是这一条。看一眼最新的内容再试。"), {}
            mark = turn.mark
            problem = self._rewind_problem(turn)
            if problem:
                return problem, {}
            found = rewind.effects(turn.events)
            if found and not confirmed:
                effects, more = rewind.listed(found)
                extra = {"effects": effects, "more": more, "operated": rewind.operated(turn.events)}
                return t("这一轮做过可能改动了电脑的事，倒回去之前要先确认。"), extra
            # 重新生成、编辑都会开始新的一轮：同时在跑的到了上限，就先不倒回去（不然倒回去了却开始不了，那一轮白白没了）。
            # 在这里就算上它（Sessions.start）；后面因为别的原因没开始的话，在 finally 里再去掉
            if not self.sessions.start(session, self._parallel_limit()):
                return self._too_many(), {}
            counted = True
            backend = self.app.agent.backend
            kept = action == "retry" and backend.turn_at(mark)  # 重新生成、用户消息还在历史里：原样留着它
            attachments: list[Attachment] = []
            if not kept:
                try:
                    attachments = rewind.attachments_of(turn, keep if action == "edit" else None, self.app.media, backend.export(), self.app.conversation.created_at)
                except media.MediaError as exc:
                    return str(exc), {}
                text = text if action == "edit" else turn.text
                if not text.strip() and not attachments:
                    return t("消息不能为空，也不能太长"), {}
                problem = self.app.attachment_problem(attachments)  # 换过模型的话，现在这个可能看不了图片
                if problem:
                    return problem, {}
            if undo_files:
                problem, extra = self._undo_with_rewind(turn, undo_force)
                if problem:
                    return problem, extra
            if action == "edit":
                self.app.retitle(turn.text, turn.attachments, text, attachments, earlier=self.ui.user_events()[:-1])
            backend.rollback(mark + 1 if kept else mark)
            # 这一轮从对话里去掉了：撤回过它改的文件、还没来得及告诉模型的那几句也不用说了（模型本来就不知道它改过）
            self.app.changes.notes.drop(self.app.conversation.id, rewind_sets(turn.events))
            self.ui.rewind(keep_user=action == "retry")
            self._save(session)
            self.publish_state()  # 重放到这里结束：之后新的一轮照常播放入场动画
            threading.Thread(target=self._run_turn, args=(session, turn.text if kept else text, attachments, mark if action == "retry" else None), daemon=True).start()
            started = True
            return "", {}
        finally:
            if not started:
                if counted:
                    self.sessions.finish(session, "")  # 没开始：不算在"同时在跑"里（它是正看着的那一段，不会被拿掉）
                if claimed:
                    self.claims.release(session.id, "web")
                self.sessions.unlock(session)

    def _undo_with_rewind(self, turn: Turn, force: bool) -> tuple[str, dict]:
        """倒回最后一轮之前，撤回它改的文件。返回（不能接着倒回去的原因，回答里另外带的东西）。调用的人拿着对话的锁。"""
        changes = self.app.changes
        set_id = changes.last_set(turn.events)
        if not set_id or not changes.applied(set_id, turn.events):
            return "", {}  # 这一轮没改文件，或者已经都撤回了
        result = changes.act("undo", set_id, self.ui.export_events(), None, force)
        details = {"undo": issues(result)}
        failed = any("failed" in item["reasons"] for item in result.blocked)
        if result.event is None:
            if force and not failed:
                return "", {}  # 你点了"仍然撤回"：做不了的跳过；一个都做不了（不能碰的文件、留底坏了……）也照样倒回去
            return result.error, details
        if failed:
            # 有的写不进去（被别的程序占着、没有权限）：已经撤回的照实记下来，这一轮先不倒回去，免得改动的记录跟着没了
            self.ui.publish("file_undo", **result.event)
            self._save()
            self.publish_state()
            return t("有的文件没能撤回，这一轮没有倒回去。看一眼那一行的说明再试。"), details
        return "", {}


def route_turn(handler: Handler, web: WebApp, path: str, body: dict) -> None:
    action, confirmed = path.removeprefix("/api/turn/"), body.get("confirmed", False)
    conversation_id, turn_id = body.get("conversation"), body.get("turn")
    text, keep = body.get("text", ""), body.get("keep")
    undo_files, undo_force = body.get("undo_files", False), body.get("undo_force", False)
    bad = not isinstance(conversation_id, str) or not isinstance(turn_id, int) or isinstance(turn_id, bool) or not isinstance(confirmed, bool)
    bad = bad or not isinstance(undo_files, bool) or not isinstance(undo_force, bool)
    if action == "edit":
        bad = bad or not isinstance(text, str) or len(text) > MAX_MESSAGE_CHARS
        indexes = keep if isinstance(keep, list) else []
        bad = bad or not (keep is None or isinstance(keep, list))
        bad = bad or len(indexes) > MAX_ATTACHMENTS or len(set(map(repr, indexes))) != len(indexes)
        bad = bad or not all(isinstance(index, int) and not isinstance(index, bool) for index in indexes)
    if bad:
        handler._send_json(400, {"error": t("参数不正确")})
    else:
        assert isinstance(conversation_id, str) and isinstance(turn_id, int)  # 给类型检查看的：上面的 bad 已经查过
        target = (conversation_id, turn_id)
        error, extra = web.rewind(action, target, text.strip() if action == "edit" else "", keep if action == "edit" else None, confirmed, undo_files, undo_force)
        if error:
            handler._send_json(409, {"error": error, **extra})
        else:
            handler._send_json(200, {"ok": True})
