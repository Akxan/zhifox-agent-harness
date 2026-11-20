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
  - 定时任务照常运行（调度线程和正式程序里一样）。全新的数据目录里预先放了几个编造的任务：
    一条已经弹出、还没关掉的提醒，一个跑过两次的"每日安排"，一个关掉了的每周任务，一个每 45 分钟的提醒。
    "立即运行"会用掉下面预设回复里的下一条。

预设的回复按顺序是：打招呼（带思考过程和一个链接）、添加待办（一次工具调用）、建文件（要你确认的命令）、
联网搜索、一次出错的工具调用、一段用齐了各种 Markdown 写法的长回复（标题、列表、表格、代码块……）；
之后是几条凑数的回复。发什么消息都行，回复只看顺序。
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
from fake_mcp import FakeMcpHttp
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
# "从 Claude 导入"会去读 HOME 下面 Claude Desktop 和 Claude Code 的配置。演示里换成临时目录里编造的那几份，
# 绝不读你电脑上真正的配置（那里面有你的密钥）
fake_home = root / "home"
os.environ["HOME"] = str(fake_home)
# 一个走 HTTP 的假 MCP 服务器，就在这个进程里跑。它的端口每次都不一样，所以配置里写 ${DEMO_MCP_URL}，启动时再换成真正的地址
docs_server = FakeMcpHttp("modern", sse=True, token="demo-header-token-9876wxyz").start()
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
# 假的 Claude Code 的 Skill（HOME 上面已经换成了临时目录，所以"从 Claude Code 导入"走的是真正的代码，读的是这里）：
# 两个 Skill，其中一个和上面的重名，导入时能看到"改名还是替换"
claude_home = fake_home / ".claude" / "skills"
for name, body in {
    "pdf-tools/SKILL.md": "---\nname: pdf-tools\ndescription: Claude Code 里的另一个 PDF 工具。\n---\n\n用 qpdf 合并 PDF。\n",
    "commit-message/SKILL.md": "---\nname: commit-message\ndescription: Write clear git commit messages. Use when the user asks for a commit message.\nargument-hint: [scope]\n---\n\nSummarize the change in one line, then explain why.\n",
}.items():
    (claude_home / name).parent.mkdir(parents=True, exist_ok=True)
    (claude_home / name).write_text(body, encoding="utf-8")

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
    folder = Path(server_module.__file__).parent
    server_module.PAGE = (folder / "index.html").read_text(encoding="utf-8")
    server_module.MARKDOWN = (folder / "markdown.js").read_text(encoding="utf-8")
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
finally:
    app.close()  # 关掉启动过的假 MCP 服务器进程
    docs_server.stop()
