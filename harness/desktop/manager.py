"""管着"操作整台电脑"这件事：开没开、用哪个后端、几次运行怎么共用一个屏幕、停下、页面上的状态。

  - 屏幕只有一个：同一时间只让一个运行操作。一个运行做第一个动作时"占住"它（claim），这一轮结束时让出来（release）。
    别的运行这时要操作，会收到一句"电脑正被……用着"（浏览器不一样：每个运行有自己的标签页，可以同时用）。
  - 页面顶上的"智狐正在控制这台电脑"：有运行占着屏幕时出现，正在做动作时那个点会动；旁边的"停止"调用 stop_all。
  - 后端只找一次（detect）。测试和演示把 detect 换成假的（tests/fake_desktop.py），绝不碰真的屏幕。

开关、"定时任务能不能用"、不许碰的程序都在用户设置里（settings.py 的 computer），这里每次现读，改了马上生效。
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..browser.shots import ShotStore
from ..config import COMPUTER_TOOLSET_TOKENS, DESKTOP_SHOT_DAYS
from ..i18n import k
from ..tools.base import ToolError
from . import backend as backends
from .backend import NO_DISPLAY, PLATFORM, WAYLAND, WINDOWS, Backend
from .frame import Frame, Screen
from .session import DesktopSession

OFF = "操作整台电脑现在是关着的（用户在设置里关掉了）。需要的话，请用户在智狐的“设置 → 操作整台电脑”里打开它。"
UNSUPPORTED = {
    WAYLAND: "这台电脑用的是 Wayland 桌面，智狐没法在上面截图、控制鼠标键盘（Wayland 不允许）。可以请用户换成 X11 桌面（登录时选“Xorg”）。",
    NO_DISPLAY: "这台电脑没有图形界面（DISPLAY 没有设置，比如在服务器上），没有屏幕可以操作。",
    WINDOWS: "智狐暂时还不能在 Windows 上操作整台电脑（现在只支持 macOS 和 Linux 的 X11 桌面）。",
    PLATFORM: "智狐只能在 macOS 和 Linux（X11 桌面）上操作整台电脑，这台电脑的系统不支持。",
}
# 谁占着屏幕（页面上翻译，给模型的回答里用原文）
CHAT, TASK, CHANNEL = k("你的对话"), k("一个定时任务"), k("聊天通道里的一个聊天")
SCREEN_CACHE_SECONDS = 30  # 状态里屏幕的大小多久重新问一次（Linux 上每问一次要运行一次 xdotool）


class DesktopManager:
    def __init__(self, folder: Path, settings: Any, clock: Callable[[], float] = time.monotonic):
        """folder 是 data/desktop/。settings 是用户设置（见 settings.py 的 computer）。"""
        self.folder = folder
        self.shots = ShotStore(folder / "shots", days=DESKTOP_SHOT_DAYS)
        self.settings = settings
        self.clock = clock
        self.on_change: Callable[[], None] | None = None  # 有运行占住、让出屏幕，忙起来、闲下来：页面上的提示跟着变
        self._lock = threading.RLock()
        self._backend: Backend | None = None
        self._reason = ""
        self._detected = False
        self._holder: DesktopSession | None = None
        self._sessions: list[DesktopSession] = []
        self._busy = 0
        self._screen: tuple[float, Any] = (0.0, None)  # （什么时候问的，屏幕的大小），见 status

    # ---- 设置 -----------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.settings.computer.get("enabled"))

    @property
    def unattended(self) -> bool:
        """定时任务、聊天通道能不能用（还要 enabled）。"""
        return self.enabled and bool(self.settings.computer.get("unattended"))

    def blocked_app(self, app: str) -> str:
        """app 是不是你设为不许碰的程序：是的话返回设置里那个名字，不是返回空字符串。不分大小写；
        设置里的名字是 app 的一部分就算（Linux 上 app 是"类名 · 标题"，写 firefox 就能认出 Firefox 的窗口）。"""
        if not app:
            return ""
        lowered = app.casefold()
        return next((name for name in self.settings.computer.get("blocked") or [] if name.casefold() in lowered), "")

    # ---- 后端 -----------------------------------------------------------------

    def backend(self) -> tuple[Backend | None, str]:
        """这台电脑上的后端（只找一次）。用不了的话是（None，原因）。"""
        with self._lock:
            if not self._detected:
                self._backend, self._reason = backends.detect()  # 每次现找 backends.detect：测试会把它换成假的
                self._detected = True
            return self._backend, self._reason

    def ready(self, kind: str) -> Backend:
        """要做一类事（look 截图、input 鼠标键盘、wait 什么都不碰）之前：开着吗？这台电脑上能做吗？有权限吗？不行就抛 ToolError。"""
        if not self.enabled:
            raise ToolError(OFF)
        found, reason = self.backend()
        if found is None:
            raise ToolError(UNSUPPORTED.get(reason, UNSUPPORTED[PLATFORM]))
        problem = found.problem(kind) if kind in ("look", "input") else ""
        if problem:
            raise ToolError(problem)
        return found

    # ---- session ----------------------------------------------------------------

    def session(self, label: str, attended: bool = False) -> DesktopSession:
        """一次运行用的 session。attended：你正看着的那段对话（只有它能选"这段对话里不再问"，见 DesktopSession.trust）。"""
        session = DesktopSession(self, label, attended)
        with self._lock:
            self._sessions.append(session)
        return session

    def forget(self, session: DesktopSession) -> None:
        with self._lock:
            if session in self._sessions:
                self._sessions.remove(session)

    def free_for(self, session: DesktopSession) -> bool:
        with self._lock:
            return self._holder is None or self._holder is session

    def claim(self, session: DesktopSession) -> None:
        """一个运行要操作屏幕了：屏幕空着（或者本来就是它的）就归它，直到它这一轮结束；被别人占着就抛 ToolError。"""
        with self._lock:
            holder = self._holder
            if holder is not None and holder is not session:
                raise ToolError(f"这台电脑现在正被{holder.label}操作着，同一时间只能有一方操作。等它结束再试，或者请用户在页面顶上点“停止”。")
            changed = holder is None
            self._holder = session
        if changed:
            self._changed()

    def release(self, session: DesktopSession) -> None:
        with self._lock:
            if self._holder is not session:
                return
            self._holder = None
        self._changed()

    def stop_all(self) -> None:
        """你在页面顶上点了"停止"：正在操作屏幕的那个运行马上停下，这一轮里不能再动电脑（见 DesktopSession.stopped_by_user）。"""
        with self._lock:
            holder = self._holder
            targets = [session for session in self._sessions if session is holder or session.busy]
        for session in targets:
            session.stopped_by_user = True
            session.stop()
        if holder is not None:
            self.release(holder)

    # ---- 忙和闲 -----------------------------------------------------------------

    def began(self) -> None:
        with self._lock:
            self._busy += 1
            changed = self._busy == 1
        if changed:
            self._changed()

    def ended(self) -> None:
        with self._lock:
            self._busy = max(0, self._busy - 1)
            changed = self._busy == 0
        if changed:
            self._changed()

    def _changed(self) -> None:
        if self.on_change is not None:
            with contextlib.suppress(Exception):  # 刷新页面出了错，不能连累正在做的动作
                self.on_change()

    # ---- 页面上的状态 -------------------------------------------------------------

    def status(self) -> dict:
        """设置里"操作整台电脑"一节和页面顶上的提示要的东西（规矩只在这里算一份，页面不另记）。"""
        found, reason = self.backend()
        with self._lock:
            holder = self._holder
            busy = self._busy > 0
        frame = self._frame(found) if found is not None and self.enabled else None
        return {
            "enabled": self.enabled,
            "unattended": bool(self.settings.computer.get("unattended")),
            "blocked": list(self.settings.computer.get("blocked") or []),
            "supported": found is not None,
            "reason": reason,  # wayland / no_display / windows / platform（页面按它写说明）
            "platform": found.platform if found is not None else "",
            "checks": _safe_checks(found),  # 权限给了没有（macOS）、要的程序装了没有（Linux）
            "active": holder is not None,
            "busy": busy,
            "who": holder.label if holder is not None else "",
            "screen": [frame.screen.width, frame.screen.height] if frame else None,
            "image": [frame.width, frame.height] if frame else None,  # 交给模型的截图多大
            # 一张截图大约多少个 token（Claude 的算法：宽×高÷750）。没开、或者看不出屏幕多大时，按一台常见的 MacBook 算
            "shot_tokens": _tokens(frame or Frame.of(TYPICAL)),
            "toolset_tokens": COMPUTER_TOOLSET_TOKENS,
            "shot_days": DESKTOP_SHOT_DAYS,
        }

    def _frame(self, found: Backend) -> Frame | None:
        asked, frame = self._screen
        if frame is None or self.clock() - asked > SCREEN_CACHE_SECONDS:
            try:
                frame = Frame.of(found.screen())
            except Exception:  # noqa: BLE001 —— 看不出屏幕多大不要紧，页面上就不写了
                frame = None
            self._screen = (self.clock(), frame)
        return frame


def _safe_checks(found: Backend | None) -> dict:
    if found is None:
        return {}
    try:
        return found.checks()
    except Exception:  # noqa: BLE001
        return {}


TYPICAL = Screen(1512, 982, 3024, 1964)


def _tokens(frame: Frame) -> int:
    return frame.width * frame.height // 750
