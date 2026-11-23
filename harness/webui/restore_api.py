"""网页上"设置 → 从备份恢复…"背后的接口：/api/restore/……。怎么检查、怎么恢复在 ../restore.py（检查在 restore_check.py、预览在 restore_plan.py），这里只管网页这一边。

  POST /api/restore/info      打开窗口时：网页上传最大多大、现在能不能恢复（对话、定时任务、聊天通道在忙就说为什么）、自动备份放在哪儿
  POST /api/restore/upload    上传备份。请求体是压缩包原样的字节（不是 JSON），边读边写进系统的临时文件夹；看过一遍，回答预览
  POST /api/restore/discard   不恢复了：删掉上传的那一份
  POST /api/restore/apply     确认恢复（stage、mode 是 overwrite 或 merge、confirm: true）
  POST /api/restore/reveal    在文件管理器里显示恢复前的自动备份：只认 restore-backups/ 里已有的、名字是那个样子的文件，不给名字就打开那个文件夹

为什么上传不用 JSON：备份可能有几个 GB（多媒体库），base64 进 JSON 要大三分之一，还要整个读进内存解析。
请求体原样边读边写进临时文件，内存里只有一小块。上限是 config.RESTORE_MAX_UPLOAD_BYTES（页面上传之前就照着它拦下）；
超过的读掉最多 RESTORE_DISCARD_BYTES 再回答 413（Windows 上不读就关连接，对方只看到"连接被中止"）。
上传好的那一份只有一个：再传一个就换掉，不确认的话 RESTORE_STAGE_SECONDS 秒以后删掉，退出时也删掉（见 lifecycle.stop）。

恢复以后怎么让程序用上新的数据：和重启一样，照着磁盘重新组装一遍（build_app），换上一个新的 WebApp。
  1. 拿住 web._turn（"没有一段对话在跑"，见 sessions.py 的 AllIdle；拿不到就说是哪几段在跑）；
     定时任务在跑、聊天通道正占着一段会话，不恢复，说明为什么
  2. 停下定时任务、聊天通道、MCP 服务器、浏览器（lifecycle.stop，和退出时一样，存好盘）
  3. 恢复（restore.apply：先自动备份，再写）
  4. 照着新的数据组装一个新的 App 和 WebApp，换到服务器上（server.web）；原来开着定时任务、聊天通道的，新的也开起来。
     组装失败的话，把数据换回去（Restored.rollback），照原来的数据再组装一次
  5. 旧的 WebApp 不再用：它的锁一直拿着不放。万一还有请求拿着它，只会得到"正在忙"，改不了任何东西
为什么不是"写完文件、请你重启智狐"：程序还开着，内存里拿着旧的设置、任务、通道的进度，下一次存盘就会把恢复的内容盖回去；
在终端里开的智狐每次启动还换一个口令，重启以后这个页面就连不上了。换一个新的 WebApp 等于在原地重启：
口令、端口、事件流（同一个 WebUI）都不变，页面收到 reset 和新的状态，照着画就行。
测试和演示换掉的东西（模型的客户端、聊天平台连本地的假服务器）照样带到新的那一个上（见 _rebuild）。
"""

from __future__ import annotations

import os
import secrets
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from io import BufferedIOBase
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from .. import restore
from ..app import build_app
from ..config import (
    RESTORE_DISCARD_BYTES,
    RESTORE_MAX_UPLOAD_BYTES,
    RESTORE_STAGE_SECONDS,
    RESTORE_STOP_SECONDS,
)
from ..i18n import t
from ..restore import BEFORE, MODES, RestoreError, backups_folder, size_text
from ..safe import clean_text
from . import lifecycle
from .ui import said

if TYPE_CHECKING:
    from .server import Server, WebApp

UPLOAD_PATH = "/api/restore/upload"  # 这个地址的请求体不是 JSON（见 server.py 的 do_POST）


@dataclass
class Stage:
    """上传好、还没确认的一份备份。"""

    path: Path  # 系统临时文件夹里的那个文件（只有自己的账号能读）
    name: str  # 浏览器那边的文件名，只是给人看的
    size: int
    archive: restore.Archive
    at: float


class RestoreAPI:
    def __init__(self, web: WebApp, reveal: Callable[[Path], None]):
        self.web = web
        self._reveal = reveal  # 在文件管理器里显示（reveal.py 的 reveal_in_file_manager）
        self._lock = threading.Lock()
        self._stages: dict[str, Stage] = {}

    def route(self, action: str, body: dict, server: Server) -> tuple[int, dict]:
        """处理一个 /api/restore/<action> 请求（上传除外，见 upload），返回（状态码，回答）。"""
        if action == "info":
            return 200, {"ok": True, "max_bytes": RESTORE_MAX_UPLOAD_BYTES, "folder": str(self._folder()), "blocked": self._blocked(look=True)}
        if action == "discard":
            self._drop(body.get("stage"))
            return 200, {"ok": True}
        if action == "apply":
            return self._apply(body, server)
        if action == "reveal":
            return self._show(body.get("name"))
        return 404, {"error": "not found"}

    def close(self) -> None:
        """退出：上传了、还没确认的删掉（里面是你的数据）。"""
        with self._lock:
            stages, self._stages = list(self._stages.values()), {}
        for stage in stages:
            stage.path.unlink(missing_ok=True)

    # ---- 上传 ------------------------------------------------------------------

    def upload(self, headers: Any, stream: BufferedIOBase) -> tuple[int, dict]:
        """收下上传的备份（请求体是原样的字节），看一遍，回答预览。headers 是请求头，stream 是还没读的请求体。

        server.py 直接调用它（不经过 _route 外面那一层"接住所有异常"），所以这里自己接住：一定要给浏览器一个回答。"""
        try:
            return self._upload(headers, stream)
        except Exception as exc:  # noqa: BLE001
            return 500, {"error": t("助手程序处理这个请求时出错了：{error}", error=str(exc) or type(exc).__name__)}

    def _upload(self, headers: Any, stream: BufferedIOBase) -> tuple[int, dict]:
        try:
            length = int(headers.get("Content-Length") or "")
        except ValueError:
            length = -1
        if length < 0:
            return 411, {"error": t("参数不正确")}
        if length > RESTORE_MAX_UPLOAD_BYTES:
            _discard(stream, min(length, RESTORE_DISCARD_BYTES))
            return 413, {"error": t("备份太大了：网页上一次最多上传 {size}。更大的备份可以在终端里用 uv run harness restore 恢复。", size=size_text(RESTORE_MAX_UPLOAD_BYTES))}
        if length == 0:
            return 400, {"error": t("没有收到文件。")}
        name = clean_text(unquote(headers.get("X-Restore-Name") or ""))[:200]
        self._sweep()
        descriptor, temp = tempfile.mkstemp(prefix="zhifox-restore-", suffix=".zip")  # 权限 600，只有自己的账号能读
        path = Path(temp)
        try:
            received = 0
            with os.fdopen(descriptor, "wb") as out:
                while received < length:
                    chunk = stream.read(min(1 << 20, length - received))
                    if not chunk:
                        break
                    out.write(chunk)
                    received += len(chunk)
            if received != length:
                path.unlink(missing_ok=True)
                return 400, {"error": t("上传没有完成：连接中途断开了。再试一次。")}
            archive = restore.check(path)
            preview = restore.preview(archive, self.web.app.config.data_dir)
        except RestoreError as exc:
            path.unlink(missing_ok=True)
            return 409, {"error": str(exc)}
        except OSError as exc:  # 临时文件写不进去（磁盘满了），或者浏览器那边断开了
            path.unlink(missing_ok=True)
            return 409, {"error": t("没能收下这个文件（{error}）。", error=exc.strerror or exc)}
        stage = secrets.token_hex(16)
        with self._lock:
            stale, self._stages = list(self._stages.values()), {stage: Stage(path, name, length, archive, time.monotonic())}
        for item in stale:
            item.path.unlink(missing_ok=True)
        for mode in preview["modes"].values():
            _worded(mode["steps"])
        return 200, {"ok": True, "stage": stage, "name": name, "size": length, "preview": preview, "blocked": self._blocked(look=True)}

    def _drop(self, stage: object) -> None:
        with self._lock:
            found = self._stages.pop(stage, None) if isinstance(stage, str) else None
        if found is not None:
            found.path.unlink(missing_ok=True)

    def _sweep(self) -> None:
        """过期的（上传了却一直没确认）删掉。"""
        now = time.monotonic()
        with self._lock:
            stale = [key for key, stage in self._stages.items() if now - stage.at > RESTORE_STAGE_SECONDS]
            dropped = [self._stages.pop(key) for key in stale]
        for stage in dropped:
            stage.path.unlink(missing_ok=True)

    # ---- 什么时候不能恢复 ------------------------------------------------------

    def _blocked(self, look: bool = False) -> str:
        """现在不能恢复的原因；能的话是空字符串。look 为真时也看一眼对话的锁（只是看，马上放开）。"""
        web = self.web
        if look:
            if not web._turn.acquire(blocking=False):
                return web._busy_everywhere()
            web._turn.release()
        running = [task["title"] for task in web.tasks.snapshot()[0] if task.get("running")]
        if running:
            return t("定时任务“{title}”正在运行。等它跑完，或者在左边的“任务”里停下它，再恢复。", title=running[0])
        for conversation, who in web.claims.held().items():
            if who != "web":
                return t("{platform} 里正在进行一轮对话。等它结束（或者在聊天里发 /stop），再恢复。", platform=web.channels.holder(conversation) or who.partition(":")[0])
        return ""

    # ---- 恢复 ------------------------------------------------------------------

    def _apply(self, body: dict, server: Server) -> tuple[int, dict]:
        stage_id, mode, confirm = body.get("stage"), body.get("mode"), body.get("confirm")
        if not isinstance(stage_id, str) or mode not in MODES or confirm is not True:
            return 400, {"error": t("参数不正确")}
        self._sweep()
        with self._lock:
            stage = self._stages.get(stage_id)
        if stage is None:
            return 409, {"error": t("上传的备份已经不在了（只留 {minutes} 分钟），请重新选一次。", minutes=RESTORE_STAGE_SECONDS // 60), "gone": True}
        web = self.web
        if not web._turn.acquire(blocking=False):
            return 409, {"error": web._busy_everywhere()}
        retired = False
        try:
            blocked = self._blocked() or web._before_leaving(web.sessions.viewed, "restore")
            if blocked:
                return 409, {"error": blocked}
            with self._lock:
                self._stages.pop(stage_id, None)  # 从这里开始它归这一次恢复：lifecycle.stop 收尾时不会把它删掉
            try:
                status, payload, retired = self._restore(stage, mode, server)
            finally:
                stage.path.unlink(missing_ok=True)
            return status, payload
        finally:
            if not retired:
                web._turn.release()

    def _restore(self, stage: Stage, mode: str, server: Server) -> tuple[int, dict, bool]:
        """停下、恢复、换上新的 WebApp（见文件开头）。返回（状态码，回答，旧的 WebApp 是不是已经换下来了）。"""
        old = self.web
        config = old.app.config
        was_started = lifecycle.started(old)
        opened = old.app.conversation.id
        lifecycle.stop(old, RESTORE_STOP_SECONDS, holding_turn=True)
        error, done = "", None
        try:
            done = restore.apply(stage.archive, config.data_dir, mode, [config.env_file], datetime.now())
        except RestoreError as exc:
            error = str(exc)
        try:
            fresh = self._rebuild()
        except Exception as exc:  # noqa: BLE001 —— 照着新的数据开不起来，什么错都要接住，把数据换回去
            if done is None:
                return 500, {"error": t("没能重新开起来（{error}）。请重启智狐。", error=f"{type(exc).__name__}: {exc}"), "gone": True}, False
            done.rollback()
            done = None
            error = t("恢复好了，可是照着新的数据没能开起来（{error}），已经换回原来的数据。恢复前的自动备份还在。", error=f"{type(exc).__name__}: {exc}")
            try:
                fresh = self._rebuild()
            except Exception as again:  # noqa: BLE001
                return 500, {"error": t("没能重新开起来（{error}）。请重启智狐。", error=f"{type(again).__name__}: {again}"), "gone": True}, False
        if done is not None:
            done.commit()
        server.web = fresh
        if was_started:
            lifecycle.start(fresh)
        # 原来开着的那段会话还在的话（合并，或者恢复失败了），接着开着它；不在了就是一段新对话
        if fresh.open_conversation(opened) or fresh.app.conversation.id != opened:
            fresh.ui.reset()
            fresh.publish_state()
        if error:
            return 409, {"error": error, "restarted": True}, True
        assert done is not None  # 给类型检查看的：没有出错，就是恢复好了、也没有换回去
        result = done.summary()
        _worded(result["steps"])
        return 200, {"ok": True, "result": result}, True

    def _rebuild(self) -> WebApp:
        """照着磁盘上的数据重新组装一遍（和启动时一样），接到同一个界面、同一个口令上。

        同一个界面是你正看着的那一段的事件日志（old.ui）：页面开着的事件流正读着它，换上新的 WebApp 以后接着读，不用重连。
        新的 WebApp 上只有这一段（恢复之前没有一段在跑，没存上盘的也在 lifecycle.stop 里存过了）。"""
        old = self.web
        app = old.app
        old.ui.forget_stop_callbacks()  # 旧的那一份登记的"停止时要做的事"不再要（新的 WebApp 会登记它自己的）
        fresh_app = build_app(app.config, old.ui, client_factory=app.client_factory)
        fresh_app.browser.headless_only = app.browser.headless_only  # 服务器上（harness serve）只能无界面运行
        fresh_app.fetcher = app.fetcher  # 读网页去取的那一个（测试换成连本地假网站的，见 fetch/client.py）
        fresh = type(old)(fresh_app, old.ui, old.token, old.service)
        # 测试和演示让聊天平台连本地的假服务器（换掉的是这两张表），新的那一个照样连那里
        fresh.channels.factories = dict(old.channels.factories)
        fresh.channels.checks = dict(old.channels.checks)
        return fresh

    # ---- 在文件管理器里显示 -----------------------------------------------------

    def _folder(self) -> Path:
        return backups_folder(self.web.app.config.data_dir)

    def _show(self, name: object) -> tuple[int, dict]:
        """在文件管理器里显示恢复前的自动备份。路径由服务器算：页面只能给一个文件名，它必须是那个样子、而且真的在那个文件夹里。"""
        folder = self._folder()
        if name is None:
            target = folder
        elif isinstance(name, str) and BEFORE.fullmatch(name):
            target = folder / name
        else:
            return 400, {"error": t("参数不正确")}
        if not target.exists() or target.is_symlink() or (name is not None and not target.is_file()):
            return 409, {"error": t("这份自动备份已经不在了：{path}", path=target)}
        try:
            self._reveal(target)
        except OSError as exc:
            return 409, {"error": t("没能打开文件管理器（{error}）。文件在：{path}", error=exc, path=target)}
        return 200, {"ok": True, "path": str(target)}


def _worded(steps: list[dict]) -> None:
    """每一样东西会怎样的那句话，由程序写好（带着原文，页面换语言时照着重译，见 ui.said）。"""
    for step in steps:
        step["said"] = said(restore.step_text(step))


def _discard(stream: BufferedIOBase, most: int) -> None:
    """把不打算收的请求体读掉扔了，最多读 most 个字节（不读就关连接的话，Windows 上对方收不到回答）。"""
    remaining = most
    while remaining > 0:
        chunk = stream.read(min(remaining, 1 << 16))
        if not chunk:
            break
        remaining -= len(chunk)
