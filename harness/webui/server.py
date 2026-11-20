"""本地 HTTP 服务器：浏览器和 agent 之间的桥。

浏览器只会发这几种请求：

  GET  /               页面本身（index.html）
  GET  /events         事件流。这个连接一直不关，服务器把 WebUI 日志里的新事件不断推过去。
                       刚连上时先补发已有的事件，然后发一个 synced 标记，后面才是实时的事件
  POST /api/message    用户发了一条消息（可以带附件：图片、PDF、文本文件，见 attachments.py）
  POST /api/confirm    用户对一个确认请求点了"允许"或"拒绝"
  POST /api/stop       用户点了"停止"
  POST /api/new        用户点了"新对话"
  POST /api/conversation/open、rename、delete   打开、重命名、删除一段保存过的会话
  POST /api/conversation/reveal                 在系统的文件管理器里显示一段会话的文件
  POST /api/memory/read、save、delete           查看、修改（内容和名字）、删除一条记忆（data/memories/ 下的一个文件，见 memories.py）
  POST /api/memory/reveal                       在系统的文件管理器里打开记忆文件夹
  POST /api/memory/show                         在系统的文件管理器里显示一条记忆的文件
  POST /api/memory/refresh                      页面回到前台：记忆文件夹变了的话，发一次新的状态
  POST /api/prompt     读出现在用的提示词（模板、内置的版本、占位符和实际发出去的样子）
  POST /api/prompt/save、reset、preview          保存改过的提示词、恢复内置的、预览还没保存的
  POST /api/search     在所有保存过的会话里搜索（标题和内容）
  POST /api/approval   用户更改了"要不要每次都问"（每次都问 / 改文件不用问 / 全部自动允许）
  POST /api/language   用户换了界面语言（中文 / English / Español）
  POST /api/model      用户换了模型
  POST /api/key        用户填写或删除了一家的 API 密钥
  POST /api/models     用户点了"同步模型"
  POST /api/price      用户填写了一个模型的价格
  POST /api/task/save、delete、toggle           新建或修改、删除、打开或关掉一个定时任务（见 tasks.py 和 scheduler.py）
  POST /api/task/run、stop                      立即运行一次、停下正在排队或者正在运行的那一次
  POST /api/task/confirm                        答复定时任务停在那里等的确认（没人看着时它要你批准才动手）
  POST /api/task/dismiss                        关掉一条（或者全部）已经弹出的提醒
  POST /api/task/preview                        还没保存的时间表接下来会在哪三个时刻运行

安全。这个服务器能让助手在你的电脑上执行命令，所以必须保证只有你自己打开的那个页面能用它。
你平时访问的任何网站，都可以让浏览器向 127.0.0.1 发请求，所以光靠"只在本机"是不够的：

  1. 只监听 127.0.0.1，局域网里的其他电脑连不上。
  2. 每次启动生成一个随机口令（token），写在打开页面用的地址里。所有请求都必须带上它；
     别的网站不知道口令，请求会被拒绝。
  3. 检查 Host 和 Origin 请求头，拒绝从别的网站发起的请求。
  4. 页面带有内容安全策略（CSP）：只运行我们自己的脚本，不加载任何外部资源。
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import IO
from urllib.parse import parse_qs, urlsplit

from .. import i18n, memories
from .. import prompt as prompts
from ..app import App, build_app, run_turn
from ..attachments import Attachment, AttachmentError
from ..attachments import parse as parse_attachments
from ..config import APP_NAME, MAX_PROMPT_CHARS, Config
from ..conversations import ConversationError
from ..i18n import k, t
from ..memories import MemoryConflict, MemoryFileError
from ..permissions import MODES
from ..pricing import estimate, price_of
from ..providers import CLAUDE_MODELS, PROVIDERS, ProviderError, has_key
from ..safe import clean, private_dir, to_json
from ..search import MAX_QUERY_CHARS
from ..settings import key_hint, valid_api_key
from ..tasks import TaskError, TaskRefused
from ..tools.shell import SECRET_ENV_VARS
from .scheduler import Scheduler
from .ui import WebUI, latest_state_only

DEFAULT_PORT = 8765
MAX_BODY_BYTES = 1_000_000
# 发消息的请求可以带附件（base64 编码后比原文件大三分之一），所以单独放宽到这么多。别的请求都是几行设置，1MB 足够
MAX_MESSAGE_BODY_BYTES = 64_000_000
MAX_DISCARD_BYTES = 16_000_000  # 请求体太大时，最多读掉这么多再回答（见 Handler._read_json）
SHUTDOWN_WAIT_SECONDS = 5  # 退出时最多等这么久，让正在进行的一轮收尾存盘
MAX_MESSAGE_CHARS = 100_000
KEEPALIVE_SECONDS = 15
LARGE_CONTEXT_TOKENS = 30_000  # 打开的旧会话超过这么大时，提醒一下接着聊的费用

PAGE = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")


def reveal_in_file_manager(path: Path) -> None:
    """在系统的文件管理器里显示一个文件或文件夹（macOS 上是访达）。失败时抛出 OSError。

    这是整个服务器里唯一一处"页面点一下，电脑上就启动一个程序"的地方，所以守两条规矩：
    路径只能是程序自己算出来的（见 WebApp.reveal_conversation、reveal_memories 和 show_memory：浏览器只能给
    会话编号、记忆文件夹里的一个相对路径（逐层检查过，出不了那个文件夹），或者什么都不给）；命令用参数列表交给系统，不经过 shell，文件名里有什么字符都不会被当成命令的一部分。
    """
    if sys.platform == "darwin":
        command = ["open", "-R", str(path)] if path.is_file() else ["open", str(path)]  # -R：在访达里选中这个文件，而不是打开它
    else:
        # Linux 上没有通用的"选中某个文件"的办法，退一步，打开它所在的文件夹
        command = ["xdg-open", str(path if path.is_dir() else path.parent)]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # 有的系统上文件管理器会作为我们的子进程启动、一直开着。不让它带着各家的 API 密钥
        # （从它里面打开的终端都能看到环境变量），也不让它因为我们退出而被一起关掉
        env={name: value for name, value in os.environ.items() if name not in SECRET_ENV_VARS},
        start_new_session=True,
    )
    try:
        if process.wait(timeout=5) != 0:
            raise OSError(t("{command} 没有成功（退出码 {code}）", command=command[0], code=process.returncode))
    except subprocess.TimeoutExpired:
        pass  # 有的文件管理器会一直开着不退出，那也算打开了，不用等它


class WebApp:
    """把助手接到网页界面上，并保证同一时间只进行一轮对话。"""

    def __init__(self, app: App, ui: WebUI, token: str):
        self.app = app
        self.ui = ui
        self.token = token
        self._turn = threading.Lock()  # 谁拿着这把锁，谁就在进行一轮对话（或者正在改设置）
        # 发状态快照时用的小锁："算出快照"和"发出去"要一口气做完。同步模型、改价格这些不拿对话锁的操作
        # 可能和一轮对话同时发快照，不这样的话，先算的旧快照可能反而后发出去，把新的盖掉。
        self._publishing = threading.Lock()
        self._unsaved = ""  # 当前会话上一次存盘失败的原因。空字符串表示内容都在磁盘上了
        self._discard_ok = ""  # 用户已经被提醒过一次"离开会丢内容"的那个操作；紧接着再做一次同样的操作就放行
        self._memories_shown: list[dict] = []  # 上一次发给页面的记忆列表（见 refresh_memories）
        # 定时任务：到时间了自己跑一轮（和你的对话轮流用 self._turn），或者弹出一条提醒。见 scheduler.py
        self.tasks = Scheduler(self, self._turn)
        ui.on_stop(app.shell.kill_running)  # 点"停止"时，连正在运行的命令一起结束
        app.agent.on_usage = self.publish_state  # 每调用完一次模型，就刷新一次用量和费用
        if app.tasks.problem:
            ui.hint(app.tasks.problem, level="error")  # 任务文件读不出来：说一声，原来的文件留在哪儿
        self.publish_state()

    def publish_state(self) -> None:
        """把页面要显示的状态（当前模型、各家的情况、待办、用量、费用）作为一个事件发给浏览器。

        注意这里永远不包含 API key 本身，只有"有没有设置"和最后 4 位。
        """
        with self._publishing:
            self._publish_state()

    def _publish_state(self) -> None:
        agent, settings, config = self.app.agent, self.app.settings, self.app.config
        usage = agent.usage
        cost, unpriced = estimate(agent.usage_by_model, settings.prices)
        price, offpeak = price_of(settings.model, settings.prices), price_of(settings.model, settings.prices, offpeak=True)
        self._memories_shown = memories.list_files(config.memory_dir)
        tasks, reminders = self.tasks.snapshot()
        self.ui.publish(
            "state",
            provider=settings.provider,
            model=settings.model,
            workspace=str(config.workspace),
            conversation=self.app.conversation.id,
            title=self.app.conversation.title,
            conversations=self.app.conversation_summaries(),
            providers=[
                {
                    "id": provider.id,
                    "label": provider.label,
                    "has_key": has_key(provider),
                    "key_hint": key_hint(provider.key_env),
                    "key_page": provider.key_page,
                    "models": self._models(provider.id),
                    # 能收哪些附件：页面据此决定附件按钮能选什么、不能选的给出说明。文本文件哪一家都能收
                    "vision": provider.vision,
                    "pdf": provider.pdf,
                }
                for provider in PROVIDERS.values()
            ],
            price=price,
            price_offpeak=offpeak if offpeak != price else None,  # 分时段计价的模型（DeepSeek）的闲时价；不分时段的是空的
            todos=[
                {"id": item["id"], "title": item["title"], "due": item["due"]}
                for item in self.app.todos.items("pending")
            ],
            # 记忆文件夹里有哪些文件（只有名字、大小和修改时间，内容要看时另外读）。每次发状态都重新看一遍文件夹，
            # 所以模型在这一轮里记下或删掉的，下一次刷新状态时页面上就跟着变了，页面不用自己去轮询
            memories=self._memories_shown,
            prompt_custom=prompts.load_template(config) != prompts.DEFAULT_TEMPLATE,  # 提示词改过没有（设置里显示）
            usage={
                "requests": usage.requests,
                "input": usage.input_tokens,
                "cache_read": usage.cache_read_tokens,
                "cache_write": usage.cache_write_tokens,
                "output": usage.output_tokens,
                "searches": usage.searches,
                "context": usage.context_tokens,
            },
            cost=round(cost, 4),
            unpriced=unpriced,
            tools=[{"name": name, "local": local} for name, local in agent.backend.tool_names()],
            approval=agent.permissions.mode,  # 要不要每次都问：ask / edits / all
            language=settings.language,  # 界面语言：zh / en / es。和页面现在用的不一样时，页面会在原地换过去
            # 定时任务：每个任务的设置、最近的运行、正在运行的那一次的进度（格式见 scheduler.py 开头的说明），
            # 以及已经弹出、你还没关掉的提醒
            tasks=tasks,
            reminders=reminders,
        )

    def _models(self, provider_id: str) -> list[str]:
        """一家可以选择的模型：同步到的列表；Claude 在同步之前先用内置的几个。"""
        models = list(self.app.settings.models.get(provider_id) or (CLAUDE_MODELS if provider_id == "anthropic" else []))
        if provider_id == self.app.settings.provider and self.app.settings.model not in models:
            models.insert(0, self.app.settings.model)
        return models

    # ---- 对话 --------------------------------------------------------------

    def send(self, text: str, attachments: list[Attachment] | None = None) -> str:
        """开始一轮对话。成功返回空字符串，否则返回不能开始的原因。"""
        provider = self.app.provider
        if not has_key(provider):
            return t("还没有设置 {provider} 的 API 密钥，请先点右上角的“设置”填写。", provider=provider.label)
        if not self._turn.acquire(blocking=False):
            return self._busy(t("上一轮对话还没有结束"))
        # agent 循环是阻塞的（等模型、等用户确认），所以放到后台线程里跑，
        # 这样服务器还能继续响应浏览器的其他请求，比如"允许"和"停止"。
        threading.Thread(target=self._run_turn, args=(text, attachments or []), daemon=True).start()
        return ""

    def _run_turn(self, text: str, attachments: list[Attachment]) -> None:
        try:
            self._discard_ok = ""  # 接着聊了：之前那句"再点一次就放弃"不再算数
            self.app.name_conversation(text, attachments)  # 新会话用第一句话当标题（没写字的话用第一个附件的名字）
            self.ui.begin_turn(text, attachments)
            self.publish_state()  # 让新会话马上出现在列表里
            run_turn(self.app.agent, self.ui, text, attachments)
        finally:
            try:
                self.ui.end_stream()
                self._save()  # 每一轮结束都存一次盘，所以最多只会丢失正在进行的这一轮
                self.publish_state()
            finally:
                # "这一轮结束了"要等存盘和刷新状态都做完、马上就要放开锁的时候才通知页面。
                # 如果先通知再存盘，页面一收到通知就发来的请求会撞上还没放开的锁，被误判成"上一轮还没结束"。
                self.ui.end_turn()
                self._turn.release()

    def _save(self) -> None:
        """把当前会话存盘。存不上就在页面上说明原因；内容还在内存里，下一轮结束时会再存一次。"""
        problem = self._try_save()
        if problem:
            self.ui.error(t("保存会话失败：{error}", error=problem))

    def _try_save(self) -> str:
        """存盘。返回失败的原因（成功返回空字符串），并记下当前会话是不是还有没存上的内容。"""
        try:
            self.app.save_conversation(self.ui.export_events())
        except Exception as exc:  # noqa: BLE001 —— 什么错都要接住：这是在一轮对话收尾时调用的，漏出去的话页面永远等不到"结束"
            self._unsaved = str(exc) or type(exc).__name__
        else:
            self._unsaved = ""
            self._discard_ok = ""
        return self._unsaved

    def _before_leaving(self, action: str) -> str:
        """要离开当前会话了（新对话、打开别的会话、换到另一家）。返回不让走的原因；空字符串表示可以走。

        当前会话要是有没存上的内容（上一次存盘失败了），一走这些内容就没了，所以先再存一次。
        还是存不上的话拦一下、把话说清楚；用户紧接着再做一次同样的操作，就当他确认要放弃。
        """
        if not self._unsaved or not self._try_save():
            return ""
        if self._discard_ok == action:
            return ""
        self._discard_ok = action
        return t("当前这段会话还没能保存（{error}），现在离开会丢掉它的内容。可以先解决保存的问题；确实要放弃的话，再点一次。", error=self._unsaved)

    def _left(self) -> None:
        """已经离开了原来的会话：关于它的"没存上"和"确认放弃"都不用再记着。"""
        self._unsaved = self._discard_ok = ""

    def _show_warnings(self) -> None:
        """把助手攒下的提醒（比如"没能记住这次选的模型"）显示出来。只显示，不存进会话。"""
        for text in self.app.take_warnings():
            self.ui.hint(text, level="error")

    def _busy(self, otherwise: str | None = None) -> str:
        """对话的锁被占着时，告诉用户为什么。被定时任务占着的话，说是哪个任务、怎么停下它。"""
        title = self.tasks.holder()
        if title is not None:
            return t("定时任务“{title}”正在运行。可以等它跑完，或者在“任务”里停下它。", title=title)
        return otherwise or t(self.BUSY)

    def stop(self) -> None:
        # 锁被定时任务拿着时，这个"停止"不是冲它来的：停任务走 /api/task/stop。
        # 不然点一下对话的"停止"，会把任务正在运行的命令杀掉
        if self._turn.locked() and self.tasks.holder() is None:
            self.ui.request_stop()

    def wait_idle(self, timeout: float) -> bool:
        """等正在进行的一轮对话结束（包括它收尾时的存盘）。等到了返回 True，超时返回 False。"""
        if not self._turn.acquire(timeout=timeout):
            return False
        self._turn.release()
        return True

    def new_conversation(self) -> str:
        """清空对话，开始新的一段。成功返回空字符串，否则返回不能这样做的原因。"""
        if not self._turn.acquire(blocking=False):
            return self._busy()
        try:
            blocked = self._before_leaving("new")
            if blocked:
                return blocked
            self.app.new_conversation()
            self._left()
            self.ui.reset()
            self.publish_state()
        finally:
            self._turn.release()
        return ""

    # ---- 会话 --------------------------------------------------------------

    def open_conversation(self, conversation_id: str) -> str:
        """打开一段保存过的会话。成功返回空字符串，否则返回原因。"""
        if not self._turn.acquire(blocking=False):
            return self._busy()
        try:
            if conversation_id == self.app.conversation.id:
                return ""  # 已经开着了
            blocked = self._before_leaving(f"open:{conversation_id}")
            if blocked:
                return blocked
            try:
                conversation = self.app.open_conversation(conversation_id)
            except ConversationError as exc:
                return str(exc)
            self._left()
            self.ui.load_events(conversation.events)
            if conversation.context_tokens >= LARGE_CONTEXT_TOKENS:
                self.ui.hint(
                    t(
                        "这段会话的历史约有 {tokens} 个 token。接着聊的第一条消息需要把它重新发给模型，会按这个量计费。",
                        tokens=i18n.number(conversation.context_tokens),
                    )
                )
            self._show_warnings()
            self.publish_state()
        finally:
            self._turn.release()
        return ""

    def rename_conversation(self, conversation_id: str, title: str) -> str:
        # 改名要读出会话文件、改掉标题、再写回去。如果这时正好有一轮对话结束、也在写同一个文件，
        # 两边会互相覆盖。所以和打开、删除一样，对话进行中不让改。
        if not self._turn.acquire(blocking=False):
            return self._busy()
        try:
            try:
                self.app.rename_conversation(conversation_id, title)
            except ConversationError as exc:
                return str(exc)
            self.publish_state()
        finally:
            self._turn.release()
        return ""

    def delete_conversation(self, conversation_id: str) -> str:
        if not self._turn.acquire(blocking=False):
            return self._busy()
        try:
            try:
                was_current = self.app.delete_conversation(conversation_id)
            except ConversationError as exc:
                return str(exc)
            if was_current:
                self._left()  # 删的就是当前这一段：它有没有存上都无所谓了
                self.ui.reset()
            self.publish_state()
        finally:
            self._turn.release()
        return ""

    def reveal_conversation(self, conversation_id: str) -> tuple[str, str]:
        """在文件管理器里显示一段会话的文件；编号为空时显示保存会话的那个文件夹。返回（失败原因，显示的路径）。

        只是看一眼文件在哪儿，不改任何东西，所以不用拿对话的锁，对话进行中也可以点。
        """
        store, current = self.app.store, self.app.conversation
        if conversation_id == current.id and not current.saved:
            return t("这段会话还没有保存：第一轮对话结束后才会写到磁盘上。"), ""
        try:
            # 路径是由编号算出来的（编号的格式在 file_of 里检查过），浏览器没有办法指定别的路径
            target = store.file_of(conversation_id) if conversation_id else store.directory
        except ConversationError as exc:
            return str(exc), ""
        if not target.exists():
            return (t("还没有保存过会话，文件夹还不存在。") if not conversation_id else t("这段会话的文件已经不在了。")), ""
        try:
            reveal_in_file_manager(target)
        except OSError as exc:
            return t("没能打开文件管理器（{error}）。文件在：{path}", error=exc, path=target), ""
        return "", str(target)

    # ---- 记忆 --------------------------------------------------------------

    def delete_memory(self, relative: str) -> str:
        """删掉一条记忆。成功返回空字符串，否则返回原因。"""
        # 对话进行中不让删。模型可能正在改这条记忆：先 view 读出来，再 str_replace 改其中一段。
        # 这时删掉它，模型的下一步会莫名其妙地失败；或者它紧接着用 create 把刚读到的内容又写回去，
        # 你以为删掉了，其实它又回来了。和删除会话一样，等这一轮结束再删。查看和打开文件夹只是读，不受限制。
        if not self._turn.acquire(blocking=False):
            return self._busy()
        try:
            try:
                memories.delete(self.app.config.memory_dir, relative)
            except MemoryFileError as exc:
                return str(exc)
            self.publish_state()  # 列表跟着状态走：删完马上发一次
        finally:
            self._turn.release()
        return ""

    def save_memory(self, relative: str, name: str, text: str, modified: float | None, force: bool) -> tuple[str, str, bool]:
        """改一条记忆的内容和名字。返回（失败原因，改完之后的路径，是不是因为"这期间被改过了"才没保存）。

        和删除一样，对话进行中不让改：模型可能正读着它、马上要改其中一段，这时把内容换掉，它的下一步会对不上。
        """
        if not self._turn.acquire(blocking=False):
            return self._busy(), "", False
        try:
            try:
                saved = memories.save(self.app.config.memory_dir, relative, name, text, modified, force)
            except MemoryConflict as exc:
                return str(exc), "", True
            except MemoryFileError as exc:
                return str(exc), "", False
            self.publish_state()
        finally:
            self._turn.release()
        return "", saved, False

    def show_memory(self, relative: str) -> tuple[str, str]:
        """在文件管理器里显示一条记忆的文件（macOS 上是在访达里选中它）。返回（失败原因，显示的路径）。"""
        try:
            target = memories.file_of(self.app.config.memory_dir, relative)
        except MemoryFileError as exc:
            return str(exc), ""
        try:
            reveal_in_file_manager(target)
        except OSError as exc:
            return t("没能打开文件管理器（{error}）。文件在：{path}", error=exc, path=target), ""
        return "", str(target)

    # ---- 提示词 ------------------------------------------------------------

    def prompt_info(self) -> dict:
        config = self.app.config
        template = prompts.load_template(config)
        web = config.web and self.app.provider.web  # 预览按现在选的这一家来填"能不能联网"
        return {
            "text": template,
            "default": prompts.DEFAULT_TEMPLATE,
            "custom": template != prompts.DEFAULT_TEMPLATE,
            "preview": prompts.render(template, config, web),
            "placeholders": [{"name": name, "value": value} for name, value in prompts.placeholder_values(config, web).items()],
        }

    def save_prompt(self, text: str | None) -> tuple[str, bool]:
        """保存（None 表示恢复内置的）提示词。返回（失败原因，当前这段是不是也用上了）。

        和改设置一样拿对话的锁：当前这段还没开始聊的话，要换掉它的适配层，不能和正在进行的一轮撞上。
        """
        if not self._turn.acquire(blocking=False):
            return self._busy(), False
        try:
            try:
                applied = self.app.set_prompt(text)
            except prompts.PromptError as exc:
                return str(exc), False
            except OSError as exc:
                return t("没能保存提示词：{error}", error=exc.strerror or exc), False
            self.publish_state()
        finally:
            self._turn.release()
        return "", applied

    def refresh_memories(self) -> None:
        """页面刚连上、或者回到前台时问一声：记忆文件夹在这期间有没有变。

        模型改的，下一次发状态时自然就带上了；可你自己在访达里删的、改的，程序不会知道，页面上的列表就旧了，
        点开一条才发现"不存在"。所以页面在这两个时刻来问。变了就发一次新的状态，列表照常跟着状态走；
        没变就什么都不发，免得每切换一次窗口，日志里就多一张状态快照。
        """
        if memories.list_files(self.app.config.memory_dir) != self._memories_shown:
            self.publish_state()

    def reveal_memories(self) -> tuple[str, str]:
        """在文件管理器里打开记忆文件夹。返回（失败原因，打开的路径）。

        和 reveal_conversation 一样：路径是程序自己定的（配置里的记忆文件夹），浏览器什么都指定不了；
        只是打开来看看，所以不拿对话的锁。文件夹还不存在的话（被你删了，或者换了数据目录）先建一个空的，
        不然点了没反应，也不知道该去哪儿找。
        """
        folder = self.app.config.memory_dir
        try:
            private_dir(folder)  # 和数据目录里别的文件夹一样，只有你自己的账号能进
            reveal_in_file_manager(folder)
        except OSError as exc:
            return t("没能打开文件管理器（{error}）。文件在：{path}", error=exc, path=folder), ""
        return "", str(folder)

    # ---- 设置 --------------------------------------------------------------
    # 下面几个操作都会改动助手的状态，所以和对话共用一把锁：对话进行中不能改。
    # 返回值的约定和 send 一样：空字符串表示成功，否则是失败的原因。

    BUSY = k("请先等当前这一轮对话结束，或者点“停止”")

    def select_model(self, provider_id: str, model: str) -> str:
        if not has_key(PROVIDERS[provider_id]):
            return t("还没有设置 {provider} 的 API 密钥", provider=PROVIDERS[provider_id].label)
        if not self._turn.acquire(blocking=False):
            return self._busy()
        try:
            if provider_id != self.app.agent.backend.provider:  # 换到另一家要开始新对话，也就是离开当前会话
                blocked = self._before_leaving(f"model:{provider_id}:{model}")
                if blocked:
                    return blocked
            continued = self.app.select_model(provider_id, model)
            if not continued:
                self._left()
                self.ui.reset()  # 换了一家：历史格式不通用，页面也清空
            label = PROVIDERS[provider_id].label
            if continued:
                self.ui.info(t("已换成 {provider} · {model}。对话继续。", provider=label, model=model))
            else:
                self.ui.info(t("已换成 {provider} · {model}。换了一家，开始新对话。", provider=label, model=model))
            self._show_warnings()
            if continued:
                self._save()  # 这段会话以后用新模型，记下来
            self.publish_state()
        finally:
            self._turn.release()
        return ""

    def set_api_key(self, provider_id: str, key: str) -> tuple[str, str]:
        """保存或删除一家的 API 密钥。返回（失败原因，提醒）。

        保存成功后顺便同步一次模型列表；如果同步失败（比如 key 不对），key 仍然保存了，
        原因放在"提醒"里告诉用户。
        """
        if not self._turn.acquire(blocking=False):
            return self._busy(), ""
        warning = ""
        try:
            self.app.set_api_key(provider_id, key)
            if key:
                try:
                    self.app.sync_models(provider_id)
                except ProviderError as exc:
                    warning = t("key 已保存，但同步模型列表失败：{error}", error=exc)
            else:
                self.app.settings.set_models(provider_id, None)
            self.publish_state()
        finally:
            self._turn.release()
        return "", warning

    def sync_models(self, provider_id: str) -> str:
        try:
            self.app.sync_models(provider_id)
        except ProviderError as exc:
            return str(exc)
        self.publish_state()
        return ""

    def set_price(self, model: str, price: dict | None) -> None:
        self.app.settings.set_price(model, price)
        self.publish_state()

    def set_language(self, language: str) -> str:
        """换界面语言。成功返回空字符串，否则返回原因。

        不刷新页面：记下新的选择、发一次新的状态，页面收到后在原地把文字换掉（见 index.html 的 applyLanguage）。
        也不用拿对话的锁：对话进行中也可以换。之后程序发给页面的话（提示、确认卡片上的说明）都用新的语言。
        """
        i18n.choose(language)
        try:
            self.app.settings.set_language(language)
        except OSError as exc:
            self.publish_state()  # 这一次照样换过去，只是下次启动还是原来的语言
            return t("界面已经换过去了，但没能记下这个选择（{error}），下次启动时还是原来的语言。", error=exc)
        self.publish_state()
        return ""

    def set_approval(self, mode: str) -> None:
        """更改"要不要每次都问"。不用拿对话的锁：对话进行中也可以改，比如看到第五张确认卡片时决定全部放开。

        已经显示出来、正在等答复的那一张卡片不受影响，还是要点；从下一次需要确认的操作开始按新的来。
        这个选择只记在内存里，下次启动回到"每次都问"（原因见 permissions.py）。
        """
        self.app.agent.permissions.mode = mode
        self.publish_state()


class Server(ThreadingHTTPServer):
    daemon_threads = True  # 事件流的连接一直开着；退出程序时不必等它们
    web: WebApp


class Handler(BaseHTTPRequestHandler):
    server: Server
    _responded = False  # 这个请求是不是已经开始回答了

    def log_message(self, format: str, *args: object) -> None:
        pass  # 不在终端里打印每一个请求

    # ---- 路由 --------------------------------------------------------------

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if not self._allowed(parse_qs(url.query).get("token", [""])[0]):
            return
        if url.path == "/":
            self._send_page()
        elif url.path == "/events":
            self._send_events()
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._allowed(self.headers.get("X-Harness-Token", "")):
            return
        # 只有发消息可以带附件，所以只有它的请求体上限放宽了
        message = urlsplit(self.path).path == "/api/message"
        body = self._read_json(MAX_MESSAGE_BODY_BYTES if message else MAX_BODY_BYTES)
        if body is None:
            return
        try:
            self._route(body)
        except Exception as exc:  # noqa: BLE001 —— 故意接住所有异常，原因见下
            # 处理请求时出了意料之外的错（比如磁盘写不进去）。一定要给浏览器一个回答：
            # 不回答的话连接会被直接掐断，页面只能当成"助手程序退出了"，让用户去重启一个其实好好的程序。
            if not self._responded:
                try:
                    self._send_json(500, {"error": t("助手程序处理这个请求时出错了：{error}", error=str(exc) or type(exc).__name__)})
                except OSError:
                    pass  # 浏览器已经不在了

    def _route(self, body: dict) -> None:
        web = self.server.web
        path = urlsplit(self.path).path

        if path == "/api/message":
            text = body.get("text")
            try:
                # 附件逐个检查：个数、大小、格式、编码（见 attachments.py）。有一个不合格，整条消息都不发，
                # 免得用户以为模型看到了其实被丢掉的那个文件
                attachments = parse_attachments(body.get("attachments"))
            except AttachmentError as exc:
                self._send_json(400, {"error": i18n.message_of(exc)})
                return
            if not isinstance(text, str) or len(text) > MAX_MESSAGE_CHARS or not (text.strip() or attachments):
                self._send_json(400, {"error": t("消息不能为空，也不能太长")})  # 只发附件、不写字是可以的
                return
            # 当前的模型收不了的附件（DeepSeek 看不了图片，只有 Claude 读得了 PDF），在开始这一轮之前就退回去
            problem = web.app.attachment_problem(attachments)
            if problem:
                self._send_json(400, {"error": problem})
            else:
                self._finish(web.send(text.strip(), attachments))
        elif path == "/api/confirm":
            confirm_id, allow, feedback = body.get("id"), body.get("allow"), body.get("feedback", "")
            if not isinstance(confirm_id, int) or not isinstance(allow, bool) or not isinstance(feedback, str):
                self._send_json(400, {"error": t("参数不正确")})
            elif web.ui.answer_confirm(confirm_id, allow, feedback.strip()):
                self._send_json(200, {"ok": True})
            else:
                self._send_json(409, {"error": t("这个确认请求已经处理过了")})
        elif path == "/api/stop":
            web.stop()
            self._send_json(200, {"ok": True})
        elif path == "/api/new":
            self._finish(web.new_conversation())
        elif path.startswith("/api/conversation/"):
            conversation_id, title = body.get("id"), body.get("title")
            action = path.removeprefix("/api/conversation/")
            if not isinstance(conversation_id, str):
                self._send_json(400, {"error": t("参数不正确")})
            elif action == "open":
                self._finish(web.open_conversation(conversation_id))
            elif action == "delete":
                self._finish(web.delete_conversation(conversation_id))
            elif action == "reveal":
                error, shown = web.reveal_conversation(conversation_id)
                if error:
                    self._send_json(409, {"error": error})
                else:
                    self._send_json(200, {"ok": True, "path": shown})
            elif action == "rename" and isinstance(title, str) and 0 < len(title.strip()) <= 200:
                self._finish(web.rename_conversation(conversation_id, title.strip()))
            else:
                self._send_json(400, {"error": t("参数不正确")})
        elif path.startswith("/api/memory/"):
            action, relative = path.removeprefix("/api/memory/"), body.get("path")
            if action == "refresh":
                web.refresh_memories()
                self._send_json(200, {"ok": True})
            elif action == "reveal":
                error, shown = web.reveal_memories()  # 请求里带什么都不看：打开哪个文件夹由服务器定
                if error:
                    self._send_json(409, {"error": error})
                else:
                    self._send_json(200, {"ok": True, "path": shown})
            elif action not in ("read", "save", "delete", "show") or not isinstance(relative, str):
                self._send_json(400, {"error": t("参数不正确")})
            elif action == "delete":
                self._finish(web.delete_memory(relative))
            elif action == "show":
                error, shown = web.show_memory(relative)
                if error:
                    self._send_json(409, {"error": error})
                else:
                    self._send_json(200, {"ok": True, "path": shown})
            elif action == "save":
                name, text, modified, force = body.get("name"), body.get("text"), body.get("modified"), body.get("force", False)
                number = isinstance(modified, (int, float)) and not isinstance(modified, bool)
                if not isinstance(name, str) or not isinstance(text, str) or not number or not isinstance(force, bool):
                    self._send_json(400, {"error": t("参数不正确")})
                else:
                    error, saved, conflict = web.save_memory(relative, name, text, float(modified), force)
                    if error:
                        self._send_json(409, {"error": error, "conflict": conflict})
                    else:
                        self._send_json(200, {"ok": True, "path": saved})
            else:
                # 只是读，对话进行中也可以看。模型正在改的话，看到的是改之前或之后的样子，不会是半截：
                # 记忆工具改文件时先写好一个临时文件，再一步换上去
                try:
                    self._send_json(200, {"ok": True, **memories.read(web.app.config.memory_dir, relative)})
                except MemoryFileError as exc:
                    self._send_json(409, {"error": str(exc)})
        elif path == "/api/prompt":
            self._send_json(200, {"ok": True, **web.prompt_info()})
        elif path in ("/api/prompt/save", "/api/prompt/reset"):
            text = body.get("text") if path.endswith("save") else None
            if path.endswith("save") and not isinstance(text, str):
                self._send_json(400, {"error": t("参数不正确")})
            else:
                error, applied = web.save_prompt(text)
                if error:
                    self._send_json(409, {"error": error})
                else:
                    self._send_json(200, {"ok": True, "applied": applied})
        elif path == "/api/prompt/preview":
            # 还没保存的模板换上占位符是什么样子。只是算一下，不改任何东西
            text = body.get("text")
            if not isinstance(text, str) or len(text) > MAX_PROMPT_CHARS:
                self._send_json(400, {"error": t("参数不正确")})
            else:
                config = web.app.config
                self._send_json(200, {"ok": True, "preview": prompts.render(text, config, config.web and web.app.provider.web)})
        elif path == "/api/search":
            query = body.get("query")
            if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
                self._send_json(400, {"error": t("搜索词必须是一段文字，最长 {n} 个字符", n=MAX_QUERY_CHARS)})
            else:
                # 只读内存里的索引，不改任何东西，所以对话进行中也可以搜
                self._send_json(200, {"ok": True, "results": web.app.store.search(query)})
        elif path == "/api/language":
            language = body.get("language")
            if not isinstance(language, str) or language not in i18n.LANGUAGES:
                self._send_json(400, {"error": t("参数不正确")})
            else:
                self._finish("", warning=web.set_language(language))
        elif path == "/api/approval":
            mode = body.get("mode")
            if not isinstance(mode, str) or mode not in MODES:
                self._send_json(400, {"error": t("参数不正确")})
            else:
                web.set_approval(mode)
                self._send_json(200, {"ok": True})
        elif path == "/api/model":
            provider, model = body.get("provider"), body.get("model")
            if provider not in PROVIDERS or not isinstance(model, str) or not 0 < len(model.strip()) <= 200:
                self._send_json(400, {"error": t("参数不正确")})
            else:
                self._finish(web.select_model(provider, model.strip()))
        elif path == "/api/key":
            provider, key = body.get("provider"), body.get("key")
            if provider not in PROVIDERS or not isinstance(key, str):
                self._send_json(400, {"error": t("参数不正确")})
            elif key.strip() and not valid_api_key(key.strip()):
                self._send_json(400, {"error": t("这不像是一个 API 密钥：里面不能有空格、换行或中文")})
            else:
                error, warning = web.set_api_key(provider, key.strip())
                self._finish(error, warning=warning)
        elif path == "/api/models":
            provider = body.get("provider")
            if provider not in PROVIDERS:
                self._send_json(400, {"error": t("参数不正确")})
            else:
                self._finish(web.sync_models(provider))
        elif path == "/api/price":
            model, price = body.get("model"), body.get("price")
            if not isinstance(model, str) or not model or not _valid_price(price):
                self._send_json(400, {"error": t("价格必须是不小于 0 的数字，输入和输出两项必填")})
            else:
                web.set_price(model, price)
                self._send_json(200, {"ok": True})
        elif path.startswith("/api/task/"):
            self._route_task(web, path.removeprefix("/api/task/"), body)
        else:
            self._send_json(404, {"error": "not found"})

    def _route_task(self, web: WebApp, action: str, body: dict) -> None:
        """定时任务的几个操作。参数不对回答 400，现在做不了（任务不存在、已经在运行……）回答 409。"""
        tasks, task_id = web.tasks, body.get("id")
        if action not in ("save", "preview", "dismiss", "delete", "toggle", "run", "stop", "confirm"):
            self._send_json(404, {"error": "not found"})
            return
        # 除了新建（save 不带编号）、看时间表（preview）和关提醒（dismiss），都要指明是哪个任务
        needs_id = action not in ("save", "preview", "dismiss")
        valid = {
            "save": task_id is None or isinstance(task_id, str),
            "dismiss": body.get("all") is True or isinstance(body.get("reminder_id"), str),
            "toggle": isinstance(body.get("enabled"), bool),
            "confirm": _valid_answer(body),
        }.get(action, True)
        if not valid or (needs_id and not isinstance(task_id, str)):
            self._send_json(400, {"error": t("参数不正确")})
            return
        reply: dict = {"ok": True}
        try:
            if action == "save":
                reply["id"] = tasks.save(task_id, body)
            elif action == "preview":
                reply["next"] = tasks.preview(body.get("schedule"))
            elif action == "dismiss":
                tasks.dismiss(None if body.get("all") is True else body["reminder_id"])
            elif action == "delete":
                tasks.delete(task_id)
            elif action == "toggle":
                tasks.toggle(task_id, body["enabled"])
            elif action == "run":
                tasks.run_now(task_id)
            elif action == "stop":
                tasks.stop(task_id)
            else:
                tasks.confirm(task_id, body["confirm_id"], body["allow"], body.get("feedback", "").strip())
        except TaskRefused as exc:
            self._send_json(409, {"error": i18n.message_of(exc)})
        except TaskError as exc:
            self._send_json(400, {"error": i18n.message_of(exc)})
        else:
            self._send_json(200, reply)

    def _finish(self, error: str, warning: str = "") -> None:
        """按"空字符串表示成功"的约定回复浏览器。"""
        if error:
            self._send_json(409, {"error": error})
        else:
            self._send_json(200, {"ok": True, "warning": warning})

    # ---- 安全检查 ----------------------------------------------------------

    def _allowed(self, supplied_token: str) -> bool:
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origin = self.headers.get("Origin")
        ok = (
            self.headers.get("Host") in hosts
            and (origin is None or origin in {f"http://{host}" for host in hosts})
            and secrets.compare_digest(supplied_token.encode(), self.server.web.token.encode())
        )
        if not ok:
            self._send_text(403, t("链接无效或已经过期。请使用终端里显示的完整地址重新打开。"))
        return ok

    # ---- 响应 --------------------------------------------------------------

    def _send_page(self) -> None:
        # nonce 是一个一次性的随机数：只有带着它的 <script> 和 <style> 才会被浏览器执行，
        # 这样即使页面里混进了别的脚本（比如模型输出里夹带的），浏览器也不会运行。
        nonce = secrets.token_urlsafe(16)
        policy = (
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        # 翻译表也在这时塞进页面（页面里放它的是一段 JSON 数据，不是脚本，浏览器不会执行）。
        # 两处一次填完（re.sub 不会回头再扫一遍填进去的内容）：翻译表里万一出现 "__NONCE__"，
        # 或者 nonce 里碰巧有 "__I18N__"，都不会被当成要填的地方。
        fills = {"__NONCE__": nonce, "__I18N__": i18n.page_payload(self.server.web.app.settings.language)}
        page = re.sub("__NONCE__|__I18N__", lambda match: fills[match.group(0)], PAGE)
        self._send(
            200,
            "text/html; charset=utf-8",
            page.encode(),
            {"Content-Security-Policy": policy, "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"},
        )

    def _send_events(self) -> None:
        """事件流（Server-Sent Events）：连接不关闭，有新事件就写过去。

        刚连上时，先把日志里已有的事件补发一遍，然后发一个名叫 synced 的标记，意思是
        "旧的补完了，后面的都是此刻正在发生的"。页面靠它区分两种内容：补发的旧内容直接画出来，
        实时的新内容才播放入场动画。标记不带编号、也不进日志，所以不影响断线重连时
        "从第几号接着发"的计算。
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
        try:
            while True:
                # 第一次不等待：有多少旧事件就发多少，紧接着发标记
                wait = KEEPALIVE_SECONDS if caught_up else 0
                events = self.server.web.ui.events_after(cursor, timeout=wait)
                if not events and caught_up:
                    self.wfile.write(b": keepalive\n\n")  # 没有新事件时也定期写一点，才能发现对方已经断开
                if events:
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

    def _read_json(self, limit: int) -> dict | None:
        """读出请求体并解析成字典。请求体超过 limit 个字节时回答 413，返回 None。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= limit:
            if limit > MAX_BODY_BYTES:
                # 发消息的请求：上限本来就大，多读掉的量也跟着放宽，不然页面还是只能看到"连接断开"
                self._discard(length, limit + MAX_DISCARD_BYTES)
                self._send_json(413, {"error": t("请求太大：一条消息连同附件最多发送约 64MB 的内容")})
            else:
                self._discard(length, MAX_DISCARD_BYTES)
                self._send_json(413, {"error": t("请求太大：一次最多发送约 1MB 的内容")})
            return None
        try:
            # clean：浏览器送来的文字（消息、标题……）在进门的地方统一洗一遍，见 safe.py
            body = clean(json.loads(self.rfile.read(length) or b"{}"))
        except (ValueError, RecursionError):  # RecursionError：嵌套了上千层的 JSON
            body = None
        if not isinstance(body, dict):
            self._send_json(400, {"error": t("请求格式不正确")})
            return None
        return body

    def _discard(self, length: int, most: int) -> None:
        """把一个不打算处理的请求体读掉扔了，最多读 most 个字节。

        不读的话，浏览器还在往这边发数据，连接就被我们关掉了：它来不及读到我们的回答，只会报"连接断开"。
        读掉再回答，页面才能把"请求太大"显示出来。读的量设了上限，免得被一个巨大的请求拖住。
        """
        remaining = min(max(length, 0), most)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _send_json(self, status: int, payload: dict) -> None:
        self._send(status, "application/json; charset=utf-8", to_json(payload, readable=True).encode())

    def _send_text(self, status: int, text: str) -> None:
        self._send(status, "text/plain; charset=utf-8", text.encode())

    def _send(self, status: int, content_type: str, body: bytes, headers: dict | None = None) -> None:
        self._responded = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def _valid_answer(body: dict) -> bool:
    """答复定时任务的确认：confirm_id 是整数，allow 是真假，feedback（可以不给）是一段文字。"""
    confirm_id, feedback = body.get("confirm_id"), body.get("feedback", "")
    return isinstance(confirm_id, int) and not isinstance(confirm_id, bool) and isinstance(body.get("allow"), bool) and isinstance(feedback, str)


def _valid_price(price: object) -> bool:
    """价格要么是 None（删除自己填的价格），要么是一组不小于 0 的数字。"""
    if price is None:
        return True
    if not isinstance(price, dict) or not {"input", "output"} <= set(price) <= {"input", "output", "cache_read"}:
        return False
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 100_000 for v in price.values())


def create_server(app: App, ui: WebUI, port: int | None = None, token: str | None = None) -> tuple[Server, str]:
    """创建服务器（还没有开始运行）。返回（服务器，在浏览器里打开的地址）。"""
    if port is not None:
        server = Server(("127.0.0.1", port), Handler)  # 指定了端口：被占用就直接报错
    else:
        try:
            server = Server(("127.0.0.1", DEFAULT_PORT), Handler)
        except OSError:
            server = Server(("127.0.0.1", 0), Handler)  # 默认端口被占用：让系统挑一个空闲的
    server.web = WebApp(app, ui, token or secrets.token_urlsafe(16))
    return server, f"http://127.0.0.1:{server.server_address[1]}/?token={server.web.token}"


def claim_data_dir(data_dir: Path) -> IO | None:
    """占住数据目录：同一个数据目录同一时间只给一个网页界面用。占到了返回锁文件（要一直留着不关），已经被别人占了返回 None。

    两个同时运行的程序各自在内存里记着会话列表，看不见对方的改动：同一段会话，后存盘的会把先存的那一轮盖掉；
    一边删掉的会话，另一边一存盘又冒出来。
    锁由操作系统管着，程序退出时（哪怕是崩溃）自动放开，不会出现"程序没了、锁还在"的情况。
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    handle = open(data_dir / ".lock", "a")  # noqa: SIM115 —— 故意不用 with：这个文件要开着直到程序退出
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    except OSError:
        pass  # 少数文件系统（比如某些网络盘）不支持这种锁：那就不拦了，照常启动
    return handle


def shut_down(server: Server, app: App) -> None:
    """退出前的收尾：让正在进行的一轮（或者正在运行的定时任务）停下来、存好盘，再关掉服务器。"""
    server.web.tasks.close()
    server.web.stop()
    app.shell.kill_running()
    # stop() 只是通知这一轮尽快收尾，收尾（包括存盘）是在另一个线程里做的。不等它的话，程序一退出那个线程就没了：
    # 这一轮里助手已经做过的事（执行过的命令、改过的文件）在保存的会话里不留一点记录。
    # 模型一直没有输出时等不到它停下，所以只等有限的几秒。
    server.web.wait_idle(SHUTDOWN_WAIT_SECONDS)
    server.server_close()


def run_web(config: Config, port: int | None = None, open_browser: bool = True) -> int:
    lock = claim_data_dir(config.data_dir)
    if lock is None:
        print(t("已经有一个{name}的网页界面在运行，用的是同一个数据目录：{path}", name=APP_NAME, path=config.data_dir))
        print(t("两个一起运行会互相覆盖对方保存的会话。请直接用已经打开的那个页面，或者先到它的终端里按 Ctrl-C 把它关掉。"))
        return 1
    try:
        return _serve(config, port, open_browser)
    finally:
        lock.close()


def _serve(config: Config, port: int | None, open_browser: bool) -> int:
    ui = WebUI()
    app = build_app(config, ui)
    try:
        server, url = create_server(app, ui, port)
    except OSError as exc:
        print(t("无法在端口 {port} 上启动：{error}", port=port, error=exc))
        return 1

    print(t("{name}（网页界面）已启动。在浏览器里打开这个地址：", name=APP_NAME) + "\n")
    print(f"    {url}\n")
    provider = app.provider
    print(t("模型：{model}    工作目录：{workspace}", model=f"{provider.label} · {app.settings.model}", workspace=config.workspace))
    if not has_key(provider):
        print(t("还没有设置 {provider} 的 API 密钥：打开页面后，点右上角的“设置”填写。", provider=provider.label))
    print(t("地址里的 token 是这次运行的口令，不要发给别人。按 Ctrl-C 退出。"))
    # 定时任务只在这个程序开着的时候运行。刚启动时先看一眼：关着的这段时间里错过的，补跑一次
    server.web.tasks.start()
    if open_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shut_down(server, app)
    print("\n" + t("再见。"))
    return 0
