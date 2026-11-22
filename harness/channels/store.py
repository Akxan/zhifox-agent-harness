"""聊天通道的设置和记录：data/channels.json。

里面按平台各有一份：

  credentials  连接这个平台用的东西（Telegram 是 {"token"}，Slack 是 {"bot_token", "app_token"}，飞书是
             {"app_id", "app_secret"}……要填哪些见 platforms.py 的 fields）。里面的密钥就是机器人的钥匙：谁拿到它，
             谁就能以机器人的身份收发消息。所以它们只存在这个文件里（权限 600，只有你的账号能读），不出现在发给浏览器的
             任何东西里（页面只看得到最后 4 位），不写进日志，导出备份时换成占位符（见 export.MASKED_FILES），
             助手的文件工具也读不了它（见 app.py 里的 protected）。旧版本存的是一个 token 字段，读进来时换成 {"token": …}
  bot        机器人自己的名字（@username）、打开和它聊天的链接，给页面显示用
  enabled    开着没有
  approval   这个通道里"要不要先问你"：ask / readonly / edits / all，和对话里的几档同名、同义（见 ../permissions.py）
  sandbox    这个通道里命令的沙箱：default 跟随设置 / off / workspace / network（见 ../sandbox/）
  owner      配对绑定的主人：账号编号、名字、什么时候配对的。只有他发来的消息会被处理（见 pairing.py）
  pairing    正在进行的配对：配对码（secret）、什么时候过期、猜错了几次、提醒过哪些人
  chats      每个聊天接着的是哪一段会话（重启以后还接着聊），最后一次说话是什么时候
  offset     Telegram 的消息读到了第几条（见 telegram.py），重启以后不会把读过的再处理一遍
  ignored    别人发来、被忽略的消息有几条（页面上显示，让你知道有人在试）

为什么要防"两个程序同时写"：在服务器上，智狐作为服务一直开着，你又在命令行里运行
`harness channels pair telegram`，两边会同时改这个文件。所以每次改动都是"拿文件锁 → 从磁盘读最新的 →
改一处 → 写回去"（见 _change），一个程序写完另一个才能读；开着的那一个每处理一条消息之前看一眼文件变了没有
（refresh），命令行里生成的配对码它马上就认得。

文件坏了（不是合法的 JSON）的时候，不改它、也不覆盖它：problem 里写着原因，所有改动都会被拒绝，
等你自己修好或者删掉。不然一点一个按钮，你原来的设置就被一份空的盖掉了。
"""

from __future__ import annotations

import copy
import fcntl
import json
import math
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..config import PAIRING_ATTEMPTS, PAIRING_SECONDS
from ..i18n import t
from ..permissions import MODES
from ..safe import private_dir, to_json, write_file
from ..sandbox import CHOICES as SANDBOX_CHOICES
from ..sandbox import DEFAULT as SANDBOX_DEFAULT
from . import pairing
from .base import ChannelError
from .platforms import fields, secret_fields

FORMAT_VERSION = 1


def pair_link(platform: str, bot: dict | None, code: str) -> str:
    """配对时页面上"在 Telegram 里打开"那个按钮的链接。Telegram 的链接能替你把配对码发过去（/start 123456，
    见 pairing.py）；别的平台只能打开和机器人的聊天，配对码要你自己发。没有的话是空字符串。"""
    if not bot:
        return ""
    if platform == "telegram" and bot.get("username"):
        return f"https://t.me/{bot['username']}?start={code}"
    return bot.get("link", "")


def _fresh() -> dict:
    return {"version": FORMAT_VERSION, "platforms": {}}


def _blank() -> dict:
    """一个平台还什么都没设置时的样子。"""
    return {"enabled": True, "approval": "ask", "sandbox": SANDBOX_DEFAULT, "credentials": {}, "bot": None, "owner": None, "pairing": None, "chats": {}, "offset": 0, "ignored": 0, "last_active": 0.0}


def _text(value: Any, limit: int = 300) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _link(value: Any) -> str:
    """只认 https:// 的链接（页面上会把它做成一个按钮）。"""
    return value[:500] if isinstance(value, str) and value.startswith("https://") else ""


def _credentials(item: dict) -> dict[str, str]:
    raw = item.get("credentials")
    found = {name[:40]: value[:400] for name, value in raw.items() if isinstance(name, str) and name and isinstance(value, str) and value} if isinstance(raw, dict) else {}
    legacy = _text(item.get("token"), 400)
    if legacy and "token" not in found:
        found["token"] = legacy  # 第一版只有 Telegram，存的是一个 token 字段
    return found


def _bot(bot: Any) -> dict | None:
    if not isinstance(bot, dict):
        return None
    return {
        "id": _text(str(bot.get("id", ""))),
        "username": _text(bot.get("username")),
        "name": _text(bot.get("name")),
        "at": bot.get("at") is not False,  # 名字前面加不加 @（Telegram、Discord、Slack 加；飞书、钉钉、QQ 的机器人没有 @用户名）
        "app": _text(str(bot.get("app", ""))),  # 平台上"应用"的编号（Discord 登记命令、Slack 打开私聊要用）
        "link": _link(bot.get("link")),  # 打开和机器人的聊天
        "install": _link(bot.get("install")),  # 把机器人加到你的账号（Discord 要先加，才能和它私聊）
    }


def handle(bot: dict | None) -> str:
    """机器人给人看的称呼：@zhifox_bot，或者（没有 @用户名的平台）它的名字。"""
    if not bot:
        return ""
    if bot.get("at", True) and bot.get("username"):
        return "@" + bot["username"]
    return bot.get("name") or bot.get("username") or ""


def _number(value: Any) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return 0.0
    return float(value)


def _normalize(raw: Any) -> dict:
    """读进来的文件可能被手改过：只留认识的字段、类型对的值，别的丢掉（不报错）。"""
    data = _fresh()
    platforms = raw.get("platforms") if isinstance(raw, dict) else None
    if not isinstance(platforms, dict):
        return data
    for name, item in platforms.items():
        if not isinstance(name, str) or not isinstance(item, dict):
            continue
        entry = _blank()
        entry["enabled"] = item.get("enabled") is not False
        entry["approval"] = item.get("approval") if item.get("approval") in MODES else "ask"
        entry["sandbox"] = item.get("sandbox") if item.get("sandbox") in SANDBOX_CHOICES else SANDBOX_DEFAULT
        entry["credentials"] = _credentials(item)
        entry["bot"] = _bot(item.get("bot"))
        owner = item.get("owner")
        if isinstance(owner, dict) and _text(owner.get("id")):
            entry["owner"] = {"id": _text(owner.get("id")), "name": _text(owner.get("name")), "paired_at": _number(owner.get("paired_at"))}
        code = item.get("pairing")
        if isinstance(code, dict) and pairing.valid(code.get("secret")):
            hinted = code.get("hinted") if isinstance(code.get("hinted"), list) else []
            entry["pairing"] = {
                "secret": code["secret"],
                "expires": _number(code.get("expires")),
                "attempts": int(_number(code.get("attempts"))),
                "hinted": [_text(user) for user in hinted if isinstance(user, str)][:50],
            }
        chats = item.get("chats")
        if isinstance(chats, dict):
            for chat, info in chats.items():
                if isinstance(chat, str) and isinstance(info, dict):
                    entry["chats"][chat] = {"conversation": _text(info.get("conversation")), "last_active": _number(info.get("last_active"))}
        entry["offset"] = int(_number(item.get("offset")))
        entry["ignored"] = int(_number(item.get("ignored")))
        entry["last_active"] = _number(item.get("last_active"))
        data["platforms"][name] = entry
    return data


class ChannelStore:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = path
        self.clock = clock
        self._lock = threading.RLock()
        self._data = _fresh()
        self._stamp: tuple | None = None  # 上一次读到的文件是什么样子（修改时间、大小、编号），用来判断变了没有
        self.problem = ""  # 文件读不出来的原因；空字符串表示没问题
        self._listeners: list[Callable[[], None]] = []
        with self._lock:
            self._load()

    # ---- 读和写 ------------------------------------------------------------

    def _seen(self) -> tuple | None:
        try:
            info = os.stat(self.path)
        except FileNotFoundError:
            return None
        return (info.st_mtime_ns, info.st_size, info.st_ino)

    def _load(self) -> None:
        """从磁盘读。调用的人拿着 self._lock。"""
        self._stamp = self._seen()
        if self._stamp is None:
            self._data, self.problem = _fresh(), ""
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
            self.problem = t("通道的设置文件读不出来（{error}）。为了不把它盖掉，现在什么都改不了。请修好或者删掉这个文件：{path}", error=getattr(exc, "strerror", None) or type(exc).__name__, path=self.path)
            return
        self._data, self.problem = _normalize(raw), ""

    def refresh(self) -> bool:
        """文件被别的程序改过的话（比如命令行里生成了配对码），重新读一遍。返回有没有变。"""
        with self._lock:
            if self._seen() == self._stamp:
                return False
            self._load()
        self._notify()
        return True

    def on_change(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def _notify(self) -> None:
        for callback in self._listeners:
            callback()

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        """拿到"只有我在改这个文件"的锁（别的程序也认这把锁）。锁是旁边的一个小文件，程序退出时自动放开。"""
        private_dir(self.path.parent)
        handle = open(self.path.with_name(self.path.name + ".lock"), "a")  # noqa: SIM115 —— 下面的 finally 里关
        try:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX)
            except OSError:
                pass  # 少数文件系统不支持这种锁：那就只靠线程锁，照常改
            yield
        finally:
            handle.close()

    def _change(self, change: Callable[[dict], Any], notify: bool = True) -> Any:
        """改一处：拿锁 → 读最新的 → change(data) → 写回去。返回 change 的返回值。文件坏了时抛出 ChannelError。"""
        with self._lock, self._file_lock():
            if self._seen() != self._stamp:
                self._load()
            if self.problem:
                raise ChannelError(self.problem)
            data = copy.deepcopy(self._data)
            result = change(data)
            try:
                write_file(self.path, to_json(data, readable=True, indent=2) + "\n")
            except OSError as exc:
                raise ChannelError(t("没能保存通道的设置：{error}", error=exc.strerror or exc)) from exc
            self._data = data
            self._stamp = self._seen()
        if notify:
            self._notify()
        return result

    def _entry(self, data: dict, platform: str) -> dict:
        """一个平台的那一份，没有就新建一份空白的。只给页面、命令行上的操作用（它们之前都检查过这个平台设置了没有）。"""
        return data["platforms"].setdefault(platform, _blank())

    @staticmethod
    def _existing(data: dict, platform: str) -> dict | None:
        """一个平台的那一份，没有就是 None，不新建。给"运行时的记录"用（见下面那一节）：
        删掉一个平台时，它收消息的线程可能还没停下，还会再记一笔；用 _entry 的话，删掉的平台又会以一份空白的样子回来。"""
        return data["platforms"].get(platform)

    def get(self, platform: str) -> dict:
        """一个平台的全部设置（一份拷贝，改它不影响存着的）。没设置过的是空白的那一份。"""
        with self._lock:
            return copy.deepcopy(self._data["platforms"].get(platform) or _blank())

    def known(self) -> list[str]:
        """设置过的平台。"""
        with self._lock:
            return list(self._data["platforms"])

    # ---- 页面、命令行上的操作 ----------------------------------------------

    def connect(self, platform: str, credentials: dict[str, str], bot: dict) -> None:
        """记下（新的）凭据和机器人的名字。换成了另一个机器人的话，原来的绑定和聊天记录作废，要重新配对。"""

        def change(data: dict) -> None:
            entry = self._entry(data, platform)
            if entry["bot"] is not None and entry["bot"].get("id") != bot.get("id"):
                entry.update(owner=None, pairing=None, chats={}, offset=0, ignored=0)
            entry.update(credentials=dict(credentials), bot=_bot(bot), enabled=True)

        self._change(change)

    def credentials(self, platform: str) -> dict[str, str]:
        return self.get(platform)["credentials"]

    def configured(self, platform: str) -> bool:
        """这个平台要填的都填了没有（见 platforms.fields）。"""
        given = self.credentials(platform)
        wanted = [field["id"] for field in fields(platform)]
        return bool(wanted) and all(given.get(name) for name in wanted)

    def set_enabled(self, platform: str, enabled: bool) -> None:
        self._change(lambda data: self._entry(data, platform).update(enabled=enabled))

    def set_approval(self, platform: str, mode: str) -> None:
        if mode not in MODES:
            raise ChannelError(t("没有 {mode} 这种模式。", mode=mode))
        self._change(lambda data: self._entry(data, platform).update(approval=mode))

    def set_sandbox(self, platform: str, level: str) -> None:
        if level not in SANDBOX_CHOICES:
            raise ChannelError(t("没有 {mode} 这种沙箱。", mode=level))
        self._change(lambda data: self._entry(data, platform).update(sandbox=level))

    def remove(self, platform: str) -> None:
        """忘掉这个平台的一切：token、绑定、聊天记录（会话本身还在会话列表里）。"""
        self._change(lambda data: data["platforms"].pop(platform, None))

    def unbind(self, platform: str) -> None:
        """解除绑定：之后谁的消息都不处理，直到重新配对。"""
        self._change(lambda data: self._entry(data, platform).update(owner=None, chats={}))

    def start_pairing(self, platform: str) -> dict:
        """生成一个新的配对码（之前的作废）。返回 {"code", "expires"}。"""
        code, expires = pairing.new_code(), self.clock() + PAIRING_SECONDS

        def change(data: dict) -> None:
            self._entry(data, platform)["pairing"] = {"secret": code, "expires": expires, "attempts": 0, "hinted": []}

        self._change(change)
        return {"code": code, "expires": expires}

    def cancel_pairing(self, platform: str) -> None:
        self._change(lambda data: self._entry(data, platform).update(pairing=None))

    def pairing(self, platform: str) -> dict | None:
        """正在进行、还没过期的配对；没有的话是 None。"""
        code = self.get(platform)["pairing"]
        return code if code is not None and code["expires"] > self.clock() else None

    def attempt(self, platform: str, user: str, name: str, text: str) -> str:
        """一个还不是主人的人发来一条消息：按配对的规则处理（见 pairing.py）。返回发生了什么：

          paired    配对码对上了，他成了主人
          wrong     在配对，发来的像配对码，但不对（还能再试）
          locked    猜错满了次数，这个配对码作废了
          hint      在配对，发来的不是配对码，而且还没提醒过他：回一句"请发送配对码"
          ignore    别的情况（没在配对、已经提醒过了）：什么都不回
        """
        now = self.clock()

        def change(data: dict) -> str:
            entry = self._existing(data, platform)
            if entry is None:
                return "ignore"  # 这个平台刚被删掉，还没停下的那个线程又交来一条：不理，也不把它记回来
            code = entry["pairing"]
            if code is None or code["expires"] <= now:
                entry["pairing"] = None
                entry["ignored"] += 1
                return "ignore"
            given = pairing.find_code(text)
            if given is None:
                if user in code["hinted"]:
                    entry["ignored"] += 1
                    return "ignore"
                code["hinted"] = [*code["hinted"], user][-50:]
                return "hint"
            if pairing.same(given, code["secret"]):
                entry["owner"] = {"id": user, "name": name, "paired_at": now}
                entry["pairing"] = None
                entry["chats"] = {}  # 新的主人：以前的聊天和会话的对应关系不再算数
                return "paired"
            code["attempts"] += 1
            if code["attempts"] >= PAIRING_ATTEMPTS:
                entry["pairing"] = None
                return "locked"
            return "wrong"

        return self._change(change)

    def is_owner(self, platform: str, user: str) -> bool:
        owner = self.get(platform)["owner"]
        return owner is not None and owner["id"] == user

    # ---- 运行时的记录 --------------------------------------------------------

    def remember_chat(self, platform: str, chat: str, conversation: str) -> None:
        """记下这个聊天接着的是哪一段会话，以及最后一次说话的时间。"""
        now = self.clock()

        def change(data: dict) -> None:
            entry = self._existing(data, platform)
            if entry is None:
                return  # 平台刚被删掉（见 _existing）
            entry["chats"][chat] = {"conversation": conversation, "last_active": now}
            entry["last_active"] = now

        self._change(change)

    def conversation_of(self, platform: str, chat: str) -> str:
        return self.get(platform)["chats"].get(chat, {}).get("conversation", "")

    def set_offset(self, platform: str, offset: int) -> None:
        # 只是记着读到了哪儿，页面上看不出变化，不用通知谁。平台刚被删掉的话什么都不记（见 _existing）
        def change(data: dict) -> None:
            entry = self._existing(data, platform)
            if entry is not None:
                entry["offset"] = offset

        self._change(change, notify=False)

    # ---- 给页面看的 --------------------------------------------------------

    def summary(self, platform: str) -> dict:
        """一个平台给页面看的样子。密钥只露出最后 4 位（不是密钥的 App ID 这类照给）；配对码要显示在页面上让你抄，所以照给。"""
        entry = self.get(platform)
        code = self.pairing(platform)
        bot, owner = entry["bot"], entry["owner"]
        given = entry["credentials"]
        hints = {}
        for field in fields(platform):
            value = given.get(field["id"], "")
            hints[field["id"]] = (f"…{value[-4:]}" if field["secret"] else value) if value else ""
        secrets = secret_fields(platform)
        shown = {"username": bot["username"], "name": bot["name"], "handle": handle(bot), "link": bot["link"], "install": bot["install"]} if bot else None
        return {
            "configured": self.configured(platform),
            "enabled": entry["enabled"],
            "approval": entry["approval"],
            "sandbox": entry["sandbox"],
            "token_hint": hints.get(secrets[0], "") if secrets else "",
            "hints": hints,
            "bot": shown,
            "owner": {"name": owner["name"], "paired_at": owner["paired_at"]} if owner else None,
            "pairing": {
                "code": code["secret"],
                "expires": code["expires"],
                "attempts_left": max(0, PAIRING_ATTEMPTS - code["attempts"]),
                "link": pair_link(platform, bot, code["secret"]),
            } if code else None,
            "last_active": entry["last_active"] or None,
            "ignored": entry["ignored"],
        }
