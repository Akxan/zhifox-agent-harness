"""用户在界面上做的选择，以及 API key 的保存。

两样东西存在两个地方：

  data/settings.json   当前用哪家的哪个模型、同步到的模型列表、自己填的价格。不含任何密钥。
  .env                 各家的 API key。这个文件助手自己读不到（见 tools/files.py），
                       执行命令时也看不到里面的内容（见 tools/shell.py），并且不会被提交到 git。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


class Settings:
    def __init__(self, path: Path, default_provider: str, default_model: str):
        self.path = path
        saved = {}
        if path.is_file():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                saved = {}  # 文件坏了就当作没有，下次保存时会重写
        self.provider: str = saved.get("provider") or default_provider
        self.model: str = saved.get("model") or default_model
        self.models: dict[str, list[str]] = saved.get("models") or {}  # 每家同步到的模型列表
        self.prices: dict[str, dict] = saved.get("prices") or {}  # 用户自己填的价格，按模型名

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"provider": self.provider, "model": self.model, "models": self.models, "prices": self.prices}
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)


# ---- API key -------------------------------------------------------------------

# key 里只允许可见的 ASCII 字符，不能有空格和换行，免得写坏 .env 文件
_KEY_PATTERN = re.compile(r"[\x21-\x7e]{8,400}")


def valid_api_key(value: str) -> bool:
    return bool(_KEY_PATTERN.fullmatch(value))


def save_api_key(env_file: Path, name: str, value: str) -> None:
    """把一个 API key 写进 .env，并让它在当前程序里立即生效。value 为空表示删除。

    只改动这一个变量所在的行，文件里的其他内容原样保留。
    """
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.is_file() else []
    assignment = re.compile(rf"\s*(export\s+)?{re.escape(name)}\s*=")
    kept = [line for line in lines if not assignment.match(line)]
    if value:
        kept.append(f"{name}={value}")
        os.environ[name] = value
    else:
        os.environ.pop(name, None)

    temp = env_file.with_name(env_file.name + ".tmp")
    temp.write_text("\n".join(kept) + "\n", encoding="utf-8")
    temp.chmod(0o600)  # 只有你自己的账号能读
    temp.replace(env_file)


def key_hint(name: str) -> str:
    """给界面显示用：只露出 key 的最后 4 位。没有设置时返回空字符串。"""
    value = os.environ.get(name, "")
    return f"…{value[-4:]}" if value else ""
