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


# 用户自己写的提示词最长多少个字符。提示词每一轮都要发给模型（命中缓存时便宜，但第一次和缓存过期后按原价），
# 两万字大约是一万到两万个 token，再长就太贵了，也会挤占对话能用的上下文
MAX_PROMPT_CHARS = 20_000

# 花费账本（见 ledger.py）。超过这么多天的明细，合并成"每天、每个模型、每种来源一行"，账本文件不会无限变大；
# 每天、每月、每个模型花了多少都不变，只是看不到一笔一笔的时间了
LEDGER_DETAIL_DAYS = 90
# 网页上"花费明细"的柱状图显示最近这么多天
LEDGER_CHART_DAYS = 30


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
    def mcp_file(self) -> Path:
        return self.data_dir / "mcp.json"  # 接入的 MCP 服务器（见 mcp/config.py）

    @property
    def ledger_file(self) -> Path:
        return self.data_dir / "ledger.jsonl"


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


def load_config() -> Config:
    env_file = PROJECT_ROOT / ".env"
    load_env_file(env_file)
    return Config(
        effort=os.environ.get("HARNESS_EFFORT", "medium"),
        max_tokens=64000,
        max_steps=50,
        workspace=Path.cwd().resolve(),
        data_dir=Path(os.environ.get("HARNESS_DATA_DIR") or PROJECT_ROOT / "data").expanduser().resolve(),
        env_file=env_file,
        web=os.environ.get("HARNESS_WEB", "1") != "0",
        max_searches=_int_env("HARNESS_MAX_SEARCHES", 5),
        default_provider=os.environ.get("HARNESS_PROVIDER", "anthropic"),
        default_model=os.environ.get("HARNESS_MODEL", "claude-opus-5-5"),
    )
