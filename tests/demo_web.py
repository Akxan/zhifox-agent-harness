"""不花钱地试用网页界面：真实的服务器和页面，后面接的是本地的假模型。

    uv run python tests/demo_web.py            # 启动后把打印出来的地址贴进浏览器
    uv run python tests/demo_web.py --no-key   # 模拟"还没有填 API 密钥"的第一次使用

改了页面想看看效果、或者只是想到处点点，用这个脚本，而不是 `uv run harness --web`：

  - 所有回复都是下面写好的，不会向任何厂商的 API 发请求，不花钱。
  - 数据写在临时目录里，不碰项目的 `data/`；密钥也是编造的，不读也不写真正的 `.env`。
    （只设置 HARNESS_DATA_DIR 再运行正式程序是不够的：那样用的还是真正的 `.env` 和真正的 API。）
  - 回复是一小段一小段慢慢发的，能看到逐字输出的效果。测试用的 fake_api.py 是一次发完的，看不出来。
  - 页面每次刷新都重新从磁盘读，改完 index.html（或 markdown.js）刷新一下就能看到，不用重启。
  - "在访达中显示"和"打开记忆文件夹"只把路径打印出来，不会真的弹出窗口。
  - 全新的数据目录里预先放了三条编造的记忆，信息栏的"记忆"一栏里能看、能删。
  - 也预先装了几个编造的 Skill（扩展 → Skill）：一个合格的、一个带脚本的、一个停用的、一个写坏了的。
    "从 Claude Code 导入"看的是临时目录里一个假的 ~/.claude/skills（HOME 换成了临时目录），不是你电脑上真正的那个。
    "从网址添加"会真的去下载（只是下载公开的文件，不花钱）。
  - 也预先接好了几个假的 MCP 服务器（tests/fake_mcp.py）：两个本地的（新一代、老一代协议各一个）、
    一个 HTTP 的（在这个进程里跑）、一个启动不了的、一个停用的。"扩展 → MCP"里都能看到、能点。
    "从 Claude 导入"读的是临时目录里编造的 Claude Desktop / Claude Code 配置，不会读你电脑上真正的那些
    （这个脚本把 HOME 换成了临时目录）。
  - 也预先放了几个编造的自定义 Agent（扩展 → Agent）：资料研究员（示例）、文件管家（能执行命令，用来看
    "子 Agent 要你确认"的样子）、一个写坏了的。"从 Claude Code 导入"读的是临时目录里假的 ~/.claude/agents。
    "新对话"旁边的箭头可以用它们开始一段对话（角色）。
  - 定时任务照常运行（调度线程和正式程序里一样）。全新的数据目录里预先放了几个编造的任务：
    一条已经弹出、还没关掉的提醒，一个跑过两次的"每日安排"，一个关掉了的每周任务，一个每 45 分钟的提醒。
    任务的回复不占下面那一串（几个任务和你的对话同时跑，按顺序就分不清谁是谁了）：每次运行先要执行一条命令
    （"每次确认"的任务会停下来等你批准），然后交代做完了。所以可以一边聊天，一边让好几个任务一起跑。

  - 也带着一个假的 Telegram（tests/fake_telegram.py，只在这台电脑上，不连真正的 Telegram）。启动时打印两样东西：
    假机器人的 token（在"扩展 → 通道"的向导里粘贴它）和一个"手机"页面的地址（在另一个标签页里打开，
    就像拿着手机：发配对码、聊天、点"允许 / 拒绝"）。聊天里的回复也不占下面那一串：单数轮打个招呼，
    双数轮执行一条要你批准的命令（"每次确认"时会发来按钮，网页上也能答复）。
  - Discord、Slack、飞书、钉钉、QQ 也各有一个假的（tests/fake_discord.py……），启动时打印每一家编造的 token
    （或者 ID 和密钥），在向导里粘贴就能连上。它们共用另一个"手机"页面（tests/demo_phone.py，上面选平台）。
    飞书、钉钉收消息本来要装官方的 SDK；演示里换成了假的长连接，当作已经装好了。加 --no-sdk 看没装时页面怎么说。

  - 浏览器（设置 → 浏览器）是开着的：发一句含有"演示商店"的话（比如"用浏览器看看演示商店里有什么"）开始一段新对话，
    助手会用真的 Chrome 打开这个脚本在本机起的一个"演示商店"网页：先问你能不能去这个网站，然后读页面、点搜索框、
    输入（又问你一次）、截图。对话里每一步下面有一张小截图，点开看大图；顶栏上会出现"浏览器正在被智狐控制"。
    默认不显示浏览器窗口；加 --browser-window 可以看着它操作。这台电脑上没有 Chrome 的话，助手会说找不到。
    这一段的回复不占下面那一串（看每次请求的历史走到了哪一步，见 browser_reply）。

  - "操作整台电脑"（设置 → 操作整台电脑）也是开着的，但背后是一台假的电脑（tests/fake_desktop.py）：截图交回的是一张编造的
    "桌面"，点击、打字只是记下来，绝不碰你真的屏幕、鼠标和键盘。发一句含有"演示电脑"的话（比如"演示电脑：在备忘录里记一笔"）
    开始一段新对话：助手先截图（确认卡片上有"这段对话里截图不再问"），再点输入框、打字，按 ⌘S 保存，最后按 ⌘Q
    （卡片上带着危险按键的警告，可以拒绝看看一串操作怎么停下）。顶栏上会出现"智狐正在控制这台电脑"和"停止"。
    这一段的回复也不占下面那一串（见 computer_reply）。

预设的回复按顺序是：打招呼（带思考过程和一个链接）、添加待办（一次工具调用）、建文件（要你确认的命令）、
联网搜索、一次出错的工具调用、一段用齐了各种 Markdown 写法的长回复（标题、列表、表格、代码块……）、
一次 MCP 工具调用；然后是两次"交给子 Agent"：先交给资料研究员（它读一个文件、联网搜索、交回一段 Markdown），
再交给文件管家（它要执行一条命令，会弹出写着"「文件管家」想要"的确认卡片）；之后是几条凑数的回复。
发什么消息都行，回复只看顺序（主助手和子 Agent 共用这一串回复）。
例外是第一条消息里写着 [ui:名字] 的对话：整段按 tests/demo_scripts.py 里同名的剧本回答（页面的自动化测试用它，
手动试也行，比如发 "[ui:approve] 试试" 就会弹出一张确认卡片）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # 让 fake_api、fake_openai 可以直接导入
sys.path.insert(0, str(Path(__file__).parent.parent))

parser = argparse.ArgumentParser(description="不花钱地试用网页界面（本地的假模型，临时的数据目录）")
parser.add_argument("--port", type=int, default=8766)
parser.add_argument("--data", type=Path, help="数据放在哪个目录（默认新建一个临时目录）。想保留会话、下次接着看时用")
parser.add_argument("--delay", type=float, default=0.035, help="逐字输出时每一小段之间隔多少秒")
parser.add_argument("--think", type=float, default=0.5, help="每次回复开口之前先停多少秒（像模型在想）。页面的自动化测试调小它")
parser.add_argument("--no-key", action="store_true", help="不设置任何密钥，看第一次使用时的样子")
parser.add_argument("--telegram-port", type=int, default=0, help="假 Telegram（\"手机\"页面）用的端口，默认随便挑一个空闲的")
parser.add_argument("--phone-port", type=int, default=0, help="别的几家共用的\"手机\"页面用的端口，默认随便挑一个空闲的")
parser.add_argument("--no-sdk", action="store_true", help="当作飞书、钉钉的官方 SDK 没装，看页面上怎么说")
parser.add_argument("--browser-window", action="store_true", help="浏览器演示时显示 Chrome 的窗口（默认在后台运行，看不到窗口）")
options = parser.parse_args()

# 这个脚本绝不能碰到真正的密钥：先把环境变量里的都清掉，再放一个编造的
for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY"):
    os.environ.pop(name, None)
if not options.no_key:
    os.environ["ANTHROPIC_API_KEY"] = "fake-claude-key-0001"

import anthropic
import demo_scripts
import fake_dingtalk
import fake_discord
import fake_feishu
import fake_qq
import fake_slack
import openai
from demo_phone import Phone
from fake_api import _events, reply, text, thinking, tool_use
from fake_desktop import FakeDesktop, install, window_or_desktop
from fake_mcp import FakeMcpHttp
from fake_openai import FakeOpenAI, call, chat
from fake_telegram import TOKEN, FakeTelegram

import harness.webui.server as server_module
from harness.agents import examples as agent_examples
from harness.app import build_app
from harness.channels import platforms
from harness.config import Config
from harness.desktop import backend as desktop_backend
from harness.tasks import new_run
from harness.webui import lifecycle
from harness.webui.ui import WebUI


def in_small_pieces(events: list[dict]) -> list[dict]:
    """把文字和思考拆成三个字一段，像真的模型那样一点点往外吐。"""
    pieces = []
    for event in events:
        delta = event.get("delta") or {}
        field = {"text_delta": "text", "thinking_delta": "thinking"}.get(delta.get("type"))
        if field is None:
            pieces.append(event)
            continue
        for start in range(0, len(delta[field]), 3):
            pieces.append({**event, "delta": {**delta, field: delta[field][start : start + 3]}})
    return pieces


def task_reply(body: dict) -> dict:
    """定时任务的一次运行该拿到的回复：第一次先执行一条命令，看到命令的结果之后交代做完了。

    只看这次运行自己的历史（有几条消息），不看先后顺序：几个任务一起跑的时候，谁先谁后都对得上。
    """
    if len(body.get("messages") or []) <= 1:
        return reply(
            thinking("这是一个定时任务。先看看工作目录里有什么，再交代结果。"),
            tool_use("toolu_task", "run_command", {"command": "sleep 2 && ls"}),
            stop_reason="tool_use",
        )
    return reply(text("做完了：看过了工作目录，里面有 notes.txt。"))


def channel_reply(body: dict) -> dict:
    """聊天通道（假 Telegram）里的请求怎么回答：刚执行完命令就交代结果；不然单数轮打招呼，双数轮执行一条命令。"""
    messages = body.get("messages") or []
    last = messages[-1] if messages else {}
    results = [block for block in (last.get("content") or []) if isinstance(block, dict) and block.get("type") == "tool_result"]
    if results:
        if any(block.get("is_error") for block in results):  # 你点了"拒绝"
            return reply(text("好的，不看了。需要的时候再叫我。"))
        return reply(text("看完了：磁盘还剩 **120 GB**，一切正常。\n\n- 系统盘：用了 38%\n- 数据盘：用了 61%"))
    asked = sum(1 for message in messages if message.get("role") == "user" and not any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in (message.get("content") or [])
    ))
    if asked % 2 == 1:
        return reply(text("你好！我是智狐，现在在 Telegram 里陪你。可以让我：\n\n1. 查资料、写东西\n2. 看看待办和日程\n3. 在电脑上执行命令（每次都先问你）\n\n试试发一句“看看磁盘还剩多少”。"))
    return reply(text("我来看一下磁盘。"), tool_use(f"toolu_chat_{asked}", "run_command", {"command": "df -h . && du -sh ~/Downloads"}), stop_reason="tool_use")


# 浏览器演示用的"演示商店"：这个脚本在本机起的一个小网页（见下面的 shop），不连外网
SHOP_PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>演示商店 · 雨具</title>
<style>
  body { margin: 0; font: 16px/1.6 system-ui, sans-serif; color: #1d1d1f; background: #f5f3ee; }
  header { display: flex; align-items: center; gap: 24px; padding: 18px 40px; background: #fff; border-bottom: 1px solid #e4e0d8; }
  header b { font-size: 20px; letter-spacing: .02em; }
  form { display: flex; gap: 8px; margin-left: auto; }
  input { width: 260px; height: 36px; padding: 0 12px; font: inherit; border: 1px solid #b9b3a7; border-radius: 10px; }
  button { height: 36px; padding: 0 16px; font: inherit; color: #fff; background: #1d1d1f; border: 0; border-radius: 10px; }
  main { display: grid; grid-template-columns: repeat(3, 1fr); gap: 22px; padding: 32px 40px; }
  article { padding: 18px; background: #fff; border-radius: 16px; box-shadow: 0 6px 20px rgba(0,0,0,.06); }
  .swatch { height: 120px; margin-bottom: 12px; border-radius: 12px; }
  h2 { margin: 0 0 4px; font-size: 17px; } p { margin: 0 0 10px; color: #5d5a54; } .price { font-weight: 600; color: #1d1d1f; }
</style></head><body>
<header><b>☂ 演示商店</b><nav><a href="#">雨伞</a> · <a href="#">雨衣</a> · <a href="#">雨靴</a></nav>
<form onsubmit="event.preventDefault(); document.title = '搜索：' + this.q.value"><input name="q" type="search" placeholder="搜索商品" aria-label="搜索商品"><button>搜索</button></form></header>
<main>
<article><div class="swatch" style="background:linear-gradient(135deg,#4f6bff,#8b5cf6)"></div><h2>折叠雨伞</h2><p>三折，220 克，放得进包里。</p><p class="price">¥ 89</p><button type="button">加入购物车</button></article>
<article><div class="swatch" style="background:linear-gradient(135deg,#ec4899,#f59e0b)"></div><h2>长柄雨伞</h2><p>十二骨，大风天也不翻。</p><p class="price">¥ 129</p><button type="button">加入购物车</button></article>
<article><div class="swatch" style="background:linear-gradient(135deg,#0d9488,#4f6bff)"></div><h2>儿童雨伞</h2><p>圆头伞骨，颜色亮，好找。</p><p class="price">¥ 59</p><button type="button">加入购物车</button></article>
</main></body></html>"""


def browser_reply(body: dict, shop: str) -> dict:
    """浏览器演示：打开演示商店 → 读页面 → 点搜索框、输入 → 截图 → 交代看到了什么。按历史走到了哪一步来回答。"""
    messages = body.get("messages") or []
    last = messages[-1] if messages else {}
    results = [block for block in (last.get("content") or []) if isinstance(block, dict) and block.get("type") == "tool_result"]
    if any(block.get("is_error") for block in results):
        return reply(text("好的，这一步不做了。需要的时候再叫我用浏览器。"))
    step = sum(1 for message in messages if message.get("role") == "assistant")
    if step == 0:
        return reply(
            thinking("用户想看看演示商店。先用浏览器打开它。"),
            text("我用智狐的浏览器打开演示商店看看。"),
            tool_use("toolu_browser_1", "navigate", {"url": shop}, toolset="browser"),
            stop_reason="tool_use",
        )
    if step == 1:
        return reply(tool_use("toolu_browser_2", "read_page", {"filter": "interactive"}, toolset="browser"), stop_reason="tool_use")
    if step == 2:
        page = "".join(block["content"] if isinstance(block.get("content"), str) else "".join(
            part.get("text", "") for part in block.get("content") or [] if isinstance(part, dict)) for block in results)
        found = next((line for line in page.splitlines() if "搜索商品" in line), "")
        ref = found[found.find("[") + 1 : found.find("]")] if "[" in found else "ref_1"
        return reply(
            text("页面上有一个搜索框和三件商品。我在搜索框里输入“雨伞”。"),
            tool_use("toolu_browser_3", "left_click", {"target": {"type": "ref", "ref": ref}}, toolset="browser"),
            tool_use("toolu_browser_4", "type", {"text": "雨伞"}, toolset="browser"),
            stop_reason="tool_use",
        )
    if step == 3:
        return reply(tool_use("toolu_browser_5", "screenshot", {}, toolset="browser"), stop_reason="tool_use")
    return reply(text("看完了。演示商店里有 **3 把雨伞**：\n\n| 商品 | 价格 |\n|:--|--:|\n| 折叠雨伞 | ¥ 89 |\n| 长柄雨伞 | ¥ 129 |\n| 儿童雨伞 | ¥ 59 |\n\n要不要我把其中一把加入购物车？（点“加入购物车”不用问你，真要下单时我会先问。）"))


def computer_reply(body: dict) -> dict:
    """操作整台电脑的演示（假的电脑）：截图 → 点输入框、打字 → ⌘S 保存、截图 → ⌘Q → 交代做了什么。按历史走到了哪一步来回答。"""
    messages = body.get("messages") or []
    last = messages[-1] if messages else {}
    results = [block for block in (last.get("content") or []) if isinstance(block, dict) and block.get("type") == "tool_result"]
    if any(block.get("is_error") for block in results):
        return reply(text("好的，这一步不做了，后面的也都没做。需要的时候再叫我。"))
    step = sum(1 for message in messages if message.get("role") == "assistant")
    if step == 0:
        return reply(
            thinking("用户想让我在备忘录里记一笔。先截一张屏幕的图，看看现在是什么样子。"),
            text("我先截一张屏幕的图看看。"),
            tool_use("toolu_computer_1", "screenshot", {}, toolset="computer"),
            stop_reason="tool_use",
        )
    if step == 1:
        return reply(
            text("屏幕上有一个“备忘录”的窗口，中间是一个输入框。我点一下它，然后输入。"),
            tool_use("toolu_computer_2", "left_click", {"coordinate": [535, 382]}, toolset="computer"),
            tool_use("toolu_computer_3", "type", {"text": "明天上午十点：和设计组过一遍首页"}, toolset="computer"),
            stop_reason="tool_use",
        )
    if step == 2:
        return reply(
            text("输好了。按 ⌘S 保存，再截一张图确认。"),
            tool_use("toolu_computer_4", "key", {"text": "cmd+s"}, toolset="computer"),
            tool_use("toolu_computer_5", "screenshot", {}, toolset="computer"),
            stop_reason="tool_use",
        )
    if step == 3:
        return reply(
            text("保存好了。最后退出备忘录（⌘Q）。"),
            tool_use("toolu_computer_6", "key", {"text": "cmd+q"}, toolset="computer"),
            stop_reason="tool_use",
        )
    return reply(text("记好了：在“备忘录”里写下了 **明天上午十点：和设计组过一遍首页**，保存以后退出了备忘录。\n\n（这是演示：背后是一台假的电脑，你真的屏幕、鼠标和键盘一样都没碰。）"))


class SlowClaude:
    """假的 Anthropic API：按顺序回放写好的回复，但是慢慢地发。定时任务的请求另外回答（见 task_reply）。"""

    def __init__(self, replies: list[dict]):
        self.replies = list(replies)
        self.lock = threading.Lock()  # 你的对话和几个任务的请求可能同时到

    def start(self) -> SlowClaude:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                first = json.dumps((body.get("messages") or [None])[0], ensure_ascii=False)
                with api.lock:
                    if "<task>" in first:
                        scripted = task_reply(body)
                    elif "<channel>" in first:
                        scripted = channel_reply(body)
                    elif (script := demo_scripts.scripted(first, body)) is not None:
                        scripted = script  # 第一条消息里写着 [ui:名字]：按那段剧本回答
                    elif "演示商店" in first:
                        scripted = browser_reply(body, shop.url)
                    elif "演示电脑" in first:
                        scripted = computer_reply(body)
                    else:
                        scripted = api.replies.pop(0) if api.replies else None
                if scripted is None:
                    self.send_error(500, "no scripted reply left")  # 回复用完了：页面上会看到一条出错提示
                    return
                if "status" in scripted:  # 剧本要这一次失败（fake_api.failure 的格式，见 demo_scripts.flaky）
                    payload = json.dumps(scripted["error"]).encode()
                    self.send_response(scripted["status"])
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                time.sleep(options.think)  # 模型开口之前先"想"一会儿
                for event in in_small_pieces(_events(scripted)):
                    self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
                    self.wfile.flush()
                    if event["type"] == "content_block_delta":
                        time.sleep(options.delay)

            def do_GET(self) -> None:  # 模型列表（"同步模型"用）
                names = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2"]
                models = [{"type": "model", "id": name, "display_name": name, "created_at": "2026-01-01T00:00:00Z"} for name in names]
                payload = json.dumps({"data": models, "has_more": False, "first_id": None, "last_id": None}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self


GREETING = (
    "你好！我是运行在你电脑上的个人助手。\n\n"
    "我能帮你做这几类事：\n"
    "1. 记事：把你的偏好和重要信息写进长期记忆，下次还记得。\n"
    "2. 待办：添加、查看、完成待办事项。\n"
    "3. 文件：读取和修改这个工作目录里的文件。\n"
    "4. 命令：在终端里运行命令（每次都会先问你）。\n\n"
    "想了解原理的话，可以看看 https://example.com/agent-loop 这篇说明。"
)
# 一段用齐了各种 Markdown 写法的回复：看渲染、逐字输出、搜索跳转、窄屏下的表格和代码块都用它
MARKDOWN_TOUR = """## 智狐能画的 Markdown

回复现在按 **Markdown** 显示：*斜体*、~~删除线~~、`行内代码`，还有 [链接](https://example.com/markdown)。
单个换行就是换行，
不用空一行。

### 列表

1. 第一步：安装依赖
2. 第二步：启动
   - 终端界面
   - 网页界面
     - [x] 浅色
     - [x] 深色
     - [ ] 窄屏
3. 第三步：开始对话

> **提示：**引用里也能用粗体和 `代码`。
> 第二行接着写。

### 表格

| 功能 | 状态 | 说明 |
|:-----|:----:|-----:|
| 标题和段落 | ✅ | 衬线字体的标题 |
| 代码块 | ✅ | 语言标签、复制按钮、简单着色 |
| 表格 | ✅ | 太宽时在框里横向滚动，这一格的文字故意写得很长很长，好看看窄屏上的样子 |
| 图片 | ⚠️ | 不加载，只显示说明和网址：![一只小狐狸](https://example.com/fox.png) |

### 代码

```python
def greet(name: str) -> str:
    \"\"\"打个招呼（这一行是字符串）\"\"\"
    count = 42  # 注释是灰色的
    return f"你好，{name}！" * count
```

```bash
uv run python tests/demo_web.py --port 8901  # 换一个端口
```

---

不安全的链接只显示成文字：[点我](javascript:alert(1))；页面里的 <script>alert(1)</script> 也只是文字。
"""
WEB_SEARCH = [
    {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "agent harness 是什么"}},
    {
        "type": "web_search_tool_result",
        "tool_use_id": "srvtoolu_1",
        "content": [
            {"type": "web_search_result", "url": "https://example.com/a", "title": "A", "encrypted_content": "x", "page_age": None},
            {"type": "web_search_result", "url": "https://example.com/b", "title": "B", "encrypted_content": "y", "page_age": None},
        ],
    },
]
# 子 Agent 交回的回答（Markdown）：卡片里照样按 Markdown 画
SUB_ANSWER = """**结论：** agent harness 是包在模型外面的那层程序，负责循环、工具和权限。

1. 模型只会"读入一段对话、写出下一段"，让它能做事的是 harness 的循环（出处：notes.txt）
2. 工具的说明书由 harness 写，执行也在 harness 这边（出处：https://example.com/a）
3. 会改动电脑的操作先问用户（出处：https://example.com/b）

**不确定的地方：** 两个网页都没写日期，可能已经过时。"""
claude = SlowClaude(
    [
        reply(thinking("用户在打招呼。简单介绍一下自己能做什么就行，不用调用工具。先列出四类能力，再给一个链接。"), text(GREETING)),
        reply(thinking("要加一条待办，截止日期是这周五。"), tool_use("toolu_1", "todo_add", {"title": "交报告", "due": "2026-11-27"}), stop_reason="tool_use"),
        reply(text("记下了：周五（11 月 27 日）下午交报告。")),
        reply(text("我来建这个文件。"), tool_use("toolu_2", "run_command", {"command": "touch made.txt && ls -la"}), stop_reason="tool_use"),
        reply(text("文件建好了。")),
        reply(*WEB_SEARCH, text("查到了两条资料：harness 是包在模型外面的那层程序。")),
        reply(tool_use("toolu_3", "todo_update", {"id": 99, "done": True}), stop_reason="tool_use"),
        reply(text("没有编号为 99 的待办，所以没有改动。")),
        reply(thinking("用户想看看各种 Markdown 的样子。"), text(MARKDOWN_TOUR)),
        # 调用一个 MCP 服务器的工具（假服务器 files 的 echo）：会弹出确认卡片，除非在"扩展 → MCP"里把它设成了不用问
        reply(text("我用 MCP 服务器试一下。"), tool_use("toolu_4", "mcp__files__echo", {"text": "你好，MCP"}), stop_reason="tool_use"),
        reply(text("MCP 服务器原样回了这句话。")),
        # 交给子 Agent（见 harness/agents/runner.py）：主助手调用 agent 工具 → 子 Agent 自己的两步 → 主助手转告
        reply(text("这件事交给资料研究员去查。"), tool_use("toolu_5", "agent", {"name": "资料研究员", "task": "查一下什么是 agent harness，结合工作目录里的 notes.txt，交回三条要点和出处。"}), stop_reason="tool_use"),
        reply(thinking("先看看工作目录里的笔记。"), tool_use("toolu_s1", "str_replace_based_edit_tool", {"command": "view", "path": "notes.txt"}), stop_reason="tool_use"),
        reply(*WEB_SEARCH, text(SUB_ANSWER)),
        reply(text("资料研究员查完了：harness 是包在模型外面、负责循环、工具和权限的那层程序。详细的要点在上面的卡片里。")),
        reply(text("我让文件管家看一眼工作目录。"), tool_use("toolu_6", "agent", {"name": "文件管家", "task": "列出工作目录里的文件和大小，告诉我哪些可以清理。"}), stop_reason="tool_use"),
        reply(tool_use("toolu_s2", "run_command", {"command": "ls -la"}), stop_reason="tool_use"),
        reply(text("工作目录里只有 notes.txt（两行字）和一个刚建的空文件 made.txt，都很小，不用清理。")),
        reply(text("文件管家说：目录里只有两个小文件，不用清理。")),
        *(reply(text("（这是一条预设的回复，用来凑数。）" * 3)) for _ in range(8)),
    ]
).start()
# GPT、Gemini、DeepSeek 共用的假服务器（OpenAI 格式）。别叫它 others：下面聊天平台的那几个假服务器叫这个名字，
# 重名的话后面那个会顶掉它，换到 DeepSeek 时就连不上了（真出过：保存 DeepSeek 的密钥、换模型都回答 500）
compat = FakeOpenAI(
    [
        chat("你好！我是 DeepSeek。", reasoning="用户换了模型，打个招呼。"),
        chat(calls=[call("call_1", "todo_list", "{}")]),
        chat("你有一条待办：交报告，11 月 27 日截止。"),
        *(chat("（预设回复用完了）") for _ in range(8)),
    ],
    models=["deepseek-flash", "deepseek-v4-pro"],
).start()

root = options.data or Path(tempfile.mkdtemp(prefix="harness-demo-"))
# "从 Claude 导入"会去读 HOME 下面 Claude Desktop 和 Claude Code 的配置。演示里换成临时目录里编造的那几份，
# 绝不读你电脑上真正的配置（那里面有你的密钥）
fake_home = root / "home"
os.environ["HOME"] = str(fake_home)
# 一个走 HTTP 的假 MCP 服务器，就在这个进程里跑。它的端口每次都不一样，所以配置里写 ${DEMO_MCP_URL}，启动时再换成真正的地址
docs_server = FakeMcpHttp("modern", sse=True, token="demo-header-token-9876wxyz").start()


class Shop:
    """浏览器演示用的"演示商店"网页（SHOP_PAGE），只在这台电脑上。"""

    def start(self) -> Shop:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                payload = SHOP_PAGE.encode()
                self.send_response(200)
                self.send_header("content-type", "text/html; charset=utf-8")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        return self


shop = Shop().start()
os.environ["DEMO_MCP_URL"] = docs_server.url
workspace = root / "work"
workspace.mkdir(parents=True, exist_ok=True)
(workspace / "notes.txt").write_text("第一行\n第二行\n", encoding="utf-8")
# 全新的数据目录里先放几条编造的记忆，信息栏的"记忆"一栏才有东西可看、可删。全删掉就能看到空的样子；
# 用 --data 接着用上次的目录时不再补，删掉的不会又冒出来
memory_dir = root / "data" / "memories"
if not (root / "data").exists():
    for name, body in (
        ("用户偏好.md", "# 用户偏好\n\n- 喜欢简短的回答，先给结论再解释\n- 用中文交流\n- 代码注释写“为什么”，不写“做了什么”\n"),
        ("projects/zhifox.md", "# 智狐\n\n手写的 agent harness，正在学习 agent 的原理。\n\n- 仓库：zhifox-agent-harness\n- 每次改动都要提交并推送\n"),
        ("projects/reading-list.md", "# 想读的\n\n1. Building effective agents\n2. 《程序员修炼之道》\n"),
    ):
        (memory_dir / name).parent.mkdir(parents=True, exist_ok=True)
        (memory_dir / name).write_text(body, encoding="utf-8")
    # 一段"有多媒体库之前"的旧会话：附件的概要里没有原件的编号，内容只在对话历史里。
    # 左边栏的"多媒体"里能看到它们：图片、PDF、文本照样能看、能下载；Word 只剩当时取出的文字
    import base64
    import struct
    import zlib

    from harness.attachments import Attachment as Item
    from harness.attachments import wrap_text
    from harness.documents import NOTE

    def gradient_png(width: int, height: int) -> bytes:
        """画一张渐变色的 PNG（不用任何图片库：PNG 就是几段带校验和的数据块）。"""
        rows = b"".join(
            b"\x00" + bytes(channel for x in range(width) for channel in (80 + x * 140 // width, 90 + y * 120 // height, 230 - x * 60 // width))
            for y in range(height)
        )

        def chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

        header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")

    picture = gradient_png(640, 400)
    thumb = "data:image/png;base64," + base64.b64encode(gradient_png(240, 150)).decode()
    paper = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    notes = Item("text", "旧笔记.md", "text/plain", 12, text="# 旧笔记\n\n第一行。")
    report = Item("text", "旧周报.docx", "text/plain", 18, text="# 周报\n\n本周完成了三件事。", note=NOTE)
    old = time.time() - 3 * 86400
    legacy = {
        "id": time.strftime("%Y%m%d-%H%M%S", time.localtime(old)) + "-01d01d",
        "provider": "anthropic", "model": "claude-opus-5-5", "title": "旧会话里的附件", "created_at": old, "updated_at": old,
        "context_tokens": 0, "version": 1,
        "events": [
            {"type": "user", "text": "看看这几个文件", "attachments": [
                {"kind": "image", "name": "旧截图.png", "media_type": "image/png", "size": len(picture), "thumb": thumb},
                {"kind": "pdf", "name": "旧报告.pdf", "media_type": "application/pdf", "size": len(paper)},
                {"kind": "text", "name": notes.name, "media_type": "text/plain", "size": len(notes.text)},
                {"kind": "text", "name": report.name, "media_type": "text/plain", "size": len(report.text)},
            ]},
            {"type": "turn_start"}, {"type": "text", "text": "收到四个文件。"}, {"type": "turn_end"},
        ],
        "messages": [
            {"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(picture).decode()}},
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(paper).decode()}, "title": "旧报告.pdf"},
                {"type": "text", "text": wrap_text(notes)}, {"type": "text", "text": wrap_text(report)},
                {"type": "text", "text": "<context>当时的时间</context>"}, {"type": "text", "text": "看看这几个文件"},
            ]},
            {"role": "assistant", "content": [{"type": "text", "text": "收到四个文件。"}]},
        ],
    }
    (root / "data" / "conversations").mkdir(parents=True, exist_ok=True)
    (root / "data" / "conversations" / f"{legacy['id']}.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    fake_mcp = str(Path(__file__).parent / "fake_mcp.py")
    servers = {
        "files": {"command": sys.executable, "args": [fake_mcp, "--era", "modern"]},
        "notes": {"command": sys.executable, "args": [fake_mcp, "--era", "legacy", "--page-size", "3"], "env": {"NOTES_TOKEN": "demo-secret-token-1234abcd"}},
        "docs": {"type": "http", "url": "${DEMO_MCP_URL}", "headers": {"Authorization": "Bearer demo-header-token-9876wxyz"}},
        "broken": {"command": "definitely-not-installed-mcp", "args": ["--serve"]},
        "paused": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"], "enabled": False},
    }
    (root / "data" / "mcp.json").write_text(json.dumps({"mcpServers": servers}, indent=2), encoding="utf-8")
    # 编造的 Claude Desktop / Claude Code 配置，给"从 Claude 导入"用（macOS 和 Linux 的位置各放一份）
    desktop = {
        "mcpServers": {
            "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/demo/Desktop"]},
            "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"], "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "fake-github-token-for-the-demo-0001"}},
            "files": servers["files"],
        }
    }
    for folder in (fake_home / "Library" / "Application Support" / "Claude", fake_home / ".config" / "Claude"):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "claude_desktop_config.json").write_text(json.dumps(desktop, indent=2), encoding="utf-8")
    claude_code = {
        "mcpServers": {
            "sentry": {"type": "http", "url": "https://mcp.sentry.example/mcp", "headers": {"Authorization": "Bearer fake-sentry-token-for-the-demo-0002"}},
            "old-sse": {"type": "sse", "url": "https://legacy.example/sse"},
        },
        "projects": {str(workspace): {"mcpServers": {"filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]}}}},
    }
    (fake_home / ".claude.json").write_text(json.dumps(claude_code, indent=2), encoding="utf-8")
    (workspace / ".mcp.json").write_text(json.dumps({"mcpServers": {"playwright": {"command": "npx", "args": ["@playwright/mcp@latest"]}}}, indent=2), encoding="utf-8")
# 几个编造的 Skill：合格的、带脚本和参考资料的、停用的、frontmatter 写坏了的（列表里会标出"需要修改"）
SKILLS = {
    "weekly-report/SKILL.md": "---\nname: weekly-report\ndescription: 整理每周的工作周报。用户说“写周报”“总结这周做了什么”时使用。\n---\n\n# 周报\n\n1. 先问清楚这周的重点项目\n2. 按“完成、进行中、下周计划”三部分写\n3. 每一条写成一句话，带上日期\n",
    "pdf-tools/SKILL.md": "---\nname: pdf-tools\ndescription: Extract text and tables from PDF files and fill PDF forms. Use when the user mentions PDFs, forms, or document extraction.\nlicense: MIT\ncompatibility: Requires python3 and pdfplumber\nmetadata:\n  author: example-org\n  version: \"1.2\"\n---\n\n# PDF tools\n\nRun `${CLAUDE_SKILL_DIR}/scripts/extract.py <file>` to get the text.\nFor forms, read references/FORMS.md first.\n",
    "pdf-tools/scripts/extract.py": "import sys\n\nimport pdfplumber\n\nwith pdfplumber.open(sys.argv[1]) as pdf:\n    for page in pdf.pages:\n        print(page.extract_text())\n",
    "pdf-tools/references/FORMS.md": "# Filling forms\n\n1. List the fields\n2. Fill them one by one\n",
    "meeting-notes/SKILL.md": "---\nname: meeting-notes\ndescription: 把会议记录整理成“结论、待办、下次讨论”三部分。用户贴来一段会议记录时使用。\n---\n\n按三部分整理，待办写成“谁 · 做什么 · 什么时候”。\n",
    "old-notes/SKILL.md": "---\nname: old-notes\ndescription: Use when: the user asks about old notes\n---\n\nThis description needs quotes.\n",
}
skills_dir = root / "data" / "skills"
if not skills_dir.exists() and not (root / "data" / "skills.json").exists():  # 用 --data 接着用上次的目录时不再补
    for name, body in SKILLS.items():
        (skills_dir / name).parent.mkdir(parents=True, exist_ok=True)
        (skills_dir / name).write_text(body, encoding="utf-8")
    (root / "data" / "skills.json").write_text(json.dumps({
        "disabled": ["meeting-notes"],
        "sources": {
            "weekly-report": {"kind": "new", "at": 1795200000},
            "pdf-tools": {"kind": "url", "url": "https://github.com/example-org/skills/tree/main/pdf-tools", "label": "github.com/example-org/skills/pdf-tools", "at": 1795200000},
        },
    }), encoding="utf-8")
# 几个编造的自定义 Agent（扩展 → Agent）：内置的示例"资料研究员"、一个能执行命令的、一个写坏了的（列表里标"需要修改"）
AGENTS = {
    "资料研究员.md": agent_examples.find("researcher").text,
    "文件管家.md": "---\nname: 文件管家\ndescription: 看看工作目录里有什么、占了多大，给出整理建议。只读和列目录，不删东西。\ntools: Read, Bash\ncolor: orange\n---\n\n你帮用户整理工作目录。先用 ls 看清楚有什么，再按类型和大小给建议；不要删除、移动任何文件。\n",
    "broken-agent.md": "---\nname: broken-agent\ndescription: 这个文件故意写坏了：颜色不对，正文也是空的。\ncolor: rainbow\n---\n",
}
agents_dir = root / "data" / "agents"
if not agents_dir.exists():  # 用 --data 接着用上次的目录时不再补
    agents_dir.mkdir(parents=True, exist_ok=True)
    for name, body in AGENTS.items():
        (agents_dir / name).write_text(body, encoding="utf-8")
# 假的 Claude Code 的 Agent（HOME 已经换成了临时目录）：一个和上面同名的、一个用 Claude Code 自己的工具名写的
claude_agents = fake_home / ".claude" / "agents"
claude_agents.mkdir(parents=True, exist_ok=True)
(claude_agents / "资料研究员.md").write_text("---\nname: 资料研究员\ndescription: Claude Code 里的研究员。\ntools: WebSearch, WebFetch\n---\n\n只联网查资料。\n", encoding="utf-8")
(claude_agents / "test-runner.md").write_text("---\nname: test-runner\ndescription: Run the test suite and summarize failures. Use after code changes.\ntools: Bash, Read, Grep, Glob\nmodel: sonnet\ncolor: cyan\npermissionMode: default\n---\n\nRun the tests, then list each failure with its file and a one-line cause.\n", encoding="utf-8")
# 假的 Claude Code 的 Skill（HOME 上面已经换成了临时目录，所以"从 Claude Code 导入"走的是真正的代码，读的是这里）：
# 两个 Skill，其中一个和上面的重名，导入时能看到"改名还是替换"
claude_home = fake_home / ".claude" / "skills"
for name, body in {
    "pdf-tools/SKILL.md": "---\nname: pdf-tools\ndescription: Claude Code 里的另一个 PDF 工具。\n---\n\n用 qpdf 合并 PDF。\n",
    "commit-message/SKILL.md": "---\nname: commit-message\ndescription: Write clear git commit messages. Use when the user asks for a commit message.\nargument-hint: [scope]\n---\n\nSummarize the change in one line, then explain why.\n",
}.items():
    (claude_home / name).parent.mkdir(parents=True, exist_ok=True)
    (claude_home / name).write_text(body, encoding="utf-8")

# 假的 Telegram：机器人的 token 是编造的（tests/fake_telegram.py 里的 TOKEN），消息只在这台电脑上转
telegram = FakeTelegram(max_poll=1.0).start(options.telegram_port)
# 别的几家：都是编造的凭据，消息只在这台电脑上转（心跳慢一点，像真的那样）
others = {
    "discord": fake_discord.FakeDiscord(heartbeat_ms=20_000).start(),
    "slack": fake_slack.FakeSlack().start(),
    "feishu": fake_feishu.FakeFeishu().start(),
    "dingtalk": fake_dingtalk.FakeDingTalk().start(),
    "qq": fake_qq.FakeQQ(heartbeat_ms=20_000).start(),
}
phone = Phone(others, {name: str(platforms.label(name)) for name in others}).start(options.phone_port)
# 飞书、钉钉的 SDK：演示里收消息用的是假的长连接，当作装好了（--no-sdk 时当作没装）
platforms.sdk_ready = lambda platform: platforms.sdk(platform) is None or not options.no_sdk
config = Config(
    effort="medium",
    max_tokens=64000,
    max_steps=50,
    workspace=workspace,
    data_dir=root / "data",
    env_file=root / "env-file",  # 在页面上保存的密钥写到这里，不是项目里真正的 .env
    web=True,
    telegram_api=telegram.base_url,  # 通道连的是上面那个假的 Telegram，不是 api.telegram.org
    **{key: value for fake in others.values() for key, value in fake.config().items()},  # 别的几家也连本地的假服务器
)


def client_factory(provider):
    """所有厂商的连接都指向上面两个假服务器。"""
    key = os.environ.get(provider.key_env) or "missing"
    if provider.kind == "anthropic":
        return anthropic.Anthropic(api_key=key, base_url=claude.base_url, max_retries=0)
    return openai.OpenAI(api_key=key, base_url=compat.base_url, max_retries=0)


# 页面每次请求都重新读一遍，改了 index.html 刷新就能看到
send_page = server_module.Handler._send_page


def send_fresh_page(self) -> None:
    folder = Path(server_module.__file__).parent
    server_module.PAGE = (folder / "index.html").read_text(encoding="utf-8")
    server_module.MARKDOWN = (folder / "markdown.js").read_text(encoding="utf-8")
    send_page(self)


server_module.Handler._send_page = send_fresh_page
# 演示里不真的打开文件管理器
server_module.reveal_in_file_manager = lambda path: print(f"（演示：这里本来会在文件管理器里显示 {path}）", flush=True)

ui = WebUI()
fresh = not (root / "data" / "tasks.json").exists()
# 演示里"这台电脑上用哪个后端"永远回答这台假的电脑：真的那个（截你的屏幕、动你的鼠标）连找都不会去找
desktop = FakeDesktop(1280, 800, density=2, app_at=window_or_desktop)
desktop_backend.detect = lambda: (desktop, "")
app = build_app(config, ui, client_factory=client_factory)
# 浏览器的设置是开着的（正式程序里默认关着，要你自己打开）。浏览器的配置文件夹也在临时的数据目录里
app.settings.set_browser({"enabled": True, "headless": not options.browser_window})
# 操作整台电脑也打开，但背后一定是假的电脑：编造的截图，点击、打字只记下来，绝不碰这台电脑真的屏幕和键盘鼠标
install(app.desktop, desktop)
app.settings.set_computer({"enabled": True})
app.refresh_tools()
if fresh:
    # 编造几个任务，"任务"那一栏才有东西可看：有运行记录的、关掉了的、还没关掉的提醒
    now = time.time()
    water = app.tasks.add({"title": "喝水", "kind": "reminder", "prompt": "起来喝杯水，顺便看看窗外", "schedule": {"type": "daily", "time": "09:00"}})
    app.tasks.remind(water["id"], now - 600, new_run(now - 600, "done"))
    plan = app.tasks.add({"title": "每日安排", "kind": "agent", "prompt": "看看我的待办，写一份今天的安排", "schedule": {"type": "weekly", "days": [1, 2, 3, 4, 5], "time": "09:00"}})
    for days_ago, extra in ((2, {"catch_up": True, "missed": 2}), (1, {})):
        started = now - days_ago * 86400
        run = new_run(started, "done", **extra)
        run.update(finished_at=started + 41, cost=0.0123, steps=3, summary="今天有一条待办：周五交报告。上午先写提纲，下午把数据补齐。")
        app.tasks.add_run(plan["id"], run)
    app.tasks.add({"title": "整理下载文件夹", "kind": "agent", "prompt": "把下载文件夹里超过 30 天的文件列出来，按类型分好，先别删", "schedule": {"type": "weekly", "days": [5], "time": "18:00"}, "enabled": False})
    app.tasks.add({"title": "站起来活动一下", "kind": "reminder", "prompt": "", "schedule": {"type": "interval", "minutes": 45}})
server, url = server_module.create_server(app, ui, port=options.port)
for name in ("feishu", "dingtalk"):  # 收消息的长连接换成假的（不用装 SDK）；发消息照常走 HTTP，连的是假服务器
    server.web.channels.factories[name] = others[name].factory
lifecycle.start(server.web)  # 和正式程序一样，定时任务和聊天通道只在网页界面开着的时候运行
print(f"演示已启动，所有回复都是预设的，不花钱。在浏览器里打开：\n\n    {url}\n\n数据在 {root}。按 Ctrl-C 退出。", flush=True)
print(f"\n假的 Telegram：机器人的 token 是\n\n    {TOKEN}\n\n“手机”页面（在另一个标签页里打开）：\n\n    {telegram.base_url}/\n", flush=True)
print("别的几家（都是编造的，在向导里照着填）：", flush=True)
for name, fake in others.items():
    print(f"    {platforms.label(name)}：" + "  ".join(f"{key} = {value}" for key, value in fake.credentials.items()), flush=True)
print(f"\n它们的“手机”页面（上面选平台）：\n\n    {phone.url}\n", flush=True)
try:
    server.serve_forever()
except KeyboardInterrupt:
    pass
finally:
    lifecycle.stop(server.web, 3)  # 停下通道和任务（也会关掉启动过的假 MCP 服务器进程）
    telegram.stop()
    phone.stop()
    for fake in others.values():
        fake.stop()
    docs_server.stop()
    shop.server.shutdown()
