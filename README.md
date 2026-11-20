# Harness 个人助手

一个运行在终端里的个人助手，同时也是一个用来学习的项目：它没有用任何 agent 框架，
让模型变成"助手"的那层代码（harness）全部是手写的，每一部分都能在一个文件里看到。

助手能做四件事：管理待办、跨会话记住事情、读写文件和执行命令、联网搜索。

## 快速开始

需要 macOS 或 Linux，以及 [uv](https://docs.astral.sh/uv/)。

```bash
# 1. 安装依赖
uv sync

# 2. 配置 API key
cp .env.example .env
#    然后打开 .env，把 key 填在 ANTHROPIC_API_KEY= 后面

# 3. 启动
uv run harness
```

API key 在 [platform.claude.com](https://platform.claude.com) 的 API Keys 页面创建。
API 按用量计费，和 Claude 的订阅是分开的，需要先在控制台充值。

启动后直接打字就可以对话：

```
你> 提醒我下周五交报告
🔧 todo_add {"title": "交报告", "due": "2026-11-27"}
   ↳ {"id": 1, "title": "交报告", "due": "2026-11-27", ...}
记下了：下周五（11 月 27 日）交报告。

你> 这个目录里有哪些 Python 文件？
🔧 run_command $ find . -name "*.py"

⚠️  助手想要执行命令：

    $ find . -name "*.py"
允许吗？y 允许；回车拒绝；也可以直接输入你的意见来拒绝 >
```

| 命令 | 作用 |
|---|---|
| `/help` | 显示命令列表 |
| `/new` | 开始一段新对话（记忆和待办不受影响） |
| `/todos` | 查看未完成的待办 |
| `/tools` | 查看模型可以使用的工具 |
| `/usage` | 查看本次运行用了多少 token |
| `/exit` | 退出（也可以按 Ctrl-D） |

模型回复到一半时按 Ctrl-C 可以打断它。

助手的"工作目录"是你启动它时所在的目录。想让它在别的目录里工作，先 `cd` 到那里，再运行：

```bash
uv run --project "/这个项目的完整路径" harness
```

## Harness 是什么

模型只会一件事：读入一段文字，写出下一段文字。它不记得上一次对话，也执行不了任何操作。

**Harness** 就是包在模型外面、弥补这些不足的代码。**Agent = 模型 + harness**。
这个项目里 harness 的每个组成部分各占一个文件：

| 组成部分 | 文件 | 它解决什么问题 |
|---|---|---|
| 循环 | [harness/agent.py](harness/agent.py) | 模型一次只能回复一段。循环反复调用它，直到任务做完 |
| 工具 | [harness/tools/](harness/tools/) | 模型不能执行操作。工具替它执行，再把结果告诉它 |
| 对话历史 | [harness/agent.py](harness/agent.py) | 模型没有记忆。每次请求都把完整的对话重新发一遍 |
| 长期记忆 | [harness/tools/memory.py](harness/tools/memory.py) | 程序关掉后对话就没了。记忆存在磁盘上，下次还在 |
| 系统提示词 | [harness/prompt.py](harness/prompt.py) | 模型不知道自己是谁、在什么环境里 |
| 权限 | [harness/permissions.py](harness/permissions.py) | 模型可能出错或被误导。危险操作要你点头 |
| 界面 | [harness/ui.py](harness/ui.py) | 把过程显示给你，把你的输入交给循环 |
| 组装 | [harness/cli.py](harness/cli.py)、[harness/config.py](harness/config.py) | 把上面这些拼起来，读取配置 |

建议的阅读顺序：`agent.py` → `tools/base.py` → `tools/todo.py` → `permissions.py`，其余的按兴趣看。

### 循环

```
你说了一句话
  └─► 把【系统提示词 + 工具说明书 + 全部对话历史】发给模型
        ├─ 模型回复文字            → 显示给你，这一轮结束
        └─ 模型说"我要调用工具 X"  → 检查权限 → 执行 → 把结果追加进历史 → 再发给模型（循环）
```

"下一步做什么"每次都由模型决定，循环只负责照办。这是 agent 和普通程序的区别：
普通程序的步骤是写死的，agent 的步骤是模型临场决定的。

你说"提醒我下周五交报告"之后，对话历史会变成下面这样（有删减）。
模型并没有真的"调用"什么，它只是输出了一段格式固定的内容，说自己想调用 `todo_add`；
harness 看到后去执行，再把结果作为一条新消息放回历史里：

```json
[
  {"role": "user", "content": [
    {"type": "text", "text": "<context>当前时间：2026-11-20 14:03 星期五</context>"},
    {"type": "text", "text": "提醒我下周五交报告"}
  ]},
  {"role": "assistant", "content": [
    {"type": "thinking", "thinking": "下周五是 11 月 27 日……", "signature": "..."},
    {"type": "tool_use", "id": "toolu_1", "name": "todo_add",
     "input": {"title": "交报告", "due": "2026-11-27"}}
  ]},
  {"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "{\"id\": 1, \"title\": \"交报告\", ...}"}
  ]},
  {"role": "assistant", "content": [
    {"type": "text", "text": "记下了：下周五（11 月 27 日）交报告。"}
  ]}
]
```

有一条规矩贯穿整个 `agent.py`：**历史只在末尾追加，前面的内容从不修改**。
原因写在 `Agent.__init__` 的注释里，简单说就是改了前面会让缓存失效（变贵），
还会让模型之前的思考内容无法通过 API 的校验（请求被拒绝）。

### 三类工具

区别在于"说明书谁写"和"谁来执行"：

| 类别 | 说明书 | 执行 | 这个项目里的例子 |
|---|---|---|---|
| 自定义工具 | 我们写 | 我们的电脑 | 待办（[todo.py](harness/tools/todo.py)）、执行命令（[shell.py](harness/tools/shell.py)） |
| Anthropic 定义的工具 | Anthropic | 我们的电脑 | 读写文件（[files.py](harness/tools/files.py)）、记忆（[memory.py](harness/tools/memory.py)） |
| 服务器工具 | Anthropic | Anthropic 的服务器 | 联网搜索（[web.py](harness/tools/web.py)） |

### 权限规则

| 操作 | 处理 |
|---|---|
| 待办、记忆 | 直接执行（只动 `data/` 里助手自己的文件） |
| 读取工作目录里的文件 | 直接执行 |
| 读取工作目录之外的文件 | 先问你 |
| 创建、修改任何文件 | 先问你，并显示要写入的内容 |
| 执行任何 shell 命令 | 先问你，并显示完整的命令 |
| 联网搜索 | 直接执行（在 Anthropic 的服务器上进行，不碰你的电脑） |

拒绝时可以直接输入一句话（比如"不要删，改成移到废纸篓"），这句话会转告给模型。

另外有两条不经过询问的硬性限制：助手不能读写 `.env`（里面是你的 API key），
执行命令时也看不到 API key 这个环境变量。

需要了解的风险：确认提示是你和模型之间唯一的关卡。批准一条命令之前请看清楚它要做什么，
尤其是在助手刚读过网页或陌生文件之后，因为那些内容里可能藏着误导模型的话。

## 数据存在哪里

所有数据都在项目的 `data/` 目录里，都是可以直接打开的普通文件：

| 路径 | 内容 |
|---|---|
| `data/memories/` | 助手的长期记忆和笔记。可以手动编辑或删除 |
| `data/todos.json` | 待办事项 |
| `data/backups/` | 助手整个覆盖某个文件之前，旧版本的备份 |
| `data/input_history` | 你输入过的话（按上方向键可以找回） |

`data/` 和 `.env` 都写进了 `.gitignore`，不会被提交到 git。

对话内容会发送给 Anthropic 的 API 处理。对话本身不保存到磁盘，关掉程序就没有了；
跨会话保留的只有记忆和待办。

## 动手试试：加一个自己的工具

新建 `harness/tools/dice.py`：

```python
import random

from .base import Tool, function_tool


def make_dice_tool() -> Tool:
    def roll_dice(sides: int = 6) -> str:
        return f"掷出了 {random.randint(1, sides)} 点"

    return function_tool(
        name="roll_dice",
        description="掷一次骰子，返回点数。用户想要随机数、抛硬币或抽签时使用。",
        properties={"sides": {"type": "integer", "description": "骰子有几个面，默认 6"}},
        required=[],
        function=roll_dice,
    )
```

然后在 [harness/tools/\_\_init\_\_.py](harness/tools/__init__.py) 里导入它，并在 `build_registry` 的列表中加一行 `make_dice_tool(),`。
重新启动，对助手说"帮我掷个骰子"。

几点说明：

- `description` 是模型决定要不要调用这个工具的唯一依据。除了写它做什么，也要写**什么时候该用**。
- 工具返回的是给模型看的文字。出错时抛出 `ToolError("原因")`，模型会看到原因并自己调整。
- 如果新工具会改动电脑上的东西，在 [harness/permissions.py](harness/permissions.py) 的 `review` 里给它加一条规则，让它先问你。

## 配置

写在 `.env` 里，都是可选的：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HARNESS_MODEL` | `claude-opus-5-5` | 使用的模型。`claude-sonnet-5-5` 更便宜、更快 |
| `HARNESS_EFFORT` | `medium` | 模型思考的力度：`low` / `medium` / `high` / `xhigh` / `max` |
| `HARNESS_WEB` | `1` | 设为 `0` 关闭联网搜索 |

代码是按上面两个模型写的。换成其他模型时，个别参数（比如联网工具的版本）可能需要调整。

### 关于费用

API 按 token 计费（一个汉字大约 1 到 2 个 token）。写这份文档时的价格，以官网为准：

| 模型 | 输入（每百万 token） | 输出（每百万 token） |
|---|---|---|
| `claude-opus-5-5` | $4 | $20 |
| `claude-sonnet-5-5` | $2 | $10 |

因为每次请求都要重发整个对话历史，对话越长，每一步越贵。有两个机制在控制这一点，
都已经开启：重复发送的部分会命中缓存，价格低得多；对话特别长时，较早的内容会被自动压缩成摘要。
联网搜索按次数另外计费。随时可以用 `/usage` 查看用量，用 `/new` 开始新对话。

## 测试

```bash
uv run pytest
```

测试不需要 API key，也不花钱。[tests/fake_api.py](tests/fake_api.py) 是一个本地的假 API，
它按真实 API 的格式回放事先写好的回复，这样就能让真正的 SDK 把整个循环跑一遍，
并检查发出去的请求是否正确。想知道循环在各种情况下的行为（工具出错、用户拒绝、
被 Ctrl-C 打断、模型拒绝回答），可以读 [tests/test_agent.py](tests/test_agent.py)。

## 局限

- 只支持 macOS 和 Linux（执行命令的部分依赖 bash）。
- 每条命令都在全新的 shell 里运行，`cd` 和环境变量不会保留到下一条命令。
- 关掉程序后对话不保留。
- "拒绝后自动换模型重试"和"自动压缩历史"用的是 API 的 beta 功能。如果启动后每个请求都报 400，
  可以在 [harness/config.py](harness/config.py) 里把它们关掉。

## 可以接着做的方向

- 把对话保存到磁盘，下次启动时恢复。
- 给常用的只读命令（`ls`、`git status`）加一个"本次会话不再询问"的选项。
- 通过 MCP 接入日历、邮件等外部服务。
- 把终端界面换成网页：只需要重写 `ui.py`，循环不用动。
