"""本地 HTTP 服务器：浏览器和 agent 之间的桥。

这个文件只管 HTTP 这一层：谁能用（口令、Host、Origin，见 Handler._allowed）、读请求体（_read_json）、
把请求交给谁（Handler._route：整张路由表都在那里，从上往下看就知道每个地址归哪个文件）、怎么回答
（JSON、页面、事件流、清单和图标，见 _send 和几个 _send_xxx）、出了意料之外的错也给一个回答（do_POST 里的 500），以及启动和退出
（create_server、run_web）。把文件的字节发给浏览器的接口只有三个（/api/media/file、/api/browser/shot、/api/computer/shot），
加上导出的 zip（_send_export），也都留在这里，和 _send 挨着：它们的安全头一眼看得全。

每个接口查完参数以后怎么做，在各自的文件里：

  webapp.py            WebApp：把助手接到网页上。共用的状态和锁（见 sessions.py）、存盘、这段会话归谁、发给页面的状态快照
  sessions.py          几段对话同时进行：每段一个 Session（它的助手、事件日志、锁），名单和"你正看着哪一段"
  sessions_api.py      新建一段、换着看、停下一段（/api/stop）、一轮收尾、退出时都停下
  chat_api.py          发消息、答复确认：一轮对话怎么开始、在后台怎么跑、怎么收尾
  rewind_api.py        编辑最后一条消息、重新生成（倒回最后一轮，见 ../rewind.py）
  conversations_api.py 新对话、打开、改名、删除、在文件管理器里显示一段会话，搜索
  media_api.py         多媒体库：列表、显示原件（发原件的字节在这里的 _route）
  memory_api.py        记忆窗口
  prompt_api.py        提示词
  tasks_api.py         待办和定时任务
  mcp_api.py           MCP 服务器
  channels_api.py      聊天通道（Telegram……）的设置和确认
  models_api.py        换模型、密钥、同步模型列表、价格，换模型面板的目录
  settings_api.py      别的设置：语言、问不问、遮密钥、预算、花费明细、导出之前的信息
  reveal.py            在系统的文件管理器里显示（访达、文件资源管理器、xdg-open）。整个程序只有这里会启动文件管理器
  skills_api.py、agents_api.py、endpoints_api.py、browser_api.py、computer_api.py、voice_api.py、commands_api.py、
  hooks_api.py、sandbox_api.py、changes_api.py、fallback_api.py、capabilities_api.py、updates_api.py   各自那一页或那一节（见各文件开头）

交到 *_api.py 的有两种写法。较早的那几个（skills_api.py 这些）是 WebApp 上的一个对象，route(action, body) 交回
（状态码，回答），这里照着发出去。从这个文件里拆出去的（chat_api.py……settings_api.py、reveal.py）是模块里的函数
route_xxx(handler, web, …)：handler 是这个请求的 Handler，函数用它的 _send_json、_finish、_answer 直接回答，
web 是 WebApp。两种的约定一样：参数不对回答 400，现在做不了（不存在、对话进行中、已经处理过了……）回答 409，
说明写在 error 里；_finish 的"空字符串表示成功"也一样。加一个新接口：在 _route 里加一行，参数检查和做法写进那一组的 *_api.py。

浏览器只会发这几种请求：

  GET  /               页面本身（page/ 里的几块拼成的一页，见 page/__init__.py）
  GET  /events         事件流。这个连接一直不关，服务器把你正看着的那一段的 WebUI 日志里的新事件不断推过去。
                       刚连上时先补发已有的事件，然后发一个 synced 标记，后面才是实时的事件。
                       换着看另一段时，先发一个不带编号的 reset，再把那一段的日志从头补发（见 _send_events）
  GET  /manifest.webmanifest  网页应用的清单：书签、"创建快捷方式 / 安装成应用"用的名字、图标和打开的地址（见 manifest.py）
  GET  /icons/<名字>   清单和页面头上引用的图标（PNG），只认 manifest.ICONS 里一字不差的文件名
  POST /api/message    用户发了一条消息（可以带附件：图片、PDF、文本文件，见 attachments.py 和 chat_api.py）
  POST /api/confirm    用户对一个确认请求点了"允许"或"拒绝"（见 chat_api.py）
  POST /api/turn/edit、retry   编辑最后一条消息、重新生成最后一个回答（倒回最后一轮再跑一遍，见 rewind.py 和 rewind_api.py）。
                       带上 undo_files 的话，倒回去之前先撤回这一轮改的文件（见 undo/）
  POST /api/changes/diff、undo、redo   "改了 N 个文件"那一行：看一个文件改了什么、撤回、重做（见 changes_api.py）
  POST /api/stop       用户点了"停止"：只停一段（带 conversation 的是会话栏上后台的那一段，见 sessions_api.py）
  POST /api/new        用户点了"新对话"（带上 agent 的话，用这个自定义 Agent 当角色开始，见 agents/ 和 conversations_api.py）
  POST /api/conversation/open、rename、delete   打开、重命名、删除一段保存过的会话（见 conversations_api.py）
  POST /api/conversation/reveal                 在系统的文件管理器里显示一段会话的文件
  POST /api/fork       从某一轮分叉成一段新对话、换过去（原来那段一字不动，见 fork_api.py 和 ../fork.py）
  POST /api/memory/read、save、delete           查看、修改（内容和名字）、删除一条记忆（data/memories/ 下的一个文件，见 memories.py 和 memory_api.py）
  POST /api/memory/reveal                       在系统的文件管理器里打开记忆文件夹
  POST /api/memory/show                         在系统的文件管理器里显示一条记忆的文件
  POST /api/memory/refresh                      页面回到前台：记忆文件夹变了的话，发一次新的状态
  POST /api/reveal     在文件管理器里显示助手的一个数据文件（任务、待办、提示词、聊天通道），页面只说是哪一个（见 reveal.py）
  POST /api/todo/update、delete                 改一条待办（标题、截止日期、完成没有）、删掉一条（见 tasks_api.py）
  POST /api/skill/…    扩展里的 Skill：查看、新建、修改、删除、启用、从网址 / 上传 / 别的工具安装（见 skills_api.py）
  POST /api/agent/…    扩展里的 Agent：查看、新建、修改、删除、加示例、从别的工具导入（见 agents_api.py）
  POST /api/prompt     读出现在用的提示词（模板、内置的版本、占位符和实际发出去的样子，见 prompt_api.py）
  POST /api/prompt/save、reset、preview          保存改过的提示词、恢复内置的、预览还没保存的
  POST /api/mcp/...    MCP 服务器：查看详情、添加或编辑、删除、启用停用、重启、改"问不问你"、
                       从别的工具导入、粘贴 JSON（见 mcp_api.py）
  POST /api/search     在所有保存过的会话里搜索（标题和内容，见 search.py）
  POST /api/media/list 多媒体库：所有会话里发过的附件，一页一页地取（可以按种类筛、按名字找，见 media.py 和 media_api.py）
  POST /api/media/file 多媒体库里一个附件的内容（下载、看大图）。浏览器只给编号，读哪个文件由服务器定（这个文件的 _route）
  POST /api/media/reveal  在系统的文件管理器里显示一个附件的原件
  POST /api/commands/add、edit、delete、reveal   你自己加的"不用问的命令"：加、改、删、在访达中显示 commands.json（见 commands_api.py）
  POST /api/hooks/list、save、delete、enable、test、reveal   你的钩子：列出、添加或修改、删除、开关、试运行、在访达中显示 hooks.json（见 hooks_api.py）
  POST /api/sandbox/level、add、edit、delete、reveal  命令的沙箱：对话用哪一档、额外允许的路径（加、改、删、在访达中显示列表里的一条，见 sandbox_api.py）
  POST /api/approval   用户更改了"要不要每次都问"（每次都问 / 只读的命令不用问 / 改文件不用问 / 全部自动允许，见 settings_api.py）
  POST /api/language   用户换了界面语言（中文 / English / Español，见 settings_api.py）
  POST /api/model      用户换了模型（见 models_api.py）
  POST /api/model/catalog  换模型面板要的目录：每一家有没有密钥、能选的模型、能做什么、价格、换过去会怎样（见 model_catalog.py 和 models_api.py）
  POST /api/key        用户填写或删除了一家的 API 密钥（见 models_api.py）
  POST /api/models     用户点了"同步模型"（见 models_api.py）
  POST /api/price      用户填写了一个模型的价格（见 models_api.py）
  POST /api/endpoint/save、delete、probe、reveal   自定义模型接口（Ollama、LM Studio、公司的网关……）：保存、删除、
                       测试连接和同步模型（请求从这里发出去，页面从来不直接连那个地址）、在访达中显示 endpoints.json（见 endpoints_api.py）
  POST /api/fallbacks  用户排好了一家的备用模型（模型暂时用不了时改用的同一家的模型，见 fallback_api.py）
  POST /api/redact     用户开、关"遮住工具结果里的密钥"（见 redact.py 和 settings_api.py）
  POST /api/web-fetch  用户开、关"读网页"（给没有联网工具的模型用，见 fetch/ 和 settings_api.py）
  POST /api/task/save、delete、toggle           新建或修改、删除、打开或关掉一个定时任务（见 tasks.py、scheduler.py 和 tasks_api.py）
  POST /api/task/run、stop                      立即运行一次（回答里的 queued 说明是不是在排队）、停下正在排队或者正在运行的那一次
  POST /api/task/confirm                        答复定时任务停在那里等的确认（没人看着时它要你批准才动手）
  POST /api/task/dismiss                        关掉一条（或者全部）已经弹出的提醒
  POST /api/task/preview                        还没保存的时间表接下来会在哪三个时刻运行
  POST /api/ledger     打开"花费明细"：最近 30 天、每个月、每个模型、每种来源花了多少（见 ledger.py 和 settings_api.py）
  POST /api/budget     用户填写或清除了每月预算（见 budget.py 和 settings_api.py）
  POST /api/export/info  导出之前看一眼：数据文件夹有多大、多少个文件
  POST /api/export     导出全部数据：回答的是一个 zip 文件，不是 JSON（见 export.py 和这个文件的 _send_export）
  POST /api/restore/info、upload、discard、apply、reveal   从备份恢复：上传（请求体是压缩包原样的字节）、预览、确认、在访达中显示自动备份（见 restore_api.py）
  POST /api/channel/connect、pair、cancel、approval、sandbox、enable、unbind、remove   聊天通道（Telegram……）的设置（见 channels/hub.py 和 channels_api.py）
  POST /api/channel/confirm、stop               答复聊天里等你批准的操作、停下聊天里正在进行的一轮
  POST /api/browser/settings、stop、reveal、clear  浏览器：开关和设置、停下并关掉、在访达中显示配置文件夹、清除它（见 browser_api.py）
  POST /api/browser/shot                         一张浏览器截图的大图（只收编号，回答 image/jpeg，见 browser_api.py 和这个文件的 _route）
  POST /api/computer/settings、stop、trust、permission  操作整台电脑：开关和设置、停下、这段对话里截图（或者点击、打字）不再问、打开 macOS 系统设置里的那一页（见 computer_api.py）
  POST /api/computer/shot                        一张电脑截图的大图（和浏览器的一样：只收编号，回答 image/jpeg）
  POST /api/voice/settings                       语音转文字：用哪一种、哪个模型、本机服务的地址（见 voice_api.py 和 ../voice/）
  POST /api/updates、/api/updates/check           检查新版本：打开、关上、马上问一次（默认关着，见 updates_api.py 和 ../updates.py）

安全。这个服务器能让助手在你的电脑上执行命令，所以必须保证只有你自己打开的那个页面能用它。
你平时访问的任何网站，都可以让浏览器向 127.0.0.1 发请求，所以光靠"只在本机"是不够的：

  1. 只监听 127.0.0.1，局域网里的其他电脑连不上。
  2. 每次启动生成一个随机口令（token），写在打开页面用的地址里。所有请求都必须带上它；
     别的网站不知道口令，请求会被拒绝。
  3. 检查 Host 和 Origin 请求头，拒绝从别的网站发起的请求。
  4. 页面带有内容安全策略（CSP）：只运行我们自己的脚本，不加载任何外部资源。
     为了能装成应用多放行了两样，都是写死的完整地址：清单那一个（manifest-src），和自带的那几张图标（img-src，见 manifest.py）。

这几条都在这个文件里（create_server、_allowed、_send_page）。所有请求都先过 _allowed（POST 再过 _read_json：大小有上限、
文字洗过一遍），才交给 _route；拆出去的那些文件只在这之后拿到已经查过、洗过的 body。
"""

from __future__ import annotations

import secrets
import shutil
import signal
import tempfile
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import IO
from urllib.parse import parse_qs, urlsplit

from .. import export, instance, media
from ..app import App, build_app
from ..config import (
    APP_NAME,
    BROWSER_SHOT_DAYS,
    DESKTOP_SHOT_DAYS,
    Config,
)
from ..exits import EXIT_SIGNALS
from ..i18n import t
from ..providers import has_key
from ..safe import to_json
from . import (
    channels_api,
    chat_api,
    conversations_api,
    fallback_api,
    lifecycle,
    mcp_api,
    media_api,
    memory_api,
    models_api,
    prompt_api,
    reveal,
    rewind_api,
    sessions_api,
    settings_api,
    tasks_api,
)
from . import manifest as web_manifest
from .restore_api import UPLOAD_PATH as RESTORE_UPLOAD
from .server_events import KEEPALIVE_SECONDS, HandlerEvents
from .server_gate import (
    MAX_BODY_BYTES,
    MAX_DISCARD_BYTES,
    MAX_MESSAGE_BODY_BYTES,
    HandlerGate,
)
from .server_page import MARKDOWN, PAGE, HandlerPage, accepts_gzip
from .skills_api import MAX_UPLOAD_BODY_BYTES
from .ui import WebUI
from .webapp import WebApp

__all__ = [
    "DEFAULT_PORT",
    "KEEPALIVE_SECONDS",
    "MARKDOWN",
    "MAX_BODY_BYTES",
    "MAX_DISCARD_BYTES",
    "MAX_MESSAGE_BODY_BYTES",
    "PAGE",
    "SHUTDOWN_WAIT_SECONDS",
    "Handler",
    "Server",
    "WebApp",
    "accepts_gzip",
    "claim_data_dir",
    "create_server",
    "run_web",
    "shut_down",
]

DEFAULT_PORT = 8765
SHUTDOWN_WAIT_SECONDS = 5  # 退出时最多等这么久，让正在进行的一轮收尾存盘


class Server(ThreadingHTTPServer):
    daemon_threads = True  # 事件流的连接一直开着；退出程序时不必等它们
    web: WebApp


class Handler(HandlerGate, HandlerPage, HandlerEvents, BaseHTTPRequestHandler):
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
        if path == RESTORE_UPLOAD:  # 备份的压缩包：请求体是原样的字节，不是 JSON，边读边写进临时文件（上限也在那边，见 restore_api.py）
            self._send_json(*self.server.web.restore.upload(self.headers, self.rfile))
            return
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
        """路由表：每个地址交给谁（两种写法见这个文件开头）。就地写完的只有几行的小事，和把文件的字节发给浏览器的三个接口：
        它们的安全头（attachment、nosniff、sandbox）和 _send 在同一个文件里，一眼看得全。以后再加返回文件内容的接口，照它们办。"""
        web = self.server.web
        path = urlsplit(self.path).path

        if path == "/api/message":
            chat_api.route_message(self, web, body)
        elif path == "/api/confirm":
            chat_api.route_confirm(self, web, body)
        elif path in ("/api/turn/edit", "/api/turn/retry"):
            rewind_api.route_turn(self, web, path, body)
        elif path == "/api/stop":
            sessions_api.route_stop(self, web, body)
        elif path == "/api/new":
            conversations_api.route_new(self, web, body)
        elif path.startswith("/api/conversation/"):
            conversations_api.route_conversation(self, web, path, body)
        elif path.startswith("/api/memory/"):
            memory_api.route_memory(self, web, path, body)
        elif path == "/api/media/list":
            media_api.route_list(self, web, body)
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
            media_api.route_reveal(self, web, body)
        elif path.startswith("/api/skill/"):
            status, payload = web.skills.route(path.removeprefix("/api/skill/"), body)
            self._send_json(status, payload)
        elif path.startswith("/api/agent/"):
            status, payload = web.agents.route(path.removeprefix("/api/agent/"), body)
            self._send_json(status, payload)
        elif path.startswith("/api/mcp/"):
            mcp_api.route_mcp(self, path.removeprefix("/api/mcp/"), body)
        elif path == "/api/prompt":
            self._send_json(200, {"ok": True, **web.prompt_info()})
        elif path in ("/api/prompt/save", "/api/prompt/reset"):
            prompt_api.route_save(self, web, path, body)
        elif path == "/api/prompt/preview":
            prompt_api.route_preview(self, web, body)
        elif path == "/api/reveal":
            reveal.route_reveal(self, web, body)
        elif path in ("/api/todo/update", "/api/todo/delete"):
            tasks_api.route_todo(self, web, path, body)
        elif path == "/api/search":
            conversations_api.route_search(self, web, body)
        elif path == "/api/language":
            settings_api.route_language(self, web, body)
        elif path == "/api/approval":
            settings_api.route_approval(self, web, body)
        elif path == "/api/model":
            models_api.route_model(self, web, body)
        elif path == "/api/model/catalog":
            # 只读内存里的设置和环境变量，不改任何东西，所以不拿对话的锁：对话进行中也能打开换模型的面板看一看
            self._send_json(200, {"ok": True, "catalog": models_api.page_state(web.app)})
        elif path == "/api/key":
            models_api.route_key(self, web, body)
        elif path == "/api/models":
            models_api.route_models(self, web, body)
        elif path.startswith("/api/endpoint/"):
            # 自定义模型接口：保存、删除、测试连接、在访达中显示（见 endpoints_api.py）
            status, payload = web.endpoints.route(path.removeprefix("/api/endpoint/"), body)
            self._send_json(status, payload)
        elif path == "/api/ledger":
            self._send_json(200, {"ok": True, "report": web.spend_report()})
        elif path == "/api/budget":
            settings_api.route_budget(self, web, body)
        elif path == "/api/redact":
            settings_api.route_redact(self, web, body)
        elif path == "/api/web-fetch":
            settings_api.route_web_fetch(self, web, body)
        elif path == "/api/export/info":
            self._send_json(200, {"ok": True, **web.export_info()})
        elif path == "/api/export":
            self._send_export()
        elif path.startswith("/api/restore/"):
            status, payload = web.restore.route(path.removeprefix("/api/restore/"), body, self.server)
            self._send_json(status, payload)
        elif path == "/api/fallbacks":
            status, payload = fallback_api.save(web.app, body)
            if status == 200:
                web.publish_state()
            self._send_json(status, payload)
        elif path == "/api/price":
            models_api.route_price(self, web, body)
        elif path.startswith("/api/task/"):
            tasks_api.route_task(self, web, path.removeprefix("/api/task/"), body)
        elif path.startswith("/api/channel/"):
            channels_api.route_channel(self, web, path.removeprefix("/api/channel/"), body)
        elif path == "/api/browser/shot":
            shot = web.browser.shot(body.get("id"))
            if shot is None:
                self._send_json(409, {"error": t("这张截图的大图已经过期删掉了（只留 {days} 天），对话里还看得到小图。", days=BROWSER_SHOT_DAYS)})
                return
            # 和 /api/media/file 一样：当成下载的文件、不按内容猜类型、万一被直接打开也什么都不运行
            headers = {"Content-Disposition": 'attachment; filename="screenshot.jpg"', "Content-Security-Policy": "default-src 'none'; sandbox"}
            self._send(200, "image/jpeg", shot, headers)
        elif path.startswith("/api/browser/"):
            status, payload = web.browser.route(path.removeprefix("/api/browser/"), body)
            self._send_json(status, payload)
        elif path.startswith("/api/commands/"):
            status, payload = web.commands.route(path.removeprefix("/api/commands/"), body)
            self._send_json(status, payload)
        elif path.startswith("/api/hooks/"):
            status, payload = web.hooks.route(path.removeprefix("/api/hooks/"), body)
            self._send_json(status, payload)
        elif path.startswith("/api/sandbox/"):
            status, payload = web.sandbox.route(path.removeprefix("/api/sandbox/"), body)
            self._send_json(status, payload)
        elif path == "/api/fork":
            status, payload = web.fork.route(body)
            self._send_json(status, payload)
        elif path.startswith("/api/changes/"):
            status, payload = web.changes.route(path.removeprefix("/api/changes/"), body)
            self._send_json(status, payload)
        elif path == "/api/computer/shot":
            shot = web.computer.shot(body.get("id"))
            if shot is None:
                self._send_json(409, {"error": t("这张截图的大图已经过期删掉了（只留 {days} 天），对话里还看得到小图。", days=DESKTOP_SHOT_DAYS)})
                return
            # 和浏览器的截图、/api/media/file 一样：当成下载的文件、不按内容猜类型、万一被直接打开也什么都不运行
            headers = {"Content-Disposition": 'attachment; filename="screenshot.jpg"', "Content-Security-Policy": "default-src 'none'; sandbox"}
            self._send(200, "image/jpeg", shot, headers)
        elif path.startswith("/api/computer/"):
            status, payload = web.computer.route(path.removeprefix("/api/computer/"), body)
            self._send_json(status, payload)
        elif path.startswith("/api/voice/"):
            status, payload = web.voice.route(path.removeprefix("/api/voice/"), body)
            self._send_json(status, payload)
        elif path == "/api/updates" or path.startswith("/api/updates/"):
            status, payload = web.updates.route(path.removeprefix("/api/updates").strip("/"), body)
            self._send_json(status, payload)
        else:
            self._send_json(404, {"error": "not found"})

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

    def _answer(self, error: str, **data: object) -> None:
        """和 _finish 一样的约定，成功时带上 data。"""
        if error:
            self._send_json(409, {"error": error})
        else:
            self._send_json(200, {"ok": True, **data})

    # ---- 响应 --------------------------------------------------------------

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
    # Windows 上还有 Ctrl+Break（SIGBREAK，见 exits.py）
    def interrupted(signum: int, frame: object) -> None:
        for number in EXIT_SIGNALS:
            signal.signal(number, signal.SIG_DFL)
        raise KeyboardInterrupt

    previous = {number: signal.signal(number, interrupted) for number in EXIT_SIGNALS} if threading.current_thread() is threading.main_thread() else None
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
        for number in previous or ():
            signal.signal(number, signal.SIG_IGN)  # 正在收尾：这时再来一个 SIGTERM 不要打断存盘
        shut_down(server, app)
        for number, handler in (previous or {}).items():
            signal.signal(number, handler)
    print("\n" + t("再见。"))
    return 0
