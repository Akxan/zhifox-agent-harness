"""终端里正在聊的这一段：跑一轮、每一轮存盘、/retry 和 /edit 倒回最后一轮、/undo、/new 和 /open 换一段、/history 列出来。

网页上同样的事分在几处（webui/chat_api.py 发消息、rewind_api.py 倒回、conversations_api.py 新对话和打开），
这里是终端的那一份。终端只有一段对话、一个线程，用不着网页上的那几把锁。

存成什么样
----------
和网页上的会话是同一个文件、同一种格式（见 conversations.py）：messages 是发给模型的历史，events 是界面上显示过的内容。
终端自己用不着 events（屏幕上的字打出去就完了），可网页打开一段会话时是靠重放 events 把它画出来的，所以终端也记一份
（TerminalUI.log，和网页用的是同一个类，见 ui.py）：你的消息（带着倒回去用的位置 mark）、回答、工具卡片、确认和结果、
运行记录、改了哪些文件。存盘时和网页一样整理（WebUI.export_events），经过 App.save_conversation → ConversationStore.save，
搜索和多媒体的索引跟着更新（_remember）。所以终端里聊的，网页的会话栏里看得到、搜得到、打得开、能接着聊、能编辑和重新生成；
网页上聊的，终端里 /open 也一样。标题和网页的规矩一样：照着第一句话起（App.name_conversation），编辑了它就跟着换（App.retitle）。

什么时候存
----------
和网页一样，每一轮结束都存一次（被 Ctrl-C 打断的那一轮也存，第一条消息就没答出来的当它没发生过），所以最多只丢正在进行的那一轮。
/undo 撤回了文件、/retry 和 /edit 倒回去以后马上存一次（倒回去的那一截要从文件里去掉，这时退出，再打开看到的也是倒回去的样子）。
离开这一段（/new、/open、/exit、Ctrl-D）之前，上一次没存上的再存一次；还是存不上，先拦一下、说清楚，
再输入一次同样的命令才放弃（和网页上的"再点一次"一样，见 WebApp._before_leaving）。收到 SIGTERM（见 cli.py）时来不及问，
只再存一次。存不上只在屏幕上说一句为什么，终端照常能用：内容还在内存里，下一轮结束时再存。

谁能写
------
同一个数据目录同一时间只给一个智狐用（instance.py）：终端启动时也去占那把锁（kind 是 terminal），占到了才存盘、才能 /open。
网页界面、harness serve、后台服务开着的时候占不到：那边在内存里拿着会话列表，聊天通道可能正往某段会话里写
（claims.py 只管同一个进程里的几方，管不到别的进程），两边一起写会互相覆盖。这时终端照常能聊，只是不存盘（和以前一样），
/history 只是读，照样能看，/open 不让用。反过来，终端开着的时候网页界面、后台服务开不起来，会说是终端占着。
所以终端里不用再认领一段会话（claims.py）：能往会话里写的只有这一个进程，聊天通道根本没在运行。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from . import history_cli, i18n, instance, rewind
from .agents import AgentError
from .app import App, retry_turn, run_turn
from .attachments import Attachment
from .config import COMMAND, LARGE_CONTEXT_TOKENS, TERMINAL_HISTORY_ROWS
from .conversations import ConversationError
from .i18n import k, t
from .media import MediaError
from .providers import ProviderError, find, has_key
from .undo.restore import sets_in
from .undo.terminal import TerminalChanges

if TYPE_CHECKING:
    from .ui import TerminalUI

# 倒回去之前，这一轮操作过整台电脑、在浏览器里点过输入过的话，再单独说一句（网页上是同样的两句，见 webui/page/scripts/rewind.js 的 TURN_OPERATED）
OPERATED = {
    "computer": k("这一轮操作过整台电脑（点击、拖动、打字、按键）：在别的程序里已经做了的事，撤不回来。"),
    "browser": k("这一轮在浏览器里点击、输入过：网页上已经提交、发出去的东西，撤不回来。"),
}


class TerminalChat:
    def __init__(self, app: App, ui: TerminalUI, saving: bool):
        """saving：占到了数据目录（见文件开头"谁能写"）。没占到时照常聊，只是不存盘，也不让 /open。"""
        self.app = app
        self.ui = ui
        self.saving = saving
        self.changes = TerminalChanges(app, ui)  # 这一段里文件工具改了哪些文件（/undo 用）
        self.dirty = False  # 内存里有还没写到磁盘上的内容（这一轮刚跑完，或者上一次没存上）
        self.unsaved = ""  # 上一次没存上的原因
        self.discard_ok = ""  # 已经提醒过"离开会丢掉没存上的内容"的那个命令：再输入一次就放弃（见 leave）
        self.listed: list[str] = []  # /history 最近一次列出来的会话编号：/open 3 打开的是其中第 3 个

    # ---- 一轮 -------------------------------------------------------------

    def ask(self, text: str) -> None:
        """发一条消息，跑一轮。"""
        if not self._key_ready(self.app.agent.backend.provider):
            return
        app = self.app
        app.name_conversation(text)  # 新会话用第一句话当标题（和网页一样）
        # 记下这一轮从历史的哪个位置开始：以后 /edit、/retry（网页上的编辑、重新生成也一样）从这里倒回去
        self.ui.begin_turn(text, mark=app.agent.backend.mark())
        self._run(lambda: run_turn(app.agent, self.ui, text), sends_message=True)

    def _run(self, work: Callable[[], None], sends_message: bool) -> None:
        """跑一轮（日志里这一轮已经开始了），收尾：记下改了哪些文件、结束这一轮、让出屏幕、存盘。
        哪一步出了意外都照样往下收尾（finally）：存盘放在最后，这一轮的结尾（turn_end）也存进去。"""
        self.dirty = True
        try:
            self.changes.record(work, sends_message=sends_message)  # 这一轮文件工具改的文件记下来，/undo 能撤回
        finally:
            try:
                self.ui.end_turn()
                self.app.chat_desktop.release()  # 还按着的鼠标键松开，屏幕让出来（和网页界面一样）
            finally:
                self.save()

    # ---- 倒回最后一轮（/retry、/edit）------------------------------------------

    def rewind(self, command: str, argument: str) -> None:
        """/retry：重新生成最后一个回答；/edit：改最后一条消息再发。

        和网页界面一样只能倒最后一轮（原因见 rewind.py 开头），做法也一样（见 webui/rewind_api.py 的 rewind）：
        最后一轮从事件日志里找（打开的旧会话也有），这一轮做过可能改动了电脑的事先问一次（倒回去的只是对话），
        历史和事件一起截断、马上存盘，再跑新的一轮。
        """
        app, ui = self.app, self.ui
        start, events = ui.log.last_turn_events()
        turn = rewind.last_turn(events, start)
        if turn is None:
            ui.error(t("这段对话里还没有可以重新生成或编辑的消息。"))
            return
        backend = app.agent.backend
        problem = rewind.problem(turn, backend, app.conversation.task)
        if problem:
            ui.error(problem)
            return
        mark = turn.mark
        assert mark is not None  # 给类型检查看的：没记位置的，上面的 problem 已经说过了
        if not self._key_ready(self.app.agent.backend.provider):
            return
        text = turn.text
        if command == "/edit":
            if not argument and not ui.interactive:
                ui.error(t("用法：/edit 新的文字"))
                return
            text = (argument or ui.read_input(default=turn.text) or "").strip()
            if not text or text == turn.text:
                ui.info(t("没有改动，已取消。"))
                return
        found = rewind.effects(turn.events)
        if found:
            lines = "\n".join(f"    {line}" for line in rewind.shown_effects(found))
            # 终端里只打印一次、不用重译，所以后面接上一句时直接拼成普通字符串
            warning: str = t("这一轮做过可能改动了电脑的事：\n{lines}\n倒回去的只是对话，这些已经做了的事不会撤销。", lines=lines)
            for kind in rewind.operated(turn.events):  # 操作过整台电脑、在浏览器里点过输入过：再单独说一句
                warning = f"{warning}\n{t(OPERATED[kind])}"
            if not ui.agree(warning):
                ui.info(t("已取消。"))
                return
        kept = command == "/retry" and backend.turn_at(mark)  # 重新生成、用户消息还在历史里：原样留着它
        attachments: list[Attachment] = []
        if not kept and turn.attachments:
            # 编辑、再试一次要把这条消息的附件原样再发一次（网页上发的才有：终端发不了附件），从多媒体库取回原件
            try:
                attachments = rewind.attachments_of(turn, None, app.media, backend.export(), app.conversation.created_at)
            except MediaError as exc:
                ui.error(str(exc))
                return
            problem = app.attachment_problem(attachments)  # 换过模型的话，现在这个可能看不了图片
            if problem:
                ui.error(problem)
                return
        if command == "/edit":
            app.retitle(turn.text, turn.attachments, text, attachments, earlier=ui.log.user_events()[:-1])
        backend.rollback(mark + 1 if kept else mark)
        # 这一轮从对话里去掉了：撤回过它改的文件、还没来得及告诉模型的那几句也不用说了（模型本来就不知道它改过）
        app.changes.notes.drop(app.conversation.id, sets_in(turn.events))
        ui.log.rewind(keep_user=command == "/retry")
        self.dirty = True
        self.save()  # 倒回去的那一截马上从文件里去掉（和网页一样）
        if command == "/edit":
            ui.begin_turn(text, attachments, mark=backend.mark())
            self._run(lambda: run_turn(app.agent, ui, text, attachments), sends_message=True)
        elif kept:
            ui.begin_again()
            self._run(lambda: retry_turn(app.agent, ui, mark), sends_message=False)
        else:
            # 再试一次：上一次没答出第一步就失败了，消息已经撤回，同样的话（和附件）整条再发一次，运行记录上标出来
            ui.begin_again()
            self._run(lambda: run_turn(app.agent, ui, text, attachments, again=True), sends_message=True)

    # ---- 撤回文件（/undo）-------------------------------------------------------

    def undo(self) -> None:
        if self.changes.undo():
            self.dirty = True
            self.save()  # 撤回的记录（file_undo）存进会话：网页上打开，那一行写着"已撤回"

    # ---- 存盘、离开 -----------------------------------------------------------

    def save(self) -> None:
        """有还没存上的内容就存一次。存不上在屏幕上说一句为什么（和网页上同一句），内容还在内存里，下一次再存。"""
        if not self.saving:
            self.dirty = False  # 别的智狐占着数据目录：说好了不存（见文件开头）
            return
        if not self.dirty:
            return
        try:
            self.app.save_conversation(self.ui.saved_events())
        except Exception as exc:  # noqa: BLE001 —— 什么错都接住：存不上不能让终端退出，下一轮结束时再存
            self.unsaved = str(exc) or type(exc).__name__
            self.ui.error(t("保存会话失败：{error}", error=self.unsaved))
        else:
            self.dirty = False
            self.unsaved = self.discard_ok = ""

    def leave(self, command: str) -> bool:
        """要离开这一段了（command 是 /new、/open 某段、/exit）。返回 False 表示先别走（已经说了为什么）。

        上一次没存上的，一走就没了：先再存一次。还是存不上，拦一下；紧接着再输入一次同样的命令，就当你确认要放弃。"""
        self.save()
        if not self.dirty or self.discard_ok == command:
            return True
        self.discard_ok = command
        self.ui.error(t("当前这段会话还没能保存（{error}），现在离开会丢掉它的内容。可以先解决保存的问题；确实要放弃的话，再输入一次 {command}。", error=self.unsaved, command=command))
        return False

    def close(self) -> None:
        """程序退出前（包括收到 SIGTERM 的时候）：还有没存上的，最后再存一次。"""
        self.save()

    def _left(self) -> None:
        """已经换到另一段了：关于原来那段的"没存上""确认放弃"都不用再记着，撤回文件的账也换成新的这一段的。"""
        self.dirty = False
        self.unsaved = self.discard_ok = ""
        self.changes = TerminalChanges(self.app, self.ui)

    # ---- 换一段（/new、/open）----------------------------------------------------

    def new(self) -> None:
        """/new：开始一段新对话（和网页上的"新对话"一样，见 App.new_conversation）。之前那段已经在磁盘上，/open 能再打开。"""
        if not self.leave("/new"):
            return
        try:
            self.app.new_conversation()
        except (AgentError, ProviderError) as exc:
            self.ui.error(i18n.message_of(exc))
            return
        self.ui.reset()
        self._left()
        self.ui.info(t("已开始新对话。"))

    def open(self, argument: str) -> None:
        """/open 编号（/history 列出来的）或者会话的编号：换到那一段接着聊。

        换模型的规矩和网页上打开一段会话一样，都在 App.open_conversation 里：换回这段会话当时用的那一家、那个模型，
        历史交还给适配层；它是用一个 Agent 当角色开始的，照着这个 Agent 现在的样子接着聊（用不了就说一声）；
        MCP 的工具重新拍一张快照（App.fresh_tools）。会失败的步骤在换东西之前做完，失败了什么都不换。"""
        if not self.saving:
            self.ui.error(t("数据目录正被另一个智狐用着，不能在这里打开会话（两边一起写会互相覆盖）。在它的网页里打开，或者先关掉它，再重新启动终端。"))
            return
        target = self._target(argument)
        if target is None:
            return
        app, ui = self.app, self.ui
        if target == app.conversation.id:
            ui.info(t("这段会话已经开着了。"))
            return
        # 这段会话用的那一家还没有密钥：不打开。打开了也接着聊不了，而且打开就把"现在用的模型"记成了它（和网页一样，
        # 见 App._continue），下次启动终端时一看没有密钥，就只能退出了。在网页上能先打开看看、再到设置里填
        listed = next((item for item in app.store.summaries() if item["id"] == target), None)
        if listed is not None and not self._key_ready(listed["provider"], opening=True):
            return
        if not self.leave(f"/open {argument}"):
            return
        try:
            conversation = app.open_conversation(target)
        except (ConversationError, ProviderError) as exc:
            ui.error(i18n.message_of(exc))
            return
        ui.load(conversation.events)
        self._left()
        for warning in app.take_warnings():  # 角色用不了了、没能记住这次的模型……
            ui.error(warning)
        for text, style in history_cli.recap(conversation, ui.saved_events()):
            (ui.plain if style == "plain" else ui.info)(text)
        if conversation.context_tokens >= LARGE_CONTEXT_TOKENS:
            ui.info(t("这段会话的历史约有 {tokens} 个 token。接着聊的第一条消息需要把它重新发给模型，会按这个量计费。", tokens=i18n.number(conversation.context_tokens)))

    def _target(self, argument: str) -> str | None:
        """/open 后面写的是哪一段：数字是 /history 最近一次列出来的第几个（还没列过的话，按最近的那些数），
        别的当作会话的编号。认不出来的说明为什么，返回 None。"""
        if not argument:
            self.ui.error(t("用法：/open 编号（/history 列出来的），或者 /open 会话的编号"))
            return None
        if argument.isdecimal():
            listed = self.listed or [item["id"] for item in self.app.conversation_summaries()[:TERMINAL_HISTORY_ROWS]]
            number = int(argument)
            if not 1 <= number <= len(listed):
                self.ui.error(t("没有第 {n} 段。先用 /history 看看有哪些。", n=number))
                return None
            return listed[number - 1]
        if argument == self.app.conversation.id or self.app.store.exists(argument):
            return argument
        self.ui.error(t("这段会话不存在，可能已经被删除了。"))
        return None

    # ---- 列出来、搜索（/history）-------------------------------------------------

    def history(self, words: str) -> None:
        """/history：最近的会话；/history 几个词：在所有保存过的会话里搜（和网页的搜索是同一个索引，见 search.py）。
        只是读：别的智狐占着数据目录的时候也能看。"""
        current = self.app.conversation.id
        if words:
            results = self.app.store.search(words)[:TERMINAL_HISTORY_ROWS]
            lines = history_cli.found(results, words, current)
            self.listed = [item["id"] for item in results]
        else:
            # 和网页的会话栏同一份：磁盘上的，加上正在聊、还没来得及存盘的这一段
            items = self.app.conversation_summaries()[:TERMINAL_HISTORY_ROWS]
            lines = history_cli.listing(items, current)
            self.listed = [item["id"] for item in items]
        for text, style in lines:
            (self.ui.plain if style == "plain" else self.ui.info)(text)

    # ---- 小帮手 ---------------------------------------------------------------

    def _key_ready(self, provider_id: str, opening: bool = False) -> bool:
        """这一家有没有 API 密钥（自定义接口总算有）。没有的话说清楚去哪儿填，返回 False：不发请求（发了也只会被拒绝）。
        opening：是要打开一段用这一家的会话（这时没打开）。这一家不在了（自定义接口被删掉了）不在这里说：
        打开时 App.open_conversation 会说清楚。"""
        provider = find(provider_id)
        if provider is None or has_key(provider):
            return True
        if opening:
            head = t("没有打开：这段会话用的是 {provider}，还没有设置 {provider} 的 API 密钥。", provider=provider.label)
        else:
            head = t("这段对话用的是 {provider}，还没有设置 {provider} 的 API 密钥。", provider=provider.label)
        how = t(
            "在 {env_file} 里加上一行 {key_env}=你的key（或者在网页界面 {command} --web 的“设置”里填），再重新启动终端。",
            env_file=self.app.config.env_file, key_env=provider.key_env, command=COMMAND,
        )
        self.ui.error(f"{head}{how}")  # 终端里只打印一次、不用重译，拼成普通字符串就行
        return False


def busy_lines(data_dir: Path) -> list[str]:
    """占不到数据目录时，启动后先说的几行：这段终端对话不会保存，为什么、是谁占着（instance.busy_lines 说的是谁、怎么关掉它）。"""
    return [t("这段终端对话不会保存（/history 照样能看，/open 用不了）：同一个数据目录同一时间只能有一个智狐往里写。"), *instance.busy_lines(data_dir)]
