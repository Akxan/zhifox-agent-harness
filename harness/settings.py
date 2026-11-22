"""用户在界面上做的选择，以及 API key 的保存。

两样东西存在两个地方：

  data/settings.json   当前用哪家的哪个模型、同步到的模型列表、自己填的价格、界面语言、每月预算、浏览器和"操作整台电脑"的开关、
                       命令的沙箱（哪一档、额外允许的路径）。不含任何密钥。
  .env                 各家的 API key。这个文件助手自己读不到（见 tools/files.py），
                       执行命令时也看不到里面的内容（见 tools/shell.py），并且不会被提交到 git。
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from . import i18n, winacl
from .budget import valid_budget
from .config import SANDBOX_MAX_PATHS, SANDBOX_PATH_CHARS
from .safe import private_dir, to_json, write_file
from .sandbox.policy import LEVELS as SANDBOX_LEVELS


class Settings:
    def __init__(self, path: Path, default_provider: str, default_model: str):
        self.path = path
        # 网页界面里，好几个请求可能同时要改设置（一边在同步模型列表，一边打开了一段会话）。
        # "改一项、写回文件"必须一口气做完，否则两边写出来的内容会搅在一起。
        self._lock = threading.Lock()
        saved = {}
        if path.is_file():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError, RecursionError):
                saved = {}  # 文件坏了就当作没有，下次保存时会重写
        if not isinstance(saved, dict):
            saved = {}
        self.provider: str = saved.get("provider") or default_provider
        self.model: str = saved.get("model") or default_model
        self.models: dict[str, list[str]] = saved.get("models") or {}  # 每家同步到的模型列表
        self.prices: dict[str, dict] = saved.get("prices") or {}  # 用户自己填的价格，按模型名
        language = saved.get("language")
        self.language: str = language if language in i18n.LANGUAGES else i18n.DEFAULT  # 界面语言：zh / en / es
        # 每月预算（美元，None 表示不设），和"这个月为多少预算提醒过了"（见 budget.py）
        budget, alert = saved.get("budget"), saved.get("budget_alert")
        self.budget: float | None = float(budget) if budget is not None and valid_budget(budget) else None
        valid_alert = isinstance(alert, dict) and isinstance(alert.get("month"), str) and valid_budget(alert.get("amount"))
        self.budget_alert: dict | None = alert if valid_alert else None
        # 浏览器（见 browser/）：开没开、要不要窗口、Chrome 在哪儿。默认关着：要你自己打开
        self.browser: dict = browser_settings(saved.get("browser"))
        # 操作整台电脑（见 desktop/）：开没开、定时任务和聊天通道能不能用、哪些程序不许碰。默认关着，打开要你在设置里确认一次
        self.computer: dict = computer_settings(saved.get("computer"))
        # 命令的沙箱（见 sandbox/）：对话用哪一档（也是定时任务、聊天通道"跟随设置"时的那一档）、额外允许的路径。默认关着
        self.sandbox: dict = sandbox_settings(saved.get("sandbox"))

    def save(self) -> None:
        with self._lock:
            self._write()

    def choose(self, provider: str, model: str) -> None:
        """记住现在用的是哪家的哪个模型。"""
        with self._lock:
            self.provider, self.model = provider, model
            self._write()

    def set_language(self, language: str) -> None:
        """记住界面语言（网页和终端都用它）。"""
        with self._lock:
            self.language = language
            self._write()

    def set_models(self, provider: str, models: list[str] | None) -> None:
        """记下一家可以选择的模型列表；None 表示忘掉它（删掉了这一家的 key）。"""
        with self._lock:
            if models is None:
                self.models.pop(provider, None)
            else:
                self.models[provider] = models
            self._write()

    def set_price(self, model: str, price: dict | None) -> None:
        """记下用户给一个模型填的价格；None 表示删掉，改回内置的价格。"""
        with self._lock:
            if price is None:
                self.prices.pop(model, None)
            else:
                self.prices[model] = price
            self._write()

    def set_budget(self, amount: float | None) -> None:
        """记下每月预算；None 表示不设。"""
        with self._lock:
            self.budget = amount
            self._write()

    def set_budget_alert(self, alert: dict) -> None:
        """记下"这个月为这个预算提醒过了"（{"month": "2026-11", "amount": 50}），重启后不会再提醒一遍。"""
        with self._lock:
            self.budget_alert = alert
            self._write()

    def set_browser(self, changes: dict) -> None:
        """改浏览器的设置（只改给了的那几项，见 browser_settings）。"""
        with self._lock:
            self.browser = browser_settings({**self.browser, **changes})
            self._write()

    def set_computer(self, changes: dict) -> None:
        """改"操作整台电脑"的设置（只改给了的那几项，见 computer_settings）。"""
        with self._lock:
            self.computer = computer_settings({**self.computer, **changes})
            self._write()

    def set_sandbox(self, changes: dict) -> None:
        """改沙箱的设置（只改给了的那几项，见 sandbox_settings）。"""
        with self._lock:
            self.sandbox = sandbox_settings({**self.sandbox, **changes})
            self._write()

    def _write(self) -> None:
        """写回文件。调用的人要先拿到 self._lock。"""
        private_dir(self.path.parent)
        data = {
            "provider": self.provider,
            "model": self.model,
            "models": self.models,
            "prices": self.prices,
            "language": self.language,
        }
        # 预算只在设了的时候才写：没设预算的人，设置文件和以前一模一样
        if self.budget is not None:
            data["budget"] = self.budget
        if self.budget_alert is not None:
            data["budget_alert"] = self.budget_alert
        if self.browser != BROWSER_DEFAULTS:  # 和预算一样：没动过的人，设置文件和以前一模一样
            data["browser"] = self.browser
        if self.computer != COMPUTER_DEFAULTS:
            data["computer"] = self.computer
        if self.sandbox != SANDBOX_DEFAULTS:
            data["sandbox"] = self.sandbox
        write_file(self.path, to_json(data, readable=True, indent=2))


BROWSER_DEFAULTS = {"enabled": False, "headless": False, "path": ""}


def browser_settings(saved: object) -> dict:
    """浏览器的设置，只认这三项、这几种类型；不认识的、类型不对的按默认的来（文件被改坏了也不出错）。"""
    saved = saved if isinstance(saved, dict) else {}
    path = saved.get("path")
    return {
        "enabled": saved.get("enabled") is True,
        "headless": saved.get("headless") is True,
        "path": path.strip()[:1000] if isinstance(path, str) else "",
    }


COMPUTER_DEFAULTS = {"enabled": False, "unattended": False, "blocked": []}
MAX_BLOCKED_APPS = 50  # "不许碰的程序"最多记这么多个
MAX_APP_NAME_CHARS = 100


def computer_settings(saved: object) -> dict:
    """"操作整台电脑"的设置，只认这三项、这几种类型；不认识的、类型不对的按默认的来。

    enabled     开没开（默认关着）
    unattended  定时任务、聊天通道能不能用（默认不能；能用的时候按任务、通道自己的那一档问你，没有"这段对话里不再问"）
    blocked     不许碰的程序的名字（前台是它们的时候，智狐不截图也不操作，见 desktop/session.py）
    """
    saved = saved if isinstance(saved, dict) else {}
    blocked: list[str] = []
    for name in saved.get("blocked") if isinstance(saved.get("blocked"), list) else []:
        if isinstance(name, str) and name.strip() and name.strip() not in blocked and len(blocked) < MAX_BLOCKED_APPS:
            blocked.append(" ".join(name.split())[:MAX_APP_NAME_CHARS])
    return {"enabled": saved.get("enabled") is True, "unattended": saved.get("unattended") is True, "blocked": blocked}


SANDBOX_DEFAULTS = {"level": "off", "paths": []}


def sandbox_settings(saved: object) -> dict:
    """沙箱的设置，只认这两项、这几种类型；不认识的、类型不对的按默认的来（文件被改坏了也不出错）。

    level   对话用哪一档：off / workspace / network（定时任务、聊天通道选"跟随设置"时也是它）
    paths   额外允许的路径：[{"path": 绝对路径, "write": 能不能写}]。不是绝对路径的、重复的丢掉。
            数据目录和 .env 这类加进来也不算（sandbox/policy.py 的 build 会跳过；网页上加的时候就拒绝，见 webui/sandbox_api.py）
    """
    saved = saved if isinstance(saved, dict) else {}
    level = saved.get("level")
    paths: list[dict] = []
    seen: set[str] = set()
    for item in saved.get("paths") if isinstance(saved.get("paths"), list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        path = item["path"].strip()
        if not path or len(path) > SANDBOX_PATH_CHARS or not path_is_absolute(path) or path in seen or len(paths) >= SANDBOX_MAX_PATHS:
            continue
        seen.add(path)
        paths.append({"path": path, "write": item.get("write") is True})
    return {"level": level if level in SANDBOX_LEVELS else "off", "paths": paths}


def path_is_absolute(path: str) -> bool:
    """一个路径是不是绝对路径（~ 开头的也算：用的时候展开成家目录）。不认换行这类控制字符。"""
    if any(not char.isprintable() for char in path):
        return False
    return path.startswith("~") or Path(path).is_absolute()


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
    temp.write_text("\n".join(kept) + "\n", encoding="utf-8", newline="")
    temp.chmod(0o600)  # 只有你自己的账号能读
    winacl.restrict(temp, folder=False)  # Windows 上靠访问控制列表（见 winacl.py）；先设好再换上去，换上去的那一刻就已经是这样了
    temp.replace(env_file)


def key_hint(name: str) -> str:
    """给界面显示用：只露出 key 的最后 4 位。没有设置时返回空字符串。"""
    value = os.environ.get(name, "")
    return f"…{value[-4:]}" if value else ""
