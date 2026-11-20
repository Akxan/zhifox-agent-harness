"""不花钱地试用网页界面：真实的服务器和页面，后面接的是本地的假模型。

    uv run python tests/demo_web.py            # 启动后把打印出来的地址贴进浏览器
    uv run python tests/demo_web.py --no-key   # 模拟"还没有填 API 密钥"的第一次使用

改了页面想看看效果、或者只是想到处点点，用这个脚本，而不是 `uv run harness --web`：

  - 所有回复都是下面写好的，不会向任何厂商的 API 发请求，不花钱。
  - 数据写在临时目录里，不碰项目的 `data/`；密钥也是编造的，不读也不写真正的 `.env`。
    （只设置 HARNESS_DATA_DIR 再运行正式程序是不够的：那样用的还是真正的 `.env` 和真正的 API。）
  - 回复是一小段一小段慢慢发的，能看到逐字输出的效果。测试用的 fake_api.py 是一次发完的，看不出来。
  - 页面每次刷新都重新从磁盘读，改完 index.html 刷新一下就能看到，不用重启。
  - "在访达中显示"和"打开记忆文件夹"只把路径打印出来，不会真的弹出窗口。
  - 全新的数据目录里预先放了三条编造的记忆，信息栏的"记忆"一栏里能看、能删。
  - 定时任务照常运行（调度线程和正式程序里一样）。全新的数据目录里预先放了几个编造的任务：
    一条已经弹出、还没关掉的提醒，一个跑过两次的"每日安排"，一个关掉了的每周任务，一个每 45 分钟的提醒。
    "立即运行"会用掉下面预设回复里的下一条。

预设的回复按顺序是：打招呼（带思考过程和一个链接）、添加待办（一次工具调用）、建文件（要你确认的命令）、
联网搜索、一次出错的工具调用；之后是几条凑数的回复。发什么消息都行，回复只看顺序。
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
parser.add_argument("--no-key", action="store_true", help="不设置任何密钥，看第一次使用时的样子")
options = parser.parse_args()

# 这个脚本绝不能碰到真正的密钥：先把环境变量里的都清掉，再放一个编造的
for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY"):
    os.environ.pop(name, None)
if not options.no_key:
    os.environ["ANTHROPIC_API_KEY"] = "fake-claude-key-0001"

import anthropic
import openai
from fake_api import _events, reply, text, thinking, tool_use
from fake_openai import FakeOpenAI, call, chat

import harness.webui.server as server_module
from harness.app import build_app
from harness.config import Config
from harness.tasks import new_run
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


class SlowClaude:
    """假的 Anthropic API：按顺序回放写好的回复，但是慢慢地发。"""

    def __init__(self, replies: list[dict]):
        self.replies = list(replies)

    def start(self) -> SlowClaude:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers["content-length"]))
                if not api.replies:
                    self.send_error(500, "no scripted reply left")  # 回复用完了：页面上会看到一条出错提示
                    return
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                time.sleep(0.5)  # 模型开口之前先"想"一会儿
                for event in in_small_pieces(_events(api.replies.pop(0))):
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
        *(reply(text("（这是一条预设的回复，用来凑数。）" * 3)) for _ in range(8)),
    ]
).start()
others = FakeOpenAI(
    [
        chat("你好！我是 DeepSeek。", reasoning="用户换了模型，打个招呼。"),
        chat(calls=[call("call_1", "todo_list", "{}")]),
        chat("你有一条待办：交报告，11 月 27 日截止。"),
        *(chat("（预设回复用完了）") for _ in range(8)),
    ],
    models=["deepseek-flash", "deepseek-v4-pro"],
).start()

root = options.data or Path(tempfile.mkdtemp(prefix="harness-demo-"))
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
config = Config(
    effort="medium",
    max_tokens=64000,
    max_steps=50,
    workspace=workspace,
    data_dir=root / "data",
    env_file=root / "env-file",  # 在页面上保存的密钥写到这里，不是项目里真正的 .env
    web=True,
)


def client_factory(provider):
    """所有厂商的连接都指向上面两个假服务器。"""
    key = os.environ.get(provider.key_env) or "missing"
    if provider.kind == "anthropic":
        return anthropic.Anthropic(api_key=key, base_url=claude.base_url, max_retries=0)
    return openai.OpenAI(api_key=key, base_url=others.base_url, max_retries=0)


# 页面每次请求都重新读一遍，改了 index.html 刷新就能看到
send_page = server_module.Handler._send_page


def send_fresh_page(self) -> None:
    server_module.PAGE = (Path(server_module.__file__).parent / "index.html").read_text(encoding="utf-8")
    send_page(self)


server_module.Handler._send_page = send_fresh_page
# 演示里不真的打开文件管理器
server_module.reveal_in_file_manager = lambda path: print(f"（演示：这里本来会在文件管理器里显示 {path}）", flush=True)

ui = WebUI()
fresh = not (root / "data" / "tasks.json").exists()
app = build_app(config, ui, client_factory=client_factory)
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
server.web.tasks.start()  # 和正式程序一样，定时任务只在网页界面开着的时候运行
print(f"演示已启动，所有回复都是预设的，不花钱。在浏览器里打开：\n\n    {url}\n\n数据在 {root}。按 Ctrl-C 退出。", flush=True)
try:
    server.serve_forever()
except KeyboardInterrupt:
    pass
