"""配置：路径和各种开关。所有可以调的东西都集中在这里。

用哪家的哪个模型不在这里，而是在界面上选择，保存在 data/settings.json（见 settings.py）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 这个助手给人看的名字。代码里的包名和命令仍然叫 harness（它讲的就是 harness 是怎么回事），
# 只有显示给用户的地方用这个名字：终端的标题、网页、系统提示词。
APP_NAME = "智狐"
# 英文名。界面上在任何语言下都写"智狐"；用中文以外的语言回答时，助手用这个名字称呼自己（见 prompt.py）。
# 不定一个的话，模型会自己把"智狐"音译成 Zhihu，正好和另一个知名网站同名
APP_NAME_LATIN = "ZhiFox"

# 下面三个开关只对 Claude 生效，用的是 Anthropic API 的 beta 功能。
# 如果你的账号不支持、每个请求都报 400，把对应的开关改成 False 即可。

# 请求被安全分类器误拦、模型拒绝回答时，让 API 自动换一个模型重试。
ENABLE_FALLBACKS = True
# 只有下面这些模型支持；换成别的模型时这个功能会自动关闭。
FALLBACK_CAPABLE_MODELS = {
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-sonnet-5-5",
    "claude-fable-5-2",
    "claude-fable-5-1",
}

# 对话太长时，让 API 把较早的内容自动压缩成摘要。
ENABLE_COMPACTION = True

# 接着一段从磁盘读回来的旧会话聊时，如果系统提示词或工具和当初不一样了（比如换了工作目录、
# 程序升级后工具变了），Claude 之前的"思考内容"就和现在的对话对不上了。打开这个开关后，
# API 会悄悄丢掉对不上的思考内容，而不是拒绝整个请求。
ENABLE_THINKING_DROP = True

# 备用模型（见 fallback.py）：一个模型暂时用不了（过载、服务器出错、限流）时，这一轮接下来改用同一家的另一个。
# 用哪几个、按什么顺序由你在设置里选，默认一个都没有（不换）。每一家最多选这么多个
FALLBACK_MAX_MODELS = 3

# 下面几项只对 GPT、Gemini、DeepSeek 生效（OpenAI 格式的适配层）。这几家的 API 不会替我们压缩历史，
# 所以由智狐自己做：上一次请求占用的上下文超过了模型上限的一定比例，就在下一次请求之前，
# 请同一个模型把较早的对话写成摘要，换掉原文。做法和理由见 providers/compaction.py。
# 改成 False 就不压缩：对话每一轮都整段重发，越聊越贵，直到超出模型的上限、请求被拒绝。
ENABLE_OPENAI_COMPACTION = True
# 各个模型的上下文上限（token）。DeepSeek 的数字来自它的定价页（https://api-docs.deepseek.com/quick_start/pricing）。
# 不在表里的模型按 DEFAULT_CONTEXT_WINDOW 算：这个数取得保守，宁可早一点压缩，也不要等到请求被拒绝
CONTEXT_WINDOWS = {
    "deepseek-flash": 1_000_000,
    "deepseek-v4-flash": 1_000_000,  # deepseek-flash 的旧名字，官方说照样能用
    "deepseek-v4-pro": 1_000_000,
}
DEFAULT_CONTEXT_WINDOW = 128_000
# 上下文用到上限的这个比例就压缩。剩下的余量要装得下下一条消息、工具的结果和模型的回复
COMPACTION_THRESHOLD = 0.7
# 压缩时，最近的这几轮对话保持原样，不写进摘要。"一轮"从用户的一条消息开始，到下一条之前；
# 正在进行的那一轮也算在里面，而且总是保留（所以至少是 1）
COMPACTION_KEEP_TURNS = 2


# 自定义模型接口（见 endpoints/）：Ollama、LM Studio、vLLM、llama.cpp、公司自己的网关……任何 OpenAI 格式的地址。
# 最多接这么多个，每个最多记这么多个模型（vLLM、网关的模型列表可能很长，全记下来换模型的面板就没法看了）
MAX_ENDPOINTS = 20
MAX_ENDPOINT_MODELS = 300
# 一个模型没填上下文上限、服务器也没说的时候按多少算（压缩历史看它，见 providers/compaction.py）。
# 本地的模型上限各不相同：Ollama 默认只给 4096（要用 OLLAMA_CONTEXT_LENGTH 调大），智狐自己的系统提示词和工具说明书
# 就要好几千个 token。取 32768：比它小的装不下几轮带工具结果的对话；比它大的，你在设置里填上真实的数就行
CUSTOM_CONTEXT_WINDOW = 32_768
# 测试连接、同步模型列表时，一个请求最多等这么久（秒）。本地的服务器一般零点几秒就回答；等太久，页面上的按钮就一直在转
ENDPOINT_PROBE_SECONDS = 8
# 同步模型列表时，最多为这么多个模型逐个去问"你能做什么"（Ollama 的 /api/show）。再多的照名字猜，不再一个个问
ENDPOINT_DETECT_MODELS = 40

# 倒回最后一轮（编辑最后一条消息、重新生成，见 rewind.py）之前，这一轮做过的、可能改动了东西的事要列给你看：
# 最多列这么多行，再多的合成一句"还有几项"
MAX_REWIND_EFFECTS = 6

# 撤回一轮改的文件（见 undo/）。文件工具每改一个文件，先把改之前的样子留一份底，存在 data/backups/turns/ 里。
# 留底是要占磁盘的，所以有几道上限；超了的那个文件照样改，只是撤不回来（页面上写明为什么）
UNDO_FILE_BYTES = 10_000_000  # 一个文件最多留这么大的底（文件工具改的是文本文件，一般只有几 KB）
UNDO_TURN_BYTES = 50_000_000  # 一轮里所有留底加起来最多这么大
UNDO_TURN_FILES = 100  # 一轮里最多记这么多个文件，再多的不记（页面上说还有几个没记下）
UNDO_KEEP_DAYS = 30  # 留底存这么多天，过期的在启动时删掉（会话里那一行还在，只是写着"留底已经清理掉了"）
UNDO_DIFF_LINES = 400  # 改动的预览（diff）最多显示这么多行，再多的写一句"还有几行没显示"
UNDO_DIFF_LINE_CHARS = 400  # 预览里一行最多显示这么多个字（压缩过的 JS、很长的一行 JSON 不会把页面撑满）
UNDO_DIFF_BYTES = 2_000_000  # 改之前、改之后都不超过这么大才算 diff（算 diff 的时间随大小增长）；更大的只说改了
UNDO_NOTE_LINES = 20  # 撤回、重做以后告诉模型的那段说明（见 undo/notes.py）最多列这么多个文件

# 用户自己写的提示词最长多少个字符。提示词每一轮都要发给模型（命中缓存时便宜，但第一次和缓存过期后按原价），
# 两万字大约是一万到两万个 token，再长就太贵了，也会挤占对话能用的上下文
MAX_PROMPT_CHARS = 20_000

# 联网搜索的回答引用的网页（见 citations.py）：每一处引用旁边一个编号，整段回答的末尾列出来源。
# 网页标题、被引用的那段原文最多显示这么多个字（API 给的原文本身最多 150 个字）；网址再长的不要（多半是写坏了的，
# 页面上的 Markdown 链接也是这个上限，见 webui/markdown.js 的 MAX_URL）
SOURCE_TITLE_CHARS = 120
SOURCE_QUOTE_CHARS = 150
SOURCE_URL_CHARS = 2048

# 定时任务最多同时运行几个（你自己的对话不算在里面，它照常进行）。每个运行都在调用模型、花钱，还可能开着命令，
# 同时跑得太多既费钱又难盯住。再到时间的先排队，前面的跑完一个，排在最前面的那个就开始（见 webui/scheduler.py）
MAX_PARALLEL_TASKS = 3

# 网页上你自己的对话最多同时进行几段（见 webui/sessions.py）：一段在跑的时候，你可以打开别的会话、开新对话、在那里接着发消息，
# 原来那段在后台接着跑。每段都在调用模型、花钱，可能开着命令、等你确认，同时太多既费钱又盯不过来。到了上限，
# 再发一条会被拒绝（页面上说明原因），等其中一段结束再发。定时任务和聊天通道不算在里面，它们各有各的规矩
MAX_PARALLEL_CHATS = 3

# 命令的沙箱（见 sandbox/）：设置里"额外允许的路径"最多这么多条，每条最长这么多个字符
SANDBOX_MAX_PATHS = 50
SANDBOX_PATH_CHARS = 1000
# 看一眼这台电脑上的沙箱能不能用（真的建一个什么都不做的沙箱试一下）：最多等这么久；
# 结果记这么久（秒），之后再问就重新试一次：你可能刚装好 bubblewrap，不用重启智狐
SANDBOX_PROBE_SECONDS = 10
SANDBOX_PROBE_TTL = 60

# 花费账本（见 ledger.py）。超过这么多天的明细，合并成"每天、每个模型、每种来源一行"，账本文件不会无限变大；
# 每天、每月、每个模型花了多少都不变，只是看不到一笔一笔的时间了
LEDGER_DETAIL_DAYS = 90
# 网页上"花费明细"的柱状图显示最近这么多天
LEDGER_CHART_DAYS = 30

# 运行记录（见 trace/）：每一轮记下每一次请求、每一次工具调用的时间和用量，跟着会话存下来。
# 一轮最多存这么多段（子 Agent 里的也算），再多的只算进合计、不一段段存（一轮 25 步加子 Agent 也远到不了）；
# 工具调用的一行说明、出错的那一句最多这么多字。记录只放数字和短说明，不放工具的输出
TRACE_MAX_SPANS = 400
TRACE_SUMMARY_CHARS = 120

# 遮住工具结果里像密钥的东西（见 redact.py）。总开关在设置里（默认开着），这里是"多像才算"的几个数。
# 宁可漏掉一个可疑的，也不要把正常的输出遮坏（git 的提交号、UUID、图片的 base64 都不遮）
REDACT_MIN_KNOWN = 8  # 按值认的密钥（环境变量、.env、自定义接口的密钥……）至少这么长才算：太短的值会把输出里到处都换掉
# 紧跟在 password=、token: 这类字后面的值：至少这么长、每个字符的信息量（香农熵，比特）至少这么多，而且字母和数字都有，
# 才当成密钥。"hunter2"太短不遮；"request.headers.get"没有数字、像代码，不遮
REDACT_MIN_VALUE_CHARS = 12
REDACT_MIN_ENTROPY = 3.0
# 连着这么多个 base64 的字符（中间没有空白）当作一段数据（图片、文件的内容），里面碰巧长得像密钥的也不遮
REDACT_DATA_RUN = 200

# 你的钩子（见 hooks/）：在固定的时刻（工具调用之前和之后、你发出一条消息时、一轮答完时）运行你配置的命令。
# 钩子运行的时候这一轮在等它，所以默认的时限比 Claude Code 的（600 秒）短得多；每个钩子可以自己设，最长 HOOK_MAX_TIMEOUT
HOOK_TIMEOUT = 30
HOOK_MAX_TIMEOUT = 600
HOOK_MAX = 50  # 最多配置这么多个钩子
HOOK_OUTPUT_BYTES = 100_000  # 一个钩子的标准输出、标准错误各最多收这么多字节，再多的读出来扔掉（不让它卡住，也不让它吃光内存）
# 钩子交给模型的话最多这么多字：接在工具结果后面的补充（PostToolUse）、附在你的消息里的说明（UserPromptSubmit）。
# 每一次请求都要把它们重发一遍，太长就贵了，也会盖过真正的内容
HOOK_NOTE_CHARS = 2000
HOOK_REASON_CHARS = 1000  # 拦下的原因（交给模型或者给你看）最多这么多字
HOOK_TEST_OUTPUT_CHARS = 4000  # 页面上"试运行"显示的输出最多这么多字

# 聊天通道：在 Telegram 这类聊天软件里和智狐说话（见 channels/）。
# 配对码：网页或命令行上显示一个 6 位数字，你从自己的账号发给机器人，这个账号就绑定成了"主人"，只有主人能用。
PAIRING_SECONDS = 600  # 配对码多久过期
# 一个配对码最多能猜错几次，错满了就作废、要重新生成。6 位数字只有一百万种，不限次数的话，知道机器人名字的人
# 可以在十分钟里一个个试；限成 5 次，猜中的机会是二十万分之一
PAIRING_ATTEMPTS = 5
CHANNEL_QUEUE = 5  # 一个聊天里最多排着几条还没开始处理的消息（正在跑的那一轮不算）。再多就请你等一等
CHANNEL_POLL_SECONDS = 25  # Telegram 的长轮询：一次 getUpdates 最多在服务器那边等这么久（有消息会马上返回）
CHANNEL_TIMEOUT = 20  # 发消息、下载文件这类普通请求的超时（秒）
CHANNEL_RETRY_SECONDS = 60  # 连不上时，重试的间隔从 1 秒起每次翻倍，最多隔这么久
CHANNEL_TYPING_SECONDS = 4  # 一轮还在进行时，每隔这么久发一次"正在输入"（Telegram 的这个状态只维持 5 秒）
CHANNEL_DOWNLOAD_BYTES = 20_000_000  # 聊天里发来的文件最多下载这么大（Telegram 机器人能下载的上限就是 20 MB）
# 智狐往聊天里发文件（send_file 工具，见 tools/send.py）：一个文件最多这么大（要整个读进内存再上传）。
# 各平台自己的上限更小（Telegram 50 MB、Discord 20 MiB、飞书 30 MB、钉钉 20 MB），按平台的算，见各平台的 UPLOADS
CHANNEL_SEND_BYTES = 50_000_000
CHANNEL_UPLOAD_TIMEOUT = 120  # 上传一个文件的超时（秒）。比普通请求长得多：在慢的网络上传 20 MB 要好一会儿
# 聊天里发来的照片，网页上会话里显示的预览图至少要这么大（长边，像素）。页面上预览图大约 240 个点宽，
# Retina 屏幕上一个点是 2 个像素，所以要 480 左右才清楚。Telegram 一张照片给好几种尺寸（大约 90、320、800、1280），
# 用不小于它的里面最小的一张；最小的那张只有 90 像素，放大以后是糊的
CHANNEL_THUMB_EDGE = 480
CHANNEL_THUMB_BYTES = 400_000  # 预览图最多下载这么大（800 像素的 JPEG 一般一百来 KB）
CHANNEL_SHUTDOWN_SECONDS = 5  # 退出时最多等聊天里正在进行的几轮这么久，让它们收尾存盘
# Discord、QQ 的网关断开以后重连：间隔从 1 秒起每次翻倍，最多隔这么久。比别的长，是因为 Discord 一天只许
# "登录"（Identify）1000 次，超了会把机器人的 token 作废（见 channels/gateway.py）：隔 120 秒，一天最多 720 次
GATEWAY_RETRY_SECONDS = 120
CHANNEL_IDLE_SECONDS = 30  # Slack 这类连接这么久没收到任何东西，就发一个 ping 看看它还活着没有；再等这么久还没动静就重连
CHANNEL_SEEN_MESSAGES = 500  # 记住最近这么多条消息的编号：QQ、飞书偶尔会把同一条推两次，第二次不处理
# 手写的 WebSocket 客户端（见 net/websocket.py）
WEBSOCKET_MAX_BYTES = 8_000_000  # 一条消息最大这么多字节（看到长度就拒绝，不会先读进内存）
WEBSOCKET_CLOSE_SECONDS = 3  # 关闭时最多等对方回应这么久
# 自定义 Agent（见 agents/）。主助手可以用 agent 工具把一项任务交给一个 Agent（"子 Agent"）去做：
# 子 Agent 能不能再往下交，最多交几层。1 表示只有主助手能交，子 Agent 自己不能再叫别的 Agent。
# 调大之前想一想：每多一层，花的钱、要你点的确认都会翻倍，出了问题也更难看清是谁做的
AGENT_MAX_DEPTH = 1
# 一个子 Agent 做一项任务，最多调用这么多次模型。主助手一轮最多 max_steps 次（见 Config）；
# 子 Agent 做的是一件具体的事，用不了那么多，到了上限就停下，把做到哪儿交回给主助手
AGENT_MAX_STEPS = 25
# 子 Agent 交回给主助手的回答最多这么多字。它会留在主助手的历史里，之后每次请求都要重发
AGENT_MAX_RESULT_CHARS = 40_000
# 一个 Agent 文件最多这么大（frontmatter 加正文）。正文每次都要整份放进它的系统提示词里
MAX_AGENT_BYTES = 200_000
# Agent 一页最多列这么多个
MAX_AGENTS = 200

# 浏览器（见 browser/）：助手操作一个智狐自己启动的 Chrome，和你平时用的浏览器分开。
# 页面的大小（宽、高，CSS 像素）。截图就是这么大：模型看到的、点击用的坐标都在这个范围里。
# 调大了看得更全，但每张截图的 token 也跟着涨（Claude 大约按 宽×高÷750 计，1280×800 约 1400 个）
BROWSER_VIEWPORT = (1280, 800)
# 截图的 JPEG 质量（1~100）。60 看得清字，大小只有 PNG 的几分之一；截图是要按输入 token 付钱的
BROWSER_SHOT_QUALITY = 60
# 对话里那张小预览图的宽度（像素）。只给你看，不发给模型
BROWSER_THUMB_WIDTH = 320
# 截图的大图存在 data/browser/shots/ 里，点开小图时看的就是它。超过这么多天的删掉（小图还在会话里）
BROWSER_SHOT_DAYS = 30
# 这么多分钟没有用过浏览器，就把它关掉（省内存；下次要用时再自动打开，登录状态在配置文件夹里，不会丢）
BROWSER_IDLE_MINUTES = 10
# 发给 Chrome 的一个命令最多等这么久（秒）
# Chrome 启动以后，最多等这么久等它答应第一声（Browser.getVersion）。新的配置文件夹第一次启动要建好一堆文件，
# 慢的电脑（和 GitHub 上的测试机器）上十几秒是常有的事；平时一两秒就好了，不会真的等这么久
BROWSER_START_TIMEOUT = 45
BROWSER_CALL_TIMEOUT = 20
# 打开一个网页最多等它加载这么久（秒）。到了还没加载完，先把现在的样子交给模型
BROWSER_NAV_TIMEOUT = 30
# 点击、按键之后等页面"安静下来"最多这么久（秒）：点了一个链接，要等新页面开始加载
BROWSER_SETTLE_SECONDS = 3
# 读页面（read_page、get_page_text）一次最多交给模型这么多字。再多就告诉它"截断了，请缩小范围"
BROWSER_MAX_PAGE_CHARS = 50_000
# 支持 Anthropic 的"浏览器工具包"（browser_toolset_20260801）的 Claude 模型。用它们时，浏览器的工具按这个工具包发
# （模型专门为它训练过，Anthropic 还会扫描页面内容里有没有冲着模型来的指令）；别的模型用智狐自己写的函数工具。
# 名单来自 https://platform.claude.com/docs/en/agents-and-tools/tool-use/browser-use-tool 的"API support"
BROWSER_TOOLSET_MODELS = {
    "claude-fable-5-2",
    "claude-mythos-5-2",
    "claude-fable-5-1",
    "claude-mythos-5-1",
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-sonnet-5-5",
    "claude-sonnet-5",
    "claude-haiku-5-5",
    "claude-opus-4-8",
}

# 操作整台电脑（"computer use"，见 desktop/）：进阶选项，默认关着。助手看到的是整个屏幕，能点、能打字的是你电脑上的任何程序。
# 截图缩到多大再交给模型。屏幕的长边、总像素数都不超过这两个数（文档的例子用的就是它们，四家模型都收得下）：
# 1512×982 的 MacBook 屏幕交出去是 1330×864。模型说的坐标按这张图算，智狐再换算回屏幕上真正的位置（见 desktop/frame.py）。
# 支持工具包的 Claude 模型其实收得下长边 2576、约 375 万像素的图，但图越大 token 越多，点得也不见得更准
DESKTOP_MAX_EDGE = 1568
DESKTOP_MAX_PIXELS = 1_150_000
# 交给模型的截图的 JPEG 质量（1~100）。桌面上的字比网页小，比浏览器的截图（60）高一点
DESKTOP_SHOT_QUALITY = 70
# 对话里那张小预览图的宽度（像素）。只给你看，不发给模型
DESKTOP_THUMB_WIDTH = 320
# 截图的大图存在 data/desktop/shots/ 里。整个屏幕比一个网页敏感得多（可能拍到别的程序、通知里的消息），所以只留这么多天
DESKTOP_SHOT_DAYS = 7
# 点击、按键之后等这么久（秒）再截小预览图：程序要一点时间才画出点击的结果
DESKTOP_SETTLE_SECONDS = 0.4
# 滚轮转一格，在 macOS 上滚几行（Linux 上一格就是一下滚轮，由系统决定滚多少）
DESKTOP_SCROLL_LINES = 3
# 一次 wait、hold_key 最多等多久（秒）。文档允许到 300 秒；等得太久的话，停下之前你什么都做不了
DESKTOP_MAX_WAIT = 30
# 一个外部命令（截图、xdotool……）最多等这么久（秒）
DESKTOP_COMMAND_TIMEOUT = 15
# 一次 type 最多打这么多个字。更长的让模型分几次打：每一段你都要批准，看得清它要打什么
DESKTOP_MAX_TYPE_CHARS = 2000
# 支持 Anthropic 的"电脑工具包"（computer_toolset_20260801）的 Claude 模型。名单来自
# https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool 的"API support"（Claude API 那一行）。
# 智狐能选的三个 Claude 模型（Opus 5.5、Sonnet 5.5、Fable 5.2）都在里面，而且它们只认这个工具包（旧的 computer_20251124 会被拒绝）。
# 不在名单里的模型用智狐自己写的替身工具（见 tools/computer.py）
COMPUTER_TOOLSET_MODELS = {
    "claude-fable-5-2",
    "claude-mythos-5-2",
    "claude-fable-5-1",
    "claude-mythos-5-1",
    "claude-fable-5",
    "claude-mythos-5",
    "claude-mythos-preview",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-sonnet-5-5",
    "claude-sonnet-5",
    "claude-haiku-5-5",
    "claude-opus-4-8",
}
# 工具包的说明书每次请求大约要多发这么多个 token（文档的 Pricing 一节：默认的 17 个成员约 4500）。只用来在设置里告诉你
COMPUTER_TOOLSET_TOKENS = 4500

# "只读的命令不用问"那一档（见 commands/ 和 permissions.py）。
# 一条命令最长这么多个字符还去判断它是不是只读的；再长的一律照常问：看懂一条很长的命令本来就难，人也很难一眼看清
READONLY_MAX_CHARS = 2000
# 会把一个文件夹里的文件内容整个读一遍的命令（grep -r、rg、diff -r），先把那个文件夹看一遍有没有像密钥的文件
# （.env、私钥、凭据……）。最多看这么多项，看不完的（文件夹太大）就照常问，不去猜剩下的里面有没有
READONLY_SCAN_ENTRIES = 3000
# 你自己加的"不用问的命令"（data/commands.json，见 commands/allowlist.py）：最多这么多条，每条最长这么多个字符。
# 默认一条都没有：常见的只读命令（ls、git status、df……）已经内置，不用你加
ALLOWED_COMMANDS_DEFAULT: tuple[str, ...] = ()
MAX_ALLOWED_COMMANDS = 100
MAX_ALLOWED_COMMAND_CHARS = 200

# `harness serve` 的退出码（见 serve.py）：0 正常退出；1 数据目录被别人占着，或者端口被占用；
# 2 设置有问题（比如 channels.json 坏了），重启也没用。后台服务据此决定要不要重新开它
# （systemd 的 RestartPreventExitStatus、Windows 上的 supervise.py），所以写在这里，不用为了一个数导入整个网页服务器
EXIT_OK, EXIT_BUSY, EXIT_CONFIG = 0, 1, 2

# 评测（见 evals/）：让真正的智狐去做一组真实的小事，按做成了没有打分，看改了提示词、换了模型以后是变好了还是变坏了。
# 任务的文件夹（一个任务一个 .toml 文件）。放在仓库里、和 tests/ 并列，不打进包里：它和测试一样是"量产品"的东西，
# 随代码一起改、一起提交（理由见 evals/__init__.py）
EVAL_TASKS_DIR = PROJECT_ROOT / "evals" / "tasks"
EVAL_RESULTS_DIR = "evals"  # 结果存在数据目录里的这个文件夹（data/evals/）：一次运行一个子文件夹，不提交
EVAL_MAX_STEPS = 30  # 评测里一轮最多调用模型这么多次（平常是 50）。评测的任务都是小事，用不了那么多，到了上限就停，省钱
EVAL_TASK_SECONDS = 300  # 一个任务最多跑这么久（秒），不管它是卡在等模型还是在跑命令；到了就停下，记成"超时"（不算模型做错）
EVAL_REQUEST_SECONDS = 180  # 一次模型请求最多等这么久（秒）。连接挂住不动时靠它脱身
EVAL_SETUP_SECONDS = 60  # 准备工作目录时运行的一条命令（比如 git commit），最多等这么久
EVAL_CHECK_SECONDS = 60  # 打分时运行的一条命令（command 那种检查），最多等这么久
EVAL_PARALLEL = 4  # 同时跑几个任务。真的花钱的运行太多了会撞上各家的频率限制
EVAL_MAX_REPEAT = 20  # --repeat 最大是多少（每个任务跑几遍）。再多就是在烧钱了
# 估算花费时，一个任务（跑一遍）假定的用量。任务文件里的 [budget] 可以改它自己的；跑过真的以后，按上一次实际花的估算。
#   requests  调用几次模型（每调用一次工具就多一次）
#   context   每次请求带着多少个 token 的上下文：系统提示词和工具的说明书（数过一次，大约五六千个；Claude 自带的文件、
#             记忆工具另有一段看不见的说明书），加上到目前为止的对话和工具的结果。取得偏高：宁可估多，不要估少
#   output    每次回复写多少个 token（思考也算在里面）
EVAL_ASSUMED_REQUESTS = 4
EVAL_ASSUMED_CONTEXT = 12_000
EVAL_ASSUMED_OUTPUT = 1_000
# 估算"大约"时，第一次请求之后的输入假定有这么多命中缓存（命中的部分便宜得多，Claude 约是原价的二十分之一）。
# "最多"那个数按一点都没命中算
EVAL_CACHE_HIT = 0.8
# 估算按上一次实际花的钱算时，"最多"在那次花得最多的一遍上再加这么多（同一个任务每次走的步数不一样）
EVAL_ESTIMATE_MARGIN = 1.5
# 让模型当评分员（judge 那种检查，默认关着，要另外花钱）：用哪个模型、估算时假定一次评分读多少写多少 token。
# 不要用被评的那个模型给自己打分：它往往偏爱和自己写得像的回答
EVAL_JUDGE_MODEL = "claude-sonnet-5-5"
EVAL_JUDGE_INPUT = 4_000
EVAL_JUDGE_OUTPUT = 1_000
EVAL_JUDGE_TRANSCRIPT_CHARS = 40_000  # 交给评分员的对话记录最多这么多字（一个工具的结果最多 2000 字，见 evals/judge.py）

# 后台服务（见 service/）：`harness service install` 把智狐装成登录时自动启动、出错退出后自己重启的服务
# （macOS 上是 launchd 的 LaunchAgent，Linux 上是 systemd 的用户服务，Windows 上是任务计划程序里的一个任务）。
# 出错退出以后，最少隔这么多秒再重启（launchd 的 ThrottleInterval、systemd 的 RestartSec、Windows 上 supervise.py 自己等）。
# 太短的话，一个一启动就出错的问题（比如数据目录被别人占着）会每秒重启一次、把日志刷满
SERVICE_RESTART_SECONDS = 10
# macOS 和 Windows 上服务的日志写进数据目录里的这个文件（Linux 上交给 journald）。写到这么大就换一个新文件，
# 旧的改名成 service.log.1、.2……，最多留这么多份：日志最多占 (1 + 份数) × 大小
SERVICE_LOG = Path("logs") / "service.log"
# 后台服务没有终端可以打印网页的地址（里面有口令），就写在数据目录里的这个文件里（见 serve.py），harness service open 读它
WEB_ADDRESS = ".web-address"
# 后台服务的网页口令存在这里，重启前后不变，开着的页面刷新一下就能接着用（见 webui/token.py）
WEB_TOKEN = ".web-token"
# 数据目录里的用户设置（模型、界面语言……，见 settings.py）。Windows 上的 supervise.py 没有 Config，也照这个名字找界面语言
SETTINGS_FILE = "settings.json"
SERVICE_LOG_BYTES = 1_000_000
SERVICE_LOG_BACKUPS = 3
# 装好、重启之后，最多等这么多秒看它开起来没有（开起来了就告诉你可以打开网页；没开起来就把日志里的错误给你看）
SERVICE_START_SECONDS = 15
# 换掉、卸载旧的服务时，最多等这么多秒让它退出、放开数据目录
SERVICE_STOP_SECONDS = 30
# 一个 launchctl、systemctl、schtasks 命令最多等这么久（秒）。停下服务的那一个要等智狐收尾存盘，所以比上面的再长一点
SERVICE_COMMAND_SECONDS = 60
# Windows 上没有"服务管家发一个 SIGTERM"这回事（schtasks /End 是直接结束进程，正在进行的一轮来不及存盘）。
# 所以 harness service stop 在数据目录里放下这个文件，后台的 supervise.py 看到了就请智狐好好地退出（见 service/supervise.py）
SERVICE_STOP_REQUEST = ".service-stop"
# supervise.py 每隔这么多秒看一眼：智狐退出了没有、有没有人请它停下
SERVICE_POLL_SECONDS = 0.5
# Windows 上停下服务时，supervise.py 给智狐 SERVICE_STOP_SECONDS 秒收尾，到时间还没退出就结束它；
# harness service stop 再多等这么几秒（让 supervise.py 自己先处理完），还没停下才用 schtasks /End 和 taskkill
SERVICE_STOP_SLACK_SECONDS = 5


# ---- 语音转文字（聊天里发来的语音，见 voice/）----------------------------------------------
# 按分钟计费（价格见 pricing.TRANSCRIBE_PRICES），所以太长的不转：说一声上限是多少，一分钱都不花
VOICE_MAX_SECONDS = 600
# 一条语音最多下载、上传这么大：Telegram 的机器人最多能下载 20 MB（https://core.telegram.org/bots/api#getfile），
# OpenAI 最多收 25 MB（https://developers.openai.com/api/docs/guides/speech-to-text），取小的那个
VOICE_MAX_BYTES = 20_000_000
VOICE_TIMEOUT = 120  # 转一条最多等多久（秒）。一条十分钟的语音上传加识别，慢的网络上要一会儿
VOICE_TEXT_CHARS = 20_000  # 转出来的文字最多留这么多个字（一个出了毛病的服务器不能把一大堆东西塞进对话）


# Claude 思考的力度能选的几档，从浅到深（见 Config.effort）
EFFORTS = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class Config:
    effort: str  # Claude 思考的力度：EFFORTS 里的一档
    max_tokens: int  # 单次回复的上限（思考也算在里面）
    max_steps: int  # 一轮对话里最多调用模型多少次，防止死循环
    workspace: Path  # 工作目录：助手可以直接读取这里面的文件
    data_dir: Path  # 助手自己的数据：记忆、待办、定时任务、文件备份、设置
    env_file: Path  # 保存各家 API key 的文件
    web: bool  # 是否启用联网搜索（只有 Claude 有）
    max_searches: int = 5  # 用户每发一条消息，最多联网搜索几次；0 表示不限制
    default_provider: str = "anthropic"  # 还没在界面上选过模型时用哪一家
    default_model: str = "claude-opus-5-5"
    max_parallel_tasks: int = MAX_PARALLEL_TASKS  # 定时任务最多同时运行几个（测试里会改小）
    max_parallel_chats: int = MAX_PARALLEL_CHATS  # 网页上你的对话最多同时进行几段（测试里会改小）
    # Telegram Bot API 的地址。一般不用改；在连不上 api.telegram.org 的地方可以换成自建的 Bot API 服务器，
    # 测试和演示把它指向本地的假服务器。只能用环境变量 TELEGRAM_API_BASE 改，页面上改不了：token 会发到这个地址去
    telegram_api: str = "https://api.telegram.org"
    # 别的聊天平台的接口地址，道理和 telegram_api 一样：只能用环境变量改（DISCORD_API_BASE……），测试和演示指向本地的假服务器
    discord_api: str = "https://discord.com/api/v10"
    slack_api: str = "https://slack.com/api"
    qq_api: str = "https://api.bot.qq.com"  # 换 access_token、开网关、发消息都在这一个域名下（2026 年 8 月起的新地址）
    feishu_api: str = "https://open.feishu.cn"
    dingtalk_api: str = "https://api.dingtalk.com"
    # 钉钉的老接口（上传图片和文件，发图片、文件消息要先传上去拿到 media_id，见 channels/dingtalk.py）在另一个域名下
    dingtalk_oapi: str = "https://oapi.dingtalk.com"
    # 语音转文字用的 OpenAI 接口（见 voice/transcribe.py）。和上面的一样只能用环境变量改（OPENAI_BASE_URL，
    # 和 OpenAI 的 SDK 认的是同一个）：OpenAI 的 API key 会发到这里去。测试把它指向本地的假服务器
    transcribe_api: str = "https://api.openai.com/v1"

    @property
    def todo_file(self) -> Path:
        return self.data_dir / "todos.json"

    @property
    def tasks_file(self) -> Path:
        return self.data_dir / "tasks.json"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def undo_dir(self) -> Path:
        return self.backup_dir / "turns"  # 每一轮改文件之前留的底，撤回用（见 undo/store.py）

    @property
    def memory_dir(self) -> Path:
        return self.data_dir / "memories"

    @property
    def settings_file(self) -> Path:
        return self.data_dir / SETTINGS_FILE

    @property
    def prompt_file(self) -> Path:
        return self.data_dir / "prompt.md"  # 用户改过的提示词（见 prompt.py）

    @property
    def conversations_dir(self) -> Path:
        return self.data_dir / "conversations"

    @property
    def media_dir(self) -> Path:
        """多媒体库：发过的附件的原件，一个文件一份（见 media.py）。"""
        return self.data_dir / "media"

    @property
    def agents_dir(self) -> Path:
        return self.data_dir / "agents"  # 自定义 Agent：一个 Agent 一个 Markdown 文件（见 agents/store.py）

    @property
    def mcp_file(self) -> Path:
        return self.data_dir / "mcp.json"  # 接入的 MCP 服务器（见 mcp/config.py）

    @property
    def ledger_file(self) -> Path:
        return self.data_dir / "ledger.jsonl"

    @property
    def browser_dir(self) -> Path:
        """浏览器：profile/ 是智狐那个 Chrome 的配置文件夹（登录状态、缓存），shots/ 是截图的大图（见 browser/）。"""
        return self.data_dir / "browser"

    @property
    def desktop_dir(self) -> Path:
        """操作整台电脑：shots/ 是截图的大图（见 desktop/）。"""
        return self.data_dir / "desktop"

    @property
    def channels_file(self) -> Path:
        return self.data_dir / "channels.json"  # 聊天通道的设置：机器人的 token（密钥）、绑定的账号（见 channels/store.py）

    @property
    def endpoints_file(self) -> Path:
        return self.data_dir / "endpoints.json"  # 自定义模型接口：地址、可选的密钥、每个模型能做什么（见 endpoints/store.py）

    @property
    def commands_file(self) -> Path:
        return self.data_dir / "commands.json"  # 你自己加的"不用问的命令"（见 commands/allowlist.py）

    @property
    def hooks_file(self) -> Path:
        return self.data_dir / "hooks.json"  # 你的钩子：在固定的时刻运行的命令（见 hooks/config.py）

    @property
    def protected_files(self) -> list[Path]:
        """文件工具碰不了、沙箱里也碰不到的几个文件（见 app.py 的 build_app 和 sandbox/policy.py）：
        .env 和 mcp.json、channels.json、commands.json、endpoints.json、hooks.json——里面是密钥，或者改了它就能让智狐运行别的东西、
        不再问你、把请求发到别处、在固定的时刻运行任何命令（钩子，见 hooks/）。"""
        return [self.env_file, self.mcp_file, self.channels_file, self.commands_file, self.endpoints_file, self.hooks_file]

    @property
    def web_address_file(self) -> Path:
        """`harness serve --web` 没有终端时（作为后台服务运行），把带口令的网页地址写在这里（权限 600，退出时删掉，见 serve.py）。"""
        return self.data_dir / WEB_ADDRESS

    @property
    def service_log(self) -> Path:
        """macOS 和 Windows 上后台服务的日志（见 SERVICE_LOG）。"""
        return self.data_dir / SERVICE_LOG


def _env_entries(path: Path) -> list[tuple[str, str]]:
    """.env 文件里的每一行 KEY=VALUE（去掉了 export、引号和注释行）。文件不存在时是空的。"""
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip("'\"")
        if key and value:
            entries.append((key, value))
    return entries


def load_env_file(path: Path) -> None:
    """把 .env 文件里的 KEY=VALUE 读进环境变量。已经存在的环境变量不会被覆盖。"""
    for key, value in _env_entries(path):
        os.environ.setdefault(key, value)


def env_file_values(path: Path) -> list[tuple[str, str]]:
    """.env 文件里的每一项（名字，值）。只给遮密钥用（见 redact.py：按值认出输出里的密钥），值从不写进日志、也不交给页面。"""
    return _env_entries(path)


def env_file_names(path: Path) -> set[str]:
    """.env 文件里设置了哪些变量（只要名字，不碰值）。装后台服务时用来看：哪些只在终端里设置了、服务读不到（见 service/plan.py）。"""
    return {key for key, _ in _env_entries(path)}


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


# 每个聊天平台的接口地址：Config 里的字段名、可以改它的环境变量、默认值
API_BASES = [
    ("discord_api", "DISCORD_API_BASE", "https://discord.com/api/v10"),
    ("slack_api", "SLACK_API_BASE", "https://slack.com/api"),
    ("qq_api", "QQ_API_BASE", "https://api.bot.qq.com"),
    ("feishu_api", "FEISHU_API_BASE", "https://open.feishu.cn"),
    ("dingtalk_api", "DINGTALK_API_BASE", "https://api.dingtalk.com"),
    ("dingtalk_oapi", "DINGTALK_OAPI_BASE", "https://oapi.dingtalk.com"),
]


def load_config(workspace: Path | None = None) -> Config:
    """workspace：工作目录，不给的话是现在所在的目录（在服务器上用 --workspace 指定，见 cli.py）。

    密钥默认在项目里的 .env；HARNESS_ENV_FILE 可以换一个位置（服务器上放在 /etc 下、测试里放在临时目录里）。
    """
    env_file = Path(os.environ.get("HARNESS_ENV_FILE") or PROJECT_ROOT / ".env").expanduser().resolve()
    load_env_file(env_file)
    return Config(
        effort=os.environ.get("HARNESS_EFFORT", "medium"),
        max_tokens=64000,
        max_steps=50,
        workspace=(workspace or Path.cwd()).expanduser().resolve(),
        data_dir=Path(os.environ.get("HARNESS_DATA_DIR") or PROJECT_ROOT / "data").expanduser().resolve(),
        env_file=env_file,
        web=os.environ.get("HARNESS_WEB", "1") != "0",
        max_searches=_int_env("HARNESS_MAX_SEARCHES", 5),
        default_provider=os.environ.get("HARNESS_PROVIDER", "anthropic"),
        default_model=os.environ.get("HARNESS_MODEL", "claude-opus-5-5"),
        telegram_api=(os.environ.get("TELEGRAM_API_BASE") or "https://api.telegram.org").rstrip("/"),
        transcribe_api=(os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/"),
        **{name: (os.environ.get(env) or default).rstrip("/") for name, env, default in API_BASES},
    )
