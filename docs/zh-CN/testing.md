[English](../en/testing.md) | **简体中文**

[智狐](../../README.zh-CN.md) › [文档](../../README.zh-CN.md#文档) › 测试

# 测试

测试怎么不花钱地跑（真的 SDK 对着本地的假服务器）、每一部分测了什么，以及怎么发布新版本。

```bash
uv run pytest          # 并行跑：有几个 CPU 核心就开几个进程（pytest-xdist），全部跑完一分钟左右
uv run pytest -n 0     # 一个一个跑，调试、想看 print 的输出时用
uvx ruff@0.17.6 check harness tests                # 代码检查
uv run --with mypy==1.19.0 mypy                    # 类型检查（配置在 pyproject.toml）
uv run --with mypy==1.19.0 mypy --platform win32   # 同上，换成 Windows 的眼光查（还有 linux、darwin）
```

这几条在 zsh、bash 和 PowerShell 里写法一样。两个检查工具都不是项目的依赖：`uvx` 和 `uv run --with` 临时拿一个钉死的版本来用一次。
mypy 要在项目自己的环境里跑（所以是 `uv run --with`，不是 `uvx`），才看得到 Anthropic、OpenAI 的 SDK 自带的类型。
各个系统不一样的代码用 `sys.platform` 判断，mypy 认得这种写法，所以在 Mac 上加 `--platform win32` 就能查只在 Windows 上走的分支，
反过来也一样；CI 里三个系统都查。新写的代码要能通过；`# type: ignore` 必须写明是哪一种错，并在同一行写明为什么。
整个 `harness` 包都查，没有哪个模块例外。网页界面里分在各自文件里的几组方法（`harness/webui/*_api.py` 里的 `XxxActions`，混进 `WebApp`）
都继承 [harness/webui/webapp_types.py](../../harness/webui/webapp_types.py) 里的 `WebAppPart`：一个只给类型检查看的 Protocol，
列出它们用到的共用状态和别的文件里的方法。

测试不需要 API 密钥，也不花钱。默认是并行跑的（[pyproject.toml](../../pyproject.toml) 里的 `addopts`）：每个进程各有自己的
假服务器和临时目录，页面测试在每个进程里各开自己的演示服务器和 Chrome，彼此不共用任何东西。
测试进程里的 socketserver 服务器（假服务器、测试里起的网页服务器）每 0.02 秒看一次"该停了没有"，而不是默认的 0.5 秒
（[tests/conftest.py](../../tests/conftest.py) 开头）：每个测试收尾时要停好几个服务器，按默认值加起来要白等七百多秒。[tests/fake_api.py](../../tests/fake_api.py) 和 [tests/fake_openai.py](../../tests/fake_openai.py)
是两个本地的假服务器，分别按 Claude 和 OpenAI 的格式回放事先写好的回复，这样就能让真正的 SDK 把整个循环跑一遍，
并检查发出去的请求是否正确。它们还能收下一个请求以后先压着不回答（发了头就不出声，或者连头都不发，见 [tests/held.py](../../tests/held.py)），
并看得出智狐有没有把连接断开：[tests/test_stop_now.py](../../tests/test_stop_now.py) 靠它查"停止"从每一个能停下的地方都马上生效。想知道循环在各种情况下的行为（工具出错、用户拒绝、被打断、模型拒绝回答），
可以读 [tests/test_agent.py](../../tests/test_agent.py)。网页界面的测试在 [tests/test_webui.py](../../tests/test_webui.py)，由测试代码扮演浏览器；
应用图标的清单和几张 PNG 在 [tests/test_web_manifest.py](../../tests/test_web_manifest.py)（口令、Host 和 Origin，名字只认固定的名单、碰不到别的文件，
响应头，安全策略，maskable 的狐狸整个在安全区里）；会话的保存和搜索分别在 [tests/test_conversations.py](../../tests/test_conversations.py) 和 [tests/test_search.py](../../tests/test_search.py)，按拼音找在 [tests/test_search_pinyin.py](../../tests/test_search_pinyin.py)（全拼、多音字、排序、2000 段会话上的快慢，以及拼音表的生成脚本：用一小段 Unihan 的样本测，不下载）；GPT、Gemini、DeepSeek 的压缩历史在 [tests/test_compaction.py](../../tests/test_compaction.py)；
花费账本和预算在 [tests/test_ledger.py](../../tests/test_ledger.py)（用固定的钟测 DeepSeek 的高峰、闲时价，文件末尾的半行，
旧明细的合并），导出在 [tests/test_export.py](../../tests/test_export.py)（压缩包里一个密钥都没有），
从备份恢复在 [tests/test_restore.py](../../tests/test_restore.py)、[tests/test_restore_cli.py](../../tests/test_restore_cli.py)、
[tests/test_restore_web.py](../../tests/test_restore_web.py)（导出再恢复一圈、合并时撞上的、各种做坏的压缩包都拒绝且什么都没写、
占位符不盖掉现在的密钥、自动备份里没有密钥、在原地重新开起来），
它们在网页上的接口在 [tests/test_spending_web.py](../../tests/test_spending_web.py)。
运行记录在 [tests/test_trace.py](../../tests/test_trace.py)（用一块假的表，每一段的开始、结束、首字时间、等你多久都按毫秒比对；钱和账本里的那一笔一样；
允许、拒绝、没问就放行；子 Agent 挂在那一步下面；停下、出错时还开着的那一段怎么收尾；有没有运行记录，发出去的请求一个字节都不差；
存进会话、重新打开、重新生成；定时任务和聊天通道；终端的 `/trace`；钩子的那几段），页面上的瀑布图在 [tests/ui/test_ui_trace.py](../../tests/ui/test_ui_trace.py)。
钩子的测试在 [tests/test_hooks.py](../../tests/test_hooks.py)：钩子都是临时目录里的一小段 Python 脚本，用 `sys.executable` 运行，
所以同样的测试在 Windows 上也能跑。测的是 `data/hooks.json` 的检查（不经过 shell、不许引用模型的密钥、时限）、环境里没有密钥、
在工作目录里跑、标准输入里的 JSON、每一种退出码和 JSON 回答、超时、输出很多也不卡住、"停止"只结束这次运行的钩子，
以及每个事件在真实的循环里的样子（确认之后才拦、再问你一次、`allow` 从不省掉一次确认、补的话只接在新的那条工具结果后面、
被拦下的消息一个请求都不发）、定时任务、聊天通道和子 Agent、运行记录里的那几段、文件工具碰不了它、导出时遮住；
页面和命令行在 [tests/test_hooks_web.py](../../tests/test_hooks_web.py) 和 [tests/ui/test_ui_hooks.py](../../tests/ui/test_ui_hooks.py)。
模型目录在 [tests/test_model_catalog.py](../../tests/test_model_catalog.py)（页面拿到的 JSON：密钥只有最后 4 位、标签、自己填的和内置的价格、闲时价；
换模型的接口和目录对得上：同一家接着聊、换一家开始新的一段而旧的那段还在、聊天通道占着这段会话时不许换、对话进行中目录照样能看但不能换、
角色不变；还有终端的 `/model`）。
定时任务的测试在 [tests/test_tasks.py](../../tests/test_tasks.py)（时间表，包括月底和夏令时；保存；工具和它们的权限）和
[tests/test_task_runs.py](../../tests/test_task_runs.py)（通过真实的服务器运行任务，用的是假的钟，所以没有哪个测试要等到早上 9 点：
任务和你的对话同时进行、两个任务一起跑、超过上限的排队、"停止"只停它自己那一次、四档确认方式；假 API 按每次运行的指令分开回答，
所以几个任务一起跑时结果也是确定的）。
MCP 的测试在 [tests/test_mcp.py](../../tests/test_mcp.py) 和 [tests/test_mcp_web.py](../../tests/test_mcp_web.py)，对着的是
[tests/fake_mcp.py](../../tests/fake_mcp.py)：一个两代协议都会说、stdio 和 HTTP 都能用的假 MCP 服务器。
自定义模型接口对着的是 `fake_openai.py`，可以装成 Ollama、LM Studio，或者没有模型列表、不认工具、不认 `stream_options` 的服务器：
[tests/test_endpoints.py](../../tests/test_endpoints.py)（地址的规矩、表单的检查、存储、猜能力、测试连接、不跟重定向、导出时遮住密钥）、
[tests/test_endpoints_chat.py](../../tests/test_endpoints_chat.py)（完整的一轮和工具调用、不认工具或 `stream_options` 时退一步、
别家的密钥和请求头一个都不带过去、按模型的上下文和价格、换模型的规矩、换模型的目录和"智狐能做的事"）、
[tests/test_endpoints_web.py](../../tests/test_endpoints_web.py)（网页的接口、"不改"的密钥不会跟着地址跑到别处、发给浏览器的东西里没有密钥）。
多媒体库的测试在 [tests/test_media.py](../../tests/test_media.py)（原件的存储和去重；从 Claude 和 OpenAI 两种格式的旧历史里找回附件；
筛选和翻页；拼不出路径的编号；只在没有会话再用时才删原件）和 `tests/test_webui.py` 里多媒体的那一节（接口和响应头）。
编辑和重新生成的测试在 [tests/test_rewind.py](../../tests/test_rewind.py)（两种格式都测：新请求的开头和原来一字不差、没有落单的工具结果、
之前的思考内容原样不变、重启以后重新打开、压缩、哪些事算"改动了东西"）、[tests/test_rewind_web.py](../../tests/test_rewind_web.py)
（接口和它拒绝的情况、先确认、两个标签页里倒回以后的事件流、会话文件、搜索和多媒体库）和
[tests/test_rewind_cli.py](../../tests/test_rewind_cli.py)（`/retry` 和 `/edit`）。
终端存会话、打开会话的测试在 [tests/test_terminal_conversations.py](../../tests/test_terminal_conversations.py)：终端的一轮存下的文件和搜索的索引、
网页打开终端的会话接着聊和重新生成（反过来终端打开网页的会话）、`/history`、换一家模型的 `/open`、`/open` 以后的 `/retry` 和 `/edit`、
存盘失败、在输入时和一轮进行中收到 SIGTERM（在同一个进程里，以及在 macOS、Linux 上对着真的 `python -m harness`），还有数据目录的锁。
撤回改动的测试在 [tests/test_undo.py](../../tests/test_undo.py) 和 [tests/test_undo_diff.py](../../tests/test_undo_diff.py)（临时的工作目录：留底、
各种改法、撤回和重做、按哈希认出冲突、不能碰的路径和链接、几道上限、存盘重开以后、不该记账的线程）、
[tests/test_undo_web.py](../../tests/test_undo_web.py)（接口、事件和会话文件、告诉模型的那段话、倒回时一起撤回）、
[tests/test_undo_cli.py](../../tests/test_undo_cli.py)（`/undo`）和 [tests/ui/test_ui_file_changes.py](../../tests/ui/test_ui_file_changes.py)
（三种语言、窄屏、深色下的页面）。
聊天通道的测试对着每个平台各一个本地的假服务器，测试代码扮演你的手机：[tests/fake_telegram.py](../../tests/fake_telegram.py)
（像 Telegram 一样检查每条消息的 HTML 和长度的假 Bot API，也能发来语音和音频文件）、[tests/fake_transcribe.py](../../tests/fake_transcribe.py)
（OpenAI 的转写接口：检查表单的字段、文件名和密钥，能回答任何一种错误；语音转文字的测试在 [tests/test_voice.py](../../tests/test_voice.py)，从不连真的接口）、[tests/fake_discord.py](../../tests/fake_discord.py) 和
[tests/fake_qq.py](../../tests/fake_qq.py)（REST 加一个网关：能让你重连、让会话失效、不回心跳、用任意代码关掉连接）、
[tests/fake_slack.py](../../tests/fake_slack.py)（Web API 加 Socket Mode，没收到回执的信封会再推一遍），以及
[tests/fake_feishu.py](../../tests/fake_feishu.py) / [tests/fake_dingtalk.py](../../tests/fake_dingtalk.py)（HTTP 接口，加一个代替 SDK 的假长连接）。
一套组装（[tests/channel_rig.py](../../tests/channel_rig.py)）把整个程序对着它们中的任何一个跑起来。测的有：配对（对的、错的、过期的配对码，
陌生人和群聊不理）、同一条消息推了两次、拆开长回复、照片和文件、用按钮、打字和在网页上答复确认（几边抢着答）、
每个通道自己的确认方式、`/new` `/stop` `/status`、断线重连和 Resume、限流、把文件发回聊天里（每个假服务器都收各平台的上传、
照着平台的上限检查；当照片还是当文件、说明、太大的文件在问你之前就拒绝、密钥永远不发、`data/` 照样问、上传到一半 `/stop`、
请求的开头在几轮之间、重启以后、到网页上接着聊都一个字节不差：[tests/test_send_file.py](../../tests/test_send_file.py)），
以及 token 和密钥从不出现在页面、日志、出错信息和导出里
（[tests/test_channels.py](../../tests/test_channels.py)、[tests/test_channels_web.py](../../tests/test_channels_web.py)、`tests/test_channels_<平台>.py`、
[tests/test_channels_platforms.py](../../tests/test_channels_platforms.py)）。发不出去的情况，让假 Telegram 接下来几次一个字都不回就关掉连接、
回答 500、429 或 403 来测：按顺序再试、一直的失败不再试、留着等通道重新在线时补发、页面上和 `/status` 里看得到、`/stop` 和退出不等退避、
send_file 的失败如实告诉模型、连接被重置以后马上显示"在线"（[tests/test_channels_delivery.py](../../tests/test_channels_delivery.py)、
[tests/ui/test_ui_channel_delivery.py](../../tests/ui/test_ui_channel_delivery.py)）。装了 SDK 的时候，飞书的事件分发器和真的钉钉 Stream 客户端
也会对着假服务器跑一遍；没装就跳过这几个。WebSocket 客户端和 HTTPS 帮手有自己的测试，对着本地的假服务器和假代理
（[tests/test_websocket.py](../../tests/test_websocket.py)、[tests/test_net_http.py](../../tests/test_net_http.py)）。
服务器模式和命令行在 [tests/test_serve.py](../../tests/test_serve.py)，其中有一个测试启动真实的 `harness serve` 进程，再用 SIGTERM 停下它。
后台服务在 [tests/test_service.py](../../tests/test_service.py)：写出来的 plist（用 `plistlib` 和 `plutil -lint` 检查）和 unit、
怪路径的引号、对着假的 `launchctl` 和 `systemctl` 发出的每一条命令、装两次、数据目录被占着时不装、读状态、`open` 和日志，
还有一个真的 `serve --service launchd` 进程自己写日志文件。没有任何测试会运行真的 `launchctl`、`systemctl`，或者打开浏览器：
`tests/conftest.py` 让它们一调用就失败。
自检在 [tests/test_doctor.py](../../tests/test_doctor.py)：每一行什么时候是好、有问题、没用上；临时的 `.env` 里放一个编造的密钥
（再加上机器人的 token、自定义接口的密钥、MCP 和钩子里的密钥、代理的密码），表格和 JSON 里都找不到它，连最后 4 位也找不到；
后台服务查不了的时候不崩；zsh、bash、PowerShell 三种修法（换掉 `machine.system`，用假的 `icacls`）；退出码；
跑之前、跑之后给磁盘拍个快照，证明它什么都没改。真的 `icacls /save` 只在 Windows 上测（[tests/test_winacl.py](../../tests/test_winacl.py)）。
Skill（frontmatter 的解析、做坏了的压缩包、对着本地 HTTPS 服务器的下载、列表怎么交给模型而不改动请求的开头）在 [tests/test_skills.py](../../tests/test_skills.py)。
从其他工具导入在 [tests/test_imports.py](../../tests/test_imports.py)：假的家目录里给每一个支持的来源放一份文件（JSON、带注释的 JSONC、
Codex 的 TOML、Goose 和 Continue 的 YAML、frontmatter 各不相同的 Markdown Agent、Kiro 和 Codex 的 Agent 文件），测每种写法怎么换、
密钥遮住、引用模型密钥的拒绝、"不用问"的设置不搬、写坏了的文件、Windows 上的位置和反斜杠路径、项目级和用户级，还有 YAML 的子集解析器；
[tests/ui/test_ui_imports.py](../../tests/ui/test_ui_imports.py) 在三页的导入里实际点一遍。
自定义 Agent 在 [tests/test_agents.py](../../tests/test_agents.py)（文件格式和检查、只会变少的工具 allow-list、在假的家目录下存取、
提示词、`<agents>` 目录、对着假 API 的委派：新的历史、放开的工具、换模型、确认、"停止"停到子 Agent 自己的 shell 和 MCP 调用、
步数和层数的上限、账本里记成 `subagent`、主助手请求的开头一字不差；还有角色的保存和恢复）和
[tests/test_agents_web.py](../../tests/test_agents_web.py)（`/api/agent/…` 接口、用 Agent 开始对话、网页里子 Agent 的事件、确认和停止）。
只读的命令有一张九百条左右的表，每一条写着该得到的结论和为什么：引号和转义、每一种操作符、选项的缩写和 BSD 的"参数后面的选项"、
指向保护起来的地方的符号链接和硬链接、大小写和 Unicode 的不同写法、对着藏着密钥的文件夹的递归命令，以及写它的时候找到的每一个漏洞
（[tests/test_commands.py](../../tests/test_commands.py)）。这一档本身（在对话、定时任务、聊天通道里放开了什么，每一步没问就做时写的"为什么"，
你自己的清单在网页上和命令行里怎么改，模型碰不到这个清单）在 [tests/test_readonly_mode.py](../../tests/test_readonly_mode.py)、
[tests/test_task_runs.py](../../tests/test_task_runs.py) 和 [tests/test_channels.py](../../tests/test_channels.py)；三处选择列的是同样的四档、
"不用问的命令"窗口，在 [tests/ui/test_ui_approval_modes.py](../../tests/ui/test_ui_approval_modes.py)。
浏览器分几层测。CDP 客户端对着进程内的两根管道，一个线程扮演 Chrome 的那一头，测试可以把一条消息拆成几段发、先发事件再回答
（[tests/test_browser_cdp.py](../../tests/test_browser_cdp.py)）。启动（描述符在 3 号和 4 号上、Chrome 的环境里没有 API 密钥、自成一个进程组）、
关掉整个进程组、用假的钟测闲置关闭，以及 session 最简单的几样，对着的是 [tests/fake_chrome.py](../../tests/fake_chrome.py)：
一个经过真正的 `chrome.launch()` 启动、按剧本回答管道协议的假 Chrome（[tests/test_browser_chrome.py](../../tests/test_browser_chrome.py)、
[tests/test_browser_unit.py](../../tests/test_browser_unit.py)，后者还试了很多种想把 `javascript:` 地址混过去的写法）。
问不问你的规矩在 [tests/test_browser_permissions.py](../../tests/test_browser_permissions.py)。工具的说明书和结果在循环里怎么走，
对着假 API 测在 [tests/test_browser_tools.py](../../tests/test_browser_tools.py)：Claude 的请求里只有一项工具包、没有替身（别家反过来）、
结果上的 `toolset_name` 和 `browser_state`、一串调用里前一个失败后面就不做的规矩，以及聊几轮之后请求的开头一个字节都不差。
设置里的接口、顶栏的提示、截图大图那个接口的响应头在 [tests/test_browser_web.py](../../tests/test_browser_web.py)。
最后，[tests/test_browser_live.py](../../tests/test_browser_live.py) 用真正的 Chrome（无界面）打开 `http.server` 在本机临时起的网页：
读、点、输入、认出密码框和提交按钮、截图、开标签页；这台电脑上没有 Chrome 就整个跳过。
操作整台电脑的测试从不碰你真的屏幕、鼠标和键盘。`tests/conftest.py` 让每个测试都找不到真的后端，要用电脑的测试换上
[tests/fake_desktop.py](../../tests/fake_desktop.py)：一个假的后端，记下每一个事件，截图交回一张编造的"桌面"（[tests/fixtures/](../../tests/fixtures/)），
屏幕的大小和 Retina 的倍数都能设。对着它，`tests/test_desktop_*.py` 检查：每个成员和替身工具变成了哪些事件、坐标换算得对不对（包括 Retina）、
默认每一步都问、这段对话里选过"不再问"（截图，或者点击和打字）和"全部自动允许"时不问、危险的组合键照样问并带着警告、
定时任务和聊天通道拿不到"不再问"、停止、每次运行各自的 session 和"一个屏幕同一时间一个运行"、
定时任务和聊天通道的开关、网页上的接口和响应头，以及聊几轮之后请求的开头一个字节都不差。
[tests/test_desktop_x11.py](../../tests/test_desktop_x11.py) 在它自己启动的虚拟屏幕（Xvfb）上跑真的 X11 后端；没有 Xvfb、`xdotool`
或者 ImageMagick 的地方就跳过，所有的 Mac 都在其中。
命令的沙箱分两层测。所有的测试里，`tests/conftest.py` 都把真的后端换成 [tests/fake_sandbox.py](../../tests/fake_sandbox.py)：
它只记下"本该在沙箱里运行"的命令（也能假装这台电脑上沙箱用不了），所以不用真的沙箱也能查规矩：一份 Policy 里有哪些路径、
SBPL 配置和 bwrap 的参数（奇怪的文件名原样交过去，`data/` 和 `.env` 永远不许，联网开和关，从不写出一条不带路径的规则）、
确认卡片、放行一条命令时哪一档都问、选了"全部自动允许"的任务和通道里直接拒绝、用不了时拒绝而不是在沙箱之外运行、
任务和通道各自的档位，以及设置的接口。然后是真的沙箱：在 Mac 上，`sandbox-exec` 在一个临时的工作目录里运行命令，
查在里面写得进去、在外面写不了、读不到一个充当 `~/.ssh` 的临时文件，`curl` 只有在"工作目录 + 联网"时才连得上本机 127.0.0.1 上的
`http.server`；在 Linux 上对着 bubblewrap 查同样几样，没装 `bwrap`、或者它建不了沙箱的地方就跳过。页面上的部分
（设置里的那一节、路径的窗口、任务和通道的选择）在下面的页面测试里。

Markdown 渲染器有自己的测试：[tests/markdown.test.mjs](../../tests/markdown.test.mjs)，用 Node 自带的测试工具跑
（`node --test tests/markdown.test.mjs`，什么都不用装）；电脑上有 Node 时，`uv run pytest` 也会顺带跑它。
测试里用的是一个很小的假文档：`innerHTML` 这类会解析 HTML 的接口一碰就报错，只允许建出固定的几种标签和属性。
它覆盖了每一种支持的写法、嵌套、转义、恶意输入、按随机大小一段段输出（结果必须和一次画完的一样），以及一万行的长输入。

[tests/test_page.py](../../tests/test_page.py) 里有几条不用浏览器的静态检查：页面的每一块都在清单里列了、只列了一次；
没有哪一块里有 `</script`、`</style`、`<!--` 或者占位符（放进页面会出问题）；没有两个函数同名；每一块脚本、
以及拼好的整段脚本都能通过 `node --check`（两块里用 `const` 定义了同一个名字，单独查都没事，拼起来整个页面就跑不起来）。

页面本身（[harness/webui/page/](../../harness/webui/page/) 里的标记、样式和脚本）由 [tests/ui/](../../tests/ui/) 里的页面测试在真正的 Chrome（无界面）里测。
`uv run pytest` 会带上它们；`uv run pytest -m ui` 只跑它们，`-m "not ui"` 跳过它们。这台电脑上没有 Chrome 时跳过，CI 里必须跑（单独的 job，见下面的 [CI](#ci)）。
一共一分钟左右，不加任何依赖：驾驶 Chrome 用的是智狐自己手写的 CDP 客户端（不用 Playwright、Selenium，也不用 npm），
整个测试会话只把 `tests/demo_web.py` 作为子进程启动一次（数据在临时目录里），每个测试在一个新的、像隐身窗口一样的浏览器上下文里打开页面。
点击、悬停和按键都是真的输入事件，会等目标不再移动、没有被别的东西盖住才点；所有的等待都是"等页面里某个条件成立"，从不固定地睡几秒。
页面里出现任何脚本异常、`console.error` 或者被安全策略（CSP）拦下的东西，测试就失败。测的有：打开时没有报错、不请求外部的东西；
回复一点点出现，刷新以后整段重放、不播入场动画；确认卡片（允许、带着原因拒绝、Esc 不会替你答复）和"全部自动允许"要多确认的那一步；
默认浅色、深色、跟随系统，以及文字的对比度；跟着系统、也跟着外观面板的"减少动态效果"；在原地切换中文、英文、西班牙语
（对话、滚动位置、写了一半的话、开着的面板都留着），以及把每一个面板和窗口打开一遍，找没翻译的中文；三种语言下 390 像素宽的窄屏上文字有没有冲出容器；
每一种存在本地的东西（会话、待办、定时任务、记忆、Skill、MCP 服务器、Agent、聊天通道、沙箱的额外路径）悬停和键盘移进来时都有
"在访达中显示 / 编辑 / 删除"，删除先在原地确认；有没保存的修改时关窗口先问（Esc、×、取消、点外面、焦点不在窗口里时按 Esc）；
下拉框的箭头是页面自己画的；扩展的四页和空状态；多媒体库和查看器；换模型的面板（列出来的东西、同一家马上换、换到另一家之前在原地先问、
没有密钥时带你去设置里填、键盘、就地重译、手机上从底下升起来、减少动态效果时直接出现），以及在设置里填好密钥、再在面板里换到那一家；
语言面板（每种语言用自己的文字写、键盘、手机上从底下升起来）；齿轮和 ⌘, / Ctrl+, 打开设置，模型按钮不打开设置；浏览器每一步的小截图；
Chrome 自己怎么看网页应用清单（`Page.getAppManifest` 没有错、`Page.getInstallabilityErrors` 是空的、每张图标都取得到且尺寸对、
别的图片照样被安全策略拦下）和标题栏的颜色跟着选的外观走；设置里沙箱的那一节（选哪一档、这台电脑上用不了时的提醒、额外路径的窗口），
以及任务的编辑器和通道的向导里沙箱的选择；编辑最后一条消息和重新生成（按钮在哪儿、输入框空着时按 ↑、改过了按 Esc 先提醒、这一轮执行过命令时先确认、失败的那一轮单独一排"重新生成"、
另一个标签页跟着变并留住它没发出去的修改）。
这些测试里演示的回复按 [tests/demo_scripts.py](../../tests/demo_scripts.py) 里的剧本走（一段对话的第一条消息写着 `[ui:名字]`），
所以测试之间不依赖先后顺序。测试失败时会存下截图、控制台输出和页面的 HTML（路径会打印出来；CI 会把它们传上去）。
[tests/ui/checks.py](../../tests/ui/checks.py) 里的检查是启发式的，带着几张选择器的表，加了新的面板就往表里添一项。
它们判断不了的（看起来精不精致、动画顺不顺、真正的触屏），仍然要在真实的浏览器里对着演示手动看一遍。

想不花钱地到处点点、看看界面，可以运行 `uv run python tests/demo_web.py`：它启动的是真实的服务器和页面，
后面接的是本地的假模型，回复都是预设的，数据写在临时目录里，不碰你的密钥和 `data/`。
里面预先装了几个编造的 Skill，还预先接好了几个假的 MCP 服务器（其中一个启动不了、一个停用了）；
"从其他工具导入"读的是临时目录里编造的 Claude Desktop、Claude Code、Cursor、VS Code、Codex（TOML）、Goose（YAML）、
GitHub Copilot 的文件，还有一个故意写坏了的 Kiro 配置，不是你电脑上的。
全新的演示目录里预先放了一段"有多媒体库之前"的旧会话，"多媒体"里一打开就有东西可看，包括只剩文字的 Word 文件。
演示还会给每个聊天平台各启动一个假的：每一家打印一个编造的 token（或者 ID 和密钥），粘贴到"通道"的向导里；
另外打印两个简陋的"手机"页面的地址（一个给 Telegram，一个上面可以选别的平台），在那里可以发配对码、聊天、点"允许 / 拒绝"。
加 `--no-sdk` 看飞书、钉钉的 SDK 没装时页面是什么样子。
演示里浏览器是开着的：开始一段新对话，发一句含有"演示商店"的话（比如"用浏览器看看演示商店里有什么"），助手会用真的 Chrome
打开演示在本机起的一个小商店网页：先问你能不能去这个网站，然后读页面、点搜索框、输入（又问你一次）、截图。
加上 `--browser-window` 可以看着 Chrome 的窗口操作；这台电脑上没有 Chrome 的话，助手会说找不到。
演示里"操作整台电脑"也是开着的，背后是那台假的电脑（绝不碰你真的屏幕）：发一句含有"演示电脑"的话，会走一段写好的过程，
能看到截图、确认卡片和上面的"不再问"按钮、危险按键的警告，以及顶栏上的提示。
第一条消息里写着 `[ui:approve]` 这类暗号的对话，按 `tests/demo_scripts.py` 里同名的剧本回答（页面测试用的就是它们）：
`[ui:approve]` 要执行一条命令（弹出确认卡片），`[ui:long]` 回一段很长的话，`[ui:todo]` 加一条标题很长的待办，`[ui:hello]` 打个招呼，
`[ui:flaky]` 每条消息第一次都会失败（可以试试在失败的那一轮上"重新生成"）。
`--script 名字` 让没写暗号的对话也按那段剧本回答；README 里的截图用的是 `--script readme`，问的是中文就答中文，否则答英文。

### CI

每次推送到 GitHub、每个 pull request，都会在 Ubuntu、macOS 和 Windows 上自动跑一遍测试和代码检查
（[.github/workflows/tests.yml](../../.github/workflows/tests.yml)），结果就是 [README](../../README.zh-CN.md) 顶上的 Tests 徽章。
为了快，测试分成几种 job 同时跑；`test` 和 `ui` 合起来正好是全部测试（每个测试要么带 ui 标记、要么不带）：

| job | 跑什么 | 在哪儿 |
|---|---|---|
| `test` | 页面测试以外的全部（`-m "not ui"`），还有代码检查 | 三个系统 × Python 3.11、3.13 |
| `ui` | 页面测试（`-m ui`），用 GitHub 的机器上预装的 Chrome | 三个系统 × Python 3.13；macOS、Windows 上分成两份，在两台机器上同时跑 |
| `types` | 类型检查（mypy），按三个系统的眼光各查一遍 | 一台 Ubuntu |
| `desktop` | 装好 Xvfb、`xdotool` 和 ImageMagick，操作电脑的集成测试对着虚拟屏幕跑 | Ubuntu |
| `sandbox` | 装好 bubblewrap，沙箱在 Linux 上真的后端 | Ubuntu |
| `package` | 打出 wheel 和源码包，在干净的环境里试装 | 三个系统 |

页面测试只用一个 Python 版本，因为它们查的是页面和浏览器里的交互，Python 只是在后面跑演示服务器；那些代码在 `test` 里两个版本都测，
只在某个版本上才有的行为也是在那里抓到的。用 3.13，是因为本机开发用的是 3.11，本机常跑页面测试，这样两个版本都覆盖到了。
分成两份靠的是 [tests/shards.py](../../tests/shards.py)：设了 `HARNESS_TEST_SHARD=第几份/一共几份`（比如 `1/2`）时只跑其中一份，
按收集到的先后轮流分，几份合起来一个不少、一个不重（[tests/test_shards.py](../../tests/test_shards.py) 会查），写错了整轮不跑；
不设就照旧跑全部。想在本机照 CI 的样子跑某一份：

```bash
HARNESS_TEST_SHARD=1/2 uv run pytest -m ui                 # zsh / bash
$env:HARNESS_TEST_SHARD = "1/2"; uv run pytest -m ui       # PowerShell
```

为什么这样分（2026-11-24）：以前测试和页面测试在同一个 job 里一起跑，macOS 上要十九分钟左右（三个核，最后十一分钟都在跑两百多个页面测试），
Windows 十三分钟，Ubuntu 九分钟。页面测试每个都要 Chrome 真的把页面画出来，很吃 CPU，所以分到两台机器上。
另一个大头是收尾时等服务器停下（见上面的 0.02 秒）：改了以后，本机页面测试以外的部分从 97 秒降到 35 秒，所有测试的耗时加起来从 943 秒降到 233 秒。

只在某一个系统上才说得通的测试，跳过时会写明原因：Windows 的路径装不进 launchd 的 plist 和 systemd 的 unit；Windows 上没有
3、4 号文件描述符和进程组；`icacls`、作业对象和真的 PowerShell 只在 Windows 上测；Windows 上建符号链接要管理员权限或者开发者模式，
做不到时那几个测试照这个原因跳过。别的在三个系统上都跑。

## 发布

版本号只写在 `pyproject.toml` 里，而且只在发布的时候才改，和 `CHANGELOG.md` 一起改：`uv version 0.3.0`（会顺手更新
`uv.lock`），再把 `[Unreleased]` 那一节改成 `## [0.3.0] - 日期`，文件末尾加上这一版的链接。`pyproject.toml` 里的版本号
在 CHANGELOG 里没有写着日期的一节和链接时，[tests/test_release.py](../../tests/test_release.py) 会失败，所以忘了写 CHANGELOG
在平常跑测试时就能发现，不用等到发布那一刻。

这个提交到了 `main`、测试全绿以后，推一个 `v0.3.0` 的 tag，就会运行
[.github/workflows/release.yml](../../.github/workflows/release.yml)：

1. **build**：用 [tests/release_notes.py](../../tests/release_notes.py) 把 tag 和 `pyproject.toml`、`CHANGELOG.md` 对一遍（同时写出
   发布说明），然后在干净的检出里打出 wheel 和源码包。
2. **smoke**：在 Ubuntu、macOS、Windows 上，各在一个什么别的都没装的环境里装上这两个包，运行
   [tests/smoke_release.py](../../tests/smoke_release.py)：`harness/` 下的文件一个不少、网页拼得出来、`harness --version`、
   `--help` 和 `harness doctor --json` 都能用。
3. **GitHub Release**：用那份说明建好发布版，挂上两个安装包。
4. **PyPI**：用 PyPI 的"受信任的发布者"（Trusted Publishing）发上去，哪里都不存令牌。仓库设置好之前（pypi.org 上的受信任的发布者、
   仓库里叫 `pypi` 的环境、仓库变量 `PYPI_PUBLISH` 设成 `true`）这一项跳过；智狐现在还没有上 PyPI。

这个工作流里的 action 钉在提交编号上，不用 `v4` 这样的 tag：它能改发布版、能发包，能拿到发布凭据的那一项不运行仓库里的任何代码。

打包和试装平常每次推送也跑（普通测试里的 `package` 那几项），所以哪个系统上装不上、启动不了，用不着等到发布时才发现。
