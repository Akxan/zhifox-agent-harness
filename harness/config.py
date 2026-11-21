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


# 倒回最后一轮（编辑最后一条消息、重新生成，见 rewind.py）之前，这一轮做过的、可能改动了东西的事要列给你看：
# 最多列这么多行，再多的合成一句"还有几项"
MAX_REWIND_EFFECTS = 6

# 用户自己写的提示词最长多少个字符。提示词每一轮都要发给模型（命中缓存时便宜，但第一次和缓存过期后按原价），
# 两万字大约是一万到两万个 token，再长就太贵了，也会挤占对话能用的上下文
MAX_PROMPT_CHARS = 20_000

# 定时任务最多同时运行几个（你自己的对话不算在里面，它照常进行）。每个运行都在调用模型、花钱，还可能开着命令，
# 同时跑得太多既费钱又难盯住。再到时间的先排队，前面的跑完一个，排在最前面的那个就开始（见 webui/scheduler.py）
MAX_PARALLEL_TASKS = 3

# 花费账本（见 ledger.py）。超过这么多天的明细，合并成"每天、每个模型、每种来源一行"，账本文件不会无限变大；
# 每天、每月、每个模型花了多少都不变，只是看不到一笔一笔的时间了
LEDGER_DETAIL_DAYS = 90
# 网页上"花费明细"的柱状图显示最近这么多天
LEDGER_CHART_DAYS = 30

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


@dataclass(frozen=True)
class Config:
    effort: str  # Claude 思考的力度：low / medium / high / xhigh / max
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
    # Telegram Bot API 的地址。一般不用改；在连不上 api.telegram.org 的地方可以换成自建的 Bot API 服务器，
    # 测试和演示把它指向本地的假服务器。只能用环境变量 TELEGRAM_API_BASE 改，页面上改不了：token 会发到这个地址去
    telegram_api: str = "https://api.telegram.org"
    # 别的聊天平台的接口地址，道理和 telegram_api 一样：只能用环境变量改（DISCORD_API_BASE……），测试和演示指向本地的假服务器
    discord_api: str = "https://discord.com/api/v10"
    slack_api: str = "https://slack.com/api"
    qq_api: str = "https://api.bot.qq.com"  # 换 access_token、开网关、发消息都在这一个域名下（2026 年 8 月起的新地址）
    feishu_api: str = "https://open.feishu.cn"
    dingtalk_api: str = "https://api.dingtalk.com"

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
    def memory_dir(self) -> Path:
        return self.data_dir / "memories"

    @property
    def settings_file(self) -> Path:
        return self.data_dir / "settings.json"

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


def load_env_file(path: Path) -> None:
    """把 .env 文件里的 KEY=VALUE 读进环境变量。已经存在的环境变量不会被覆盖。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip("'\"")
        if key and value:
            os.environ.setdefault(key, value)


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
        **{name: (os.environ.get(env) or default).rstrip("/") for name, env, default in API_BASES},
    )
