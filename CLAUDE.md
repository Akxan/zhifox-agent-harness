# CLAUDE.md

这个文件写给在本仓库里工作的 Claude Code，每次会话开始时会自动读取。请按这里的规则做事。

## 用户定下的规则

1. **用中文交流。** 回复、代码注释、文档、提交说明都用中文。用户在通过这个项目学习 agent 和 harness，
   解释概念时要说清楚"为什么"，不要只给结论。
   例外是 GitHub 上给外人看的部分（用户原话："github项目描述用英文来写吧，英文为主，中文为辅"）：
   `README.md` 是英文的项目主页，`README.zh-CN.md` 是完整的中文文档，两份内容要一致；
   仓库的描述（About）和标签也用英文。许可证是 MIT（用户定的）。
2. **每次改动都提交并推送。** 改完并通过测试后，直接在 `main` 上提交，推送到 `origin`，然后告诉用户提交编号。
   这是长期有效的规则，不需要每次再问。只做了排查、没有改文件时不用提交。
3. **花钱之前先问。** 任何会消耗 API 额度的操作（向真实的模型 API 发消息、触发联网搜索等），
   先说明预计花多少钱，得到同意再做。上一次的同意不能沿用到下一次。
   优先用不花钱的办法验证：本地的假服务器（`tests/fake_api.py`、`tests/fake_openai.py`），
   以及 Anthropic 不计费的接口 `models.retrieve` 和 `messages.count_tokens`。
4. **并行、尽快。**（用户原话："高并发，快速做"。）互不依赖的工作拆开并行做，比如让并行的子代理写测试，
   自己同时写功能。几步就能做完的事直接做，不要为了并行而并行。
5. **按产品的标准做。**（用户原话："能做的专业点最好"。）界面和交互要做完整：空状态、出错提示、
   需要确认的步骤、深色模式都要考虑到。功能做完要有测试，两份 README 都要同步更新。
6. **网页界面要精致，动效要流畅，但不能太吃性能。**（用户原话："网页的动画要流畅，交互、UI、UX 要高级、
   精美，可以参考 https://www.awwwards.com/ ，但是不要太吃性能！大胆点，创新，现代，精致。"）
   - 设计上向 awwwards 上获奖网站的水准看齐：大胆的排版、克制统一的配色、有辨识度的视觉、细腻的微交互。
     可以大胆创新，不要做成千篇一律的后台模板；但这是一个每天要用的工具，好看不能妨碍好用。
   - 动画只改 `transform` 和 `opacity`，让浏览器在合成线程上完成；需要改布局的过渡要短、范围要小。
   - 不在大面积的滚动区域上用 `backdrop-filter` 这类昂贵的效果；不写靠 JavaScript 逐帧驱动的动画。
   - 重放历史（刷新页面、打开旧会话）时不逐条播放入场动画，只有实时发生的内容才播放。
   - 必须尊重系统的"减少动态效果"设置（`prefers-reduced-motion`）。默认跟随系统，用户也可以在外观面板里
     改成"减少"或"完整"（用户原话："减少动态效果这个设置好像没有入口"）。要不要播动画一律看 `<html data-motion>`
     （样式里是 `:root[data-motion="reduce"]`，脚本里是 `calm()`），不要再直接查 `prefers-reduced-motion`，
     否则用户的选择会失效。新加的动画用 `play()` 播，或者写在 CSS 里，都会自动遵守这条。
   - 不引入外部的字体、图片、脚本或样式：页面的安全策略不允许，离线时也要能用。图标用内嵌的 SVG 线条画。
   - 浅色和深色两套外观都要做，都要检查对比度。**默认是浅色**（用户原话："我想默认是白色"），
     不跟随系统；深色和"跟随系统"由用户自己在右上角的外观面板里切换。
   - **界面的细节规矩**（下面每一条都是用户指出过的问题，改界面时逐条对照）：
     - **每一样存在本地的东西，交互都一样**：会话、记忆、待办、定时任务、Skill、MCP 服务器……每一行悬停时右边是
       "在访达中显示 / 编辑 / 删除"三个按钮（同样的图标、顺序和位置，触屏上一直显示），删除先在原地确认。
       别的操作（立即运行、启用开关）放在这三个前面，或者放进详情里。（用户原话："所有在本地交互的文件，内容，都要有这个"）
     - **控件里的东西要对称**：下拉框一律用 `select.field`，用页面自己画的箭头（离右边框和文字离左边框一样远、
       颜色跟着主题），不用浏览器自带的箭头；新的输入框、按钮沿用 `.field`、`.button` 的高度、圆角和留白。
       （用户原话："箭头在内容框里太偏右了"）
     - **文字永远不能冲出容器**：单行的用 `white-space: nowrap; overflow: hidden; text-overflow: ellipsis`，
       并且它和它的上层 flex 子项都要 `min-width: 0`；网格的列写成 `minmax(0, 1fr)`（默认会被最长的内容撑开）；
       很长的路径、命令用 `overflow-wrap: anywhere`。三种语言都要看（英文、西班牙语通常更长），窄屏也要看。
     - **悬停出现的按钮不能挤动文字**：要么一直给按钮留好位置，要么让按钮浮在文字上面（左边一段渐隐的底色），
       不要悬停时才加大内边距，那样整行会重新排版、跳一下。按钮后面垫的渐变只是底，不接点击（`pointer-events` 只给按钮），
       否则那一截就成了点了没反应的地方（多媒体的方格曾经因此点中间打不开）。
     - **类名带上前缀**（`task-`、`skill-`、`mcp-`……），不要用 `reminder`、`agent`、`file` 这种通用的名字：
       撞上别处的样式就会画错（"只提醒"的任务曾经被画成一张紫色的提醒卡片，文字冲出了边框）。
     - **能编辑的窗口，关上之前先问**：有没保存的修改时，Esc、×、点外面、"取消"都先提醒一次，再来一次才放弃。
       会被藏起来的元素（切换标签、换选项）正拿着焦点时，先把焦点挪到看得见的地方，否则 Esc 会绕过 keydown
       直接关掉窗口；同时监听 `<dialog>` 的 `cancel` 事件做保险。
     - **窗口里的提示写在窗口里**：浮在页面上的提示（toast）会被窗口的暗底挡住。
     - **顶栏上的小面板（外观、换模型、语言）是同一种东西**：开、关、位置、点外面收起、Esc、Tab 都在 `scripts/flyouts.js` 和
       `styles/flyouts.css`，新的同类面板用 `flyout(panel, button, {sheet, trap, focus, escape})`。窄屏（≤560px）上从底下升起来，
       点暗底只收起、不点到下面。页面测试用 `steps.open_flyout` / `flyout_open` / `escape_closes_flyout`，新面板加进 `_sweep` 和 `_surfaces`。
     - **设置和换模型分开**：齿轮（⌘, / Ctrl+,）开设置，模型按钮（⌘. / Ctrl+.，⌘⇧M 被浏览器占了）开换模型的面板。

7. **界面有中文、英文、西班牙语三种语言。**（用户原话："要求有中文，英文，和西班牙语"；"在工作中切换语言的时候
   不要直接刷新并回退到主页，而是直接在原来的位置上直接翻译"。）
   - 换语言不能刷新页面、不能清空对话：正在进行的一轮、滚动位置、写了一半的消息、打开着的面板都要原样留着。
   - 界面上的每一句话都用中文写，包一层 `t()`（页面里是 `t()`、`k()`、`tel()`、`retext()`、`tattr()`，
     见 `harness/webui/page/scripts/i18n.js`；程序里是 `harness/i18n.py` 的 `t()`、`k()`），然后在
     `harness/locales/en.json` 和 `es.json` 里加上译文。`tests/test_i18n.py` 会检查有没有漏。
   - 程序发给页面的话要用 `t()` 生成，这样事件里会带着原文（key、values），页面换语言时才能就地重译。
   - 写给模型看的（系统提示词、工具说明、工具结果）保持中文，不进翻译表。
   - 西班牙语用"tú"称呼用户，用西班牙本地的说法（coste、Ajustes），但要让拉美的读者也看得懂。

8. **有利于后期维护和开发。**（用户原话："要记住有利于后期维护和开发"。）
   - 一个文件只讲一件事。新功能放进新的模块，不往已经很大的文件里堆；页面里的新功能也自成一块：样式写成
     `harness/webui/page/styles/` 里的新文件、脚本写成 `page/scripts/` 里的新文件，开头写清这一块管什么，再按顺序把文件名加进
     `page/__init__.py` 的清单（`STYLES` / `SCRIPTS`；漏了、写了两次 `tests/test_page.py` 会失败）。脚本各块共用一个作用域，
     用 const/let 定义的名字要排在用它的块前面；样式里后写的盖过先写的，`reduce-motion.css` 永远在最后。
     一块长到几百行以上，就按它自己的小节拆成同一前缀的几个文件：`skills.js` 放这一页的总说明、共用的状态和小工具，
     后面接着 `skills-list.js`、`skills-detail.js`……（`agents-*`、`mcp-*`、`channels-*`、`tasks-*` 也一样），在清单里挨着排、
     保持原来的先后，挂到 `extensionTabs` 的那几行留在最后一个文件的末尾。用前缀、不用子文件夹。拆的时候先单独提交一次
     只搬家，证明 `assemble()` 拼出来的页面一字不差，再另外提交改注释。
   - 同一条规则只写在一处：价格、时间、格式检查这类由助手程序算，页面不再复制一份（页面要用就请程序算好发过来）。
   - 每个功能都有测试；修 bug 先写一个能复现它的测试。
   - 有意破例的地方（比如压缩会改动前面的历史）在代码注释和本文件里都写明为什么、边界在哪。
   - 能调的数（上限、阈值、时间）集中在 `config.py` 或设置里，不散落在各处写死。

9. **macOS、Windows、Linux 三个系统都要做好。**（用户原话："无论是 mac 还是 windows 还是 linux 都要进行对应的优化和操作"；
   "要针对 windows 用户友好，快捷键啊，指令啊 还有其他的"。）每加一个功能、改一处界面，都要想三个系统上各是什么样，不能只在 Mac 上好用：
   - **快捷键和说法**：Mac 写 ⌘、⌥，Windows 和 Linux 写 Ctrl、Alt（页面里看 `ON_MAC`，不要写死 ⌘）；"在访达中显示"在 Windows 上是
     "在文件资源管理器中显示"，Linux 上是"在文件管理器中显示"（由助手程序告诉页面是哪个系统）。
   - **命令和文档**：README 里的命令三个系统都给（zsh/bash 和 PowerShell 各一份，比如 `zhifox` 的 alias 和 PowerShell 里的 function），
     装 uv、加快捷命令、看日志的写法都要有 Windows 的版本。
   - **系统相关的代码只放在一处**：文件锁、路径检查（盘符、反斜杠、设备名、末尾的点和空格这些 Windows 的花招）、执行命令用哪个 shell、
     后台服务（launchd / systemd / 任务计划程序）、"在文件管理器里显示"、文件权限（600 和 Windows 的 ACL）、启动 Chrome 的管道，
     都各有一个小模块按系统分开写，别处只调用它。新加的功能要用到这类东西时，复用它们，不要自己判断 `sys.platform`。
   - **某个系统上做不到的**（比如操作整台电脑暂时没有 Windows 版）：界面上明说"这个系统上暂时不支持"和原因，测试里按系统跳过并写明原因，
     不要让它在那个系统上悄悄出错。
   - **测试三个系统都跑**：GitHub Actions 的矩阵里有 ubuntu、macos、windows；只在某个系统上才有的测试（比如 Xvfb、Windows 的服务）
     单独标出来。本机是 Mac，Windows 和 Linux 上的行为以 CI 的结果为准，汇报时说清楚哪些只在 CI 上验证过。

## 不能碰的东西

- **`.env` 里是各家的 API key。** 不要读出、打印或在回复里引用它的内容。需要检查时只看变量名，
  例如 `sed -E 's/=.*/=<已隐藏>/' .env`。永远不要提交它。
- **`data/` 是用户的私人数据**（会话、记忆、待办、设置）。不要提交。测试和试运行不要写进这个目录，
  改用临时目录：测试里用 `tmp_path`；想在浏览器里试，运行 `uv run python tests/demo_web.py`
  （假模型、临时目录、编造的密钥，不花钱）。
  注意：只设置 `HARNESS_DATA_DIR` 再运行 `uv run harness` 是不够的，那样用的仍然是真正的 `.env` 和真正的 API，
  发消息会花钱，在页面上保存密钥会改写真正的 `.env`。
- 提交之前确认这两样仍被 `.gitignore` 排除，并扫一遍待提交的文件里有没有 key。
- 在浏览器里测试时只用编造的测试密钥，对着本地假服务器，绝不输入真实的密钥。
- 不要截取用户的屏幕。需要知道屏幕上的情况时，直接问用户。

## 常用命令

```bash
uv sync                          # 安装依赖
uv run pytest -q                 # 跑全部测试（默认并行，一分钟左右），不需要 API key，不花钱
uv run pytest -q -n 0            # 一个一个跑（调试、看 print 的输出）
uvx ruff check harness tests     # 代码检查（可以加 --fix，不要加 --unsafe-fixes）
uv run harness                   # 终端界面
uv run harness --web             # 网页界面
```

## 这个项目是什么

一个手写的 agent harness：运行在用户自己电脑上的个人助手，有终端和网页两种界面，支持 Claude、GPT、Gemini、
DeepSeek。它同时是教学项目，所以没有用任何 agent 框架，每个文件只讲一件事，注释写的是"为什么"。
详细的说明在 `README.md`（英文）和 `README.zh-CN.md`（中文），改了行为就要两份一起更新。
每次推送都会由 GitHub Actions 在 Ubuntu 和 macOS 上跑测试（`.github/workflows/tests.yml`），推送后看一眼结果。

**给人看的名字是"智狐"，英文名 ZhiFox，标志是一只小狐狸**（都是用户定的）。GitHub 仓库名是
`Akxan/zhifox-agent-harness`（2026-11-20 用户选定改名）。界面里在任何语言下都写"智狐"；助手用中文以外的语言回答时称自己 ZhiFox
（写在系统提示词里）。名字只有一处定义：`harness/config.py` 里的 `APP_NAME` 和 `APP_NAME_LATIN`，页面里另有几处写死的文字和内嵌的 SVG 标志（`page/index.html` 里的 `#i-fox`）。包名、命令（`uv run harness`）、
请求头和浏览器里保存设置用的键名仍然叫 harness，不要跟着改。

| 位置 | 内容 |
|---|---|
| `harness/agent.py` | 循环。不出现任何和具体厂商、具体界面有关的代码 |
| `harness/providers/` | 各家模型的适配层：`claude.py` 用 Anthropic 原生接口，`openai_compat.py` 供另外三家共用，`compaction.py` 是这三家的压缩历史 |
| `harness/tools/` | 工具。`base.py` 里的 `function_tool` 用来创建自定义工具 |
| `harness/permissions.py` | 哪些操作要先问用户 |
| `harness/conversations.py` | 会话的保存和读取 |
| `harness/search.py` | 会话的全文搜索：内存里的索引，三档匹配（原样、相近的词、隔字） |
| `harness/attachments.py`、`harness/documents.py` | 附件的检查；从 Word、Excel、PowerPoint 里取出文字 |
| `harness/tasks.py`、`harness/webui/scheduler.py`、`harness/tools/tasks.py` | 定时任务：任务是什么、什么时候跑、存在哪（tasks.py，不碰线程和网页）；网页界面里的调度、排队、执行（scheduler.py）；模型用的工具 |
| `harness/memories.py` | 网页"记忆"窗口背后的部分：列出、查看、修改（内容和名字）、删除记忆文件，浏览器送来的路径出不了记忆文件夹 |
| `harness/prompt.py` | 系统提示词：内置的模板、用户改过的版本（`data/prompt.md`）、占位符；"能联网"那一行只给有联网搜索的那一家 |
| `harness/ledger.py`、`harness/budget.py`、`harness/export.py` | 花费账本（data/ledger.jsonl）、每月预算的提醒规则、导出全部数据（遮住密钥） |
| `harness/webui/markdown.js` | 回复里的 Markdown → 页面元素；逐字输出时只重画最后一两块 |
| `harness/mcp/` | MCP 客户端：jsonrpc、stdio、http、client（两代协议）、bridge、manager（快照）、config、importers |
| `harness/media.py` | 多媒体库：附件原件按内容存在 `data/media/`；哪段会话、哪条消息里有哪些附件；旧会话从对话历史里找回附件 |
| `harness/skills/` | Skill：SKILL.md 的解析和检查、安装（新建、上传、网址、从 Claude Code 导入）、交给模型的列表、`skill` 工具 |
| `harness/agents/`、`harness/tools/agent.py` | 自定义 Agent：同一种文件两种用法——主助手交给它的子 Agent（`runner.py`），和用它开始对话的角色（`App.new_conversation(agent)`） |
| `harness/catalog.py` | Skill 和 Agent 共用的"附在用户消息里的目录"（`<skills>`、`<agents>` 块） |
| `harness/channels/` | 聊天通道（Telegram、Discord、Slack、飞书、钉钉、QQ）：base（Channel、PushChannel）、platforms + guides（平台名单、要填的东西、建机器人的步骤，网页向导和 `harness channels setup` 共用）、store（channels.json，凭据在 `credentials` 里）、hub（FACTORIES、CHECKS、CLASSES 三张表）、router、session、ui、markup、gateway（Discord 和 QQ 共用的网关）、各平台一个文件、cli |
| `harness/net/` | 手写的 WebSocket 客户端（RFC 6455）、JSON-over-HTTPS 帮手和上传用的 multipart 编码 |
| `harness/tools/send.py`、`harness/channels/outbound.py` | 把文件发回聊天里：工具（看一眼、问不问、发之前再读一遍）、平台收什么（`plan`）和发到哪个聊天（`Outlet`） |
| `harness/serve.py`、`harness/log.py`、`harness/webui/lifecycle.py` | 无界面模式 `harness serve`（服务器上用，跑定时任务和聊天通道）、适合 journald 的日志、调度器和通道统一的启动和停止 |
| `harness/service/`、`harness/instance.py` | 后台服务：plan（服务怎么运行）、launchd（plist + launchctl）、systemd（用户 unit + systemctl）、logs、cli（`harness service …`）；数据目录的锁和"是谁占着" |
| `harness/commands/` | "只读的命令不用问"：手写的命令行切词（不用 shlex）、选项、路径检查、每个程序的规则、你自己加的命令（`data/commands.json`） |
| `harness/capabilities.py`、`harness/webui/capabilities_api.py` | 信息栏"智狐能做的事"和终端 `/tools` 的功能清单：工具对应到功能、状态和原因、问不问你、在哪儿执行、怎么打开 |
| `harness/webui/manifest.py`、`harness/webui/icons/`、`tests/make_icons.py` | 网页应用的清单和图标；PNG 由 `make_icons.py` 用无界面的 Chrome 从 `docs/assets/logo.svg` 画一次再提交（改了标志就重画） |
| `harness/model_catalog.py`、`harness/webui/models_api.py` | 模型目录：每一家有没有密钥、能选的模型、能做什么、价格、换过去会怎样（continue / fresh / leave）；网页的换模型面板（`/api/model/catalog`）和终端的 `/model` 都只照着画 |
| `harness/imports/` | 从其他工具导入：登记表（`registry.py`：哪个工具把 MCP / Skill / Agent 放在哪儿，注明出自哪份文档）、各种格式的读法（readers、yaml_lite、mcp_shapes、agent_formats、agent_tools、refs）、总览（overview）；`mcp/importers.py`、`skills/from_tools.py`、`agents/from_tools.py` 照着它找 |
| `harness/evals/`、`evals/tasks/` | 评测：任务格式（spec）、临时目录（seed）、记录员（recorder）、按结果打分（graders）、评分员（judge，花钱）、演习（dryrun，用 tests/ 的假服务器）、跑（runner）、估算（estimate）、结果和对比；起步任务在仓库根目录的 `evals/tasks/` |
| `harness/trace/` | 运行记录：每一轮的时间线（请求模型、工具调用、等你确认），页面在 `page/scripts/trace.js`、`trace-chart.js`；终端 `/trace` |
| `harness/endpoints/`、`harness/webui/endpoints_api.py` | 自定义模型接口：地址规矩、`data/endpoints.json`、不带别家密钥的客户端、猜能力、测试连接 |
| `harness/undo/`、`harness/webui/changes_api.py` | 撤回一轮改的文件：留底（`data/backups/turns/`）、撤回和重做、冲突检查、告诉模型；页面在 `page/scripts/file-changes.js` |
| `harness/sandbox/`、`harness/webui/sandbox_api.py` | 命令的沙箱（可选，默认关）：policy（三档、哪些路径）、session、macos（sandbox-exec）、linux（bwrap）、windows（为什么没有）、backend（唯一看系统的地方）、cli（`harness sandbox`） |
| `harness/claims.py` | 一段会话同一时间只让一边写：聊天通道正在用的会话，网页上不能发消息、打开、改名、删除 |
| `harness/browser/`、`harness/tools/browser.py`、`harness/webui/browser_api.py` | 浏览器：智狐自己启动的 Chrome（`data/browser/profile`），只走 `--remote-debugging-pipe`，不开端口；手写的 CDP 客户端、读页面、截图 |
| `harness/desktop/`、`harness/tools/computer.py`、`harness/webui/computer_api.py` | 操作整台电脑（进阶选项，默认关着）：截图和缩放、macOS（screencapture + CoreGraphics）和 X11（xdotool）的后端、每次运行的会话、屏幕同一时间只给一个运行 |
| `harness/i18n.py`、`harness/locales/` | 多语言：`t()` 和英文、西班牙语的翻译表（键是中文原文） |
| `harness/ui.py`、`harness/webui/` | 终端界面、网页界面（`server.py` 是本地服务器；`page/` 是页面：骨架 `index.html`、`styles/`、`scripts/`，由 `page/__init__.py` 按清单拼成一页，启动时拼一次） |
| `harness/app.py`、`cli.py`、`config.py`、`settings.py`、`pricing.py` | 组装、入口、配置、用户设置、计价 |

## 改代码时必须守住的几条

- **对话历史只在末尾追加。** 一段对话里，系统提示词和工具说明书保持不变，历史里已有的内容不修改、不删除
  （出错时从末尾撤回是允许的）。改了前面的内容，Claude 的缓存会失效，它之前的思考内容也通不过 API 的校验。
  唯一的有意破例是 GPT、Gemini、DeepSeek 的压缩（`harness/providers/compaction.py`）：上一次请求占用的上下文超过模型上限的
  `COMPACTION_THRESHOLD` 时，在下一次请求之前把较早的那一段一次性换成一问一答两条摘要消息，然后又回到只追加。
  边界：只在 OpenAI 格式的适配层里做（Claude 由 API 在服务器上压缩）；切口只落在一轮的开头，系统提示词和最近
  `COMPACTION_KEEP_TURNS` 轮一字不动，工具调用和结果永远在同一边；`mark()` / `rollback()` 按压缩掉的条数换算。
  会话文件里 `messages` 存压缩后的历史，`events` 保持完整。别处不要再改前面的历史；确实要改，先在这里写明理由和边界。
- **倒回最后一轮（编辑、重新生成，`harness/rewind.py`）是从末尾撤回，不是改中间。** 只能倒最后一轮。每一轮开始时
  `begin_turn(..., mark=backend.mark())` 把位置记在 `user` 事件上；OpenAI 格式的压缩掉了多少条存进 `Conversation.dropped`，
  `restore(messages, dropped)` 交还，位置才在重新打开、重启后对得上。倒之前先用 `Backend.turn_at(mark)` 查：还在就倒，
  没答出第一步就失败了的再发一次，压缩进摘要的不能倒。新的入口要记位置就传 `mark=`；不传的那一轮倒不回去
  （定时任务自己跑的那一轮故意不传：发给模型的指令和页面上显示的不一样）。
  倒回以后服务器先截断历史和事件、马上存盘（经过 `_remember`），再发 `reset` 重放留下的事件、发一次 `state`；
  不要另做一个只删页面元素的事件。请求要带会话编号和那条 `user` 事件的编号，对不上就拒绝（409）。
  这一轮做过可能改动东西的事，倒回之前在原地问一次（`rewind.effects`；操作过整台电脑、浏览器的另外单独一句，`rewind.operated`）。
  新加的工具默认算"改动"；只读的动作要在 `rewind.py` 里登记。账本和信息栏的用量都不因为倒回而减少。
  压缩的切口只落在 `compaction.is_turn_start` 认的一轮开头：工具结果后面补的图片消息（`TOOL_IMAGES_NOTE`）不算。
- **历史里只放普通的字典和字符串**，这样才能原样存成 JSON 文件、读回来原样发给 API。
- **每个工具调用都必须有对应的结果**，包括被用户中断的情况。相关逻辑在 `Agent._run_tools` 的 `finally` 里。
- **会改动用户电脑的工具必须经过 `permissions.review`。** 新增这类工具时要加上对应的规则。
- **网页界面的安全措施不能削弱**：只监听 `127.0.0.1`；所有请求校验 token、Host 和 Origin；
  页面不加载任何外部资源；模型和工具输出的文字只用 `textContent` 放进页面，不当作 HTML 解析；
  回复的 Markdown 由 `harness/webui/markdown.js` 画：只用 createElement / createTextNode 和固定几个属性，从不解析 HTML；
  链接只认 http、https、mailto；图片不加载。服务器把它填进页面的 `__MARKDOWN__`（同一个 nonce），所以这个文件里
  不能出现 `</script`、`<!--` 和页面的占位符（`tests/test_markdown.py` 会查）。改了它要跑 `node --test tests/markdown.test.mjs`
  （pytest 也会跑）；演示页面第 6 条消息是一段用齐各种 Markdown 写法的回复。
  页面本身也一样：`harness/webui/page/` 的每一块原样拼进同一个 `<style nonce>` 或 `<script nonce>`，所以每一块里都不能出现
  `</script`、`</style`、`<!--` 和 `__XXX__` 这样的占位符（`tests/test_page.py` 会查）。拆开只为好改：不加地址、不放宽安全策略，
  发出去的仍是一个自给自足的页面。
  API key 不出现在发给浏览器的任何内容里。
  服务器里会替页面启动系统程序的地方有两类。一类是在文件管理器里显示：`/api/conversation/reveal`（会话的文件）、
  `/api/memory/show`（一条记忆）、`/api/memory/reveal`（记忆文件夹）、`/api/reveal`（只认 tasks / todos / prompt / channels）、
  `/api/mcp/reveal`、`/api/commands/reveal`（什么都不给，只显示 `data/commands.json`）、`/api/browser/reveal`（只打开浏览器的配置文件夹）、`/api/computer/permission`（只认 `screen`、`input` 两个词，只打开 macOS 系统设置的那一页，不改任何设置）、`/api/skill/reveal`（只认 data/skills/ 里已有的、不是链接的一段文件夹名，或者什么都不给）。
  `/api/media/reveal` 只接受附件编号（`会话编号/第几排/第几张`，用 `media.ID` 检查格式、在索引里查到条目），
  路径是 `data/media/<sha256>.<扩展名>`，由服务器算。
  **只有 `/api/media/file`、`/api/browser/shot` 和 `/api/computer/shot` 会把文件的字节发给浏览器**。两个 shot 只收 64 位十六进制编号，固定 `image/jpeg`，
  安全头和下面一样。`/api/media/file` 只收附件编号，Content-Type 只按 `media.STORED_TYPES` 的白名单给
  （文本一律 `text/plain`），带 `Content-Disposition: attachment`、`X-Content-Type-Options: nosniff` 和
  `Content-Security-Policy: default-src 'none'; sandbox`。以后再加返回文件内容的接口，照这几条办。
  另外 `GET /icons/<名字>` 发智狐自带的几张图标：名字只认 `manifest.ICONS` 里一字不差的文件名（查表，不解码、不拼路径），固定 `image/png`、
  `nosniff`、`default-src 'none'; sandbox`、`private, max-age=86400`；`GET /manifest.webmanifest` 里的地址带着口令，所以 `no-store`。两样都要口令、查 Host 和 Origin。
  页面的 CSP 只为装成应用放宽了两处，都是写死的完整地址（`manifest.policy`）：`manifest-src` 只有清单，`img-src` 在 `data:` 之外只有那几张图标，
  不是 `'self'`。Chrome 下载清单里的图标时也按页面的 `img-src` 检查，去掉它们就装不成应用（`tests/ui/test_ui_install.py` 会失败）。
  在没有文件管理器的机器上（服务器），状态里的 `file_manager` 为假，这些按钮改成告诉你文件的完整路径。
  页面最多只能给编号、列表里的名字或固定的几个词，路径由服务器算，命令用参数列表、不经过 shell。
  另一类是保存、启用、重启、导入一个 stdio 的 MCP 服务器，这会让电脑运行用户配置的命令：只有带口令的页面能做；
  命令用参数列表启动、不经过 shell；子进程的环境里去掉各家模型的密钥，配置里引用它们（`${ANTHROPIC_API_KEY}`）会被拒绝；
  模型没有任何能添加或修改 MCP 服务器的工具，`data/mcp.json` 和 `.env` 一样在文件工具的 protected 名单里；
  只有用户打开导入页时才读别的工具的配置（`imports/registry.py` 登记的位置），什么都不运行，只复制勾选的；别家"不用问"的设置（autoApprove、alwaysAllow、trust，以及文件里的 approval）一律不搬；引用模型密钥的服务器列出来但不能导入（`config.model_key_references`）；mcp.json 里环境变量和请求头的值
  不发给浏览器（只有最后 4 位），编辑时没重填的用 keep 交回去。以后再加这类接口，照这几条办。
- **事件流的约定不能破坏。** 浏览器刚连上时，服务器先补发日志里已有的事件（过时的"状态"只留最后一个），
  然后发一个名叫 `synced` 的标记（没有编号、不进日志），之后才是实时事件。页面靠这个标记，加上
  "`reset` 到下一个 `state` 之间算重放"，来决定哪些内容播放入场动画。
- **发给浏览器的、写进文件的，都要用 `harness/safe.py`。** 模型的输出和用户粘贴的文字里可能有半个表情
  （落单的代理字符）、NaN 这类非法的数，直接 `json.dumps` 会写出浏览器解析不了的东西，或者干脆编码失败，
  让事件流永久断开。所以：事件和 HTTP 回答用 `to_json`，文件用 `write_file`（先写临时文件、fsync、再替换，
  权限 600），入口处的文字用 `clean_text` / `clean` 洗一遍。
- **"问不问用户"只能放宽，不能绕开。** `Permissions.review` 决定哪些操作需要确认，`auto_allows` 只决定
  当前模式下这一次可不可以不问；没有问就执行的，要调用界面的 `auto_allowed()` 留下记录。
  `.env` 在任何模式下都碰不了；读 `data/` 在任何模式下都要问（"全部自动允许"除外）。
  `readonly`（只读的命令不用问）排在 ask 和 edits 之间：只放开 `commands.classify` 判为只读的命令、你在 `data/commands.json`
  里加的命令（只在这一档、只在单独一条时算）、读工作目录之外的文件。它不包括改文件，edits 也不包括只读命令。判断必须保守：
  看不懂的一律不算；改 `commands/rules.py` 之前先看 `tests/test_commands.py`，新发现的绕过先加一行会失败的测试。
  `auto_allowed(why)` 带着为什么（readonly / listed / mode）。几档的名字和说明只写在 `page/scripts/approval-modes.js`，
  对话、任务、通道三处都照它画。`data/commands.json` 在文件工具的 protected 名单里，模型没有任何能改它的工具。
- **定时任务和对话同时进行，不拿 `WebApp._turn`。** 这把锁只属于你的对话，以及会改动对话状态的操作（换模型、开新对话、
  改提示词、改记忆、改 Skill……），拿不到时用 `self._busy()` 回答；任务运行时这些照常可以做。凡是"会被停下"的东西，
  每次运行各有一份（`App.unattended_agent` 返回的 `UnattendedRun`）：自己的 `Shell`、自己名下的 MCP 调用
  （`mcp.snapshot(owner)` / `cancel_running(owner)`）、自己的 `RunUI`、适配层和权限。对话的"停止"只停对话自己的
  （MCP 的 owner 是 None），停任务走 `/api/task/stop`，只停那一次；退出时 `cancel_all()`、`Scheduler.wait_idle`。
  共用的只有磁盘上的东西，每样都有锁：`TodoStore`、`TaskStore`、`ConversationStore`、`tools/files.py` 的 `_EDITING`、
  `tools/memory.py` 的 `_LOCK`、`Ledger`、`MediaIndex`、`SkillStore`。新加会被对话和任务同时用到的共享状态，也要加锁，
  并写一个去掉锁就会失败的并发测试。同时最多跑 `config.MAX_PARALLEL_TASKS` 个，多的排队；同一个任务不会同时跑两次。
  提醒不调用模型，不排队。
- **任务按自己的那一档问不问。** 每个 agent 任务有 `approval`（ask / edits / all，默认 ask），运行时用
  `Permissions.unattended(mode)`，和对话里选的那一档无关；在"扩展 → MCP"里设成"不用问"的工具在任务里不算数
  （`trust_mcp` 为假）。没问就放行的照样调用 `auto_allowed()`。`.env` 和 `data/` 和对话里一样严。
  **模型添加的任务一律是 ask**：`task_add` 写死 ask、没有这个参数，只有用户能在页面上放宽。
- **任务运行不碰当前对话。** 用 `App.unattended_agent`（新的适配层、空的历史）和单独的 `RunUI`，事件不进页面的日志；
  跑完存成普通会话（`Conversation.task` 记着任务编号），照样经过 `ConversationStore.save`。
  进度和确认里给人看的话由程序写好（已经带着工具名），页面原样显示，不再加工具名。
- **调度器的锁规矩：** 拿着 `Scheduler._lock` 时不调用 RunUI 的方法、不发状态、不写任务文件。
- **任务的时间按本地时间、按日历算**（`tasks.next_after`），不要"加 24 小时"。测试用假的钟加 `tick()`，不真的等。
  定时任务只在网页界面运行，终端界面不运行。
- **Skill 的列表放在用户消息里交给模型**（`skills/catalog.py`），从不放进系统提示词：一段对话的第一条消息带一个
  `<skills>` 块，之后列表变了，才在下一条消息上再附一份，所以系统提示词和工具说明书一个字都不变。怎么用 Skill
  写在这个块和 `skill` 工具的说明里，用户改了提示词也去不掉。Skill 永远不放宽确认（`allowed-tools` 只是显示）；
  模型不能安装、修改或删除 Skill；Claude Code 的文件夹只有用户点"导入"时才读，而且是复制过来。
- **花费账本只在末尾追加。** `ledger.py` 每次模型调用写一行；超过 `LEDGER_DETAIL_DAYS` 天的明细会被合并重写
  （有意的例外，金额不变，读不懂的行原样保留）。新的调用方要记账，就给它的 agent 接 `ledger.recorder(..., source="…")`
  （定时任务用 source="task"），不要另算价格。
- **导出不能带出密钥。** 新增存着密钥的配置文件时，把它加进 `export.MASKED_FILES`，并在 `tests/test_export.py` 里
  加一条"压缩包里找不到这个密钥"的测试。`export.LEFT_OUT_FOLDERS` 里的文件夹整个不导出（`browser/profile`：里面是登录过的网站的 cookie）。
- **聊天通道和定时任务是同一个做法。** 每个聊天的每一轮用 `App.channel_agent`（和 `unattended_agent` 共用 `_side_run`：
  自己的 shell、自己名下的 MCP 调用、`Permissions.unattended(通道的档位)`，记账 source=`channel`），不拿 `WebApp._turn`；
  开始之前先用 `Claims.take` 占住这段会话，网页这时不能写它（409），一轮结束放开、网页上开着的话重新读一遍。
  只有配对过的主人能用：不是主人的消息不处理（不在配对的时候一个字都不回），群聊一律不理，按钮也只认主人点的。
  确认请求在聊天里写明是谁在问（主助手还是哪个子 Agent）、完整的命令和工作目录；网页上也能答，先到的算数。
- **`channels.json` 里是密钥。** 机器人的 token 和配对码：文件权限 600、在文件工具的 protected 名单里、在
  `export.MASKED_FILES` 里，从不进状态、日志和错误信息（页面只看到最后 4 位，`store.summary` 的 hints；日志经过 `log.hide`）；命令看不到各平台的密钥
  （`tools/shell.SECRET_ENV_VARS`）。各平台的接口地址只能用环境变量改（`TELEGRAM_API_BASE`、`DISCORD_API_BASE`……，密钥会发到那里去），页面上改不了。
- **加聊天平台**：写一个 Channel 子类（推送式的继承 PushChannel），在 hub 的三张表和 `platforms.py` 里登记，步骤写在 `guides.py`。
  新的密钥字段标 `secret: True`，它的环境变量加进 `SECRET_ENV_VARS`（`tests/test_channels_platforms.py` 会对一遍）。
  HTTP 一律走 `net/http.py`（有超时、换了域名的重定向不带 token、回答有大小上限），WebSocket 走 `net/websocket.py`
  （明文 ws:// 只许连本机）。`ask` 必须返回非空的编号，没有按钮的平台给一个合成的，否则打字的"允许"不算数。
  新平台的测试照 `tests/channel_rig.py` 的 FakePlatform 写一个假服务器，组装和演示的"手机"页面（`tests/demo_phone.py`）就都能对着它跑。
- **官方 SDK 只用来收消息，而且是可选的**：`lark-oapi`（飞书）和 `dingtalk-stream`（钉钉）只在各自模块的 Listener 里、用到时才导入；
  发消息、改卡片、下载文件手写 HTTP。没装时 connect 拒绝，并给出 `uv sync --extra …`。我们用到了 SDK 内部的名字，
  所以 pyproject 里有版本上限，升级前先重新看源码。SDK 的事件处理函数里不做慢事，交给 `PushChannel._hand_off`。
- **Discord 和 QQ 的网关**：重连间隔最多 `GATEWAY_RETRY_SECONDS`（Discord 一天登录超过 1000 次会作废 token）；
  token 不对、intents 不许这类关闭代码直接停下，不再重连。
- **聊天通道每一轮结束调用 `run.close`**，关掉这个聊天自己的浏览器标签页；`_side_run` 的 close 换一个新的 BrowserSession，
  同一个助手接着用，工具说明书不变。
- **send_file 只属于在聊天通道里开始的会话（`Conversation.channel`）。** `App._side_run(source="channel")` 带着这个聊天的 `Outlet`；
  网页上打开这类会话时 `conversation_tools` 也放一份（`outlet=None`，回答"不在聊天软件里"），两边都放在电脑工具之后、agent 工具之前，
  清单一字不差；网页的普通对话和定时任务没有它。说明书里不写平台和上限。问不问写在 `Permissions._review_send`：每次都问
  （只有"全部自动允许"不问，"自动允许改文件"照样问：放开的是改文件，不是往外发），`data/` 里的带提醒，`FileEditor.protected` 里的
  永远不发；平台不收的在 `check` 里拒绝、不弹卡片；发之前 `_read_again` 和卡片上的对一对。发送走 `router.send_file`
  （排在发送队列里、等结果；`/stop` 不再等，排着的不再发）。新平台实现 `Channel.send_file` 和 `Capabilities.uploads`，上传走
  `net/multipart.py`，上限照文档写在平台模块的 `UPLOADS` 里，总上限是 `config.CHANNEL_SEND_BYTES`。
- **有意的破例**：`i18n.fill` 和页面的 `t()` 填 `{platform}` 时，汉字和中文平台名之间的那个空格去掉（"连接飞书"）；别的值不动。
- **调度器和聊天通道只从 `webui/lifecycle.py` 启动和停止**（网页界面和 `harness serve` 都走它）。
  `harness serve --web` 照样只监听 127.0.0.1，带口令的地址不写进日志（没有终端时写进 `data/.web-address`，600，退出时删掉）。
- **后台服务（`harness service`）**：plist / unit 只照着 `service/plan.py` 的 Plan 写，只放路径和开关，不放密钥和口令
  （口令存在 `data/.web-token`，权限 600，重启前后不变，浏览器里开着的页面刷新一下就能接着用；卸载服务时删掉，见 `webui/token.py`；
  带口令的地址写在 `data/.web-address`；两个都不导出）。在终端里开的 `harness --web` 照旧每次换口令。命令一律用参数列表、不经过 shell，launchctl 写完整路径 `/bin/launchctl`。
  运行的是 `.venv/bin/python -P -m harness … serve --service launchd|systemd`。**开发和测试永远不运行真的 launchctl / systemctl / journalctl，
  也不打开浏览器**：conftest 把 `service.base.run_command`、`run_attached` 和 `service.cli._open_browser` 换成一调用就失败的函数，
  测试用 `tests/test_service.py` 里的假系统。数据目录的锁在 `instance.py`：`claim(data_dir, kind)` 写下进程编号和 kind，`in_use` 只看不改；
  装、开、重启之前先看锁。launchd 下的日志由 `log.ServiceLog` 写。能调的数在 `config.SERVICE_*`。
  `harness --web` 和 `harness serve` 一样接住 SIGTERM，和 Ctrl-C 走同一条收尾的路（存好盘再退出），而且在打印地址之前就接上。
- **操作整台电脑是进阶选项，默认关着**；打开必须经过页面上的风险确认，服务器要求 `acknowledge: true`；定时任务和聊天通道要另外打开才有。
  **测试和演示永远不碰真的屏幕、鼠标和键盘**：conftest 把 `desktop.backend.detect` 换成"用不了"，要测的用 `tests/fake_desktop.py` 的 `install`，
  演示页面用的也是假电脑；真后端只在 Xvfb 上测（`tests/test_desktop_x11.py`，CI 只在 ubuntu 跑）。macOS 上只能调用不弹窗的预检函数
  （`CGPreflightScreenCaptureAccess`、`AXIsProcessTrusted`），不许调用任何 `Request…`，也不改系统设置。
  问不问写在 `permissions.review_computer` 和 `Permissions._computer_allowance`：默认除了 `wait` 和 `cursor_position` 每一步都问，
  "只读的命令""改工作目录里的文件"两档不放开它。只有两种情况不问（2026-11-21 用户定的）：一是这段对话里放开了这一类——截图一类
  （`trust_screenshots`）或点击打字一类（`trust_actions`），归哪一类看 `desktop/session.trust_of`，记在这段对话自己的 `DesktopSession` 上；
  只有 `attended` 的 session（`App.chat_desktop`）能打开，只能由网页对话里正在等答复、而且给了这个选择的那张确认卡片打开
  （`/api/computer/trust` 带 `what` 和 `confirm`，由 `WebUI.offered` 核对），设置里的"恢复每一步都问"（`/api/computer/untrust`）收回，
  换一段对话从头再问；二是这次运行实际用的那一档是 all（对话的、任务自己的、聊天通道的都算）。定时任务和聊天通道的 session 不是
  attended 的：卡片上不给，也打不开。放开了也不变的：危险的组合键（`keys.DANGEROUS`，按系统分）每次都问，问题标 `always`、不带 offer；
  不许碰的程序照样拒绝；没问就做的每一步都调用 `auto_allowed()`（这段对话里放开的 why 是 `trusted`）。
  一轮结束时先 `chat_desktop.release()` 再 `end_turn()`：让出屏幕会刷新状态，顺序反过来，页面马上发来的请求会撞上还没放开的锁。
  键盘动作前把问你时最前面的程序换回来（`_focus`），鼠标动作前那一点换了窗口就不做（`_guard`），因为点"允许"会把浏览器换到最前面。
  屏幕只有一个：`DesktopManager.claim` 让一个运行占着它，这一轮结束 `release` 或 `run.close`；"停止"之后这一轮里一直拒绝。
  工具包和浏览器同一套写法：Claude 用 `computer_toolset_20260801`，一轮里失败后的结果用 `tools/computer.HALTED`（和浏览器那句不同，文档规定）；
  看不了图的模型（DeepSeek）靠 `Tool.vision` 拿不到这些工具。
- **i18n 里的嵌套**：值本身是一句 `t()` 时，`Message.nested` 记着它的原文，页面的 `t()` 递归重译。拼确认文字时让每一段都保持是 Message，
  不要用 `+` 把它们拼成普通字符串，否则换语言时会半中半英。
- **联网回答的来源**（`harness/citations.py`、`page/scripts/citations.js`）：`messages` 里 API 给的 `citations` 原样不动，给人看的另抄一份
  （只认 http、https，标题和原文洗过、截短）。网页上每一处引用一个 `cite` 事件（`marks`、`sources`），编号由程序按"一段回复"算：
  `webui/ui.py` 的 `PARAGRAPH` 必须和页面 `streaming.js` 里哪些事件结束一段回复保持一致，改了一边要改另一边。终端和聊天通道各自一段（一条消息）一数。
  `block_end` 只是让回复停一下（`pauseBlock`）：紧接着还是文字就算同一段，别的内容事件到来才收尾（新的内容事件的处理要先调用 `endBlock`），
  这是为了以前存下的、被切成好几块的联网回答。markdown.js 认 `\uE000编号\uE001` 这个记号，交给 `hooks.cite` 建元素；只能由页面插入
  （模型写的这两个字符先换成 �），代码和链接文字里不放；原文 `block.text` 不插记号，搜索跳转和复制都靠它。
- **换模型的规矩只在 `model_catalog.py`（会怎样）和 `App.select_model`（真的换）**，页面不判断；同一家之内换模型时，
  这段会话正被聊天通道占着就拒绝。
- **加一个可以导入的工具**：在 `imports/registry.py` 写一个函数，位置只写官方文档写明的（只在源码里的要注明）；写法是新的，
  再在 `mcp_shapes.py` 或 `agent_formats.py` 加一种读法，在 `tests/test_imports.py` 放一份假文件。转换出来的 Agent 只会更少、不会更多。
  换写法时改了什么、丢了什么，用 t() 写成 notes 或 changes 交给页面。改了某个工具放文件位置的环境变量要加进 `imports/places.VARIABLES`：
  测试和演示会把它们清掉，免得读到真的配置。
- **评测（`harness eval`）不确认就不花钱**：不写 `--real` 一个真的请求都不发（默认演习：tests/ 的假服务器按任务的 `[[dry_run]]` 回答）；
  写了先打印估算，`--yes` 或在终端里输入 yes 才跑，没有终端又没有 `--yes` 的不跑，`CI` 有值时一律拒绝。花的钱记进账本，来源 eval；
  评分员默认关着。每遍一个全新的临时目录、真的 `build_app`，结果只写 `data/evals/`。每个任务必须有标准答案：`tests/test_evals.py`
  验证标准答案满分、什么都不做（`--null`）拿不到满分。打分看留下的结果，不看说法。改了工具的名字或参数，同时改 `evals/tasks/` 里的检查和标准答案。
- **运行记录（`harness/trace/`）**：agent.py 只在几个记录点各写一行（turn_began / request_began / request_ended / tool_began /
  approval_asked / approval_decided / tool_ended / turn_ended），钩子、输出脱敏以后也挂在这几处，不要把别的逻辑塞进记录器。
  钱只用账本交回的那一笔（`Ledger.record` 的返回值），不另算；合计由记录器算好，页面和终端只照着画。每轮一个 `trace` 事件存进 `events`
  （在 `REPLAYABLE` 里、不在 `PARAGRAPH` 里），排在这一轮的内容之后、`turn_end` 之前；从不进 `messages`。记录里只放数字和截短的说明，
  不放工具输出（`TRACE_SUMMARY_CHARS`、`TRACE_MAX_SPANS`）。改了一轮的走法，记录器和 `trace/__init__.py` 的格式说明一起改。
  断言事件序列的测试要写上 `"trace"`。翻译表的键是中文原文，同一句不能有两个意思：任务详情里那一节叫"历次运行"，"运行记录"只指这个功能。
- **自定义模型接口（`harness/endpoints/`）**：编号是 `custom-<id>`，找一家用 `providers.find` / `every`，`PROVIDERS` 只有内置的四家；
  能力按模型问 `providers.caps`；"能不能接着聊"只看 `providers.continues`（能不能用工具不一样也算换一家）。用量和价格按"接口/模型"记
  （`pricing.price_key`），算钱用 `App.prices()`。`data/endpoints.json` 权限 600、在 protected 名单和 `export.MASKED_FILES` 里，密钥不进环境变量，
  页面只看到最后 4 位。客户端只用 `endpoints/client.make`（或 `make_client(provider, timeout=…)`），不要对它调 SDK 的 `with_options`：
  会照着环境变量重建。外面的地址只许 https，明文 http 只许本机和内网（只看写法，不查 DNS）；不跟重定向。
  **有意的破例**：自定义接口拒绝工具、而这段对话还没调用过工具时，从这一次起不发工具（被拒绝的请求服务器没处理，历史一字不改）；
  已经用过工具的照常报错。不认 `stream_options` 的不再要用量。两样都记进 endpoints.json；内置的几家不退。
- **撤回一轮改的文件（`harness/undo/`）**：只记文件工具的改动，记账靠这一轮线程上的 `ContextVar`，`tools/files.py` 里只有 `watching` 一行；
  定时任务和聊天通道不记。留底在 `data/backups/turns/<编号>/`，清单写好后不再改，撤回、重做的状态只记在 `file_undo` 事件里（由 `restore.fold` 推出来）。
  `file_changes` 排在 `trace` 之后、`turn_end` 之前；断言事件顺序的测试要写上它。撤回走 `/api/changes/…`：页面只给一轮的编号和清单里的第几个文件，
  路径由程序照着清单算；拿 `WebApp._turn`、认领这段会话；查和写都拿 `_EDITING`。保存密钥的文件、留底文件夹、换成链接的路径永远不写；
  工作目录之外、数据目录里、哈希对不上的，先问再写。模型没有撤回的工具。撤回之后在下一条消息前附 `<file_changes>`（`undo/notes.py`），不进系统提示词。
  **有意的破例**：倒回最后一轮时，这一轮之后才撤回的更早几轮的 `file_undo` 事件不跟着截掉，挪到最后一条用户消息前面（`webui/ui.kept_after_rewind`）。
  命令改动的文件不假装能撤：界面上明说撤不回来；不替用户的仓库做 git 快照。
- **命令的沙箱是第二道关卡，不替代"问不问"。** 每次运行（对话、每次任务、每个聊天）一个 `Sandbox`，子 Agent 用叫它的那一方的同一个。
  路径只在 `policy.build` 里算；SBPL 里的路径一律用 `-D` 参数，配置的文字里不出现路径；一组路径是空的就不写那条规则（空的 allow 等于全放开）。
  `data/`、`.env`、`config.protected_files` 永远碰不到；智狐自己的程序在沙箱里只读。选了一档、这台电脑上却用不了：`tool.check` 直接拒绝，
  绝不悄悄在沙箱之外运行。`unsandboxed: true` 在任何模式下都问；没人看着的运行（任务、通道）模式是 all 时直接拒绝。说明书里一直带着
  `unsandboxed`，不随设置变。测试默认用 `tests/fake_sandbox.py`；真的沙箱只在 `test_sandbox_macos.py`、`test_sandbox_linux.py`。
  `/api/sandbox/reveal` 只认设置里"额外允许的路径"列表里已有的一条。
- **写测试别赌时机**：GitHub 的 macOS、Windows 机器比本机慢得多。等条件成立（`until`、`page.wait`）不写固定的 sleep；不要"赶在 0.3 秒的动画里
  去看它在不在播"，而是记下调用过没有；换外观以后等过渡播完再量颜色；量耗时的测试连着几次超了才算。推送以后一定看 CI 的结果，红了马上修。
- **"智狐能做的事"只有一处判断。** 工具属于哪种功能、为什么关着、问不问你，都在 `capabilities.py` 里算，页面和终端只照着画。
  "问不问你"不另写规矩，而是拿有代表性的调用去问 `permissions`（`Tool.check` → `review` → `allowance`，"哪一档都问"就是换成 `all` 也问）。
  新加一种工具，要在 `KINDS` 和 `_GROUP_OF` 里登记，并写它的状态函数；忘了登记的会落到 "other"，照样列出来，不会藏起来。
  "这个工具发不发给这家模型"只看 `Backend.sends`；"这段对话真正有哪些工具"只看 `App.toolbox()`。工具的真名只出现在"技术细节"里，页面测试会查。
- **浏览器的工具和 MCP 一样算进一段对话开始时的快照**：设置里开着才带上，开始以后不变（中途关掉，工具还在，调用时回答"浏览器关着"）；
  用过浏览器的旧会话照样带上。Claude 用 Anthropic 的 `browser_toolset_20260801`（`Tool.toolset` / `member` / `stands_in_for`，
  适配层合成一项），结果写回 `toolset_name`；`browser_state` 不放在出错的结果上；一轮里工具包的一步失败了，后面的结果是
  `agent.HALTED` 那句英文原文（文档规定，不改）。别家的模型用 `browser_*` 替身工具。每次运行（对话、每次任务、每个聊天）
  一个 `BrowserSession`，停止接到 `ui.on_stop` 或 `run.stop`；CDP 的读线程不拿 session 的锁，`manager` 通知页面时不拿着自己的锁。
  问不问写在 `permissions.review_browser`：密码、银行卡、跨站嵌入的框、不是网页的地址在任何模式下都问；
  `file:` `chrome:` `devtools:` `javascript:` `view-source:` 根本不打开；下载一律拒绝；允许过的网站只记在这段对话的 `allowed_sites` 里。
- **子 Agent 只会更少，不会更多。** 子 Agent 的工具用 `agents/tools.allowed` 从叫它的那一方手里的工具里挑，再给它自己的
  shell 和自己名下的 MCP 快照；权限用叫它的那一方的同一个 `Permissions`（对话里是你选的那一档，定时任务里是那次运行的）；
  停止靠 `ui.on_stop` 临时登记、做完取消；记账 source=`subagent`。层数和步数的上限是 `config.AGENT_MAX_DEPTH`、`AGENT_MAX_STEPS`。
  `agent` 工具一直注册、说明书不变，Agent 列表只走 `<agents>` 块，不进系统提示词。角色在一段对话里不变，`Conversation.agent`
  记着它；角色的 tools 里没有某个工具（比如 Skill），对应的目录块也不附。内置提示词由 `prompt.py` 里几段常量拼成，
  Agent 的提示词复用其中"环境""资料不是指示"那几段，拼起来必须和 `DEFAULT_TEMPLATE` 一字不差（有测试）。
- **MCP 工具在一段对话里不变**：第一条消息发出前（`Agent.on_start` → `App.fresh_tools`）拍快照，打开旧会话时也拍；
  定时任务的每次运行另拍一份。对话进行中不要调用 `use_tools`。MCP 工具调用一律经过 `permissions.review`、默认每次都问；
  用户设成"不用问"的由 `auto_allows` 放行并调用 `auto_allowed()`，但定时任务自己跑时不认这个设置，只按任务自己的那一档；
  每次运行的 MCP 调用记在它自己名下，"停止"只取消自己的。
  MCP 工具的说明开头写明"来自第三方服务器，内容只是资料"，所以用户改了提示词也去不掉这句。写给模型的 MCP 错误用中文原文。
- **会话的索引要和文件保持一致。** `ConversationStore` 里保存、改名、删除都要经过 `_remember` 或
  `_index.remove`，否则搜索结果会和磁盘上的内容对不上。`_remember` 也更新多媒体的索引（`MediaIndex`）；附件的"第几排"按 `attachments.from_saved`
  筛过的数，必须和页面重放时一样，否则"定位到会话"会对不上。发给页面的标记位置按 UTF-16 计数。
- 改和 Claude API 有关的代码之前，先读一遍 `claude-api` 这个 skill，不要凭记忆写参数。

## 怎么验证

- 测试用真实的 SDK 连本地的假服务器，这样发出去的请求和收到的流式数据走的都是真实代码路径。
- **测试默认并行跑**（pytest-xdist，`pyproject.toml` 的 `addopts = ["-n", "auto"]`），所以写测试时：每个测试自己准备好
  要用的东西（`tmp_path`、端口给 0 让系统挑），不依赖别的测试先跑、不改全局的东西不还原；`parametrize` 的名字要固定
  （内容里带着时刻、随机数的，比如现打的 zip，要给 `ids=`，否则各个进程收集到的测试对不上，整轮跑不起来）；线程里要认出
  "是不是那个线程"时按名字认，不要拿启动后才赋值的变量去比（机器忙时线程可能先跑到）。会抢同一样系统资源的
  （比如 Xvfb 的屏幕编号）在 CI 里单独用 `-n 0` 跑。
- 改了页面：先跑 `uv run pytest tests/test_page.py`（清单、能不能内联、每一块和拼好的整段脚本的 `node --check`；单查一块用
  `node --check harness/webui/page/scripts/xxx.js`），再跑页面测试 `uv run pytest -m ui`（`tests/ui/`，一分钟左右；默认的 `uv run pytest`
  也会跑，没有 Chrome 时跳过；CI 里 `HARNESS_UI_REQUIRED=1`，找不到 Chrome 算失败）。它用无界面的 Chrome（我们自己的 CDP 客户端）
  对着 `tests/demo_web.py` 实际操作：打开没有报错、不请求外部资源；逐字输出和刷新重放；确认卡片；浅色、深色、跟随系统、对比度、
  减少动态效果；三种语言原地切换和没翻译的中文；390 宽时文字不冲出容器；八种本地东西的三个按钮和原地删除确认；编辑窗口关之前先问；
  下拉框的箭头；扩展、空状态、多媒体库、换模型、浏览器的小截图。页面里任何脚本异常、console.error、CSP 拦截都算失败；
  失败时截图和日志存在 `HARNESS_UI_ARTIFACTS`（CI 里是 artifact）。
- 加了新界面就加页面测试：常用的几步在 `tests/ui/steps.py`，点、按、等在 `pilot.py`（`page.click` 是真的鼠标，会等目标不动、没被盖住；
  等条件用 `page.wait`，不写 sleep；等动画用 `steps.idle`；只查静态样子的用 `steps.calm`）。新的本地列表加进 `test_ui_rows.py` 的 `ROWS`，
  新的面板和窗口加进 `test_ui_language.py` 的 `_sweep` 和 `test_ui_layout.py` 的 `_surfaces`，显示用户内容的地方把选择器加进
  `checks.USER_CONTENT` / `NAMES`。演示的回复用 `tests/demo_scripts.py` 的 `[ui:名字]` 剧本，不要依赖 demo_web.py 里按顺序取的回复。
  `tests/ui` 是一个包（有 `__init__.py`，免得 conftest 撞名），模块之间用相对导入。
- 页面测试判断不了的仍然手动过一遍：看起来精不精致、动画顺不顺、真正的触屏、读屏软件、没有密钥、输出到一半往上翻。
  做法照旧：`uv run python tests/demo_web.py`（回复是一小段一小段慢慢发的；加 `--no-key` 看没有密钥时的样子；页面每次刷新都重新读、重新拼
  （`harness/webui/page/` 和 `markdown.js`），不用重启，正式的 `uv run harness --web` 只在启动时拼一次、改了要重启；
  `--script 名字` 让没写暗号的对话也按那段剧本回答（README 的截图用的是 `--script readme`）；发一句含"演示商店"的话会走一遍浏览器，含"演示电脑"的会走一遍操作电脑，用的是假电脑）。真的 Chrome 的测试是
  `tests/test_browser_live.py`（本机没有 Chrome 时跳过）。
- 用户喜欢在演示页面上跟着点来点去、边看边提意见。自动操作要另开一个实例（另一个端口、另一个临时数据目录），
  不要和用户正在看的那个标签页共用，否则两边的操作会互相打乱。做完之后把演示用的服务器都停掉。
- 汇报时把"验证过的"和"没验证过的"分开说清楚。目前没有用真实密钥实测过的部分：GPT 和 Gemini 的对话和附件，GPT、Gemini、DeepSeek 的压缩历史；MCP 只对着 `tests/fake_mcp.py` 测过，没有接过真实的第三方服务器。自定义 Agent 的委派和角色对话只对着假服务器测过（包括子 Agent 用另一家模型）。聊天通道（Telegram、Discord、Slack、飞书、钉钉、QQ）只对着本地的假服务器测过（装了 SDK 时，飞书的事件分发器和真的钉钉 Stream 客户端也对着假服务器跑过），没有接过真的机器人；journald 和 systemd 只是模拟过。真的 Chrome 只在 macOS 上测过。后台服务只对着假的 launchctl / systemctl 测过，没在真机上装过（macOS 的隐私拦截、登录自启、崩溃后重启，Linux 的用户服务和 linger 都没实测）。沙箱：macOS 的 sandbox-exec 在本机实测过；Linux 的 bwrap 只在 CI 的 ubuntu 上测过；Windows 没有沙箱（有意的）。操作整台电脑只对着假电脑和 Xvfb 测过，macOS 的截图、输入、窗口列表都没在真机上跑过。评测只做过演习，没有真的跑过（起步任务在真模型上的分数、估算准不准都没核对）。发文件（send_file）在各个平台上都只对着假服务器测过；钉钉的 photoURL 填 media_id、QQ 只收公网网址都是照文档推断的。
  2026-11-20 用真实密钥测过：Claude 读截图、PDF、文本附件；DeepSeek 的对话（带文本附件）。
  2026-11-21 用真实密钥测过（claude-opus-5-5，共约 0.36 美元）：联网搜索带引用的回答（一整段、cite 事件的编号和来源）；
  浏览器工具包（navigate、get_page_text，API 认我们交回的 toolset_name）；带思考和工具调用的一轮"重新生成"（API 认倒回后的历史）。
  另外用户自己在 Telegram 里用过：截屏、文件工具看图片。
  2026-11-21 用户装上后台服务（macOS launchd，给智狐用的 Python 授了"完整磁盘访问权限"和"屏幕与系统录音"）以后，
  在 Telegram 里让它截屏并用 send_file 发回聊天，收到了：后台服务读"文稿"、截屏，和 Telegram 的 sendPhoto 都实际走通了。
  没授权之前后台服务连 `.venv/pyvenv.cfg` 都读不了（Operation not permitted），所以项目在"文稿"里时必须授权。
