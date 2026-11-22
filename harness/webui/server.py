"""本地 HTTP 服务器：浏览器和 agent 之间的桥。

浏览器只会发这几种请求：

  GET  /               页面本身（page/ 里的几块拼成的一页，见 page/__init__.py）
  GET  /events         事件流。这个连接一直不关，服务器把 WebUI 日志里的新事件不断推过去。
                       刚连上时先补发已有的事件，然后发一个 synced 标记，后面才是实时的事件
  GET  /manifest.webmanifest  网页应用的清单：书签、"创建快捷方式 / 安装成应用"用的名字、图标和打开的地址（见 manifest.py）
  GET  /icons/<名字>   清单和页面头上引用的图标（PNG），只认 manifest.ICONS 里一字不差的文件名
  POST /api/message    用户发了一条消息（可以带附件：图片、PDF、文本文件，见 attachments.py）
  POST /api/confirm    用户对一个确认请求点了"允许"或"拒绝"
  POST /api/turn/edit、retry   编辑最后一条消息、重新生成最后一个回答（倒回最后一轮再跑一遍，见 rewind.py）
  POST /api/stop       用户点了"停止"
  POST /api/new        用户点了"新对话"（带上 agent 的话，用这个自定义 Agent 当角色开始，见 agents/）
  POST /api/conversation/open、rename、delete   打开、重命名、删除一段保存过的会话
  POST /api/conversation/reveal                 在系统的文件管理器里显示一段会话的文件
  POST /api/memory/read、save、delete           查看、修改（内容和名字）、删除一条记忆（data/memories/ 下的一个文件，见 memories.py）
  POST /api/memory/reveal                       在系统的文件管理器里打开记忆文件夹
  POST /api/memory/show                         在系统的文件管理器里显示一条记忆的文件
  POST /api/reveal     在文件管理器里显示助手的一个数据文件（任务、待办、提示词），页面只说是哪一个
  POST /api/todo/update、delete                 改一条待办（标题、截止日期、完成没有）、删掉一条
  POST /api/memory/refresh                      页面回到前台：记忆文件夹变了的话，发一次新的状态
  POST /api/skill/…    扩展里的 Skill：查看、新建、修改、删除、启用、从网址 / 上传 / 别的工具安装（见 skills_api.py）
  POST /api/agent/…    扩展里的 Agent：查看、新建、修改、删除、加示例、从别的工具导入（见 agents_api.py）
  POST /api/prompt     读出现在用的提示词（模板、内置的版本、占位符和实际发出去的样子）
  POST /api/prompt/save、reset、preview          保存改过的提示词、恢复内置的、预览还没保存的
  POST /api/mcp/...    MCP 服务器：查看详情、添加或编辑、删除、启用停用、重启、改"问不问你"、
                       从别的工具导入、粘贴 JSON（见 WebApp 的"MCP"一节）
  POST /api/search     在所有保存过的会话里搜索（标题和内容）
  POST /api/media/list 多媒体库：所有会话里发过的附件，一页一页地取（可以按种类筛、按名字找，见 media.py）
  POST /api/media/file 多媒体库里一个附件的内容（下载、看大图）。浏览器只给编号，读哪个文件由服务器定
  POST /api/media/reveal  在系统的文件管理器里显示一个附件的原件
  POST /api/commands/add、edit、delete、reveal   你自己加的"不用问的命令"：加、改、删、在访达中显示 commands.json（见 commands_api.py）
  POST /api/approval   用户更改了"要不要每次都问"（每次都问 / 只读的命令不用问 / 改文件不用问 / 全部自动允许）
  POST /api/language   用户换了界面语言（中文 / English / Español）
  POST /api/model      用户换了模型
  POST /api/model/catalog  换模型面板要的目录：每一家有没有密钥、能选的模型、能做什么、价格、换过去会怎样（见 model_catalog.py）
  POST /api/key        用户填写或删除了一家的 API 密钥
  POST /api/models     用户点了"同步模型"
  POST /api/price      用户填写了一个模型的价格
  POST /api/fallbacks  用户排好了一家的备用模型（模型暂时用不了时改用的同一家的模型，见 fallback_api.py）
  POST /api/task/save、delete、toggle           新建或修改、删除、打开或关掉一个定时任务（见 tasks.py 和 scheduler.py）
  POST /api/task/run、stop                      立即运行一次（回答里的 queued 说明是不是在排队）、停下正在排队或者正在运行的那一次
  POST /api/task/confirm                        答复定时任务停在那里等的确认（没人看着时它要你批准才动手）
  POST /api/task/dismiss                        关掉一条（或者全部）已经弹出的提醒
  POST /api/task/preview                        还没保存的时间表接下来会在哪三个时刻运行
  POST /api/ledger     打开"花费明细"：最近 30 天、每个月、每个模型、每种来源花了多少（见 ledger.py）
  POST /api/budget     用户填写或清除了每月预算（见 budget.py）
  POST /api/export/info  导出之前看一眼：数据文件夹有多大、多少个文件
  POST /api/export     导出全部数据：回答的是一个 zip 文件，不是 JSON（见 export.py）
  POST /api/channel/connect、pair、cancel、approval、enable、unbind、remove   聊天通道（Telegram……）的设置（见 channels/hub.py）
  POST /api/channel/confirm、stop               答复聊天里等你批准的操作、停下聊天里正在进行的一轮
  POST /api/browser/settings、stop、reveal、clear  浏览器：开关和设置、停下并关掉、在访达中显示配置文件夹、清除它（见 browser_api.py）
  POST /api/browser/shot                         一张浏览器截图的大图（只收编号，回答 image/jpeg，见 browser_api.py）
  POST /api/computer/settings、stop、trust、permission  操作整台电脑：开关和设置、停下、这段对话里截图（或者点击、打字）不再问、打开 macOS 系统设置里的那一页（见 computer_api.py）
  POST /api/computer/shot                        一张电脑截图的大图（和浏览器的一样：只收编号，回答 image/jpeg）

安全。这个服务器能让助手在你的电脑上执行命令，所以必须保证只有你自己打开的那个页面能用它。
你平时访问的任何网站，都可以让浏览器向 127.0.0.1 发请求，所以光靠"只在本机"是不够的：

  1. 只监听 127.0.0.1，局域网里的其他电脑连不上。
  2. 每次启动生成一个随机口令（token），写在打开页面用的地址里。所有请求都必须带上它；
     别的网站不知道口令，请求会被拒绝。
  3. 检查 Host 和 Origin 请求头，拒绝从别的网站发起的请求。
  4. 页面带有内容安全策略（CSP）：只运行我们自己的脚本，不加载任何外部资源。
     为了能装成应用多放行了两样，都是写死的完整地址：清单那一个（manifest-src），和自带的那几张图标（img-src，见 manifest.py）。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import webbrowser
import zlib
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import IO
from urllib.parse import parse_qs, quote, urlsplit

from .. import export, i18n, instance, media, memories, model_catalog, rewind
from .. import prompt as prompts
from ..agents import AgentError
from ..app import App, build_app, retry_turn, run_turn
from ..attachments import MAX_ATTACHMENTS, Attachment, AttachmentError
from ..attachments import parse as parse_attachments
from ..budget import valid_budget
from ..channels.base import ChannelError
from ..channels.hub import ChannelHub
from ..claims import Claims
from ..config import (
    APP_NAME,
    BROWSER_SHOT_DAYS,
    DESKTOP_SHOT_DAYS,
    LEDGER_CHART_DAYS,
    MAX_PROMPT_CHARS,
    Config,
)
from ..conversations import ConversationError
from ..i18n import k, t
from ..imports.overview import overview
from ..imports.places import Env
from ..imports.registry import MCP
from ..mcp import ConfigError
from ..mcp import importers as mcp_imports
from ..media import Entry, MediaError
from ..memories import MemoryConflict, MemoryFileError
from ..permissions import MODES
from ..pricing import estimate, price_of
from ..providers import PROVIDERS, ProviderError, has_key
from ..safe import clean, private_dir, to_json
from ..search import MAX_QUERY_CHARS
from ..settings import key_hint, valid_api_key
from ..tasks import TaskError, TaskRefused
from ..tools.base import ToolError
from ..tools.shell import SECRET_ENV_VARS
from . import fallback_api, lifecycle, models_api
from . import manifest as web_manifest
from .agents_api import AgentsAPI
from .browser_api import BrowserAPI
from .capabilities_api import page_state as capabilities_state
from .commands_api import CommandsAPI
from .computer_api import ComputerAPI
from .page import assemble as assemble_page
from .scheduler import Scheduler
from .skills_api import MAX_UPLOAD_BODY_BYTES, SkillsAPI
from .ui import WebUI, latest_state_only, said

DEFAULT_PORT = 8765
MAX_BODY_BYTES = 1_000_000
# 发消息的请求可以带附件（base64 编码后比原文件大三分之一），所以单独放宽到这么多。别的请求都是几行设置，1MB 足够
MAX_MESSAGE_BODY_BYTES = 64_000_000
MAX_DISCARD_BYTES = 16_000_000  # 请求体太大时，最多读掉这么多再回答（见 Handler._read_json）
SHUTDOWN_WAIT_SECONDS = 5  # 退出时最多等这么久，让正在进行的一轮收尾存盘
MAX_MESSAGE_CHARS = 100_000
KEEPALIVE_SECONDS = 15
LARGE_CONTEXT_TOKENS = 30_000  # 打开的旧会话超过这么大时，提醒一下接着聊的费用

# 页面由 page/ 里的几块拼成（骨架、样式、脚本，见 page/__init__.py），启动时拼一次。
# 拼好的仍然是一个自给自足的 HTML，里面留着 __NONCE__ 这几处，每次发页面时填（见 Handler._send_page）
PAGE = assemble_page()
# 回复里的 Markdown 怎么画，单独写在 markdown.js 里（一个文件只讲一件事，也方便用 Node 单独测试）。
# 发页面时把它原样填进页面里那个带 nonce 的 <script>，所以不用为它另开一个地址，也不用放宽安全策略。
MARKDOWN = (Path(__file__).parent / "markdown.js").read_text(encoding="utf-8")


def has_file_manager() -> bool:
    """这台电脑上能不能打开文件管理器。服务器上通常没有图形界面（没有 DISPLAY），也没有 xdg-open。"""
    if sys.platform == "darwin":
        return True
    return bool(shutil.which("xdg-open")) and bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def reveal_in_file_manager(path: Path) -> None:
    """在系统的文件管理器里显示一个文件或文件夹（macOS 上是访达）。失败时抛出 OSError。
    没有文件管理器的电脑（服务器）上什么都不做：回答里的 opened 是 false，页面改成告诉你文件在哪儿（见 Handler._send_json）。

    这是整个服务器里唯一一处"页面点一下，电脑上就启动一个程序"的地方，所以守两条规矩：
    路径只能是程序自己算出来的（见 WebApp.reveal_conversation、reveal_memories、show_memory、reveal_media 和
    skills_api.py、agents_api.py 的 reveal：浏览器只能给会话编号、记忆文件夹里的一个相对路径（逐层检查过，出不了那个文件夹）、
    附件的编号（查过格式，文件名由内容的 SHA-256 定）、一个 Skill 的名字（只能是 Skill 文件夹里的一个子文件夹），
    或者什么都不给（commands_api.py 的 reveal：只显示 data/commands.json））；命令用参数列表交给系统，不经过 shell，文件名里有什么字符都不会被当成命令的一部分。
    """
    if not has_file_manager():
        return
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

    def __init__(self, app: App, ui: WebUI, token: str, service: str | None = None):
        self.app = app
        self.ui = ui
        self.token = token
        # 智狐是怎么开着的，设置里"后台运行"一节照着它说（见 page/scripts/service.js）：
        # "launchd" / "systemd" 是 harness service install 装的后台服务；"terminal" 是在终端里开的 harness --web；
        # None 是不清楚（在终端或者系统服务里开的 harness serve），那一节就不显示
        self.service = service
        self._turn = threading.Lock()  # 谁拿着这把锁，谁就在进行一轮对话（或者正在改设置）
        # 发状态快照时用的小锁："算出快照"和"发出去"要一口气做完。同步模型、改价格这些不拿对话锁的操作
        # 可能和一轮对话同时发快照，不这样的话，先算的旧快照可能反而后发出去，把新的盖掉。
        self._publishing = threading.Lock()
        self._unsaved = ""  # 当前会话上一次存盘失败的原因。空字符串表示内容都在磁盘上了
        self._discard_ok = ""  # 用户已经被提醒过一次"离开会丢内容"的那个操作；紧接着再做一次同样的操作就放行
        self._memories_shown: list[dict] = []  # 上一次发给页面的记忆列表（见 refresh_memories）
        self._live: tuple[object, tuple[Entry, ...]] = (None, ())  # 当前会话里的附件，和算它时的依据（见 _live_media）
        # Skill 一页的接口。"在文件管理器里显示"每次都按名字现找这个函数（测试会把它换掉）
        self.skills = SkillsAPI(self, lambda path: reveal_in_file_manager(path))
        self.agents = AgentsAPI(self, lambda path: reveal_in_file_manager(path))  # Agent 一页的接口，同样的道理
        self.browser = BrowserAPI(self, lambda path: reveal_in_file_manager(path))  # 设置里"浏览器"一节的接口
        self.computer = ComputerAPI(self)  # 设置里"操作整台电脑"一节的接口
        self.commands = CommandsAPI(self, lambda path: reveal_in_file_manager(path))  # "不用问的命令"窗口的接口
        # 定时任务：到时间了自己跑一轮，或者弹出一条提醒。任务不拿 self._turn：它们和你的对话、和彼此同时进行，
        # 各用各的界面和 shell（见 scheduler.py）
        self.tasks = Scheduler(self)
        # 聊天通道（Telegram……）：聊天里的对话和你在网页上的对话同时进行，也不拿 self._turn。
        # 同一段会话不能两边同时写，所以每一轮开始前先认领它（见 claims.py）
        self.claims = Claims()
        self.channels = ChannelHub(app, self.claims, changed=self.publish_state, saved=self.conversation_saved_elsewhere)
        ui.on_stop(app.shell.kill_running)  # 点"停止"时，连正在运行的命令一起结束
        ui.on_stop(app.mcp.cancel_running)  # 也取消正在等的 MCP 工具调用：只是对话自己的，任务的各自管各自的（见 McpManager.call）
        # 还有正在做的浏览器动作：只是这段对话自己的（换一段对话就换一个，所以每次现找）
        ui.on_stop(lambda: app.chat_browser.stop())
        ui.on_stop(lambda: app.chat_desktop.stop())  # 正在操作电脑的那一步也停下（同样只是这段对话自己的）
        app.browser.on_change = self.publish_state  # 浏览器开了、关了、忙起来了：页面顶上的提示跟着变
        app.desktop.on_change = self.publish_state  # 有运行开始、停止操作电脑：页面顶上"智狐正在控制这台电脑"跟着变
        app.agent.on_usage = self.publish_state  # 每调用完一次模型，就刷新一次用量和费用
        app.mcp.on_change = self.publish_state  # MCP 服务器连上了、断开了、工具变了，页面上跟着变
        if app.tasks.problem:
            ui.hint(app.tasks.problem, level="error")  # 任务文件读不出来：说一声，原来的文件留在哪儿
        self.publish_state()
        # 上次运行时一轮做到一半就退出了（存了原件、会话却没来得及存盘），留下没人用的原件：启动时清一遍。
        # 这时还没有开始接受请求，不会有哪一轮正在存新的原件
        self._collect_media(None)

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
        skills, agents = self.skills.summaries(), self.agents.summaries()
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
            fallbacks=fallback_api.state(self.app),  # 每一家的备用模型（按顺序，见 fallback.py）；没设的不出现
            todos=[
                {"id": item["id"], "title": item["title"], "due": item["due"]}
                for item in self.app.todos.items("pending")
            ],
            # 记忆文件夹里有哪些文件（只有名字、大小和修改时间，内容要看时另外读）。每次发状态都重新看一遍文件夹，
            # 所以模型在这一轮里记下或删掉的，下一次刷新状态时页面上就跟着变了，页面不用自己去轮询
            memories=self._memories_shown,
            # 多媒体库里一共有多少个附件，和一个"版本"。版本变了，开着的多媒体一栏就重新取一次列表；页面不用轮询
            media=self._media_state(),
            # 装好的 Skill（名字、说明、启用没有、有没有问题）。和记忆一样每次都重新看一遍文件夹
            skills=skills,
            # 自定义 Agent（名字、说明、模型、颜色、几样工具、有没有问题），和 Skill 一样每次都重新看一遍文件夹
            agents=agents,
            # 当前这段对话是用哪个 Agent 开始的（顶上显示它的名字）；普通的对话是 null
            persona=self.app.persona_info(),
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
            spend=self.app.spending(),  # 今天、本月花了多少，预算超了没有（这些记在账本里，重启也不会清零）
            # 信息栏里"智狐能做的事"：每个功能在这段对话里用不用得了、为什么、问不问你、在哪儿执行、关着的怎么打开
            # （由 capabilities.py 算，格式见那里的开头）。页面只照着画
            capabilities=capabilities_state(self.app, skills, agents),
            mcp=self._mcp_state(),
            approval=agent.permissions.mode,  # 要不要每次都问：ask / readonly / edits / all
            commands=self.commands.state(),  # 你自己加的"不用问的命令"（见 commands_api.py）
            language=settings.language,  # 界面语言：zh / en / es。和页面现在用的不一样时，页面会在原地换过去
            # 定时任务：每个任务的设置、最近的运行、正在运行的那一次的进度（格式见 scheduler.py 开头的说明），
            # 以及已经弹出、你还没关掉的提醒
            tasks=tasks,
            reminders=reminders,
            # 聊天通道：每个平台的设置和连接情况、各个聊天在做什么、聊天里等你批准的操作（格式见 channels/hub.py 的 snapshot）。
            # 机器人的 token 不在里面，只有最后 4 位
            channels=self.channels.snapshot(),
            # 这台电脑能不能打开文件管理器。服务器上不能：页面上"在访达中显示"之后改成告诉你文件在哪儿
            file_manager=has_file_manager(),
            service=self.service,  # 智狐是作为后台服务开着的，还是跟着终端（见 __init__ 里的说明）
            # 浏览器：开没开、在不在运行、正在操作没有、找到的 Chrome、配置文件夹（见 browser_api.py）
            browser=self.browser.state(),
            # 操作整台电脑：开没开、这台电脑上能不能用、权限、谁正在操作、截图留几天（见 computer_api.py）
            computer=self.computer.state(),
            # 最后一轮能不能编辑、重新生成（见 _rewind_state）。页面上的按钮照着它画，能不能、为什么不能都由这里说了算
            rewind=self._rewind_state(),
        )

    def _mcp_state(self) -> dict:
        """MCP 服务器的概况。密钥只有名字和最后几位（见 mcp/config.py 的 display），工具的详情点开时另外取。"""
        mcp = self.app.mcp
        started = bool(self.app.agent.backend.export())  # 这段对话发过消息了没有：发过了，工具就定下来了
        servers = mcp.status(started)
        for item in servers:
            item["error"] = said(item["error"]) if item["error"] else None
        problem = mcp.config.problem
        return {
            "servers": servers,
            "problem": said(problem) if problem else None,
            "file": str(mcp.config.path),
            "started": started,
            "dropped": mcp.active.dropped if started else 0,
        }

    def _models(self, provider_id: str) -> list[str]:
        """一家可以选择的模型（规矩在 model_catalog.models_of，换模型的面板用的也是它）。"""
        return model_catalog.models_of(self.app.settings, provider_id)

    # ---- 对话 --------------------------------------------------------------

    def send(self, text: str, attachments: list[Attachment] | None = None) -> str:
        """开始一轮对话。成功返回空字符串，否则返回不能开始的原因。"""
        provider = self.app.provider
        if not has_key(provider):
            return t("还没有设置 {provider} 的 API 密钥，请先点右上角的“设置”填写。", provider=provider.label)
        if not self._turn.acquire(blocking=False):
            return self._busy(t("上一轮对话还没有结束"))
        elsewhere = self._claim()
        if elsewhere:
            self._turn.release()
            return elsewhere
        # agent 循环是阻塞的（等模型、等用户确认），所以放到后台线程里跑，
        # 这样服务器还能继续响应浏览器的其他请求，比如"允许"和"停止"。
        threading.Thread(target=self._run_turn, args=(text, attachments or []), daemon=True).start()
        return ""

    def _claim(self) -> str:
        """认领当前这段会话（见 claims.py）。它正在聊天通道里跑一轮的话返回原因，不认领。调用的人拿着 self._turn。"""
        conversation = self.app.conversation
        holder = self.claims.take(conversation.id, "web")
        if holder is not None:
            return t("这段会话正在 {platform} 上进行。等那边这一轮结束再发。", platform=self.channels.holder(conversation.id) or holder)
        if conversation.saved and not self._unsaved:
            # 聊天那边可能刚在这段会话里聊过一轮（存到了磁盘上，这里的还是旧的）：先读回来，不然这一轮存盘时会把那一轮盖掉
            try:
                newer = self.app.store.load(conversation.id).updated_at > conversation.updated_at
            except ConversationError:
                newer = False
            if newer:
                self._reload_current()
        return ""

    def _reload_current(self) -> None:
        """从磁盘把当前这段会话重新读一遍，页面跟着重画。调用的人拿着 self._turn。"""
        try:
            conversation = self.app.open_conversation(self.app.conversation.id)
        except ConversationError:
            return
        self.ui.load_events(conversation.events)
        self.publish_state()

    def conversation_saved_elsewhere(self, conversation_id: str) -> None:
        """聊天通道刚把一段会话存了盘。网页正开着它、手上又没事的话，跟着刷新：页面上看到的就是最新的，
        你接着在网页上聊，也不会用一份旧的历史把聊天里的那一轮盖掉。"""
        if conversation_id != self.app.conversation.id or self._unsaved or not self._turn.acquire(blocking=False):
            return
        try:
            if conversation_id == self.app.conversation.id:
                self._reload_current()
        finally:
            self._turn.release()

    def _held_elsewhere(self, conversation_id: str) -> str:
        """这段会话正在聊天通道里跑一轮的话，返回不能打开、改名、删除的原因。"""
        platform = self.channels.holder(conversation_id)
        return t("这段会话正在 {platform} 上进行。等那边这一轮结束再试。", platform=platform) if platform else ""

    def _run_turn(self, text: str, attachments: list[Attachment], again: int | None = None) -> None:
        """跑一轮。again：重新生成时，这一轮开始的位置（见 rewind）。那条用户消息已经在页面上了，不再显示一遍；
        它还在历史里的话原样留着，只重跑模型的那部分（Agent.retry_turn），不在的话（上一次在模型答出第一步之前就失败了）
        用同样的文字和附件再发一次。"""
        try:
            self._discard_ok = ""  # 接着聊了：之前那句"再点一次就放弃"不再算数
            self.app.name_conversation(text, attachments)  # 新会话用第一句话当标题（没写字的话用第一个附件的名字）
            backend = self.app.agent.backend
            if again is None:
                attachments = self._keep_originals(attachments)  # 原件存进多媒体库，概要里记上编号
                # 记下这一轮从历史的哪个位置开始：以后编辑这条消息、重新生成回答，从这里倒回去
                self.ui.begin_turn(text, attachments, mark=backend.mark())
            else:
                self.ui.begin_again()
            self.publish_state()  # 让新会话马上出现在列表里
            if again is not None and backend.turn_at(again):
                retry_turn(self.app.agent, self.ui, again)
            else:
                run_turn(self.app.agent, self.ui, text, attachments, again=again is not None)
        finally:
            try:
                self.ui.end_stream()
                self._save()  # 每一轮结束都存一次盘，所以最多只会丢失正在进行的这一轮
                self.publish_state()
            finally:
                try:
                    # 这一轮结束：屏幕让出来，还按着的鼠标键松开（见 desktop/session.py）。让出屏幕会刷新一次状态，
                    # 所以放在通知页面之前：放在后面的话，页面收到"结束了"马上发来的请求（比如开新对话）会撞上还拿着的锁
                    self.app.chat_desktop.release()
                    self.claims.release(self.app.conversation.id, "web")
                finally:
                    # "这一轮结束了"要等存盘和刷新状态都做完、马上就要放开锁的时候才通知页面。
                    # 如果先通知再存盘，页面一收到通知就发来的请求会撞上还没放开的锁，被误判成"上一轮还没结束"。
                    self.ui.end_turn()
                    self._turn.release()

    # ---- 倒回最后一轮（编辑最后一条消息、重新生成，见 rewind.py）-------------------

    def _rewind_state(self) -> dict | None:
        """状态里的 rewind：最后一轮能不能倒回去，不能的话为什么，倒回去之前要不要先问。一条用户消息都没有时是 None。

          turn     这一轮的 user 事件的编号。页面发请求时带回来（连同会话的编号），这边对一下，免得倒回的是别的一轮
          mark     这一轮在历史里的位置（见 rewind.py）。只是给人看、给测试看的，页面不用
          ok       能不能编辑、重新生成（正在进行一轮时页面自己把按钮收起来，这里不管）
          reason   不能的原因（换语言时页面照着原文重译）
          effects  这一轮做过的、可能改动了东西的事（每件一行）。不是空的，倒回去之前在原地问你一次
          more     太多了没列出来的还有几项（"……还有几项"那一句由页面自己写）
          operated 这些事里有没有操作过整台电脑（"computer"）、在浏览器里点过输入过（"browser"）：页面为它们单独写一句"撤不回来"
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
        }

    def _rewind_problem(self, turn: rewind.Turn) -> str:
        """最后一轮为什么倒不回去；能倒回去返回空字符串。"""
        conversation = self.app.conversation
        if turn.mark is None:
            if conversation.task:
                return t("定时任务自己跑的那一轮不能编辑或重新生成：发给模型的指令和这里显示的不一样。可以接着发一条新消息。")
            return t("这一轮是旧版本的智狐记下的，没有记下倒回去的位置。可以接着发一条新消息。")
        platform = self.channels.holder(conversation.id)
        if platform:
            return t("这段会话正在 {platform} 上进行。等那边这一轮结束再试。", platform=platform)
        running = self.tasks.running(conversation.task)
        if running:
            return t("定时任务“{title}”正在运行。等它跑完再编辑或重新生成。", title=running)
        backend = self.app.agent.backend
        if backend.turn_at(turn.mark) or backend.mark() == turn.mark:
            return ""  # 还在历史里；或者这一轮还没答出第一步就失败了，消息已经撤回（见 rewind.py 开头）
        if backend.mark() > turn.mark:
            return t("这一轮已经压缩进了摘要（对话太长时较早的内容会写成摘要），倒不回去了。可以接着发一条新消息。")
        return t("这一轮在对话历史里找不到了，倒不回去。可以接着发一条新消息。")

    def rewind(self, action: str, target: tuple[str, int], text: str = "", keep: list[int] | None = None, confirmed: bool = False) -> tuple[str, dict]:
        """编辑最后一条消息（action 是 edit，text 是改过的文字，keep 是留下第几个附件，None 是全部）、
        重新生成最后一个回答（retry）。返回（不能做的原因，回答里另外带的东西）；做成了返回空字符串，新的一轮在后台开始。

        target 是页面看到的那一轮：（会话的编号，它的 user 事件的编号）。和现在的最后一轮对不上（另一个标签页刚倒回过、
        已经换了一段会话、聊天通道刚聊过一轮），就不做。
        这一轮做过可能改动了东西的事、而你还没有确认（confirmed），也不做，回答里带上 effects，页面在原地问你。

        和发消息一样拿着对话的锁、认领这段会话（见 claims.py），新的一轮跑完才放开。倒回去以后马上存一次盘：
        会话文件里的 messages 和 events 一起截断，搜索和多媒体的索引跟着更新（ConversationStore.save → _remember），
        这时候程序退出，再打开看到的也是倒回去以后的样子。
        """
        provider = self.app.provider
        if not has_key(provider):
            return t("还没有设置 {provider} 的 API 密钥，请先点右上角的“设置”填写。", provider=provider.label), {}
        if not self._turn.acquire(blocking=False):
            return self._busy(t("上一轮对话还没有结束")), {}
        started = False
        try:
            elsewhere = self._claim()
            if elsewhere:
                return elsewhere, {}
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
            if action == "edit":
                self.app.retitle(turn.text, turn.attachments, text, attachments, earlier=self.ui.user_events()[:-1])
            backend.rollback(mark + 1 if kept else mark)
            self.ui.rewind(keep_user=action == "retry")
            self._save()
            self.publish_state()  # 重放到这里结束：之后新的一轮照常播放入场动画
            threading.Thread(target=self._run_turn, args=(turn.text if kept else text, attachments, mark if action == "retry" else None), daemon=True).start()
            started = True
            return "", {}
        finally:
            if not started:
                self.claims.release(self.app.conversation.id, "web")
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
        """对话的锁被占着（你的这一轮还没结束，或者正在改设置）时，告诉用户为什么。

        定时任务不拿这把锁，所以任务在跑的时候不会走到这里：你照常发消息、换模型、打开会话。
        """
        return otherwise or t(self.BUSY)

    def stop(self) -> None:
        """对话里的"停止"：只停你的这一轮（连同它正在运行的命令）。

        定时任务的运行各有各的界面和 shell，这里碰不到它们；停任务走 /api/task/stop。
        """
        self.ui.request_stop()

    def wait_idle(self, timeout: float) -> bool:
        """等正在进行的一轮对话结束（包括它收尾时的存盘）。等到了返回 True，超时返回 False。"""
        if not self._turn.acquire(timeout=timeout):
            return False
        self._turn.release()
        return True

    def new_conversation(self, agent: str | None = None) -> str:
        """清空对话，开始新的一段。成功返回空字符串，否则返回不能这样做的原因。

        agent：用哪个自定义 Agent 当角色（见 App.new_conversation）。它选的模型是另一家、那一家又还没有密钥的话，先说清楚。
        """
        if not self._turn.acquire(blocking=False):
            return self._busy()
        try:
            if agent:
                try:
                    persona = self.app.agents.usable(agent)
                except AgentError as exc:
                    return str(exc)
                provider = PROVIDERS[persona.spec.provider] if persona.spec.model else self.app.provider
                if not has_key(provider):
                    return t("「{agent}」用的是 {provider} 的模型，还没有设置 {provider} 的 API 密钥。", agent=persona.name, provider=provider.label)
            blocked = self._before_leaving("new:" + (agent or ""))
            if blocked:
                return blocked
            try:
                self.app.new_conversation(agent)
            except AgentError as exc:
                return str(exc)
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
            blocked = self._held_elsewhere(conversation_id) or self._before_leaving(f"open:{conversation_id}")
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
            blocked = self._held_elsewhere(conversation_id)
            if blocked:
                return blocked
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
            blocked = self._held_elsewhere(conversation_id)
            if blocked:
                return blocked
            # 这段会话用过的原件。删完以后，别的会话不再用的就从多媒体库里删掉
            used = self.app.store.media().hashes(conversation_id)
            if conversation_id == self.app.conversation.id:
                used |= {entry.sha256 for entry in self._live_media() if entry.sha256}
            try:
                was_current = self.app.delete_conversation(conversation_id)
            except ConversationError as exc:
                return str(exc)
            if was_current:
                self._left()  # 删的就是当前这一段：它有没有存上都无所谓了
                self.ui.reset()
            self._collect_media(used)
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

    # ---- 多媒体 ------------------------------------------------------------
    # 多媒体库的条目来自两处：保存过的会话（ConversationStore 维护的索引），和当前这一段（内存里的界面事件）。
    # 当前这一段以内存为准：它可能还没存盘，或者刚发的附件还没来得及存进文件。

    def _keep_originals(self, attachments: list[Attachment]) -> list[Attachment]:
        """把附件的原件存进多媒体库。存不成也照样发这条消息，只是提醒一句。"""
        if not attachments:
            return attachments
        kept, problem = self.app.media.keep(attachments)
        if problem:
            self.ui.hint(t("附件的原件没能存进多媒体库（{error}）。消息照常发送，只是以后在多媒体里下载不到原件。", error=problem), level="error")
        return kept

    def _live_media(self) -> tuple[Entry, ...]:
        """当前会话里的附件。每发一次状态都要用（数一共有多少个），所以记着上一次算的结果：
        只有换了会话、有了新消息、历史变长了（旧附件的内容可能要从历史里找）时才重算。"""
        current, events = self.app.conversation, self.ui.user_events()
        messages = self.app.agent.backend.messages
        key = (current.id, current.created_at, tuple(event["id"] for event in events), len(messages))
        cached_key, cached = self._live
        if key != cached_key:
            cached = tuple(media.entries_of(events, messages, current.created_at))
            self._live = (key, cached)
        return cached

    def _media_sources(self) -> list[tuple[str, str, tuple[Entry, ...]]]:
        """（会话编号，标题，附件）：保存过的会话，当前这一段换成内存里的样子。"""
        current = self.app.conversation
        sources = [(cid, title, entries) for cid, (title, entries) in self.app.store.media().snapshot().items() if cid != current.id]
        live = self._live_media()
        if live:
            sources.append((current.id, current.title, live))
        return sources

    def _media_state(self) -> dict:
        index, current, live = self.app.store.media(), self.app.conversation, self._live_media()
        saved = sum(len(entries) for cid, (_, entries) in index.snapshot().items() if cid != current.id)
        # 版本由这几样拼成：保存过的会话变了、换了当前会话、当前会话里多了附件、当前会话改了标题，都会让它变
        version = f"{index.version}.{current.id}.{len(live)}.{zlib.crc32(current.title.encode('utf-8', 'replace')):08x}"
        return {"count": saved + len(live), "version": version}

    def media_list(self, kind: str, query: str, cursor: int, limit: int) -> dict:
        """多媒体库的一页（见 media.listing）。只是读，对话进行中也可以看。"""
        return media.listing(self._media_sources(), self.app.media.stored(), kind, query, cursor, limit)

    def media_file(self, media_id: str) -> tuple[str, tuple[bytes, str, str] | None]:
        """一个附件的内容：（失败的原因，（字节，类型，文件名））。"""
        try:
            conversation_id, entry = media.find(self._media_sources(), media_id)
            if conversation_id == self.app.conversation.id:
                messages = lambda: self.app.agent.backend.messages  # 当前这一段：历史就在内存里
            else:
                messages = lambda: self.app.store.load(conversation_id).messages  # 真要用到才读文件
            return "", media.content_of(entry, self.app.media, messages)
        except (MediaError, ConversationError) as exc:
            return str(exc), None

    def reveal_media(self, media_id: str) -> tuple[str, str]:
        """在文件管理器里显示一个附件的原件。返回（失败原因，显示的路径）。

        浏览器只给编号（格式在路由里查过）。原件的路径由服务器自己算：编号 → 索引里的条目 → 内容的 SHA-256 →
        data/media/ 下的那个文件。和 reveal_conversation 一样只是看一眼，不改任何东西，所以不拿对话的锁。
        """
        try:
            _, entry = media.find(self._media_sources(), media_id)
        except MediaError as exc:
            return str(exc), ""
        target = self.app.media.find(entry.sha256) if entry.sha256 else None
        if target is None:
            return t("这个附件没有单独存下原件：它是在有多媒体库之前发的，内容保存在它所在的会话的文件里。"), ""
        try:
            reveal_in_file_manager(target)
        except OSError as exc:
            return t("没能打开文件管理器（{error}）。文件在：{path}", error=exc, path=target), ""
        return "", str(target)

    def _collect_media(self, candidates: set[str] | None) -> None:
        """删掉没有会话再用的原件（见 MediaStore.collect）。删不掉只是多占点地方，不影响别的，所以不报错。"""
        store = self.app.store
        used = store.media().hashes() | {entry.sha256 for entry in self._live_media() if entry.sha256}
        try:
            # 读不出来的会话文件不在索引里，它们提到的原件也要留着
            unreadable = [path for path in store.directory.glob("*.json") if not store.exists(path.stem)] if store.directory.is_dir() else []
            self.app.media.collect(used, candidates, unreadable)
        except OSError:
            pass

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

    # ---- 数据文件、待办 ------------------------------------------------------

    def reveal_data(self, what: str) -> tuple[str, str]:
        """在文件管理器里显示一个数据文件。返回（失败原因，显示的路径）。

        和 reveal_conversation 一样：页面只能说是哪一个（下面这几个名字之一），路径是程序自己定的。
        文件还不存在（还没建过任务、提示词用的是内置的），就打开它将来所在的文件夹，并说一声
        """
        config = self.app.config
        target = {"tasks": config.tasks_file, "todos": config.todo_file, "prompt": config.prompt_file, "channels": config.channels_file}[what]
        shown = target if target.exists() else target.parent
        try:
            private_dir(config.data_dir)
            reveal_in_file_manager(shown)
        except OSError as exc:
            return t("没能打开文件管理器（{error}）。文件在：{path}", error=exc, path=target), ""
        return "", str(shown)

    def change_todo(self, todo_id: int, changes: dict | None) -> str:
        """改（changes 为 None 时删掉）一条待办。成功返回空字符串，否则返回原因。

        不拿对话的锁：待办的文件自己有锁（见 TodoStore），模型下一次看待办时看到的就是改过的
        """
        try:
            if changes is None:
                self.app.todos.delete(todo_id)
            else:
                self.app.todos.update(todo_id, **changes)
        except ToolError:  # 写给模型看的说明（"先用 todo_list 查看"），这里换成给人看的
            return t("这条待办已经不在了，可能刚被助手改过或删掉了。")
        except OSError as exc:
            return t("没能保存待办：{error}", error=exc.strerror or exc)
        self.publish_state()
        return ""

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

    # ---- MCP --------------------------------------------------------------
    # 这些操作不拿对话的锁：对话进行中也可以添加、停用、重启服务器。正在进行的这段对话用的是开始时拍下的
    # 快照（见 mcp/manager.py），不受影响；快照里的服务器被停掉了，调用它的工具会得到一个说清楚原因的错误。
    #
    # 保存一个 stdio 服务器，就是让这台电脑去运行你填的那个命令。所以守这几条：只有带着口令的这个页面能做；
    # 命令用参数列表启动、不经过 shell；模型没有任何工具能添加或修改服务器（mcp.json 文件工具也碰不了）。

    def mcp_detail(self, name: str) -> tuple[str, dict]:
        try:
            return "", self.app.mcp.detail(name)
        except ConfigError as exc:
            return str(exc), {}

    def mcp_change(self, action, *args) -> str:
        """改 MCP 的配置（保存、删除、启用、重启、改"问不问"）。成功返回空字符串，否则返回原因。"""
        try:
            action(*args)
        except ConfigError as exc:
            return str(exc)
        except OSError as exc:
            return t("保存配置文件失败：{error}", error=exc)
        return ""

    def mcp_scan(self) -> dict:
        """去读别的工具（Claude Desktop、Claude Code、Cursor、VS Code、Codex……）的配置，列出可以导入的服务器（密钥遮住）。
        只在你点了"从其他工具导入"时才读。tools 是顶上的总览：每个工具里三样各有几个（见 imports/overview.py）。"""
        env = Env.here(self.app.config.workspace)
        found = mcp_imports.sources(env.workspace, env)
        return {"sources": mcp_imports.preview(found, self.app.mcp.config), "tools": overview(env, {MCP: mcp_imports.counted(found)})}

    def mcp_import(self, picks: list[tuple[str, str]]) -> tuple[str, list[str], list[str]]:
        """导入勾选的那些。配置的内容在这里重新从文件里读，页面只给了"哪个来源的哪个名字"。"""
        found = {source.id: source for source in mcp_imports.sources(self.app.config.workspace)}
        added, skipped = [], []
        for source_id in dict.fromkeys(source for source, _ in picks):
            source = found.get(source_id)
            names = [name for source, name in picks if source == source_id]
            if source is None or not source.servers:
                skipped += [t("没有找到“{name}”。", name=name) for name in names]
                continue
            try:
                more, missed = mcp_imports.apply(self.app.mcp.config, source.servers, names)
            except (ConfigError, OSError) as exc:
                return str(exc), added, skipped
            added, skipped = added + more, skipped + missed
        self.app.mcp.sync(restart=frozenset(added))
        return "", added, skipped

    def mcp_paste(self, text: str, names: list[str] | None) -> tuple[str, dict]:
        """粘贴的 JSON：names 为 None 时只是看看里面有什么；给了 names 就把这几个加进来。"""
        try:
            servers = mcp_imports.parse_pasted(text)
            if names is None:
                listed = mcp_imports.preview([mcp_imports.pasted(servers)], self.app.mcp.config)
                return "", {"servers": listed[0]["servers"]}
            added, skipped = mcp_imports.apply(self.app.mcp.config, servers, names)
        except (ConfigError, OSError) as exc:
            return str(exc), {}
        self.app.mcp.sync(restart=frozenset(added))
        return "", {"added": added, "skipped": skipped}

    def mcp_reveal(self) -> tuple[str, str]:
        """在文件管理器里显示 mcp.json（还没有的话打开数据目录）。路径是程序自己定的，浏览器什么都指定不了。"""
        target = self.app.config.mcp_file
        if not target.exists():
            target = self.app.config.data_dir
            private_dir(target)
        try:
            reveal_in_file_manager(target)
        except OSError as exc:
            return t("没能打开文件管理器（{error}）。文件在：{path}", error=exc, path=target), ""
        return "", str(target)

    def refresh_tools(self) -> bool:
        """浏览器开、关了：还没开始的这段对话马上换上新的工具。返回 True 表示这段对话已经开始了，工具不变。"""
        if not self._turn.acquire(blocking=False):
            return True  # 正在进行一轮：这段对话当然已经开始了
        try:
            started = bool(self.app.agent.backend.export())
            if not started:
                self.app.refresh_tools()
            return started
        finally:
            self._turn.release()

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
            else:
                # 同一家：这段会话接着用新模型，要存一次盘。它正在聊天通道里跑一轮的话，两边会抢着写同一个文件（见 claims.py）
                held = self._held_elsewhere(self.app.conversation.id)
                if held:
                    return held
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

    def set_budget(self, amount: float | None) -> None:
        """记下每月预算（None 表示不设）。只是提醒用，所以不用拿对话的锁，对话进行中也可以改。"""
        self.app.settings.set_budget(amount)
        self.publish_state()  # 状态里带着预算，超了没有也在这时重新算一次（见 App.spending）

    def spend_report(self) -> dict:
        """"花费明细"窗口要的全部数字。只读内存里的汇总，对话进行中也可以看。"""
        return self.app.ledger.report(self.app.agent.clock().date(), LEDGER_CHART_DAYS)

    def export_info(self) -> dict:
        """导出之前给页面看的：数据文件夹在哪、要导出的有多大、多少个文件。"""
        config = self.app.config
        work = export.plan(config.data_dir, [config.env_file])
        return {"bytes": work.size, "files": work.count, "path": str(config.data_dir)}

    def set_language(self, language: str) -> str:
        """换界面语言。成功返回空字符串，否则返回原因。

        不刷新页面：记下新的选择、发一次新的状态，页面收到后在原地把文字换掉（见 page/scripts/language.js 的 applyLanguage）。
        也不用拿对话的锁：对话进行中也可以换。之后程序发给页面的话（提示、确认卡片上的说明）都用新的语言。
        """
        i18n.choose(language)
        self.channels.relabel()  # 聊天软件里命令菜单上的说明，也换成新的语言
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
        elif url.path == web_manifest.MANIFEST_PATH:
            self._send_manifest()
        elif url.path.startswith(web_manifest.ICON_PREFIX):
            self._send_icon(url.path.removeprefix(web_manifest.ICON_PREFIX))
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._allowed(self.headers.get("X-Harness-Token", "")):
            return
        # 只有发消息（带附件）和上传 Skill（压缩包、文件夹）的请求体上限放宽了
        path = urlsplit(self.path).path
        limit = {"/api/message": MAX_MESSAGE_BODY_BYTES, "/api/skill/upload": MAX_UPLOAD_BODY_BYTES}.get(path, MAX_BODY_BYTES)
        body = self._read_json(limit)
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
        elif path in ("/api/turn/edit", "/api/turn/retry"):
            action, confirmed = path.removeprefix("/api/turn/"), body.get("confirmed", False)
            conversation_id, turn_id = body.get("conversation"), body.get("turn")
            text, keep = body.get("text", ""), body.get("keep")
            bad = not isinstance(conversation_id, str) or not isinstance(turn_id, int) or isinstance(turn_id, bool) or not isinstance(confirmed, bool)
            if action == "edit":
                bad = bad or not isinstance(text, str) or len(text) > MAX_MESSAGE_CHARS
                indexes = keep if isinstance(keep, list) else []
                bad = bad or not (keep is None or isinstance(keep, list))
                bad = bad or len(indexes) > MAX_ATTACHMENTS or len(set(map(repr, indexes))) != len(indexes)
                bad = bad or not all(isinstance(index, int) and not isinstance(index, bool) for index in indexes)
            if bad:
                self._send_json(400, {"error": t("参数不正确")})
            else:
                target = (conversation_id, turn_id)
                error, extra = web.rewind(action, target, text.strip() if action == "edit" else "", keep if action == "edit" else None, confirmed)
                if error:
                    self._send_json(409, {"error": error, **extra})
                else:
                    self._send_json(200, {"ok": True})
        elif path == "/api/stop":
            web.stop()
            self._send_json(200, {"ok": True})
        elif path == "/api/new":
            agent = body.get("agent")
            if agent is not None and (not isinstance(agent, str) or not 0 < len(agent) <= 200):
                self._send_json(400, {"error": t("参数不正确")})
            else:
                self._finish(web.new_conversation(agent))
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
                    self._send_json(200, {"ok": True, "path": shown, "opened": has_file_manager()})
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
                    self._send_json(200, {"ok": True, "path": shown, "opened": has_file_manager()})
            elif action not in ("read", "save", "delete", "show") or not isinstance(relative, str):
                self._send_json(400, {"error": t("参数不正确")})
            elif action == "delete":
                self._finish(web.delete_memory(relative))
            elif action == "show":
                error, shown = web.show_memory(relative)
                if error:
                    self._send_json(409, {"error": error})
                else:
                    self._send_json(200, {"ok": True, "path": shown, "opened": has_file_manager()})
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
        elif path == "/api/media/list":
            asked = _media_query(body)
            if asked is None:
                self._send_json(400, {"error": t("参数不正确")})
            else:
                # 只是读：内存里的索引，加上看一眼原件的目录。对话进行中也可以看
                self._send_json(200, {"ok": True, **web.media_list(*asked)})
        elif path == "/api/media/file":
            media_id = body.get("id")
            # 编号只能是"会话编号/第几排/第几张"：拼不出任何路径（读哪个文件由服务器自己定，见 media.py）
            if not isinstance(media_id, str) or not media.ID.fullmatch(media_id):
                self._send_json(400, {"error": t("参数不正确")})
                return
            error, found = web.media_file(media_id)
            if error or found is None:
                self._send_json(409, {"error": error})
                return
            content, content_type, name = found
            # 让浏览器当成下载的文件（attachment），不按内容去猜类型（nosniff 在 _send 里），
            # 万一被直接打开，也不运行里面的任何东西（sandbox）
            headers = {"Content-Disposition": media.disposition(name), "Content-Security-Policy": "default-src 'none'; sandbox"}
            self._send(200, content_type, content, headers)
        elif path == "/api/media/reveal":
            media_id = body.get("id")
            if not isinstance(media_id, str) or not media.ID.fullmatch(media_id):
                self._send_json(400, {"error": t("参数不正确")})
                return
            error, shown = web.reveal_media(media_id)
            if error:
                self._send_json(409, {"error": error})
            else:
                self._send_json(200, {"ok": True, "path": shown})
        elif path.startswith("/api/skill/"):
            status, payload = web.skills.route(path.removeprefix("/api/skill/"), body)
            self._send_json(status, payload)
        elif path.startswith("/api/agent/"):
            status, payload = web.agents.route(path.removeprefix("/api/agent/"), body)
            self._send_json(status, payload)
        elif path.startswith("/api/mcp/"):
            self._route_mcp(path.removeprefix("/api/mcp/"), body)
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
        elif path == "/api/reveal":
            what = body.get("what")
            if what not in ("tasks", "todos", "prompt", "channels"):
                self._send_json(400, {"error": t("参数不正确")})
            else:
                error, shown = web.reveal_data(what)
                if error:
                    self._send_json(409, {"error": error})
                else:
                    self._send_json(200, {"ok": True, "path": shown, "opened": has_file_manager()})
        elif path in ("/api/todo/update", "/api/todo/delete"):
            todo_id = body.get("id")
            changes: dict | None = None
            bad = not isinstance(todo_id, int) or isinstance(todo_id, bool)
            if path.endswith("update") and not bad:
                changes = {}
                if "title" in body:
                    title = body["title"]
                    bad = bad or not isinstance(title, str) or not 0 < len(title.strip()) <= 200
                    changes["title"] = title.strip() if isinstance(title, str) else ""
                if "due" in body:
                    due = body["due"]
                    bad = bad or not (due is None or due == "" or (isinstance(due, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", due)))
                    changes["due"] = due or None
                if "done" in body:
                    bad = bad or not isinstance(body["done"], bool)
                    changes["done"] = body["done"]
                bad = bad or not changes
            if bad:
                self._send_json(400, {"error": t("参数不正确")})
            else:
                self._finish(web.change_todo(todo_id, changes))
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
        elif path == "/api/model/catalog":
            # 只读内存里的设置和环境变量，不改任何东西，所以不拿对话的锁：对话进行中也能打开换模型的面板看一看
            self._send_json(200, {"ok": True, "catalog": models_api.page_state(web.app)})
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
        elif path == "/api/ledger":
            self._send_json(200, {"ok": True, "report": web.spend_report()})
        elif path == "/api/budget":
            amount = body.get("amount")
            if "amount" not in body or not valid_budget(amount):  # 不设预算要明说（null），不能是忘了带
                self._send_json(400, {"error": t("预算要填一个大于 0 的数（美元）")})
            else:
                web.set_budget(None if amount is None else float(amount))
                # 带上存完以后的花费：设的预算比这个月已经花的还少时，页面当场就能说清楚（提示框会被设置面板挡住）
                self._send_json(200, {"ok": True, "spend": web.app.spending()})
        elif path == "/api/export/info":
            self._send_json(200, {"ok": True, **web.export_info()})
        elif path == "/api/export":
            self._send_export()
        elif path == "/api/fallbacks":
            status, payload = fallback_api.save(web.app, body)
            if status == 200:
                web.publish_state()
            self._send_json(status, payload)
        elif path == "/api/price":
            model, price = body.get("model"), body.get("price")
            if not isinstance(model, str) or not model or not _valid_price(price):
                self._send_json(400, {"error": t("价格必须是不小于 0 的数字，输入和输出两项必填")})
            else:
                web.set_price(model, price)
                self._send_json(200, {"ok": True})
        elif path.startswith("/api/task/"):
            self._route_task(web, path.removeprefix("/api/task/"), body)
        elif path.startswith("/api/channel/"):
            self._route_channel(web, path.removeprefix("/api/channel/"), body)
        elif path == "/api/browser/shot":
            content = web.browser.shot(body.get("id"))
            if content is None:
                self._send_json(409, {"error": t("这张截图的大图已经过期删掉了（只留 {days} 天），对话里还看得到小图。", days=BROWSER_SHOT_DAYS)})
                return
            # 和 /api/media/file 一样：当成下载的文件、不按内容猜类型、万一被直接打开也什么都不运行
            headers = {"Content-Disposition": 'attachment; filename="screenshot.jpg"', "Content-Security-Policy": "default-src 'none'; sandbox"}
            self._send(200, "image/jpeg", content, headers)
        elif path.startswith("/api/browser/"):
            status, payload = web.browser.route(path.removeprefix("/api/browser/"), body)
            self._send_json(status, payload)
        elif path.startswith("/api/commands/"):
            status, payload = web.commands.route(path.removeprefix("/api/commands/"), body)
            self._send_json(status, payload)
        elif path == "/api/computer/shot":
            content = web.computer.shot(body.get("id"))
            if content is None:
                self._send_json(409, {"error": t("这张截图的大图已经过期删掉了（只留 {days} 天），对话里还看得到小图。", days=DESKTOP_SHOT_DAYS)})
                return
            # 和浏览器的截图、/api/media/file 一样：当成下载的文件、不按内容猜类型、万一被直接打开也什么都不运行
            headers = {"Content-Disposition": 'attachment; filename="screenshot.jpg"', "Content-Security-Policy": "default-src 'none'; sandbox"}
            self._send(200, "image/jpeg", content, headers)
        elif path.startswith("/api/computer/"):
            status, payload = web.computer.route(path.removeprefix("/api/computer/"), body)
            self._send_json(status, payload)
        else:
            self._send_json(404, {"error": "not found"})

    def _route_mcp(self, action: str, body: dict) -> None:
        web = self.server.web
        mcp = web.app.mcp
        name = body.get("name")
        bad = lambda: self._send_json(400, {"error": t("参数不正确")})
        if action == "refresh":  # 页面打开了 MCP 这一页：配置文件被你手动改过的话，照着新的同步
            mcp.refresh()
            self._send_json(200, {"ok": True})
        elif action == "scan":
            self._send_json(200, {"ok": True, **web.mcp_scan()})
        elif action == "reveal":
            error, shown = web.mcp_reveal()
            self._answer(error, path=shown)
        elif action == "import":
            picks = body.get("picks")
            if not isinstance(picks, list) or not picks or len(picks) > 200 or not all(
                isinstance(pick, dict) and isinstance(pick.get("source"), str) and isinstance(pick.get("name"), str) for pick in picks
            ):
                bad()
                return
            error, added, skipped = web.mcp_import([(pick["source"], pick["name"]) for pick in picks])
            self._answer(error, added=added, skipped=skipped)
        elif action == "paste":
            text, names = body.get("text"), body.get("names")
            if not isinstance(text, str) or not (names is None or (isinstance(names, list) and all(isinstance(item, str) for item in names))):
                bad()
                return
            error, data = web.mcp_paste(text, names)
            self._answer(error, **data)
        elif action == "save":
            original, form = body.get("original"), body.get("server")
            if not (original is None or isinstance(original, str)) or not isinstance(form, dict):
                bad()
                return
            self._finish(web.mcp_change(mcp.save, form, original))
        elif not isinstance(name, str):
            bad()
        elif action == "detail":
            error, detail = web.mcp_detail(name)
            self._answer(error, server=detail)
        elif action == "delete":
            self._finish(web.mcp_change(mcp.remove, name))
        elif action == "restart":
            self._finish(web.mcp_change(mcp.restart, name))
        elif action == "enable" and isinstance(body.get("enabled"), bool):
            self._finish(web.mcp_change(mcp.set_enabled, name, body["enabled"]))
        elif action == "approval" and body.get("approval") in ("ask", "auto") and (body.get("tool") is None or isinstance(body.get("tool"), str)):
            self._finish(web.mcp_change(mcp.set_approval, name, body.get("tool"), body["approval"]))
        else:
            bad()

    def _answer(self, error: str, **data: object) -> None:
        """和 _finish 一样的约定，成功时带上 data。"""
        if error:
            self._send_json(409, {"error": error})
        else:
            self._send_json(200, {"ok": True, **data})
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
                reply["queued"] = tasks.run_now(task_id)  # 同时运行的到了上限，在排队
            elif action == "stop":
                tasks.stop(task_id)
            else:
                tasks.confirm(task_id, body["confirm_id"], body["allow"], body.get("feedback", "").strip())
        except TaskRefused as exc:
            # 做不了，常常是因为页面看到的已经过时了：任务文件可能被别的程序改过（终端版、重启前的旧进程），
            # 而页面只在有改动时才收到新状态。所以先把最新的状态发过去，页面上那一行才不会一直留着、怎么删都删不掉
            web.publish_state()
            self._send_json(409, {"error": i18n.message_of(exc)})
        except TaskError as exc:
            self._send_json(400, {"error": i18n.message_of(exc)})
        else:
            self._send_json(200, reply)

    def _route_channel(self, web: WebApp, action: str, body: dict) -> None:
        """聊天通道的几个操作（见 channels/hub.py）。参数不对回答 400，现在做不了（token 不对、确认已经答复过了……）回答 409。

        这些都不拿对话的锁：聊天里的对话和网页上的对话各跑各的，你正在网页上聊的时候也能连上 Telegram、答复它的确认。
        """
        hub, platform = web.channels, body.get("platform")
        checks = {
            "connect": lambda: _valid_credentials(body),
            "pair": lambda: True,
            "cancel": lambda: True,
            "approval": lambda: body.get("mode") in MODES,
            "enable": lambda: isinstance(body.get("enabled"), bool),
            "unbind": lambda: True,
            "remove": lambda: True,
            "confirm": lambda: isinstance(body.get("chat"), str) and _valid_answer(body),
            "stop": lambda: isinstance(body.get("chat"), str),
        }
        if action not in checks:
            self._send_json(404, {"error": "not found"})
            return
        if not isinstance(platform, str) or not platform or not checks[action]():
            self._send_json(400, {"error": t("参数不正确")})
            return
        reply: dict = {"ok": True}
        try:
            if action == "connect":
                reply["bot"] = hub.connect(platform, body["credentials"] if isinstance(body.get("credentials"), dict) else {"token": body["token"]})
            elif action == "pair":
                reply.update(hub.pair(platform))
            elif action == "cancel":
                hub.cancel_pairing(platform)
            elif action == "approval":
                hub.set_approval(platform, body["mode"])
            elif action == "enable":
                hub.set_enabled(platform, body["enabled"])
            elif action == "unbind":
                hub.unbind(platform)
            elif action == "remove":
                hub.remove(platform)
            elif action == "confirm":
                hub.confirm(platform, body["chat"], body["confirm_id"], body["allow"])
            else:
                hub.stop_chat(platform, body["chat"])
        except ChannelError as exc:
            self._send_json(409, {"error": i18n.message_of(exc)})
            return
        self._send_json(200, reply)

    def _send_export(self) -> None:
        """导出全部数据：把 zip 先写进一个临时文件，再整个发给浏览器（见 export.py）。

        页面用 fetch 拿到它、变成一个 Blob，再交给一个带 download 属性的链接去存盘：这样请求照样带着口令、
        经过上面的检查，地址里不用露出口令，页面的安全策略也不用为下载放宽什么。
        临时文件在系统的临时文件夹里，只有自己的账号能读，发完（或者出错）就删掉，不会留在数据文件夹里。
        """
        config = self.server.web.app.config
        moment = datetime.now()
        with tempfile.TemporaryFile() as archive:
            export.write_archive(config.data_dir, archive, moment, [config.env_file])
            size = archive.tell()
            archive.seek(0)
            self._responded = True
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{export.archive_name(moment)}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                shutil.copyfileobj(archive, self.wfile)
            except OSError:
                pass  # 浏览器那边取消了

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
            # 作为后台服务运行时口令重启前后不变（见 token.py），过期多半是重装过服务、或者是终端里开的那一份换了口令
            self._send_text(403, t("链接无效或已经过期。请使用终端里显示的完整地址重新打开。") + "\n" + t("智狐作为后台服务运行时，在终端里运行 uv run harness service open 重新打开。"))
        return ok

    # ---- 响应 --------------------------------------------------------------

    def _send_page(self) -> None:
        # nonce 是一个一次性的随机数：只有带着它的 <script> 和 <style> 才会被浏览器执行，
        # 这样即使页面里混进了别的脚本（比如模型输出里夹带的），浏览器也不会运行。
        nonce = secrets.token_urlsafe(16)
        # 图片和清单这两项由 manifest.policy 写：img-src 除了 data: 只多了自带的那几张图标的地址，manifest-src 只有清单那一个地址
        # （为什么要放行、为什么这样就够窄，见那里）。Host 已经在 _allowed 里查过，只能是 127.0.0.1:端口 或 localhost:端口
        policy = (
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; "
            f"{web_manifest.policy('http://' + self.headers.get('Host', ''))}; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        # 翻译表和 Markdown 渲染器也在这时塞进页面（翻译表放在一段 JSON 数据里，浏览器不会执行）。
        # 口令填进头上清单和图标的地址里：只是把打开这一页的地址里已有的东西再写一遍，quote 之后只剩字母、数字和 %-_.~。
        # 几处一次填完（re.sub 不会回头再扫一遍填进去的内容）：翻译表里万一出现 "__NONCE__"，
        # 或者 nonce 里碰巧有 "__I18N__"，都不会被当成要填的地方。
        fills = {
            "__NONCE__": nonce,
            "__TOKEN__": quote(self.server.web.token, safe=""),
            "__I18N__": i18n.page_payload(self.server.web.app.settings.language),
            "__MARKDOWN__": MARKDOWN,
        }
        page = re.sub("__NONCE__|__TOKEN__|__I18N__|__MARKDOWN__", lambda match: fills[match.group(0)], PAGE)
        self._send(
            200,
            "text/html; charset=utf-8",
            page.encode(),
            {"Content-Security-Policy": policy, "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"},
        )

    def _send_manifest(self) -> None:
        """网页应用的清单（见 manifest.py）。里面的地址带着口令，所以和页面一样不进缓存。"""
        self._send(
            200,
            "application/manifest+json; charset=utf-8",
            to_json(web_manifest.manifest(self.server.web.token), readable=True).encode(),
            {"Content-Security-Policy": "default-src 'none'; sandbox"},
        )

    def _send_icon(self, name: str) -> None:
        """一张图标。name 是地址里 /icons/ 后面的那一段，原样拿去查表（不解码、不拼路径），
        不是 manifest.ICONS 里一字不差的文件名就是 404。内容是程序自带的固定的几张图，不是你的数据，
        所以让浏览器缓存一天（地址里带着口令，换了口令就是另一个地址）；类型写死成 image/png，不让浏览器猜。"""
        data = web_manifest.icon_bytes(name)
        if data is None:
            self._send_json(404, {"error": "not found"})
            return
        self._send(
            200,
            "image/png",
            data,
            {"Cache-Control": "private, max-age=86400", "Content-Security-Policy": "default-src 'none'; sandbox"},
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
            if limit == MAX_UPLOAD_BODY_BYTES:
                self._discard(length, limit + MAX_DISCARD_BYTES)
                self._send_json(413, {"error": t("上传的内容太大了：一次最多约 20 MB。")})
            elif limit > MAX_BODY_BYTES:
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
        # 默认不进缓存、不让浏览器猜类型；headers 里另写了 Cache-Control 的（图标）用它自己的
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", **(headers or {})}
        self._responded = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def _media_query(body: dict) -> tuple[str, str, int, int] | None:
    """多媒体库的一次查询：（种类，搜索词，从第几个开始，要几个）。参数不对返回 None。"""
    kind, query, cursor, limit = body.get("kind", "all"), body.get("query", ""), body.get("cursor", 0), body.get("limit", media.PAGE_SIZE)
    if kind not in ("all", *media.KINDS) or not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
        return None
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in (cursor, limit)):
        return None
    if cursor < 0 or not 0 < limit <= media.MAX_PAGE:
        return None
    return kind, query.strip(), cursor, limit
def _valid_credentials(body: dict) -> bool:
    """连接一个聊天平台时填的东西：credentials 是 {名字: 文字}（最多 8 项，每项最多 400 个字），至少有一项不是空的
    （没重填的那几项是空的，接着用存着的，见 channels/hub.merge）。只有一个 token 的旧写法（Telegram）也认。"""
    given = body.get("credentials")
    if given is None and "token" in body:
        given = {"token": body["token"]}
    if not isinstance(given, dict) or not 0 < len(given) <= 8:
        return False
    if not all(isinstance(name, str) and len(name) <= 40 and isinstance(value, str) and len(value.strip()) <= 400 for name, value in given.items()):
        return False
    return any(value.strip() for value in given.values())


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


def create_server(app: App, ui: WebUI, port: int | None = None, token: str | None = None, service: str | None = None) -> tuple[Server, str]:
    """创建服务器（还没有开始运行）。返回（服务器，在浏览器里打开的地址）。service 见 WebApp.service。"""
    if port is not None:
        server = Server(("127.0.0.1", port), Handler)  # 指定了端口：被占用就直接报错
    else:
        try:
            server = Server(("127.0.0.1", DEFAULT_PORT), Handler)
        except OSError:
            server = Server(("127.0.0.1", 0), Handler)  # 默认端口被占用：让系统挑一个空闲的
    server.web = WebApp(app, ui, token or secrets.token_urlsafe(16), service)
    return server, f"http://127.0.0.1:{server.server_address[1]}/?token={server.web.token}"


def claim_data_dir(data_dir: Path, kind: str = "web") -> IO | None:
    """占住数据目录：同一个数据目录同一时间只给一个智狐用。占到了返回锁文件（要一直留着不关），已经被别人占了返回 None。
    为什么、怎么占，见 instance.py。"""
    return instance.claim(data_dir, kind)


def shut_down(server: Server, app: App) -> None:
    """退出前的收尾：让正在进行的一轮、正在运行的定时任务、聊天里正在进行的对话停下来、存好盘，再关掉服务器。"""
    lifecycle.stop(server.web, SHUTDOWN_WAIT_SECONDS)
    server.server_close()


def run_web(config: Config, port: int | None = None, open_browser: bool = True) -> int:
    lock = claim_data_dir(config.data_dir)
    if lock is None:
        # 说清楚是谁占着：作为后台服务在运行的话，告诉你怎么打开它的页面、怎么停下它（见 instance.py）
        for line in instance.busy_lines(config.data_dir):
            print(line)
        return 1
    try:
        return _serve(config, port, open_browser)
    finally:
        lock.close()


def _serve(config: Config, port: int | None, open_browser: bool) -> int:
    ui = WebUI()
    app = build_app(config, ui)
    try:
        # "terminal"：跟着这个终端，终端关了它也停（设置里会说怎么装成后台服务，见 page/scripts/service.js）
        server, url = create_server(app, ui, port, service="terminal")
    except OSError as exc:
        print(t("无法在端口 {port} 上启动：{error}", port=port, error=exc))
        return 1

    # 普通的 kill（SIGTERM，关机、注销、别的程序叫它退出时发的就是它）和 Ctrl-C 走同一条收尾的路：
    # 停下正在进行的一轮、存好盘，再放开数据目录。不接的话 SIGTERM 会直接把进程杀掉，这一轮做过的事在会话里不留记录。
    # 在打印地址之前就接上：看到地址就发 SIGTERM 的人（测试、脚本），不能赶在接上之前。
    # 收到第一个之后换回默认的处理：真想马上结束，再发一次就行
    def interrupted(signum: int, frame: object) -> None:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupted) if threading.current_thread() is threading.main_thread() else None
    try:
        print(t("{name}（网页界面）已启动。在浏览器里打开这个地址：", name=APP_NAME) + "\n")
        print(f"    {url}\n")
        provider = app.provider
        print(t("模型：{model}    工作目录：{workspace}", model=f"{provider.label} · {app.settings.model}", workspace=config.workspace))
        if not has_key(provider):
            print(t("还没有设置 {provider} 的 API 密钥：打开页面后，点右上角的“设置”填写。", provider=provider.label))
        print(t("地址里的 token 是这次运行的口令，不要发给别人。按 Ctrl-C 退出。"))
        # 定时任务和聊天通道只在这个程序开着的时候运行。刚启动时先看一眼：关着的这段时间里错过的任务，补跑一次
        lifecycle.start(server.web)
        if open_browser:
            webbrowser.open(url)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)  # 正在收尾：这时再来一个 SIGTERM 不要打断存盘
        shut_down(server, app)
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
    print("\n" + t("再见。"))
    return 0
