# CLAUDE.md

这个文件写给在本仓库里工作的 Claude Code，每次会话开始时会自动读取。请按这里的规则做事。

## 用户定下的规则

1. **用中文交流。** 回复、代码注释、文档、提交说明都用中文。用户在通过这个项目学习 agent 和 harness，
   解释概念时要说清楚"为什么"，不要只给结论。
   例外是 GitHub 上给外人看的部分（用户原话："github项目描述用英文来写吧，英文为主，中文为辅"）：
   `README.md` 是英文的项目主页，`README.zh-CN.md` 是中文的主页，两份都只是概览和目录；详细的文档按主题分在
   `docs/en/`（英文）和 `docs/zh-CN/`（中文）下，两边是同样的几页、文件名一样（2026-11-23 从两份几千行的 README 拆出来的）。
   **两份 README 和 docs/ 下中英两份对应的页面，内容要一致。** 每一页开头是换语言的一行（链到另一种语言的同一页）、
   回 README 的一行和标题；`tests/test_docs.py` 查每个相对链接和 `#锚点`（照 GitHub 的规矩算）都找得到、两边页面一样、
   每页互相链着、README 的目录列着每一页。挪了一节、改了标题，别处指向它的链接要跟着改（测试会找出来）。
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
   需要确认的步骤、深色模式都要考虑到。功能做完要有测试，docs/ 下中英两份对应的页面都要同步更新、内容一致
   （概览变了的话还有两份 README）。
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
     - **鼠标按着的时候别把列表整个换掉**：按下和松开落在同一个元素上才算一次点击，取回新数据重画（`replaceChildren`）正好落在中间，
       这一下就丢了（换模型的面板在慢的电脑上真的丢过）。会自己重画的列表，鼠标正按在里面就先记下、松开以后再画：取回新数据以后的重画一律经过 `hold-redraw.js` 的 `holdRedraws(区域)`（换模型、扩展 · 钩子、扩展 · 通道在用；钩子页、通道页在 GitHub 的机器上也丢过：钩子页打开时取了两次列表，通道页刚接上时状态接连变了几次）。
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
   - 写给模型看的（系统提示词、工具说明、工具结果）保持中文；系统提示词和工具说明不进翻译表。
     工具结果里程序自己写的话（出错的原因、"（没有输出）""[退出码 1]"这类说明）也显示在工具卡片上、终端里，所以用
     `harness/i18n.py` 的 `zh()` 写：交出去的就是中文原文（模型、历史里和直接写中文一个字节都不差），同时和 `t()` 一样记着原文和值，
     界面用 `retell()` 译成界面的语言，网页的 `tool_result` 事件带着 key、values 在原地重译（规矩写在 i18n.py 开头）。
     命令的输出、文件、网页、MCP 的结果是资料，只作为值原样填进去，从不查翻译表。`agent._shown` 只在交给模型的正好就是那一句时才带原文
     （遮过密钥、钩子补过话、截短了的照原样显示：原文里的值是没遮的样子）。zh() 的句子一样要有译文（`tests/test_i18n.py` 会查）。
     手里是一句 `t()` 译好的话（TaskError、Skill 的问题、平台的错误）却要交给模型时，填进 zh() 或用 `chinese()` 换回中文原文，
     不要直接拼进中文句子（界面是英文时模型会看到英文）；几段话接起来用 `joined()`。
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
     Python 的模块也一样（`app_*.py`、`browser/session_*.py`、`capabilities_*.py`、`restore_*.py`、`tasks_*.py`、`webui/server_*.py`、
     `commands/rules_*.py`）：
     原来的文件留作入口，开头写清由哪几块拼成，别处照旧从它 import（写在 `__all__` 里）；拆出去的文件只 import 前面的、不 import 入口
     （不然就成了互相导入，先导入哪一个就看运气），所以入口里留的是最上面那一层。一个类太大就拆成混进来的基类，加一个只给 mypy 看的
     `*_types.py`；拆出去的文件只为类型注解要入口里的东西时，放进 `if TYPE_CHECKING:`（`commands/rules_*.py` 的 `Context`）。
     测试换名字（`monkeypatch.setattr`）要换在读它的函数所在的那个模块上：函数搬了家，换入口上的名字它看不到。
     只搬家的那一次要证明一字不差（每个定义的原文、注释、每个名字解析到的东西），再另外提交改注释。
   - 同一条规则只写在一处：价格、时间、格式检查这类由助手程序算，页面不再复制一份（页面要用就请程序算好发过来）。
   - 每个功能都有测试；修 bug 先写一个能复现它的测试。
   - 有意破例的地方（比如压缩会改动前面的历史）在代码注释和本文件里都写明为什么、边界在哪。
   - 能调的数（上限、阈值、时间）集中在 `config.py` 或设置里，不散落在各处写死。

9. **macOS、Windows、Linux 三个系统都要做好。**（用户原话："无论是 mac 还是 windows 还是 linux 都要进行对应的优化和操作"；
   "要针对 windows 用户友好，快捷键啊，指令啊 还有其他的"。）每加一个功能、改一处界面，都要想三个系统上各是什么样，不能只在 Mac 上好用：
   - **快捷键和说法**：Mac 写 ⌘、⌥，Windows 和 Linux 写 Ctrl、Alt（页面里看 `ON_MAC`，不要写死 ⌘）；"在访达中显示"在 Windows 上是
     "在文件资源管理器中显示"，Linux 上是"在文件管理器中显示"（由助手程序告诉页面是哪个系统）。
   - **命令和文档**：README 和 docs/ 的页面里的命令三个系统都给（zsh/bash 和 PowerShell 各一份，比如 `zhifox` 的 alias 和
     PowerShell 里的 function），装 uv、加快捷命令、看日志的写法都要有 Windows 的版本；Windows 上不一样的地方另外汇总在
     `docs/en/windows.md`、`docs/zh-CN/windows.md`。
   - **系统相关的代码只放在一处**：文件锁、路径检查（盘符、反斜杠、设备名、末尾的点和空格这些 Windows 的花招）、执行命令用哪个 shell、
     后台服务（launchd / systemd / 任务计划程序）、"在文件管理器里显示"、文件权限（600 和 Windows 的 ACL）、启动 Chrome 的管道，
     都各有一个小模块按系统分开写，别处只调用它。新加的功能要用到这类东西时，复用它们，不要自己判断 `sys.platform`。
     这些模块是：`filelock`（文件锁：fcntl / msvcrt）、`pathnames`（Windows 的路径花招、比较用的 `key`、目录联接）、`winacl`（icacls 设 ACL）、
     `winjob`（作业对象：停下时连孙进程一起结束）、`console`（管道输出用 UTF-8；Windows 上按下 Ctrl-C 的那一刻，`on_ctrl_c`）、`exits`（SIGTERM 之外还有 SIGBREAK）、`machine`（mac / windows / linux）、
     `tools/shell`（`command_line`、`kill_tree`、`system32`、`NO_WINDOW`）、`browser/chrome`（`_launch_windows`）、`service/windows` 加 `service/supervise`、
     `webui/reveal`（在文件管理器里显示）；页面是 `platform.js`：`forKeys` / `commandHeld` 看浏览器所在的系统（快捷键），`byServer` / `showLabel` 看助手程序所在的电脑
     （"访达""文件资源管理器""文件管理器"）。页面里不要再写"`ON_MAC` 就写访达"这种判断。
   - **Windows 上的几条规矩**（2026-11-22 合并 Windows 支持时定下的）：
     - 命令用 PowerShell（优先 pwsh 7），`-EncodedCommand` 传进去，`-NoProfile`，不改执行策略；环境里照样去掉 `SECRET_ENV_VARS`。
     - "只读命令不用问"和你自己加的命令在 Windows 上一律不算（`commands.WINDOWS`）：拆词和名单是照 bash 写的，看不准 PowerShell。测试里 conftest 把它设成 False。
     - Chrome 只走管道（`--remote-debugging-io-pipes`，只让它继承那两个句柄）加作业对象，不开端口：端口本机任何程序都连得上。
     - 替换文件被拒绝（旧文件正被杀毒软件、索引服务、另一个线程开着）时 `safe.replace` 隔一会儿再试几次（`WINDOWS_REPLACE_TRIES`），只在 Windows 上；
       测试里读演示数据目录的文件用 `steps.saved_json`（服务器正在替换的那一刻读也会被拒绝一下）。
     - 文件按字节原样写（`safe.write_file` 用 O_BINARY 和 `newline=""`），文件工具保留原来的换行，页面的编辑器比较内容用 `sameText`（记事本的 \r\n 不算改过）。
       **有意的破例**：`tools/memory.py` 在 Windows 上替换了 Anthropic SDK 记忆工具里的 os（`_BinaryOs`），为的是不改换行。
     - 文件权限靠 `winacl`（去掉继承，只留你的 SID、SYSTEM、Administrators）；设不成功只写警告、不拦着用。
     - 服务器拒绝 POST 之前先读掉（不大的）请求体：Windows 上关掉还有没读完数据的连接会发 RST，对方连 403 都收不到。
     - 测试里查文件权限用 `tests/modes.has_mode`，开子进程用 `tests/processes`（不要用 `os.kill(pid, 0)`：它在 Windows 上会发 Ctrl-C），
       真要执行的命令用 `tests/oscmd`。测试用的真 Chrome 要这个账号真正的 USERPROFILE（换了它就不回答），所以只在别处换家目录。
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
  测试里 conftest 给每个测试设好了临时的 `HARNESS_DATA_DIR` 和 `HARNESS_ENV_FILE`（`load_config()` 默认就是仓库里真正的 `data/` 和 `.env`），
  要看"默认在哪儿"的测试自己 `delenv`，只算路径、不写东西。
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
uv run --with mypy==1.19.0 mypy  # 类型检查（配置在 pyproject.toml）；加 --platform win32 / linux 换成那个系统的眼光查，CI 三个都查
uv run harness                   # 终端界面
uv run harness --web             # 网页界面
uv run harness --version         # 智狐的版本（从仓库里运行时带着提交）
```

## 发布新版本

发布是公开的，推出去的 tag、GitHub Release（以后还有 PyPI）别人马上就能装，**用户说了"发"才发**，上一次的同意不能沿用到下一次
（2026-11-22 用户让发了第一个版本 0.2.0）。做法（`docs/en/testing.md` 和 `docs/zh-CN/testing.md` 的"Releases / 发布"一节写给别人看的是同一套）：

1. 版本号只写在 `pyproject.toml`，只在发布时改：`uv version 0.3.0`（顺手改 `uv.lock`），程序里读它的只有 `harness/version.py`。
   `CHANGELOG.md` 的 `[Unreleased]` 改成 `## [0.3.0] - 日期`，上面另留一个空的 `## [Unreleased]`；文件末尾加
   `[0.3.0]: https://github.com/Akxan/zhifox-agent-harness/releases/tag/v0.3.0`，`[Unreleased]` 的对比链接改成从 v0.3.0 起。
   `docs/en/installation.md` 和 `docs/zh-CN/installation.md` 里写着版本号的安装地址（"装某一个发布版 / Installing a released version"
   那一段，和"检查新版本"那张表里的例子）一起改。
   `tests/test_release.py` 会查 pyproject 的版本号在 CHANGELOG 里有没有写着日期的一节和链接。
2. 提交、推送，等这个提交在 main 上的 CI 全绿。
3. `git tag -a v0.3.0 -m "v0.3.0"`，`git push origin v0.3.0`。`.github/workflows/release.yml` 接着做：对一遍 tag、版本号和 CHANGELOG
   （`tests/release_notes.py`，同时写出发布说明：英文为主，附一句中文），在干净的检出里打包，三个系统上各试装一次
   （`tests/smoke_release.py`：harness/ 下 git 记着的文件一个不少、命令能启动），再建 GitHub Release、挂上 wheel 和源码包。
   平常每次推送，tests.yml 的 package 那几项也打包、试装一遍，所以第 2 步全绿时这一步一般不会出意外。推完照样稀疏地看结果。哪一步红了：修好、合进 main 以后删掉 tag（`git push origin :refs/tags/v0.3.0`、`git tag -d v0.3.0`）
   重新打；Release 已经建好的，工作流再跑只换安装包。
4. PyPI 那一项要用户自己设置，之前一直显示为跳过：在 pypi.org 加一个"待定的受信任的发布者"（项目名 harness-assistant、
   Owner Akxan、仓库 zhifox-agent-harness、工作流 release.yml、环境 pypi），在仓库的 Settings → Environments 建 `pypi`，
   Settings → Variables 加 `PYPI_PUBLISH=true`。设好以后在 Actions 里把这一版那次运行 "Re-run all jobs"（30 天以内）就能补发。
   我们不碰 PyPI 的账号和令牌。
5. release.yml 里的 action 都钉在 40 位的提交编号上，后面注释写版本（`tests/test_release.py` 会查）：这个工作流能改 Release、
   能拿发布凭据，tag 是可以被挪走的。能拿到 `id-token: write` 的只有 pypi 那一项，它不签出代码、不运行仓库里的任何东西。

## 这个项目是什么

一个手写的 agent harness：运行在用户自己电脑上的个人助手，有终端和网页两种界面，支持 Claude、GPT、Gemini、
DeepSeek。它同时是教学项目，所以没有用任何 agent 框架，每个文件只讲一件事，注释写的是"为什么"。
`README.md`（英文）和 `README.zh-CN.md`（中文）是概览：智狐是什么、亮点、快速开始，和一节列着全部页面的"Documentation / 文档"。
详细的说明按主题分在 `docs/en/` 和 `docs/zh-CN/` 下（两边是同样的几页、文件名一样），改了行为就要把中英两份对应的页面一起更新。
哪个主题在哪一页，看 README 的那一节目录；要加一个新主题，两边各加一页、在两份 README 的目录里各加一行。
每次推送都会由 GitHub Actions 在 Ubuntu、macOS 和 Windows 上跑测试（`.github/workflows/tests.yml`），推送后看一眼结果。

**给人看的名字是"智狐"，英文名 ZhiFox，标志是一只小狐狸**（都是用户定的）。GitHub 仓库名是
`Akxan/zhifox-agent-harness`（2026-11-20 用户选定改名）。界面里在任何语言下都写"智狐"；助手用中文以外的语言回答时称自己 ZhiFox
（写在系统提示词里）。名字只有一处定义：`harness/config.py` 里的 `APP_NAME` 和 `APP_NAME_LATIN`，页面里另有几处写死的文字和内嵌的 SVG 标志（`page/index.html` 里的 `#i-fox`）。包名、命令（`uv run harness`）、
请求头和浏览器里保存设置用的键名仍然叫 harness，不要跟着改。

| 位置 | 内容 |
|---|---|
| `harness/agent.py` | 循环。不出现任何和具体厂商、具体界面有关的代码 |
| `harness/parallel.py` | 一步里连着的只读调用一起跑：哪些能（规矩写在开头）、线程池、每个调用一份 context、停下时取消没开始的；挑哪些在 `Agent._batch_at` |
| `harness/providers/` | 各家模型的适配层：`claude.py` 用 Anthropic 原生接口，`openai_compat.py` 供另外三家共用，`compaction.py` 是这三家的压缩历史，`inflight.py` 是"停止"时断开正在进行的那一次请求 |
| `harness/tools/` | 工具。`base.py` 里的 `function_tool` 用来创建自定义工具 |
| `harness/permissions.py` | 哪些操作要先问用户 |
| `harness/conversations.py` | 会话的保存和读取 |
| `harness/search.py`、`harness/search_pinyin.py`、`harness/search_pinyin.txt`、`tests/make_pinyin.py` | 会话的全文搜索：内存里的索引，五档匹配（原样、相近的词、隔字、全拼、首字母）；拼音的两档在 `search_pinyin.py`，读音表由 `make_pinyin.py` 从 Unihan 生成一次（Unicode License v3） |
| `harness/attachments.py`、`harness/documents.py` | 附件的检查；从 Word、Excel、PowerPoint 里取出文字 |
| `harness/tasks.py`、`harness/tasks_*.py`、`harness/webui/scheduler.py`、`harness/tools/tasks.py` | 定时任务：任务是什么、什么时候跑、存在哪（不碰线程和网页：`tasks.py` 只有说明和交出去的名字，`tasks_check` 格式检查、`tasks_schedule` 按日历算时间、`tasks_records` 一次运行的记录和提醒、`tasks_store` 的 TaskStore；别处照旧 `from .tasks import …`）；网页界面里的调度、排队、执行（scheduler.py）；模型用的工具 |
| `harness/memories.py` | 网页"记忆"窗口背后的部分：列出、查看、修改（内容和名字）、删除记忆文件，浏览器送来的路径出不了记忆文件夹 |
| `harness/prompt.py` | 系统提示词：内置的模板、用户改过的版本（`data/prompt.md`）、占位符；"能联网"那一行只给有联网搜索的那一家 |
| `harness/ledger.py`、`harness/budget.py`、`harness/export.py` | 花费账本（data/ledger.jsonl）、每月预算的提醒规则、导出全部数据（遮住密钥；恢复的白名单 `PARTS` 和写给程序看的 manifest 也在这里） |
| `harness/restore.py`、`restore_*.py`、`restore_cli.py`、`harness/webui/restore_api.py` | 从备份恢复：检查压缩包（restore_check.py）、预览两种方式（restore_plan.py）、先自动备份再恢复（restore.py，别处照旧 `from .restore import …`）；占位符不盖掉现在的密钥（restore_keys.py）；终端的 `harness restore`；网页的上传、确认和原地重新开起来（restore_api.py），页面在 `page/scripts/restore.js` |
| `harness/webui/markdown.js` | 回复里的 Markdown → 页面元素；逐字输出时只重画最后一两块 |
| `harness/mcp/` | MCP 客户端：jsonrpc、stdio、http、client（两代协议）、bridge、manager（快照）、config、importers |
| `harness/media.py` | 多媒体库：附件原件按内容存在 `data/media/`；哪段会话、哪条消息里有哪些附件；旧会话从对话历史里找回附件 |
| `harness/skills/` | Skill：SKILL.md 的解析和检查、安装（新建、上传、网址、从 Claude Code 导入）、交给模型的列表、`skill` 工具 |
| `harness/agents/`、`harness/tools/agent.py` | 自定义 Agent：同一种文件两种用法——主助手交给它的子 Agent（`runner.py`），和用它开始对话的角色（`App.new_conversation(agent)`） |
| `harness/catalog.py` | Skill 和 Agent 共用的"附在用户消息里的目录"（`<skills>`、`<agents>` 块） |
| `harness/channels/` | 聊天通道（Telegram、Discord、Slack、飞书、钉钉、QQ）：base（Channel、PushChannel）、platforms + guides（平台名单、要填的东西、建机器人的步骤，网页向导和 `harness channels setup` 共用）、store（channels.json，凭据在 `credentials` 里）、hub（FACTORIES、CHECKS、CLASSES 三张表）、router、outbox（发送队列：再试、留着补发）、session、ui、markup、gateway（Discord 和 QQ 共用的网关）、各平台一个文件、cli |
| `harness/net/` | 手写的 WebSocket 客户端（RFC 6455）、JSON-over-HTTPS 帮手和上传用的 multipart 编码 |
| `harness/tools/send.py`、`harness/channels/outbound.py` | 把文件发回聊天里：工具（看一眼、问不问、发之前再读一遍）、平台收什么（`plan`）和发到哪个聊天（`Outlet`） |
| `harness/serve.py`、`harness/log.py`、`harness/webui/lifecycle.py` | 无界面模式 `harness serve`（服务器上用，跑定时任务和聊天通道）、适合 journald 的日志、调度器和通道统一的启动和停止 |
| `harness/service/`、`harness/instance.py` | 后台服务：plan（服务怎么运行）、launchd（plist + launchctl）、systemd（用户 unit + systemctl）、windows（任务计划程序）和 supervise（自己的看护进程：任务计划程序失败重启时不看退出码）、logs、cli（`harness service …`）；数据目录的锁和"是谁占着" |
| `harness/commands/` | "只读的命令不用问"：手写的命令行切词（不用 shlex）、选项、路径检查、每个程序的规则、你自己加的命令（`data/commands.json`）。规则的写法（`Context`、`Program`）和总表 `RULES` 在 `rules.py`，各类程序自己看一遍的函数按种类在 `rules_system`、`rules_files`、`rules_search`、`rules_text`、`rules_subcommands`、`rules_wrappers` |
| `harness/capabilities.py`、`harness/capabilities_*.py`、`harness/webui/capabilities_api.py` | 信息栏"智狐能做的事"和终端 `/tools` 的功能清单：工具对应到功能、状态和原因、问不问你、在哪儿执行、怎么打开。`capabilities.py` 是入口（`overview`），`capabilities_kinds`（格式、`KINDS`、`group_of`、`label_of`）、`capabilities_approval`（问不问你）、`capabilities_context`（`_Context`）、`capabilities_builtin` / `capabilities_switched` / `capabilities_mcp`（每一种功能的状态） |
| `harness/webui/manifest.py`、`harness/webui/icons/`、`tests/make_icons.py` | 网页应用的清单和图标；PNG 由 `make_icons.py` 用无界面的 Chrome 从 `docs/assets/logo.svg` 画一次再提交（改了标志就重画） |
| `harness/model_catalog.py`、`harness/webui/models_api.py` | 模型目录：每一家有没有密钥、能选的模型、能做什么、价格、换过去会怎样（continue / fresh / leave）；网页的换模型面板（`/api/model/catalog`）和终端的 `/model` 都只照着画 |
| `harness/imports/` | 从其他工具导入：登记表（`registry.py`：哪个工具把 MCP / Skill / Agent 放在哪儿，注明出自哪份文档）、各种格式的读法（readers、yaml_lite、mcp_shapes、agent_formats、agent_tools、refs）、总览（overview）；`mcp/importers.py`、`skills/from_tools.py`、`agents/from_tools.py` 照着它找 |
| `harness/evals/`、`evals/tasks/` | 评测：任务格式（spec）、临时目录（seed）、记录员（recorder）、按结果打分（graders）、评分员（judge，花钱）、演习（dryrun，用 tests/ 的假服务器）、跑（runner）、估算（estimate）、结果和对比；起步任务在仓库根目录的 `evals/tasks/` |
| `harness/trace/` | 运行记录：每一轮的时间线（请求模型、工具调用、等你确认），页面在 `page/scripts/trace.js`、`trace-chart.js`；终端 `/trace` |
| `harness/endpoints/`、`harness/webui/endpoints_api.py` | 自定义模型接口：地址规矩、`data/endpoints.json`、不带别家密钥的客户端、猜能力、测试连接 |
| `harness/undo/`、`harness/webui/changes_api.py` | 撤回一轮改的文件：留底（`data/backups/turns/`）、撤回和重做、冲突检查、告诉模型；页面在 `page/scripts/file-changes.js` |
| `harness/fork.py`、`harness/webui/fork_api.py` | 从某一轮分叉成一段新对话：截到哪、复制什么、什么不跟过来（fork.py，不碰网页）；`/api/fork` 和状态里的 `fork`（fork_api.py）；页面在 `page/scripts/fork.js` |
| `harness/sandbox/`、`harness/webui/sandbox_api.py` | 命令的沙箱（可选，默认关）：policy（三档、哪些路径）、session、macos（sandbox-exec）、linux（bwrap）、windows（为什么没有）、backend（唯一看系统的地方）、cli（`harness sandbox`） |
| `harness/hooks/`、`harness/webui/hooks_api.py` | 你的钩子：config（`data/hooks.json`）、runner（启动、沙箱、超时）、protocol（退出码和 JSON）、engine（每次运行的 HookRun）、cli（`harness hooks`） |
| `harness/redact.py` | 遮住工具结果里像密钥的东西：各家密钥的格式（`KEY_SHAPES`，导出和 MCP 设置页也用它）、已知的值、`password=` 后面的长串；页面在 `page/scripts/redact.js` |
| `harness/fallback.py`、`harness/webui/fallback_api.py` | 备用模型：模型暂时用不了时，这一轮接下来改用同一家的哪个（`candidates`、`usable`、`use`）；页面在 `page/scripts/fallback.js` |
| `harness/voice/`、`harness/webui/voice_api.py` | 语音转文字：设置（settings）、选哪一种服务和发出去（transcribe）、聊天通道收到语音时（listen）；页面在 `page/scripts/voice.js` |
| `harness/claims.py` | 一段会话同一时间只让一边写：聊天通道正在用的会话，网页上不能发消息、打开、改名、删除 |
| `harness/fetch/`、`harness/tools/fetch.py` | 读网页（`web_fetch`）：给自己不能联网的模型的只读小工具。guard（哪些地址不去、网址怎么理解）、client（解析、检查、连到查过的地址、自己跟跳转、上限）、charset、readable（HTML → 文字）、content（按类型挑读法）、session（允许过的网站、每条消息几次） |
| `harness/browser/`、`harness/tools/browser.py`、`harness/webui/browser_api.py` | 浏览器：智狐自己启动的 Chrome（`data/browser/profile`），只走 `--remote-debugging-pipe`，不开端口；手写的 CDP 客户端、读页面、截图。一次运行的 `BrowserSession` 在 `session.py`（状态、run / facts / stop / close、收件箱、和 Chrome 的连接、每个动作的收尾），动作按小节以基类混进来：`session_tabs`（标签页）、`session_page`（调用 page.js、等待、截图）、`session_look`（打开、读页面）、`session_input`（鼠标、键盘）、`session_facts`（动作之前先看一眼），类型见 `session_types.py` 的 `SessionPart`；别处照旧 `from .session import BrowserSession, Facts, Outcome, Target` |
| `harness/desktop/`、`harness/tools/computer.py`、`harness/webui/computer_api.py` | 操作整台电脑（进阶选项，默认关着）：截图和缩放、macOS（screencapture + CoreGraphics）和 X11（xdotool）的后端、每次运行的会话、屏幕同一时间只给一个运行 |
| `harness/doctor/` | 自检（`harness doctor [--json]`、终端 `/doctor`）：report（三种状态和 JSON 的格式）、fixes（按 zsh / bash / PowerShell 写的修法）、environment（环境、目录的权限、占用）、keys（密钥、只看设置的网络）、features（浏览器、沙箱、操作整台电脑、后台服务）、extensions（MCP、通道、钩子、定时任务）、text（表格）、cli。只看不改、不联网、不写出密钥（连最后几位也不写），判断照着各自原来的那一处；Windows 的 ACL 用 `winacl.others`（`icacls /save` 读 SDDL）|
| `harness/filelock.py`、`pathnames.py`、`winacl.py`、`winjob.py`、`console.py`、`exits.py`、`machine.py` | 系统之间的差别（见第 9 条）：文件锁、路径花招、Windows 的 ACL 和作业对象、输出编码和 Windows 的 Ctrl-C、退出信号、这是哪个系统 |
| `harness/i18n.py`、`harness/locales/` | 多语言：`t()`、写给模型又要给人看的 `zh()`、按现在的语言重译的 `retell()`，和英文、西班牙语的翻译表（键是中文原文） |
| `harness/webui/server.py`、`webapp.py`、`webapp_types.py`、`*_api.py`、`reveal.py` | 网页界面背后的本地服务器。`server.py` 只管 HTTP 这一层：路由表（`Handler._route`）、回答和安全头（`_send`）、发文件字节的三个接口和导出、启动和退出（开头有全部接口的目录）；Handler 的另外几块以基类混进来：`server_gate.py`（口令、Host、Origin 的检查，读请求体）、`server_page.py`（页面的 nonce 和 CSP、清单和图标）、`server_events.py`（事件流），类型见 `server_types.py` 的 `HandlerPart`。`webapp.py` 是 WebApp 的核心：共用的状态和锁、存盘、这段会话归谁、状态快照。`sessions.py` 是几段对话同时进行的名单（Session、Sessions、AllIdle，锁的规矩写在开头），`sessions_api.py` 是新建一段、换着看、只停一段、一轮收尾。每一组功能一个 `*_api.py`：`XxxActions` 混进 WebApp（方法名照旧是 `web.open_conversation`），继承 `webapp_types.WebAppPart`（只给 mypy 看的 Protocol，运行时是空的：列出别的文件里定义、这一组要用的状态和方法签名；mypy 报"没有这个属性"就在那里补一行，签名和实现对不上会报 override，写了没实现会报 abstract），`route_xxx(handler, web, …)` 由路由表调用；较早的几个（`skills_api.py` 这些）是 WebApp 上的对象，`route` 交回（状态码，回答）。加接口：路由表里加一行，做法写进那一组的 `*_api.py`。`reveal.py` 是唯一启动文件管理器的地方，别处写 `reveal.reveal_in_file_manager(...)`、调用时现找；测试换它要换 `harness.webui.reveal` 上的名字（`tests/test_webui.py` 会查别处没有第二份） |
| `harness/chat_cli.py`、`harness/history_cli.py` | 终端里的会话：一轮一轮地跑、每一轮存盘、`/retry` `/edit` 倒回、`/undo`、`/new` `/open` 换一段（chat_cli.py）；`/history` 和 `/open` 写出来的那几行（history_cli.py，只管怎么写） |
| `harness/ui.py`、`harness/webui/` | 终端界面、网页界面（`server.py` 等见上一行；`page/` 是页面：骨架 `index.html`、`styles/`、`scripts/`，由 `page/__init__.py` 按清单拼成一页，启动时拼一次） |
| `harness/app.py`、`harness/app_*.py` | 组装：`build_app`、`run_turn`；App 的字段（全部状态）和你正看着的那一段在 `app.py`，每一组方法一个文件、以基类混进 App（和 WebApp 一样）：`app_chat`（Chat 装着什么、怎么建）、`app_conversations`（会话：新建、打开、分叉、存盘、改名、删除）、`app_tools`（一段对话的工具、`toolbox`）、`app_models`（换模型、提示词、适配层、密钥、自定义接口）、`app_prices`（价格和花费）、`app_runs`（定时任务和聊天通道的一次运行、`UnattendedRun`），类型见 `app_types.py` 的 `AppPart`。别处照旧 `from harness.app import …` |
| `harness/cli.py`、`config.py`、`settings.py`、`pricing.py` | 入口、配置、用户设置、计价 |
| `harness/version.py`、`.github/workflows/release.yml`、`tests/release_notes.py`、`tests/smoke_release.py` | 版本号（只写在 pyproject.toml，`harness --version` 和 doctor 都从 `version.py` 拿）和发布（见上面的"发布新版本"） |
| `harness/install.py` | 两种运行方式：从仓库里运行（git clone + uv sync），还是装成了工具（uv tool install）；装成工具时数据和 `.env` 的默认位置、说给用户听的命令 |
| `harness/updates.py`、`harness/updates_cli.py`、`harness/webui/updates_api.py` | 检查新版本（默认关着）：问 GitHub、比版本、缓存（`data/updates.json`）、后台线程、按装法给的升级命令（updates.py，不碰网页）；`harness updates`（updates_cli.py）；设置里那一节的接口和状态（updates_api.py），页面在 `page/scripts/updates.js` |
| `README.md`、`README.zh-CN.md`、`docs/en/`、`docs/zh-CN/`、`tests/test_docs.py` | 文档：两份 README 是概览和目录（"Documentation / 文档"一节按阅读顺序列着每一页），详细的按主题分页，中英两边文件名一样；每页开头换语言、回 README。`test_docs.py` 不联网地查相对链接、`#锚点`、两边页面一样、代码块关没关上。已经装好的 systemd 服务里写着 `仓库主页#run-it-in-the-background`（`service/systemd.py` 的 `DOCUMENTATION`），README 的目录里为它留着一个同名的 `<a name>` |

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
- **从某一轮分叉（`harness/fork.py`）是另存一个前缀，不是改中间。** 原会话只读：从磁盘读（`ConversationStore.load` 拿着会话库的锁，
  读不到写了一半的），不认领、一个字节都不写，所以聊天通道正占着的会话也能分叉。新会话的 `messages` 是原来那段切在某一轮开头的前缀，
  `dropped` 原样带过去（事件里每一轮的 mark 照样对得上，新会话里照样能倒回最后一轮），之后照常只追加；provider、model、agent 不变。
  切口只用 `user` 事件上的 mark：在消息上分叉（at=message）切在这一轮自己的 mark，在回答上分叉（at=reply）切在下一轮的 mark，
  最后一轮是整段。位置认不认只由 `providers.saved_before` 说了算（和 `Backend.turn_at` 同一笔账，各家的格式写在各自的适配层里）：
  落在压缩进摘要的那一段的、没记 mark 的一律拒绝，不猜。`events` 截到同一个位置；`file_changes`、`file_undo` 不复制
  （`fork.LEFT_OUT`：留底按一轮的编号存、磁盘上的文件只有一份，两段会话都能撤回同一轮就会对不上，所以撤回只在原会话里做，
  `forked_from.changes` 让新会话顶上说一句）；`task`、`channel` 不带（新会话是普通的网页会话，没有 send_file）。
  分叉要换会话，所以和打开会话一样（见"你的几段对话同时进行"）：拿 `_switch`、试一下正看着的那一段的锁；它在跑就留在后台，
  分叉出来的另建一段给你看（`fork_conversation(..., chat=)`）。读的是磁盘上存好的样子，所以正在跑的那一轮（还没存盘）分不了。
  页面只给会话编号、那一轮 user 事件的编号和 at，对不上就 409。
  以后新加"跟着一轮走、但不能在两段会话里各管一份"的事件，加进 `fork.LEFT_OUT`。
- **历史里只放普通的字典和字符串**，这样才能原样存成 JSON 文件、读回来原样发给 API。
- **每个工具调用都必须有对应的结果**，包括被用户中断的情况。相关逻辑在 `Agent._run_tools` 的 `finally` 里。
- **"停止"马上生效（`harness/providers/inflight.py`，2026-11-24）**：以前只立一面旗子，模型在想、在写长长的工具参数、请求还在排队时
  服务器什么都不发，要等它再开口才停得下来。现在循环每一步把 `backend.step` 包在 `inflight.calling()` 里，并在这一步的时间里把
  `Call.abort` 登记到 `ui.on_stop`（WebUI 登记时已经点过停止的马上调用，"刚要开始"的那一下也不漏）。断开在发出停止的那个线程里做：
  只 `socket.shutdown`（Windows 上再 close），**不替读的线程关回复**（两个线程一起收拾 httpcore 的连接池会出错）。读的线程醒来抛 `Aborted`：
  它是 KeyboardInterrupt 的一种、不是 Exception，所以 SDK 不重试、循环不试备用模型，收尾和以前"被中断"一样。
  适配层的每一次 HTTP 请求（真正的请求、写摘要的那一次）包在 `inflight.current().request()` 里，拿到 SDK 的流以后
  `claim(network_stream(stream.response))` 再 `check()`；以后新加向模型发请求的地方照这样办。新建 SDK 客户端的地方给它的 httpx 客户端装
  `inflight.event_hooks()`（`make_client`、`endpoints/client.make` 已经装了；没装的客户端回答的头没到时断不开，头一到马上断）。
  头没到、复用了一条空闲连接时 trace 不说是哪一条，断开这个客户端上没人认领的连接：一个适配层一个客户端、同一时间一个请求，所以正好是它。
  trace 的钩子在 `response_closed` 和不是 Exception 的失败（GeneratorExit）上不抛，否则 httpcore 来不及把连接从池子里拿掉。
  被停下的那一步不记账（用量在回复的最后才报告，宁可不记也不猜）。终端在 macOS、Linux 上靠信号本身；Windows 上 `TerminalUI.on_stop`
  真的登记（`console.on_ctrl_c`：SetConsoleCtrlHandler，系统在另一个线程里调用它）。测试用 `tests/held.py` 让假服务器压着请求不回答。
- **一步里只有"只读、又根本不用问"的调用才一起跑**（`harness/parallel.py`，判断在 `Agent._batch_at`）。三样都要：工具自己写了
  `Tool.parallel`（opt-in，默认 None：新加的工具一律一个一个来；现在只有文件和记忆的 `view`、`todo_list`、`task_list`、`skill`）、
  `_gate`（`Tool.check` + `permissions.review`，和一个一个跑时是同一段）说不用问、`HookRun.looks_before` 说没有 PreToolUse 钩子要看它。
  这一档放行的（`allowance` 不是 None）也单独跑：`auto_allowed()` 的记录要在执行之前。连着的至少两个才成一批，别的照旧单独、按顺序；
  确认永远不和别的调用同时出现。执行命令、浏览器、电脑、MCP、子 Agent、send_file、会写的永远不写 `parallel`：它们有自己的 Shell、
  会话或屏幕，或者看不见副作用。线程里只跑 `Agent._execute`（每个调用一份 `contextvars.copy_context()`，撤回的记账才看得到）；
  界面事件、记录点、钩子、遮密钥、放进结果都回到这一轮的线程、按调用的顺序做。**事件顺序不变**（tool_call、结果、tool_call、结果）：
  页面按"结果跟在它的卡片后面"配对、`rewind._executed` 也靠这个顺序，所以不要改成谁先做完谁先发。运行记录里同一批标着 `batch`，
  合计按它们一共占的时间算（`recorder._covered`）。上限是 `config.MAX_PARALLEL_TOOLS`（`Config.max_parallel_tools`，1 就是关掉）。
  给一个工具加 `parallel` 之前确认它线程安全、不碰共享的会话状态，并且 `rewind.changes` 认为它没改东西（`tests/test_parallel_tools.py` 会查）。
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
  `/api/mcp/reveal`、`/api/commands/reveal`（什么都不给，只显示 `data/commands.json`）、`/api/restore/reveal`（只认 `data/restore-backups/` 里已有的、名字是 `restore.BEFORE` 那个样子的文件，或者什么都不给：打开那个文件夹）、`/api/browser/reveal`（只打开浏览器的配置文件夹）、`/api/computer/permission`（只认 `screen`、`input` 两个词，只打开 macOS 系统设置的那一页，不改任何设置）、`/api/skill/reveal`（只认 data/skills/ 里已有的、不是链接的一段文件夹名，或者什么都不给）。
  `/api/media/reveal` 只接受附件编号（`会话编号/第几排/第几张`，用 `media.ID` 检查格式、在索引里查到条目），
  路径是 `data/media/<sha256>.<扩展名>`，由服务器算。
  **只有 `/api/media/file`、`/api/browser/shot` 和 `/api/computer/shot` 会把文件的字节发给浏览器**（都写在 `server.py` 的 `_route` 里，和 `_send` 在一处）。两个 shot 只收 64 位十六进制编号，固定 `image/jpeg`，
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
  事件流读的是你正看着的那一段的日志（`Sessions.watching`）。换着看另一段时（`Sessions.view`，拿着 `_publishing`），先发一个
  **不带编号**的 `{"type":"reset"}`（那一段的日志本来就从 reset 开始的就不补），再从头补发那一段的日志，之后实时的接着来；
  状态只发给正看着的那一段，换走的那一段的日志里的状态当场清掉（`forget_states`），换回来时"重放"不会停在一张旧状态上。
  几段的事件编号从同一个数往下排（`ui.EventIds`），每段记着它从哪一号起被看着（`Session.since`）：重连时报上来的 Last-Event-ID
  比它小（或者比谁都新），就当作换着看，从头补发。读日志和换过去在同一把锁里做，所以浏览器不会拿着上一段的编号被当成"这一段收到这里了"。
  事件流每一圈现找 `server.web`（从备份恢复会换掉 WebApp）。
- **系统通知只给实时的事**（`page/scripts/notify.js`）：确认卡片、一轮结束、出错的提示，只有 `isLive()`、页面在后台（`document.hidden`）、
  设置里开着（`harness.notify`，记在浏览器里）、浏览器允许了、同一个智狐的别的标签页没被看着（BroadcastChannel）时才弹；刷新、打开旧会话、
  倒回一轮时重放出来的一律不弹。弹通知只用 `systemNotify`，问浏览器要权限只用 `askNotify`（只在 click 里调，浏览器只认用户点过以后的询问），
  别处不再 `new Notification`、`requestPermission`。标签页的标题只经过 `setPageTitle`（后台时前面加标记），不要直接写 `document.title`；
  图标上的点是把 `<link rel="icon">` 换成另一个 data: 地址，页面的 CSP 本来就允许 `img-src data:`，不为它放宽任何一项。
  页面测试用 `steps.fake_notifications` / `background` / `notes`（假的 Notification 和 `document.hidden`），不弹真的系统通知。
  后台另一段对话的事（`page/scripts/live.js`）照着状态里 `live` 的变化提醒：刚开始等你确认的弹 urgent 的通知（页面在眼前、你正看着别的会话时
  另有 toast），在后台跑完、出错的弹普通的；刚连上、换着看的那一下收到的状态是"现在的样子"，不算新的。标题和图标走 `noteChatsWaiting`、`noteBackground`。
- **发给浏览器的、写进文件的，都要用 `harness/safe.py`。** 模型的输出和用户粘贴的文字里可能有半个表情
  （落单的代理字符）、NaN 这类非法的数，直接 `json.dumps` 会写出浏览器解析不了的东西，或者干脆编码失败，
  让事件流永久断开。所以：事件和 HTTP 回答用 `to_json`，文件用 `write_file`（先写临时文件、fsync、再替换，
  权限 600），入口处的文字用 `clean_text` / `clean` 洗一遍。
- **"问不问用户"只能放宽，不能绕开。** `Permissions.review` 决定哪些操作需要确认，`auto_allows` 只决定
  当前模式下这一次可不可以不问；没有问就执行的，要调用界面的 `auto_allowed()` 留下记录。
  `.env` 在任何模式下都碰不了；读 `data/` 在任何模式下都要问（"全部自动允许"除外）。
  `readonly`（只读的命令不用问）排在 ask 和 edits 之间：只放开 `commands.classify` 判为只读的命令、你在 `data/commands.json`
  里加的命令（只在这一档、只在单独一条时算）、读工作目录之外的文件。它不包括改文件，edits 也不包括只读命令。判断必须保守：
  看不懂的一律不算；改 `commands/rules.py`、`commands/rules_*.py` 之前先看 `tests/test_commands.py`，新发现的绕过先加一行会失败的测试。
  `commands/words.py` 认的写法只多了两样（2026-11-23，真跑评测时三家模型都撞上的）：`;`、`&&`、`||` 连起来的几段（和管道一样每段各自判断，`cd` 不在名单里所以换目录的一串照样问），和只写到 `/dev/null`、只并到另一路的重定向（`2>/dev/null`、`&>/dev/null`、`2>&1`、`>&2`）。之后又加了通配符（同一次评测里三家都写 `du -sk */`）：`words.py` 只把没加引号的 `*`、`?`、`[ab]`、`[!.]` 记成 `Glob`，`commands/globs.py` 照 bash 在命令运行的目录里展开，展开出来的每一个路径照原来的规则查（对不上的原样、`.*` 补上 `.` 和 `..`、超过 `READONLY_GLOB_MAX` 就问；区间、`[^…]`、字符类、命令名里的通配符、你加的命令里的通配符都不认）。写文件的重定向、单独的 `&` 照旧不认，再放宽要有同样的证据。
  `auto_allowed(why)` 带着为什么（readonly / listed / mode）。几档的名字和说明只写在 `page/scripts/approval-modes.js`，
  对话、任务、通道三处都照它画。`data/commands.json` 在文件工具的 protected 名单里，模型没有任何能改它的工具。
- **你的几段对话、定时任务、聊天通道同时进行**（`harness/webui/sessions.py`、`sessions_api.py`）。
  - 网页上一段"活着的"对话是一个 `Session`：`chat`（`app_chat.py` 的 `Chat`：Agent 和适配层、会话、自己的 `Shell`、浏览器和电脑的 session、
    MCP 的快照和调用记在谁名下 `owner`）、它自己的事件日志 `ui`（WebUI）、一把锁 `lock`、`running`、存盘失败的原因 `unsaved`。
    `App.chat` 是你正看着的那一段，`App.agent`、`conversation`、`shell`、`chat_browser`、`chat_desktop` 都是它的：读它们的代码不用管有几段。
    **会在后台接着跑的代码**（`chat_api._run_turn` 和它调用的收尾）只用 Session 自己的东西，调用 App 的方法时传 `chat=`，
    不要用 `App.agent`、`self.ui`：跑到一半你换去看别的，它们就是别的那一段的了。终端界面只有一个 Chat。
  - 你正看着的那一段在跑时，打开别的会话、开新对话、分叉：它留在名单里（后台）接着跑，新建一段（`_fresh_session` = `app.new_chat`
    加它自己的日志）给你看；它没在跑就直接换掉它的内容（还是同一个 Session，和以前一样）。打开一段正活着的会话 = 换过去看它（`_show_live`）。
    后台的一段跑完、存好盘就从名单里拿掉（`Sessions.finish`），会话栏上留"跑完了""出错了"，打开时去掉；没存上盘的、第一条消息就没答出来的留着。
    同一段会话最多一个 Session；同时在跑最多 `config.MAX_PARALLEL_CHATS` 段（`Sessions.start` 在锁里数，重新生成也算），超了回答原因，页面上也不发。
  - **锁的顺序**（只能从前往后拿）：`WebApp._switch`（RLock：改动"哪几段活着、正看着哪一段"的操作一个一个来）→ `_publishing`
    （算状态快照；换"正看着哪一段"也拿着它）→ `Sessions._lock`（只保护名单和几个字段）→ 每段 WebUI 自己的锁。`_switch` 用 `sessions.take_switch` 拿：
    最多等 `config.SWITCH_WAIT_SECONDS`（连着点"新对话""打开"时上一下还在收尾，不该回答"请先等"），只在网页请求的入口（手里什么锁都没拿）这样等；
    聊天通道存盘以后的回调照旧只试一下。各段的 `Session.lock` 一律只"试一下"
    （`_holding`、`_take`），拿着 `_switch` 不会卡在别人身上。丢掉一段要先拿着它的锁；拿到一段的锁以后要再看它还在不在名单里。
  - **谁拿什么**：发消息、重新生成拿正看着的那一段的锁（一直拿到这一轮收尾，在跑它的线程里放）；撤回文件、换模型、填密钥、改提示词、
    改自定义接口拿 `_switch` 加正看着那一段的锁（`_viewed_idle`：拿着时谁也换不走"正看着哪一段"，`self.app.agent` 就是它的）；
    新对话、打开、分叉、改名、删除拿 `_switch`，再拿要动的那一段的锁（在跑的那一段不能改名、删除；别的会话随时能改）。
    **`WebApp._turn` 不再是"那一把对话锁"**，而是 `AllIdle`：拿着 `_switch` 和每一段的锁，"没有任何一段在跑、也开始不了"。
    全局的操作拿它：改记忆、改 Skill（原来拿 `_turn` 是因为助手这一轮可能正在用它）、从备份恢复（拿不到用 `_busy_everywhere()` 说是哪几段在跑）。
    拿着它的线程再拿某一段的锁（`_holding`）算拿到了。以后新加的全局操作照这样办；只关系到一段的，拿那一段的。
  - **每段各一份、不能串的**：确认卡片（编号全局唯一，`/api/confirm` 只答到正在等它的那一段，`/api/computer/trust` 只放开那一段的 DesktopSession）、
    "这段对话里截图 / 点击打字不再问"、浏览器允许过的网站、"停止"（`/api/stop` 带 `conversation` 停那一段，不带停正看着的；只停它自己的
    shell、MCP 调用、浏览器和电脑的动作、钩子）、撤回文件的记账（`undo/journal.py` 的 ContextVar 挂在每一轮自己的线程上）、运行记录、钩子的 HookRun、
    多媒体库里还没存盘的附件（`_live_sources`，清理原件时也算"在用"）、聊天通道的认领（每段在自己那一轮里 `Claims.take(id, "web")`）。
    **共用一份的**：权限（`App.permissions`：对话里选的那一档是全局的，几段都按它）、遮密钥、备用模型、账本（source 照旧是 chat）、
    屏幕（`DesktopManager.claim`：两段都要操作电脑时后到的被拒绝）。一轮结束时先 `chat.desktop.release()`、放开认领，再 `Sessions.finish`、
    发状态、`end_turn`、放开锁（`sessions_api._settle`：认领要在拿掉之前放开，"结束了"和放开锁之间不做别的）。
  - 几个标签页看的是服务器上同一段（正看着的那一段），和以前一样；刷新页面补发的也是它，会话栏的标记从状态里的 `live` 来。
  - 退出时 `lifecycle.stop`：`stop_all`（每段都停、各自的命令都结束）、`wait_idle`（等每段收尾存盘）、`save_all`（没存上的再存一次）。
  - **定时任务不受影响**：它们本来就不拿这些锁。凡是"会被停下"的东西，每次运行各有一份（`App.unattended_agent` 返回的 `UnattendedRun`）：
    自己的 `Shell`、自己名下的 MCP 调用（`mcp.snapshot(owner)` / `cancel_running(owner)`）、自己的 `RunUI`、适配层和权限。停任务走
    `/api/task/stop`，只停那一次；退出时 `cancel_all()`、`Scheduler.wait_idle`。
  - 共用的只有磁盘上的东西，每样都有锁：`TodoStore`、`TaskStore`、`ConversationStore`、`tools/files.py` 的 `_EDITING`、
    `tools/memory.py` 的 `_LOCK`、`Ledger`、`MediaIndex`、`SkillStore`。新加会被几段对话和任务同时用到的共享状态，也要加锁，
    并写一个去掉锁就会失败的并发测试（`tests/test_parallel_chats.py` 的上限检查就是这样）。定时任务同时最多跑 `config.MAX_PARALLEL_TASKS` 个，
    多的排队；同一个任务不会同时跑两次。提醒不调用模型，不排队。
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
  加一条"压缩包里找不到这个密钥"的测试。`export.LEFT_OUT_FOLDERS` 里的文件夹整个不导出（`browser/profile`：里面是登录过的网站的 cookie；
  `restore-backups`：恢复之前自动留的备份，带进去会越套越大）。
- **从备份恢复（`harness/restore.py`）只认白名单，密钥不覆盖，恢复前先备份。**
  - 白名单只有一份：`export.PARTS`，和导出写在一起。新功能在数据文件夹里放了新的文件或文件夹，导出不用改，但要在 `PARTS` 里加一行，
    否则它的备份恢复不了（`tests/test_restore.py` 拿 `Config` 里的每个数据路径对一遍）。名单外的名字、路径花招（`pathnames.archive_trick`，
    Skill 的安装也用它）、链接和设备文件、加密的项、只差大小写的两项、太大的（`config.RESTORE_*`；写的大小还要和实际读到的对一遍）
    一律整个拒绝、一个字节都不写。`.env` 这类、`browser/profile/`、`.web-token`、`.web-address`、`.lock` 永远不收；
    `README-backup.txt`、`backup-manifest.json`（格式写在 `export.manifest` / `read_manifest`）只读不写。
  - 密钥不覆盖：`export.MASKED_FILES` 里的文件，备份里的占位符按"同一处"去找现在的值（`restore_keys.py`：字典按键、带 id/name 的列表按它、
    参数列表要除了密钥一模一样），有就留着现在的，没有才写占位符、告诉用户去哪儿填（`restore.KEY_HOMES`）；现在的读不懂的不动，
    备份里没有的存着密钥的文件覆盖时也留着。
  - 恢复前先备份：`restore.backup_first` 把现在的数据导出一份到 `data/restore-backups/`（和导出一样遮密钥，留 `RESTORE_KEEP_BACKUPS` 份），
    备份不成就不恢复。覆盖先在 `.restore-xxxx/` 里摆好、一样一样换上去，出错换回原样，确认新程序开起来了才 `commit` 删掉旧的；
    合并只加（会话、记忆、Agent、多媒体按文件，Skill 按文件夹），整份的文件现在有就留着现在的。写文件一律 `safe.write_stream`。
  - 网页上拿 `WebApp._turn`（没有一段对话在跑；拿不到用 `_busy_everywhere()`，说是哪几段在跑），定时任务在跑、聊天通道占着会话（`Claims.held`）就拒绝。
    恢复以后照着磁盘 `build_app` 一遍、换上一个新的 WebApp（`server.web`；口令、端口、你正看着的那一段的 WebUI 不变，新的上面只有这一段；
    旧的那个的 `_turn` 永远不放）。**有意的破例**：WebApp 不再是
    启动时那一个，所以拿着它的地方要用 `server.web` 现找（`serve.py` 的收尾就是这样）；测试和演示换过的东西（client_factory、
    聊天平台的 factories / checks、headless_only）在 `restore_api._rebuild` 里带过去，再加这类东西也要带。
  - 上传是原样的字节（不是 JSON），在 server.py 的 do_POST 里先于 `_read_json` 交给 `restore_api.upload`：边读边写进系统的临时文件，
    上限 `RESTORE_MAX_UPLOAD_BYTES`，超了读掉最多 `RESTORE_DISCARD_BYTES` 再回 413。终端的 `harness restore` 先占住数据目录的锁（kind `restore`），
    没有终端又没有 `--yes` 不恢复。
- **聊天通道和定时任务是同一个做法。** 每个聊天的每一轮用 `App.channel_agent`（和 `unattended_agent` 共用 `_side_run`：
  自己的 shell、自己名下的 MCP 调用、`Permissions.unattended(通道的档位)`，记账 source=`channel`），不拿网页上的那几把锁（`_turn`、`_switch`、各段的锁）；
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
  平台的错误要如实填上 `ChannelError.status`（HTTP 状态码；连不上、超时、连接被重置这类根本没得到回答的填 0，`NetError.status` 原样交过去）、
  `retry_after`，平台自己说"太快了"的错误编号设 `busy`；"再试有没有用"只在 `base.retry_delay` 判断，平台里不另写。
  `settle` 这类改消息的：暂时的失败原样抛出（`base.transient`），改成了才从记着的消息里拿掉，改不了的一直的失败才另发一条。
  平台里被限流时等的那一下用 `base.pause`，不用 `time.sleep`（退出时要能马上醒过来）。
- **发到聊天里的东西不悄悄丢掉**（`channels/outbox.py`，2026-11-23 加的：真实日志里 Telegram 的连接每十分钟左右被重置一次，落在里面的回答就丢了）。
  暂时的失败（没得到回答、5xx、429、平台的"太快了"）等一会儿再试（`CHANNEL_SEND_RETRIES` 次，间隔从 `CHANNEL_SEND_RETRY_SECONDS` 翻倍，
  `Retry-After` 最多听 `CHANNEL_SEND_RETRY_AFTER_MAX` 秒），一直的失败（别的 4xx、token 不对）不再试。试完了还是不行的回答、确认请求、
  确认的结果按聊天、按顺序留在内存里（每个聊天最多 `CHANNEL_UNDELIVERED_MAX` 条），通道变回在线（hub 的 `_status_of` → `router.status_changed`）、
  这个聊天又来了消息、通道一直在线时每 `CHANNEL_UNDELIVERED_RETRY_SECONDS` 秒按顺序补发；这个聊天之后的都排在它们后面。
  没发到的确认请求在 `Waiting.delivery` 上记一笔，页面上写明、网页上照样能答。页面那一行（`delivery`）和 `/status` 写着还有几条、最后一次失败；
  `/status` 的回答不排在留着的后面（outbox 的 `status` 一种）。send_file 再试但不留着：结果如实交给模型。`/stop` 不等重试，退出时（`Outbox.close`）
  不等退避，每样再试一次，还是不行的写进日志。**有意的取舍**：请求到了平台、只是回答丢了的，再试会让聊天里多一条一样的；
  拆成几条发的长回答中间失败，前面几条会再发一遍。宁可偶尔重复，也不丢回答。新加"要发到聊天里的东西"，在 `outbox.KINDS` 里写明再不再试、留不留着。
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
  终端界面也占它（kind `terminal`，见下面"终端也存会话"），所以"智狐在不在跑定时任务和聊天通道"要用 `serving`（不算终端），不要直接用 `in_use`；
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
  一轮结束时先 `chat.desktop.release()`（每一段自己的那个）再 `end_turn()`：让出屏幕会刷新状态，顺序反过来，页面马上发来的请求会撞上还没放开的锁。
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
  路径由程序照着清单算；只撤你正看着的那一段的，拿 `_switch` 和它的锁（`_viewed_idle`）、认领这段会话；查和写都拿 `_EDITING`。保存密钥的文件、留底文件夹、换成链接的路径永远不写；
  工作目录之外、数据目录里、哈希对不上的，先问再写。模型没有撤回的工具。撤回之后在下一条消息前附 `<file_changes>`（`undo/notes.py`），不进系统提示词。
  **有意的破例**：倒回最后一轮时，这一轮之后才撤回的更早几轮的 `file_undo` 事件不跟着截掉，挪到最后一条用户消息前面（`webui/ui.kept_after_rewind`）。
  命令改动的文件不假装能撤：界面上明说撤不回来；不替用户的仓库做 git 快照。
- **命令记着当前目录（`harness/tools/cwd.py`），问你和运行看的是同一个目录。** 每个 Shell 有一个 `CurrentDir`（`Shell.current`）：
  命令外面包一层（bash 的 `trap … EXIT`、PowerShell 的 `try/finally`），结束时打出"记号 + 目录"，`take` 取出来、从输出里去掉；
  超时、停止、没打出记号的留在原来的目录。卡片上的"在这个目录里运行"、只读的判断（`Places.cwd`，不在工作目录时目录本身也 `check(".")`：
  不带路径的 `ls` 看的就是它）、沙箱的 `--chdir`、真正运行，一律用 `Shell.where()`：记着的目录不在了、沙箱里看不到（`Sandbox.reaches`）
  就当场回到工作目录、结果里说一声；问过你以后才变了的不执行（`CurrentDir.gone`）。以后新加"照着命令在哪儿运行来判断"的地方也用 `where()`，
  不要拿 `workspace`。只给了名字、认不出是哪个 Shell 的按工作目录算（真正执行的 Agent 一定会把工具交过来）。
  做什么要看所在文件夹里的设置的命令只在工作目录里才不问（2026-11-24 安全复查：答应一条 cd 以后，它们就能在别人的项目里不问就运行那里的代码）：
  你加的命令和查版本（`VERSIONS`）看 `Places.at_workspace`，git 看 `Places.same_repository`（和 `-C` 不许是同一个道理）；
  `Places.workspace` 由 `Permissions.places` 填。以后再加这类程序（会读所在文件夹里的设置、设置里能运行命令的），也这样办。
  回到工作目录的地方：新对话第一条消息之前（`App._fresh_start`，各个"新对话"的入口不用各写一遍）、重新打开和分叉（`App._continue`，
  历史里执行过命令的第一条命令后面说一声"之前 cd 到的不再算数"）、聊天通道接着一段旧会话（`channels/session.py`）；子 Agent 自己的 Shell
  从工作目录开始。每一轮开始 `start_turn`（网页 `_run_turn`、终端 `ask_turn`），倒回最后一轮 `rewind_turn`（`rewind_api.rewind`、`cli.rewind_last`）。
  改包的那一层要同时守住两种 shell 的退出码（PowerShell："最后一条不成功是 1"由 finally 里记下的 `$?` 和最后的 `exit 1` 照旧成立）：
  `tests/test_shell_cwd.py` 在装了 pwsh 的 macOS、Linux 上也跑 PowerShell 那一层。
- **命令的沙箱是第二道关卡，不替代"问不问"。** 每次运行（对话、每次任务、每个聊天）一个 `Sandbox`，子 Agent 用叫它的那一方的同一个。
  路径只在 `policy.build` 里算；SBPL 里的路径一律用 `-D` 参数，配置的文字里不出现路径；一组路径是空的就不写那条规则（空的 allow 等于全放开）。
  `data/`、`.env`、`config.protected_files` 永远碰不到；智狐自己的程序在沙箱里只读。选了一档、这台电脑上却用不了：`tool.check` 直接拒绝，
  绝不悄悄在沙箱之外运行。`unsandboxed: true` 在任何模式下都问；没人看着的运行（任务、通道）模式是 all 时直接拒绝。说明书里一直带着
  `unsandboxed`，不随设置变。测试默认用 `tests/fake_sandbox.py`；真的沙箱只在 `test_sandbox_macos.py`、`test_sandbox_linux.py`。
  `/api/sandbox/reveal` 只认设置里"额外允许的路径"列表里已有的一条。
- **写测试别赌时机**：GitHub 的 macOS、Windows 机器比本机慢得多。等条件成立（`until`、`page.wait`）不写固定的 sleep；不要"赶在 0.3 秒的动画里
  去看它在不在播"，而是记下调用过没有；换外观以后等过渡播完再量颜色；量耗时的测试连着几次超了才算。推送以后一定看 CI 的结果，红了马上修。
- **你的钩子只能更严，不能放宽。** agent.py 在记录点旁边各一行（UserPromptSubmit 在 `_submit`、PreToolUse 在确认之后、PostToolUse 在
  `results.append` 之后、Stop 在 `turn_ended` 之前），逻辑都在 `hooks/engine.py`，不进记录器。PreToolUse 只能拦下或者 ask，`allow`、改参数都不认；
  补的话只接在正在生成的那条结果、那条消息里。命令用参数列表在工作目录里启动、不经过 shell，环境里去掉 `SECRET_ENV_VARS`，引用它们的保存和运行前都拒绝；
  这次运行的沙箱开着就进同一个沙箱，开着却用不了就不运行。`data/hooks.json` 在 protected 名单和 `export.MASKED_FILES` 里，模型没有能改它的工具；
  只有带口令的页面和 `harness hooks` 能改。定时任务、聊天通道默认也跑，子 Agent 只跑工具前后的两个。测试里的钩子一律用 `sys.executable` 跑 Python 脚本。
  命令行里 `--` 后面的内容由 `cli.main` 先切出来再交给 argparse（Python 3.11 的 argparse 不认子命令里的 `--`）。
- **遮密钥（`harness/redact.py`）**：工具的结果在 `clean_text` 之后、放进结果和交给 `after_tool` 钩子之前遮（`Agent._redact`），钩子、模型、历史、
  页面、运行记录只见遮好的；浏览器标签页的网址、标题也遮。只遮新产生的这一条结果，历史照样只追加。各家密钥的格式只写在 `redact.KEY_SHAPES`，
  导出和 MCP 设置页也用它，别处不再另写。已知的值（`SECRET_ENV_VARS`、`.env`、自定义接口的密钥、`log.hidden()`）按值比较，从不写进日志。
  git 提交号、UUID、base64 不遮，宁漏勿错：这是一张安全网，模型故意改写过的密钥认不出来。阈值是 `config.REDACT_*`，开关是 `Settings.redact_secrets`
  （默认开，关之前在原地问一次）。改文件的工具不肯写回 `‹已遮住：…›`（会把真的密钥盖掉）。新加存密钥的地方，把值交给 `Redactor` 的 `known` 或 `log.hide`。
- **备用模型只在同一家之内换，只换这一轮剩下的步骤**（默认没有，每家最多 3 个）。过载、5xx、429（SDK 重试过以后）、流里的 `overloaded_error` 这类
  抛 `providers.base.Unavailable`；400、401、`insufficient_quota` 不换。能不能换看 `fallback.usable`（`providers.continues` 加上 `Backend.can_switch_to`），
  换上和换回都用 `fallback.use`（`switch_model`）。只改请求里的 `model`，历史、系统提示词、工具说明书一字不动，不剥思考块（API 自己丢掉新模型读不了的），
  `conversation.model` 不变；一轮结束换回你选的那个。不跨家换：历史格式、系统提示词、工具都不一样。运行记录用 `request_failed` 和 `fallback` 标出来，
  钱按实际回答的模型算。新加会建 Agent 的地方，照 `_side_run` 一样接上 `fallbacks` 和 `redact`。
- **语音转文字（`harness/voice/`）**：OpenAI 的密钥只发给 `Config.transcribe_api`（只能用 `OPENAI_BASE_URL` 改，明文的别的电脑上的地址不发）。
  自己填地址的服务不发任何密钥，明文的 http 只许连本机；选自定义接口的，只认本机和内网的，带它自己的密钥（外面的网关每分钟多少钱不知道，不给选）。
  太长（`VOICE_MAX_SECONDS`）、太大（`VOICE_MAX_BYTES`）的在下载之前就拦下。每转一条用 `Ledger.record_audio(source="voice")` 记一笔，
  价格只在 `pricing.transcribe_cost` 里算（按分钟估，有的模型其实按 token 计费）。现在只有 Telegram 收语音，别的平台回答"暂时只有 Telegram 支持"。
- **本机的 Python 是 3.11，CI 在三个系统上各跑 3.11 和 3.13**：只在一个版本上才有的行为（argparse 的 `--`、绕圈的符号链接）本机不一定看得到，
  拿不准时用 `uv run --python 3.13 --isolated pytest …` 在另一个版本上也跑一遍。
- **新代码要过类型检查（mypy）**，三个系统都要过（`--platform win32`、`linux`、`darwin`，CI 的 types 一项）。版本钉在 `mypy==1.19.0`，
  不进项目依赖；用 `uv run --with` 而不是 `uvx`：要在项目自己的环境里跑才看得到 SDK 自带的类型。规矩：
  - 先改成正确的类型（判断 None、窄化、标注、`TYPE_CHECKING` 下的导入、Protocol），不用 `Any`、`cast` 糊过去。
  - 真是 mypy 看不懂的才写 `# type: ignore[错误码]`，同一行写明为什么（配置里开着 ignore-without-code 和 warn_unused_ignores，
    没写错误码的、用不着了的都会报错）；只在某个系统上才需要的写成 `[错误码, unused-ignore]`。
  - 系统相关的代码用 `sys.platform == "win32"` 判断，不用 `os.name`：mypy 只认前者。测试里会换掉的 `WINDOWS` 常量照旧留着，
    管写法、路径、说法；真去调用某个系统才有的东西（msvcrt、fcntl、killpg、posix_spawn、CREATE_NO_WINDOW……）的地方另外写成
    sys.platform 的判断（两者在真机上是一回事，所以行为不变）。creationflags 用 `tools/shell.NO_WINDOW`。
  - t() 给的是 Message（str 的子类）：和普通字符串放在一起时把容器标成 `list[str]`，不要 `str(t(...))`（会丢掉原文，换语言时没法重译）。
  - 整个 `harness` 包都查，没有哪个模块例外（`harness/webui/` 和 `harness/app.py` 以前不查，2026-11-22 收紧）。混进 WebApp 的那几组方法
    用到的 self 上的东西写在 `harness/webui/webapp_types.py` 的 `WebAppPart` 里（为什么、新的一组怎么写，见那个文件开头），
    App 的那几组写在 `harness/app_types.py` 的 `AppPart`，BrowserSession 的写在 `harness/browser/session_types.py` 的 `SessionPart`，
    网页服务器的 Handler 的写在 `harness/webui/server_types.py` 的 `HandlerPart`（标准库 BaseHTTPRequestHandler 的那几样照着 typeshed 抄，原因见那里开头）
    （混进来的方法会整个换掉的属性，要在 Protocol 里写一个值，原因见 `app_types.py` 开头），
    不要在 mixin 里写 `self: Any`、`getattr`，也不要给它们另开 `ignore_errors`。
- **两种运行方式（`harness/install.py`）。** 从仓库里运行还是装成了工具（`uv tool install`）只看 `config.FROM_SOURCE`
  （`install.from_source`：包的上一层有写着 harness-assistant 的 pyproject.toml，而且不叫 site-packages），别处不要再判断。
  从仓库里运行时 `data/` 和 `.env` 在仓库里，一字不变；装成工具时都在 `install.user_dir`（macOS 的 Application Support、
  Linux 的 XDG_DATA_HOME、Windows 的 LOCALAPPDATA），`.env` 在数据目录里，所以挡它的几处（`protected_files`、导出的 secrets、
  沙箱的 deny）一直按路径认，不能改成只认仓库里的那个。`HARNESS_DATA_DIR`、`HARNESS_ENV_FILE` 永远优先、各管各的。
  说给用户听的命令写成 `{command}`、填 `config.COMMAND`（`uv run harness` / `harness`），叫人修好安装的填 `config.REPAIR`，
  不要再写死 `uv run harness`（还没改的：`cli.py` 的 SETUP_HELP 和两句提示、`server.py` 的一句、页面上"后台运行"那一节）。
  运行时读的文件只能放在 `harness/` 包里（wheel 只带它）；用到 `PROJECT_ROOT` 的只有说明过的几处（`tests/test_install_location.py` 会查）。
- **检查新版本默认关着，它是智狐唯一一个自己发出去的请求**（`harness/updates.py`）。打开以后最多每 `UPDATE_CHECK_HOURS` 小时
  （没问成的隔 `UPDATE_RETRY_HOURS`）一个 `GET …/repos/Akxan/zhifox-agent-harness/releases/latest`：不登录、不带 cookie，
  User-Agent 只写 `ZhiFox`（不带版本号、不带系统），GitHub 只看得到 IP 和时间；别的什么都不发。以后不要往这个请求里加任何东西，
  也不要再加别的"自己发出去"的请求；真要加，先问用户、默认关着、在中英两份文档里写明发了什么（检查新版本写在
  `docs/en/installation.md`、`docs/zh-CN/installation.md` 的"Updates / 更新"一节）。
  规矩只在 `updates.py`（版本按数字比、草稿和预发布不算、缓存新不新、每一种装法的升级命令 `upgrade`、发布页和 wheel 的地址照 tag 拼，
  不用回答里的网址）；页面（`webui/updates_api.py`、`page/scripts/updates.js`）、终端、`harness doctor`、`harness updates`（`updates_cli.py`）只照着说。
  后台线程只从 `webui/lifecycle.py` 开和关；终端只在设置开着、缓存过期时在后台问一次，不耽误输入。`harness doctor` 只读缓存，不联网。
  缓存 `data/updates.json` 不导出、不恢复（`export._is_scratch`），删了下次再问。地址只能用环境变量 `HARNESS_UPDATE_API` 改
  （只认 https，明文的 http 只认本机）。测试里 conftest 把 `updates.fetch` 换成只许连本机的，真的 GitHub 一个请求都到不了；
  要测的对着 `tests/fake_github.py`，演示也一样。
- **"智狐能做的事"只有一处判断。** 工具属于哪种功能、为什么关着、问不问你，都在 `capabilities.py` 里算，页面和终端只照着画。
  "问不问你"不另写规矩，而是拿有代表性的调用去问 `permissions`（`Tool.check` → `review` → `allowance`，"哪一档都问"就是换成 `all` 也问，见 `capabilities_approval.py`）。
  新加一种工具，要在 `capabilities_kinds.py` 的 `KINDS` 和 `_GROUP_OF` 里登记，并写它的状态函数（`capabilities_builtin.py` 或 `capabilities_switched.py`，再加进 `overview` 的 builders）；忘了登记的会落到 "other"，照样列出来，不会藏起来。
  "这个工具发不发给这家模型"只看 `Backend.sends`；"这段对话真正有哪些工具"只看 `App.toolbox()`。工具的真名只出现在"技术细节"里，页面测试会查。
- **浏览器的工具和 MCP 一样算进一段对话开始时的快照**：设置里开着才带上，开始以后不变（中途关掉，工具还在，调用时回答"浏览器关着"）；
  用过浏览器的旧会话照样带上。Claude 用 Anthropic 的 `browser_toolset_20260801`（`Tool.toolset` / `member` / `stands_in_for`，
  适配层合成一项），结果写回 `toolset_name`；`browser_state` 不放在出错的结果上；一轮里工具包的一步失败了，后面的结果是
  `agent.HALTED` 那句英文原文（文档规定，不改）。别家的模型用 `browser_*` 替身工具。每次运行（对话、每次任务、每个聊天）
  一个 `BrowserSession`，停止接到 `ui.on_stop` 或 `run.stop`；CDP 的读线程不拿 session 的锁，`manager` 通知页面时不拿着自己的锁。
  问不问写在 `permissions.review_browser`：密码、银行卡、跨站嵌入的框、不是网页的地址在任何模式下都问；
  `file:` `chrome:` `devtools:` `javascript:` `view-source:` 根本不打开；下载一律拒绝；允许过的网站只记在这段对话的 `allowed_sites` 里。
- **读网页（`web_fetch`，`harness/fetch/`）的安全边界是硬规矩，没有开关**（要看本机、局域网的网页用浏览器）：
  - 只认 http、https；网址里带用户名密码的、古怪的数字主机名（`2130706433`、`0x7f.1`，在 idna 换成 ASCII 以后再查）不读。
  - 地址的规矩只在 `fetch/guard.py` 的 `refusal`：自己列的表（IPv4 列不能去的，IPv6 只认 2000::/3 再去掉几段），**不用** `ipaddress` 的
    `is_private` / `is_global`（3.11 和 3.13 的说法不一样）；藏在 IPv6 里的 IPv4 按里面那个判断；一个名字解析出的地址有一个不能去就整个不读。
  - 域名只解析一次、连的就是查过的那个地址（`client.Fetcher._connect`，防 DNS 重绑定），TLS 按域名验证、Host 头写域名；跳转自己跟、每一跳从头再查；
    不走代理（走代理时是代理去解析和连接）。时间靠"看门的"（`_Watchdog`）到点掐断，掐断以后读到的只是一截，一律当超时。
  - 上限都在 `config.FETCH_*`；每条消息几次和 Claude 的联网搜索共用 `HARNESS_MAX_SEARCHES`，"哪一轮"看 `Agent.turns`。
  - 问不问写在 `permissions.review_fetch`：这段对话里第一次读一个网站之前问，卡片上是完整的网址；只有 all 放开（readonly、edits 照样问）；
    允许过的网站记在每次运行自己的 `FetchSession`（不进会话文件，换一段对话从头再问），跳到别的主机不算允许了那个主机；
    跳转只跟到同一个主机和允许过的网站（`Fetcher.get` 的 `follow`，在解析下一跳的域名之前就问），别的网站不跟，告诉模型跳到了哪儿、
    要读得再调用一次，第一次读照样问你（不然允许过的网站上的"开放跳转"能把模型编出来的网址带到一个你从没允许过的网站）。
  - 发给谁只看 `Backend.sends`：它是 Anthropic 服务器上 `web_fetch` 的替身（`stands_in_for="web_fetch"`），Claude 有服务器上那个时不发。
    两个同名，所以工具箱里服务器工具的编号是 `server:名字`（`Tool.key`），`registry.get("web_fetch")` 永远是本地的那个。
  - **测试的接缝**：`App.fetcher`（`Fetcher` 的 `resolve`、`policy`），测试用 `tests/fetch_rig.py` 只放行本地的假网站；设置和模型都碰不到它。
    conftest 把 `fetch.client.system_resolve` 换成一调用就失败的，测试永远不解析真的域名；真的规矩只拿地址测（`tests/test_fetch_guard.py`）。
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
- **终端也存会话**（`harness/chat_cli.py`，2026-11-23 加的）。和网页存的是同一个文件、同一种格式，经过 `App.save_conversation`，所以两边聊的都能在另一边打开、接着聊、倒回最后一轮。
  - 终端也记一份和网页一模一样的事件日志：`TerminalUI.log` 就是一个 `WebUI`，事件长什么样、存之前怎么整理（`export_events`）只写在那一处。循环调用的界面方法，`TerminalUI` 先写屏幕、再交给 `log`；以后给循环的界面加新方法，终端这边也要交给 `log`，不然终端存下的会话在网页上少一块。只有 `info`、`error` 只在一轮进行中（`begin_turn` 到 `end_turn`）才记：斜杠命令也用它们往屏幕上写。只写屏幕、不进会话的用 `hint`（"改了 N 个文件"那一行：会话里记的是 `file_changes` 事件）。屏幕自己收尾用 `_flush`，不碰日志：网页只在循环说"写完了"时才记 `block_end`，多记一个两边就对不上。
  - 每一轮的 `user` 事件带着 `mark`（`begin_turn(..., mark=backend.mark())`），`/retry`、`/edit` 和网页一样从事件日志里找最后一轮（`rewind.last_turn`、`rewind.problem`），打开的旧会话也能倒回。每一轮结束、`/undo`、倒回以后马上存盘；离开（`/new`、`/open`、`/exit`、Ctrl-D）之前没存上的再存一次，还是不行先拦一下、再输入一次同样的命令才放弃。SIGTERM（和 Windows 的 Ctrl+Break）在 `cli.run_terminal` 里接：停下这一轮、存盘、退出。
  - 终端启动时占数据目录的锁（kind `terminal`）：占到了才存盘、才能 `/open`；占不到（网页界面、后台服务开着）照常聊、不存盘（启动时说清楚是谁占着）。**有意不在终端里认领会话（claims.py）**：claims 只管同一个进程里的几方，终端开着时跑聊天通道的那个智狐开不起来，能往会话里写的只有终端自己。
  - `/open` 走 `App.open_conversation`（换模型、角色、MCP 快照的规矩和网页一样）；用的那一家没有密钥的会话不打开（打开会把"现在用的模型"记成它，下次启动终端一看没有密钥就只能退出）。`ConversationStore.save` 不先扫一遍所有会话（`_load`），终端不用 `/history` 就不付这个代价。
- **会话的索引要和文件保持一致。** `ConversationStore` 里保存、改名、删除都要经过 `_remember` 或
  `_index.remove`，否则搜索结果会和磁盘上的内容对不上。`_remember` 也更新多媒体的索引（`MediaIndex`）；附件的"第几排"按 `attachments.from_saved`
  筛过的数，必须和页面重放时一样，否则"定位到会话"会对不上。发给页面的标记位置按 UTF-16 计数。
- **按拼音搜索（`harness/search_pinyin.py`）只往后加，不改原来三档。** 全拼、首字母排在"隔字包含"后面，所以原来找得到的结果一个不少、
  先后不变（`tests/test_search_pinyin.py` 拿关掉拼音的结果对一遍）；和"相近的词"一样只在这个词哪儿都原样找不到时才试。
  不给会话预先存拼音：关键词编成正则（每个音节换成读这个音的所有汉字），每段会话只多记两个布尔值（`Document.han`、`rare`）。
  拼音表 `harness/search_pinyin.txt` 是生成的，**不要手改**：改 `tests/make_pinyin.py`（版本和 SHA-256 钉死在里面），运行它再提交；
  `--check` 核对仓库里那份是不是它生成的。数据来自 Unicode 的 Unihan（kMandarin 定收哪些字，kTGHZ2013、kXHC1983、kHanyuPinlu
  补上多音字的读音；不用 kHanyuPinyin，古音太多），许可证是 Unicode License v3：声明的全文必须留在文件开头，两份 README 的
  "Third-party data / 第三方数据"写着出处。表只在第一次需要拼音时才读（`_table`），收录会话、按中文或原样找得到的词搜索都不读。
- **两家模型的 SDK 用到时才导入。** `anthropic`、`openai` 光导入就要大半秒，只在真正连接模型的地方导入（`providers` 的 `make_client`、`make_backend`、`list_models`，`tools/memory.make_memory_tool`，评测的 `dryrun`、`judge`；适配层 `claude.py`、`openai_compat.py` 和 `endpoints/client.py` 本身就只在连接时才被导入），不要写进 `providers/__init__.py`、`tools/` 这类会被顺路带进来的文件的开头，否则 `harness --help`、`harness service status` 这些用不到模型的命令又要多等大半秒。`tests/test_startup.py` 在干净的子进程里查。
- 改和 Claude API 有关的代码之前，先读一遍 `claude-api` 这个 skill，不要凭记忆写参数。

## 怎么验证

- 测试用真实的 SDK 连本地的假服务器，这样发出去的请求和收到的流式数据走的都是真实代码路径。
- **测试默认并行跑**（pytest-xdist，`pyproject.toml` 的 `addopts = ["-n", "auto"]`），所以写测试时：每个测试自己准备好
  要用的东西（`tmp_path`、端口给 0 让系统挑），不依赖别的测试先跑、不改全局的东西不还原；`parametrize` 的名字要固定
  （内容里带着时刻、随机数的，比如现打的 zip，要给 `ids=`，否则各个进程收集到的测试对不上，整轮跑不起来）；线程里要认出
  "是不是那个线程"时按名字认，不要拿启动后才赋值的变量去比（机器忙时线程可能先跑到）。会抢同一样系统资源的
  （比如 Xvfb 的屏幕编号）在 CI 里单独用 `-n 0` 跑。测试进程里 socketserver 的 `serve_forever` 默认 0.02 秒看一次"该停了没有"
  （conftest 开头换掉了标准库的 0.5 秒：每个测试收尾时停几个假服务器，按默认值五千多个测试加起来白等七百多秒），新写的假服务器照常
  `serve_forever()` 就行，不用自己传 `poll_interval`。
- **CI 分成两种 job 跑测试**（`.github/workflows/tests.yml`，2026-11-24 拆开，为了快）：`test` 跑 `-m "not ui"`，三个系统 × Python 3.11、3.13；
  `ui` 跑页面测试（`-m ui`），三个系统 × 3.13，macOS、Windows 上用 `HARNESS_TEST_SHARD=1/2`、`2/2` 分到两台机器（`tests/shards.py`：按收集的先后轮流分，
  合起来一个不少、一个不重，`tests/test_shards.py` 查）。两种合起来就是全部，所以别在 `tests/ui/` 之外给测试打 ui 标记，也别把非页面测试放进 `tests/ui/`。
  页面测试只用一个 Python 版本，是因为它们测的是页面和浏览器；演示服务器背后的 Python 代码在 `test` 里两个版本都测。
- 改了文档（两份 README、`docs/` 下的页面、CONTRIBUTING、SECURITY）：跑 `uv run pytest tests/test_docs.py`（不联网，一秒以内：
  每个相对链接和 `#锚点` 都找得到，两边的页面一样，每页第一行链到另一种语言的同一页，README 的目录列着每一页，代码块都关上了）。
  外面的网址它不查。中英两份对应的页面改完对照着读一遍，看说的是不是同一件事；表格、代码块在 GitHub 上画出来对不对，提交前看一眼预览。
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
- 汇报时把"验证过的"和"没验证过的"分开说清楚。目前没有用真实密钥实测过的部分：GPT 和 Gemini 的对话和附件，GPT、Gemini 的压缩历史（DeepSeek 的 2026-11-23 实测过：门槛调低，连着压缩两次，第四轮照样答出第一轮说的代号，约 0.0014 美元）；MCP 只对着 `tests/fake_mcp.py` 测过，没有接过真实的第三方服务器。自定义 Agent 的委派和角色对话只对着假服务器测过（包括子 Agent 用另一家模型）。聊天通道（Telegram、Discord、Slack、飞书、钉钉、QQ）只对着本地的假服务器测过（装了 SDK 时，飞书的事件分发器和真的钉钉 Stream 客户端也对着假服务器跑过），没有接过真的机器人；发不出去时的重试和补发只对着假 Telegram（不回答就关掉连接、500、429、403）和假通道测过，没在真的断网、真的平台故障里试过；journald 和 systemd 只是模拟过。真的 Chrome 只在 macOS 上测过。后台服务只对着假的 launchctl / systemctl 测过，没在真机上装过（macOS 的隐私拦截、登录自启、崩溃后重启，Linux 的用户服务和 linger 都没实测）。沙箱：macOS 的 sandbox-exec 在本机实测过；Linux 的 bwrap 只在 CI 的 ubuntu 上测过；Windows 没有沙箱（有意的）。命令记着当前目录：bash 的那一层和 sandbox-exec 里的在本机实测过，PowerShell 的那一层在本机用 pwsh 7.4（macOS）跑过，Windows PowerShell 5.1 和 bwrap 的 --chdir 只在 CI 上验证。操作整台电脑只对着假电脑和 Xvfb 测过，macOS 的截图、输入、窗口列表都没在真机上跑过。评测 2026-11-23 真跑过一遍（Sonnet 5.5 21/22、0.57 美元；Opus 5.5 20/22、1.18 美元；DeepSeek flash 17/21、0.02 美元；结果在 data/evals/）：估算原来高了 3 到 9 倍，config 的 EVAL_ASSUMED_* 已经按实际的调过，跑过一次以后估算照上一次实际花的算；GPT、Gemini 没跑过。发文件（send_file）在各个平台上都只对着假服务器测过；钉钉的 photoURL 填 media_id、QQ 只收公网网址都是照文档推断的。备用模型只对着假服务器测过（真的 Claude 换模型时怎么处理思考块是照文档写的）；语音转文字只对着假的转写接口和假的 Telegram 测过（OpenAI 收不收 Telegram 的 OGG/Opus 不知道）；遮密钥的误判率只用测试表检验过。读网页（`web_fetch`）只对着本地的假网站测过（明文 HTTP），没有读过真的网站，真的 TLS 握手和证书验证、真的 DNS、HTML 的读法在真实网页上好不好都没核对过。终端存会话、`/history`、`/open` 只对着假服务器测过（在同一个进程里，和在 macOS、Linux 上开一个真的 `python -m harness` 发 SIGTERM）；Windows 上真的终端窗口里按 Ctrl+Break 会不会马上收尾没有试过（读管道的 `input()` 收不到，那个测试在 Windows 上跳过）。按拼音搜索准不准（英文词被当成拼音、首字母对上不相干的标题）只用测试里的小例子和合成的 2000 段会话检验过，没在真实的会话库上看过。网页上几段对话同时进行只对着假服务器和无界面的 Chrome 测过（两三段同时跑、换着看、后台确认、停一段、退出），没有真的几段一起调用真模型跑长任务，
几段同时操作电脑也只对着假电脑测过。"停止"马上断开模型请求（inflight.py）只对着本地的假服务器测过（明文 HTTP、本机）：
socket.shutdown 叫醒正在等的线程在本机 macOS 上实测过，Linux、Windows（再加 closesocket）只在 CI 上验证；真的 TLS 连接、走代理时断开、
真的 Anthropic / OpenAI 服务器看到断开以后马上不再计费，都是照文档和协议推断的；Windows 终端里真的按 Ctrl-C（控制台处理函数）没有试过，CI 上只测了处理函数本身和登记、取消。从某一轮分叉只对着假服务器测过（真的 Claude 认不认切开以后的历史和思考签名，是照倒回最后一轮同一个道理推断的；倒回本身用真密钥测过）。从备份恢复只在本机（macOS）和 CI 上测过，备份都是几十 KB 的：几个 GB 的备份（上传的进度、解开要多久、磁盘空间的估算）没有试过，Windows 上换文件夹时撞上被别的程序（杀毒软件、索引）打开的文件会怎样，只是照着"出错就换回原样"写的，没有在真机上试过。Windows 上的一切（PowerShell 执行命令、ACL、作业对象、真的 Chrome 走管道、Ctrl 和"文件资源管理器"的写法）只在 GitHub 的 windows-latest 上验证过，没在真的 Windows 10、11 桌面上手动用过；任务计划程序只对着假的 schtasks 测过，没有真装过。装成工具（`uv tool install`）在本机 macOS 上真装过：本地的 wheel 和 zip（2026-11-22），发布页上 0.2.0 的 wheel 地址和 `git+https://…@v0.2.0`（2026-11-23，发布以后，临时的工具目录）；GitHub 的三个系统上每次推送由 tests.yml 的 package 在干净的环境里装 wheel 和源码包（`uv run --with`，不是 `uv tool install`）。真的 Linux、Windows 桌面上用 `uv tool install` 装、`uv tool upgrade` 从 GitHub 拉新版本，都没实测；PyPI 还没发。检查新版本只对着本地的假 GitHub（`tests/fake_github.py`）测过，没有真的问过 api.github.com（不花钱，但按规矩不在测试里连外网）；看 `direct_url.json` 认装法只用编出来的文件测过，没有拿各种真装出来的对过。
  2026-11-20 用真实密钥测过：Claude 读截图、PDF、文本附件；DeepSeek 的对话（带文本附件）。
  2026-11-21 用真实密钥测过（claude-opus-5-5，共约 0.36 美元）：联网搜索带引用的回答（一整段、cite 事件的编号和来源）；
  浏览器工具包（navigate、get_page_text，API 认我们交回的 toolset_name）；带思考和工具调用的一轮"重新生成"（API 认倒回后的历史）。
  另外用户自己在 Telegram 里用过：截屏、文件工具看图片。
  2026-11-21 用户装上后台服务（macOS launchd，给智狐用的 Python 授了"完整磁盘访问权限"和"屏幕与系统录音"）以后，
  在 Telegram 里让它截屏并用 send_file 发回聊天，收到了：后台服务读"文稿"、截屏，和 Telegram 的 sendPhoto 都实际走通了。
  没授权之前后台服务连 `.venv/pyvenv.cfg` 都读不了（Operation not permitted），所以项目在"文稿"里时必须授权。
