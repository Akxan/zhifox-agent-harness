"""配置：模型、路径、开关。所有可以调的东西都集中在这里。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 下面两个开关用的是 API 的 beta 功能。
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


@dataclass(frozen=True)
class Config:
    model: str
    effort: str  # 模型思考的力度：low / medium / high / xhigh / max
    max_tokens: int  # 单次回复的上限（思考也算在里面）
    max_steps: int  # 一轮对话里最多调用模型多少次，防止死循环
    workspace: Path  # 工作目录：助手可以直接读取这里面的文件
    data_dir: Path  # 助手自己的数据：记忆、待办、文件备份
    env_file: Path
    web: bool  # 是否启用联网搜索

    @property
    def todo_file(self) -> Path:
        return self.data_dir / "todos.json"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def memory_dir(self) -> Path:
        return self.data_dir / "memories"

    @property
    def fallbacks(self) -> bool:
        return ENABLE_FALLBACKS and self.model in FALLBACK_CAPABLE_MODELS

    @property
    def compaction(self) -> bool:
        return ENABLE_COMPACTION


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


def load_config() -> Config:
    env_file = PROJECT_ROOT / ".env"
    load_env_file(env_file)
    return Config(
        model=os.environ.get("HARNESS_MODEL", "claude-opus-5-5"),
        effort=os.environ.get("HARNESS_EFFORT", "medium"),
        max_tokens=64000,
        max_steps=50,
        workspace=Path.cwd().resolve(),
        data_dir=PROJECT_ROOT / "data",
        env_file=env_file,
        web=os.environ.get("HARNESS_WEB", "1") != "0",
    )


def has_credentials() -> bool:
    """是否能找到调用 API 的凭证（环境变量，或 `ant auth login` 保存的登录信息）。"""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    return (Path.home() / ".config" / "anthropic").is_dir()
