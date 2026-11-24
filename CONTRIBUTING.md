# Contributing to ZhiFox

**English** | [简体中文](#简体中文)

Thanks for helping. ZhiFox (智狐) is a personal assistant that runs on your own computer, and it is also a teaching
project: an agent harness written by hand, so that anyone can read how a model becomes an agent. That goal shapes how
the code is written:

- **No agent framework.** The loop is one file, [harness/agent.py](harness/agent.py); providers, tools, permissions and
  storage are plain Python on top of the official `anthropic` and `openai` SDKs. Please don't add a framework, or a
  large dependency to save a few lines: the project has three runtime dependencies on purpose.
- **One file, one job.** A new feature goes into a new module instead of a file that is already big. In the web page, a
  new part gets its own file under `harness/webui/page/styles/` or `page/scripts/`, listed in
  `harness/webui/page/__init__.py`.
- **Comments explain why**: why it's done this way, what would break otherwise, and where a deliberate exception ends.
  Comments, docstrings and commit messages in this repository are in Chinese. If that isn't comfortable for you,
  write them in English and the maintainer will translate them when merging.

The complete rulebook is [CLAUDE.md](CLAUDE.md). It is written in Chinese, for the AI coding assistant the maintainer
works with, and it is the most detailed description of the project's rules and invariants. This page sums up what
matters for a contribution.

## Before you start

For anything bigger than a small fix, please open a [feature request](https://github.com/Akxan/zhifox-agent-harness/issues/new?template=feature_request.yml)
first, so we can agree on the approach before you write code. Typos, documentation and a clear bug with a test can go
straight to a pull request.

## Set up

You need [uv](https://docs.astral.sh/uv/) (it installs Python 3.11 or later for you) and Git. Chrome is needed only for
the page tests, and Node.js only for the Markdown renderer's tests and the syntax check of the page scripts; without
them those tests are skipped. Fork the repository if you'll send a pull request, then clone your fork:

```bash
# macOS and Linux (zsh, bash)
curl -LsSf https://astral.sh/uv/install.sh | sh     # install uv, then open a new terminal
git clone https://github.com/<you>/zhifox-agent-harness.git
cd zhifox-agent-harness
uv sync
```

```powershell
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # install uv, then open a new PowerShell window
git clone https://github.com/<you>/zhifox-agent-harness.git
cd zhifox-agent-harness
uv sync
```

In a clone, data lives in `data/` and keys in `.env`, both ignored by git. `uv run harness --web` starts the real web
UI, and sending a message there calls the model's API and costs money.

## Try it for free

```bash
uv run python tests/demo_web.py            # the web UI with a fake model, a temporary data folder and made-up keys
uv run python tests/demo_web.py --no-key   # the same, as it looks before any key is set
```

The demo never touches your `.env` or `data/`, and its replies are scripted, so it costs nothing. It also runs fake chat
platforms, fake MCP servers and a fake desktop. Setting `HARNESS_DATA_DIR` alone and running `uv run harness` is **not**
the same: that still reads your real `.env` and calls the real API.

## Tests, lint and types

```bash
uv run pytest -q                                   # everything, in parallel: about a minute, no API key, costs nothing
uv run pytest -q -n 0                              # one test at a time (debugging, print output)
uv run pytest -q tests/test_agent.py               # one file
uv run pytest -m ui                                # only the page tests: headless Chrome driving tests/demo_web.py
uvx ruff@0.17.6 check harness tests                # lint, with the version CI uses
uv run --with mypy==1.19.0 mypy --platform linux   # types; run it for darwin and win32 too
```

These work the same in zsh, bash and PowerShell. CI runs the tests on Ubuntu, macOS and Windows with Python 3.11 and
3.13 for every pull request, and mypy for all three platforms. If you have only one system, say so in the pull request;
CI covers the others. For a different Python version: `uv run --python 3.13 --isolated pytest -q`. More about how the
tests work: [Testing](docs/en/testing.md).

## Rules a change must keep

- **Tests come with every change.** A feature has tests; a bug fix starts with a test that fails without the fix. Tests
  run the real SDKs against local fake servers ([tests/fake_api.py](tests/fake_api.py),
  [tests/fake_openai.py](tests/fake_openai.py), fake chat platforms, [tests/fake_mcp.py](tests/fake_mcp.py)): **nothing in
  a test may call a real paid API** or a real bot, run the real `launchctl` or `systemctl`, or touch the real screen,
  mouse and keyboard. Tests run in parallel, so each one prepares its own temporary folders (`tmp_path`) and ports (0).
  Wait for a condition instead of sleeping: CI's macOS and Windows machines are much slower than a laptop.
- **Three interface languages.** Every sentence the user sees is written in Chinese inside `t()`
  ([harness/i18n.py](harness/i18n.py) in the program, `harness/webui/page/scripts/i18n.js` in the page), with its
  translation added to `harness/locales/en.json` and `es.json`; `tests/test_i18n.py` finds the missing ones. Switching
  the language translates the page in place, without reloading. What the model reads (the system prompt, tool
  descriptions, tool results) stays in Chinese and is not translated.
- **Both languages of the docs.** The READMEs are an overview; the details are in topic pages, the same set in
  [docs/en/](docs/en/) (English) and [docs/zh-CN/](docs/zh-CN/) (Chinese), with the same file names. If behavior
  changes, update the matching page in both folders together (and [README.md](README.md) and
  [README.zh-CN.md](README.zh-CN.md) if the overview changes); they say the same thing. `tests/test_docs.py` checks
  that every relative link and `#anchor` resolves and that both folders have the same pages.
- **Three operating systems.** Think about macOS, Windows and Linux for each change: ⌘ or Ctrl, "Show in Finder", "File
  Explorer" or "file manager", zsh/bash and PowerShell versions of every command in the docs. Code that differs by
  system lives in a few small modules (`filelock`, `pathnames`, `winacl`, `tools/shell`, `webui/reveal` and the others
  listed in CLAUDE.md); reuse them instead of checking `sys.platform` yourself. If something can't work on one system,
  the interface says so and the test is skipped there with the reason.
- **Asking first.** A tool that changes the user's computer goes through `permissions.review`. The looser approval
  modes only decide when not to ask; they never open anything else, and `.env` is off-limits in every mode.
- **The web UI's security boundaries stay as they are.** It listens on `127.0.0.1` only; every request checks the
  token, Host and Origin; the page loads nothing from outside (no CDN fonts, scripts or images); text from the model or a
  tool goes in with `textContent`, never parsed as HTML; API keys never reach the browser; the page sends ids or fixed
  words, never paths, and the server works out the path. See the security model in [SECURITY.md](SECURITY.md).
- **The web page** has light and dark themes, follows the reduced-motion setting (`<html data-motion>`), works 390 px
  wide with no text spilling out of its box, and animates only `transform` and `opacity`.
- **Conversation history is only appended to**, because prompt caching and Claude's thinking signatures depend on it.
  The few deliberate exceptions, and where they end, are written down in CLAUDE.md.
- **No secrets, no private data.** Never commit `.env` or anything in `data/` (both are git-ignored; look at `git status`
  before you commit). Use made-up keys in tests, screenshots and the browser, and never paste a real key into an issue,
  a pull request or a log.

## Pull requests

Branch from `main` and keep a pull request to one change. Fill in the checklist in the template. Write commit messages
that say what changed and why. CI must pass on all three systems before a merge.

## Reporting bugs and ideas

Use the [bug report form](https://github.com/Akxan/zhifox-agent-harness/issues/new?template=bug_report.yml). It asks for
`harness --version` and the output of `harness doctor --json`; read that output before posting: it holds no keys, but
it does show paths on your computer. Ideas go in a
[feature request](https://github.com/Akxan/zhifox-agent-harness/issues/new?template=feature_request.yml).

## Security problems

**Never in a public issue.** Report them privately, as [SECURITY.md](SECURITY.md) explains; it also lists what counts as
a vulnerability and what doesn't.

## License

ZhiFox is under the [MIT License](LICENSE). By contributing, you agree that your contribution is licensed under the
same MIT License, with no other agreement to sign.

---

## 简体中文

谢谢你来帮忙。智狐（ZhiFox）是运行在你自己电脑上的个人助手，同时也是一个教学项目：一个手写的 agent harness，让谁都能读懂模型是怎么变成
agent 的。这个目标决定了代码的写法：

- **不用 agent 框架。** 循环只有一个文件 [harness/agent.py](harness/agent.py)；各家模型的适配、工具、权限、存储，都是在官方的
  `anthropic`、`openai` SDK 之上写的普通 Python。请不要引入框架，也不要为了省几行代码加一个大的依赖：项目只依赖三个包，这是有意的。
- **一个文件只讲一件事。** 新功能放进新的模块，不往已经很大的文件里堆。页面里新的一块放进 `harness/webui/page/styles/` 或
  `page/scripts/` 里的新文件，并加进 `harness/webui/page/__init__.py` 的清单。
- **注释写"为什么"**：为什么这样做、不这样会坏在哪儿、有意的破例到哪里为止。仓库里的注释、docstring 和提交说明都用中文；
  不方便写中文的话用英文写，维护者合并时会翻译。

完整的规矩在 [CLAUDE.md](CLAUDE.md) 里。它是中文的，写给维护者用的 AI 编程助手看，是项目规矩和不变式最详细的说明。这一页只总结提交改动时要紧的部分。

### 开始之前

比小修小补大的改动，请先开一个[功能建议](https://github.com/Akxan/zhifox-agent-harness/issues/new?template=feature_request.yml)，
先商量好做法再写代码。错别字、文档、带着测试的明确的 bug 修复，可以直接提 pull request。

### 准备环境

需要 [uv](https://docs.astral.sh/uv/)（它会替你装好 Python 3.11 或更新的版本）和 Git。只有页面测试要 Chrome，只有 Markdown 渲染的测试和
页面脚本的语法检查要 Node.js；没有它们时这几项测试会跳过。要提 pull request 的话先 fork，再 clone 你的 fork：

```bash
# macOS 和 Linux（zsh、bash）
curl -LsSf https://astral.sh/uv/install.sh | sh     # 装 uv，装好后另开一个终端
git clone https://github.com/<你的用户名>/zhifox-agent-harness.git
cd zhifox-agent-harness
uv sync
```

```powershell
# Windows（PowerShell）
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # 装 uv，装好后另开一个 PowerShell 窗口
git clone https://github.com/<你的用户名>/zhifox-agent-harness.git
cd zhifox-agent-harness
uv sync
```

从仓库里运行时，数据在 `data/`，密钥在 `.env`，git 都不会提交它们。`uv run harness --web` 启动的是真正的网页界面，在那里发消息会调用模型的 API、要花钱。

### 不花钱地试一试

```bash
uv run python tests/demo_web.py            # 网页界面，用假模型、临时的数据目录和编造的密钥
uv run python tests/demo_web.py --no-key   # 同上，看还没填密钥时的样子
```

演示不碰你的 `.env` 和 `data/`，回复是写好的剧本，一分钱都不花；聊天平台、MCP 服务器和电脑也都是假的。只设置 `HARNESS_DATA_DIR`
再运行 `uv run harness` **不是**一回事：那样读的仍然是你真正的 `.env`，调用的是真的 API。

### 测试、代码检查和类型检查

```bash
uv run pytest -q                                   # 全部测试，并行跑：一分钟左右，不需要 API 密钥，不花钱
uv run pytest -q -n 0                              # 一个一个跑（调试、看 print 的输出）
uv run pytest -q tests/test_agent.py               # 只跑一个文件
uv run pytest -m ui                                # 只跑页面测试：无界面的 Chrome 实际操作 tests/demo_web.py
uvx ruff@0.17.6 check harness tests                # 代码检查，和 CI 用同一个版本
uv run --with mypy==1.19.0 mypy --platform linux   # 类型检查；darwin、win32 也各跑一遍
```

这几条在 zsh、bash 和 PowerShell 里写法一样。每个 pull request 都由 CI 在 Ubuntu、macOS、Windows 上用 Python 3.11 和 3.13 跑测试
（页面测试单独一个 job，只用 3.13，macOS、Windows 上分两台机器跑，见 [测试](docs/zh-CN/testing.md#ci)），mypy 三个系统都查。手边只有一个系统的话，在 pull request 里写明，别的交给 CI。换一个 Python 版本：`uv run --python 3.13 --isolated pytest -q`。
测试是怎么做的，详见 [测试](docs/zh-CN/testing.md)。

### 改动必须守住的几条

- **每个改动都带测试。** 新功能要有测试；修 bug 先写一个没修时会失败的测试。测试用真实的 SDK 连本地的假服务器
  （[tests/fake_api.py](tests/fake_api.py)、[tests/fake_openai.py](tests/fake_openai.py)、各个聊天平台的假服务器、
  [tests/fake_mcp.py](tests/fake_mcp.py)）：**测试里不许调用任何真的收费 API**，也不许连真的机器人、运行真的 `launchctl`、`systemctl`，
  或者碰真的屏幕、鼠标和键盘。测试是并行跑的，每个测试自己准备临时目录（`tmp_path`）和端口（给 0）。等条件成立，不要写固定的 sleep：
  CI 上的 macOS 和 Windows 比笔记本慢得多。
- **界面有三种语言。** 用户看到的每一句话都用中文写在 `t()` 里（程序里是 [harness/i18n.py](harness/i18n.py)，页面里是
  `harness/webui/page/scripts/i18n.js`），再在 `harness/locales/en.json` 和 `es.json` 里加上译文；`tests/test_i18n.py` 会找出漏掉的。
  换语言时页面原地翻译，不刷新。写给模型看的（系统提示词、工具说明、工具结果）保持中文，不翻译。
- **中英两份文档一起改。** 两份 README 只是概览，详细的说明按主题分在 [docs/en/](docs/en/)（英文）和 [docs/zh-CN/](docs/zh-CN/)（中文）里，
  两边是同样的几页、文件名一样。行为变了，两个文件夹里对应的那一页一起更新（概览变了的话还有 [README.md](README.md) 和
  [README.zh-CN.md](README.zh-CN.md)），内容一致。`tests/test_docs.py` 会查每个相对链接和 `#锚点` 都找得到、两边的页面一样。
- **三个系统都要想到。** 每个改动都想一想 macOS、Windows、Linux 上各是什么样：⌘ 还是 Ctrl，"在访达中显示""在文件资源管理器中显示"
  还是"在文件管理器中显示"，文档里的每条命令都给 zsh/bash 和 PowerShell 两种写法。和系统有关的代码只放在几个小模块里（`filelock`、
  `pathnames`、`winacl`、`tools/shell`、`webui/reveal`，以及 CLAUDE.md 里列出的其他几个），复用它们，不要自己判断 `sys.platform`。
  某个系统上做不到的，界面上明说，测试里按系统跳过并写明原因。
- **先问用户。** 会改动用户电脑的工具必须经过 `permissions.review`。宽松的几档只决定什么时候可以不问，不会放开别的；`.env` 在任何一档都碰不了。
- **网页界面的安全边界不能削弱。** 只监听 `127.0.0.1`；每个请求都检查口令、Host 和 Origin；页面不加载任何外部资源（没有 CDN 的字体、脚本、图片）；
  模型和工具输出的文字用 `textContent` 放进页面，从不当作 HTML 解析；API 密钥不进浏览器；页面只给编号或固定的几个词，从不给路径，路径由服务器算。
  安全模型见 [SECURITY.md](SECURITY.md)。
- **网页**要有浅色和深色两套外观，遵守"减少动态效果"的设置（`<html data-motion>`），390 像素宽时文字也不冲出容器，动画只改 `transform` 和 `opacity`。
- **对话历史只在末尾追加**，因为提示词缓存和 Claude 的思考签名都靠它。少数有意的破例和它们的边界写在 CLAUDE.md 里。
- **不放密钥，不放私人数据。** 永远不要提交 `.env` 和 `data/` 里的任何东西（git 本来就忽略它们；提交前看一眼 `git status`）。测试、截图和浏览器里只用编造的密钥，
  issue、pull request 和日志里永远不要贴真的密钥。

### Pull request

从 `main` 拉分支，一个 pull request 只做一件事，填好模板里的检查清单。提交说明写清改了什么、为什么。合并之前 CI 要在三个系统上都通过。

### 报告 bug、提建议

用 [bug 报告表单](https://github.com/Akxan/zhifox-agent-harness/issues/new?template=bug_report.yml)。它会要 `harness --version` 和
`harness doctor --json` 的输出；贴之前先看一遍：里面没有密钥，但有你电脑上的路径。想法请写成[功能建议](https://github.com/Akxan/zhifox-agent-harness/issues/new?template=feature_request.yml)。

### 安全问题

**永远不要写在公开的 issue 里。** 照 [SECURITY.md](SECURITY.md) 私下报告；那里也写了哪些算漏洞、哪些不算。

### 许可证

智狐使用 [MIT 许可证](LICENSE)。提交贡献即表示你同意你的贡献也以同样的 MIT 许可证发布，不需要另外签任何协议。
