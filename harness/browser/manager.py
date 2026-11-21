"""管着智狐的那个 Chrome：什么时候打开、什么时候关，几次运行怎么共用它。

  - 要用的时候才打开：浏览器功能开着，但助手还没用过浏览器时，Chrome 不运行。
  - 一直没人用就关掉：BROWSER_IDLE_MINUTES 分钟（config.py）没有动作，就关掉省内存。登录状态在配置文件夹里，下次打开还在。
  - 智狐退出时关掉（App.close），连同它所有的子进程；万一智狐被强行杀掉，管道断了，Chrome 自己也会退出（见 cdp.py）。
  - 几次运行（你的对话、几个定时任务、聊天）各有一个 BrowserSession，共用这一个 Chrome，各开各的标签页（见 session.py）。

另外两件只在这里做的事：

  - 下载一律拒绝（Browser.setDownloadBehavior: deny）。网页让浏览器下载的文件，可能是任何东西；
    助手要的是网页上的内容，不需要把文件存到你的电脑上。被拒绝的下载会告诉模型一声。
  - 网页弹出的对话框（alert、confirm、prompt）自动取消，不然页面会一直卡在那里等人点。也告诉模型一声。
"""

from __future__ import annotations

import atexit
import contextlib
import shutil
import threading
import time
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import (
    BROWSER_IDLE_MINUTES,
    BROWSER_SHOT_DAYS,
    BROWSER_START_TIMEOUT,
    BROWSER_VIEWPORT,
)
from ..i18n import t
from ..tools.base import ToolError
from . import chrome
from .cdp import CDPError, Connection
from .session import BrowserSession
from .shots import ShotStore

IDLE_CHECK_SECONDS = 15
CALM_SECONDS = 2.0  # 做完一个动作，再安静这么久，页面上才从"正在操作"换回"开着"


class BrowserManager:
    def __init__(
        self,
        folder: Path,
        settings: Any,
        launcher: Callable[..., chrome.Chrome] = chrome.launch,
        clock: Callable[[], float] = time.monotonic,
    ):
        """folder 是 data/browser/。settings 是用户设置（见 settings.py 的 browser）：开没开、要不要窗口、Chrome 在哪儿。"""
        self.folder = folder
        self.profile = folder / "profile"
        self.shots = ShotStore(folder / "shots")
        self.settings = settings
        self.launcher = launcher  # 测试会换成启动假 Chrome 的
        self.clock = clock
        self.headless_only = False  # 在服务器上（harness serve）：没有屏幕，只能无界面运行
        self.on_change: Callable[[], None] | None = None  # 开了、关了、忙起来了、闲下来了：页面上的指示跟着变
        self._lock = threading.RLock()
        self._chrome: chrome.Chrome | None = None
        self.generation = 0  # 第几次启动。session 靠它发现"Chrome 重启过，我的标签页都没了"
        self._blank = ""  # Chrome 刚启动时自己开的那个空白页，还没人用
        self._sessions: weakref.WeakSet[BrowserSession] = weakref.WeakSet()
        self._busy = 0  # 正在进行的动作有几个
        self._shown_busy = False  # 页面上现在显示的是不是"正在操作"
        self._calm: threading.Timer | None = None
        self._last_used = 0.0
        self.error = ""  # 上一次打开失败的原因（给页面看）
        self._idle_thread: threading.Thread | None = None
        atexit.register(self._kill)  # 最后一道保险：程序怎么退出的都要关掉它

    # ---- 设置 -----------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.settings.browser.get("enabled"))

    @property
    def headless(self) -> bool:
        return self.headless_only or bool(self.settings.browser.get("headless")) or chrome.needs_headless()

    def executable(self) -> Path | None:
        return chrome.find_chrome(str(self.settings.browser.get("path") or ""))

    # ---- session ----------------------------------------------------------------

    def session(self, label: str) -> BrowserSession:
        session = BrowserSession(self, label)
        self._sessions.add(session)
        return session

    def forget(self, session: BrowserSession) -> None:
        self._sessions.discard(session)

    # ---- 打开和关闭 -------------------------------------------------------------

    def connection_if_running(self) -> Connection | None:
        with self._lock:
            running = self._chrome
            return running.connection if running is not None and not running.connection.closed else None

    def connect(self) -> tuple[Connection, int]:
        """拿到和 Chrome 的连接，没开的话先打开。返回（连接，第几次启动）。打不开抛 ToolError。

        打开（或者打开失败）以后要刷新页面上的状态，但一定要等放开 self._lock 再刷新：刷新时要算状态（status），
        status 也要拿这把锁；而另一个线程可能正拿着"发状态"的锁、在等这把锁，两边一等就谁也动不了了。
        """
        try:
            with self._lock:
                running = self._chrome
                if running is not None and not running.connection.closed:
                    return running.connection, self.generation
                if running is not None:
                    self._chrome = None
                    running.discard()
                self._launch()
                assert self._chrome is not None
                return self._chrome.connection, self.generation
        finally:
            if running is None or running.connection.closed:  # 这一次真的去打开了（成没成都算）
                self._changed()

    def _launch(self) -> None:
        executable = self.executable()
        if executable is None:
            chosen = str(self.settings.browser.get("path") or "")
            self.error = t("找不到 Chrome。") if not chosen else t("设置里填的 Chrome 路径不对：{path}", path=chosen)
            raise ToolError(
                "这台电脑上找不到 Chrome（或者设置里填的路径不对），浏览器打不开。请用户装一个 Chrome，"
                "或者在“设置 → 浏览器”里填上 Chrome 的路径。"
            )
        running: chrome.Chrome | None = None
        try:
            running = self.launcher(executable, self.profile, self.headless, on_close=self._closed)
            connection = running.connection
            connection.call("Browser.getVersion", timeout=BROWSER_START_TIMEOUT)
        except (OSError, CDPError) as exc:
            self.error = t("Chrome 启动失败：{error}", error=str(exc))
            if running is not None:
                running.discard()
            raise ToolError(f"浏览器启动失败：{exc}。可以请用户检查一下 Chrome 能不能正常打开。") from None
        self.error = ""
        self.generation += 1
        self._chrome = running
        self._last_used = self.clock()
        try:
            # 下载一律拒绝；告诉我们有哪些新标签页（页面自己开的要算进开它的那个 session）
            connection.call("Browser.setDownloadBehavior", {"behavior": "deny", "eventsEnabled": True})
            connection.call("Target.setDiscoverTargets", {"discover": True})
            infos = connection.call("Target.getTargets").get("targetInfos", [])
            self._blank = next((info["targetId"] for info in infos if info.get("type") == "page" and info.get("url") == "about:blank"), "")
        except CDPError:
            pass
        connection.on("Target.targetCreated", self._target_created)
        connection.on("Target.targetDestroyed", self._target_destroyed)
        connection.on("Browser.downloadWillBegin", self._download)
        connection.on("Page.javascriptDialogOpening", self._dialog)
        if self._idle_thread is None or not self._idle_thread.is_alive():
            self._idle_thread = threading.Thread(target=self._watch_idle, daemon=True, name="browser-idle")
            self._idle_thread.start()

    def claim_blank(self) -> str:
        """Chrome 刚启动时那个空白页：第一个要标签页的 session 拿去用，就不用再开一个窗口。只能拿一次。"""
        with self._lock:
            blank, self._blank = self._blank, ""
            return blank

    def close(self) -> None:
        """关掉 Chrome（闲置太久、程序退出、你点了停止）。session 们下次用的时候会发现，重新开。"""
        with self._lock:
            running, self._chrome = self._chrome, None
            self._blank = ""
        if running is not None:
            running.close()
            self._changed()

    def _kill(self) -> None:
        running = self._chrome
        if running is not None:
            running.kill()

    def _closed(self) -> None:
        """连接断了（Chrome 退出了、崩溃了、你把它的窗口全关了）。在读线程里被调用。"""
        threading.Thread(target=self._reap, daemon=True).start()

    def _reap(self) -> None:
        with self._lock:
            running = self._chrome
            if running is None or not running.connection.closed:
                return
            self._chrome = None
            self._blank = ""
        running.discard()
        self._changed()

    def stop_all(self) -> None:
        """你在页面上点了"停止"：所有正在进行的浏览器动作马上停下，关掉浏览器。

        每个 session 记一笔：它下一次要用浏览器时，先收到一句"用户关掉了浏览器，先别再用"（见 BrowserSession.run）。
        """
        for session in list(self._sessions):
            session.closed_by_user = True
            session.stop()
        self.close()

    def clear_profile(self) -> None:
        """清除配置文件夹（登录状态、缓存、历史记录……）。先关掉浏览器。失败抛 OSError。"""
        if self._busy:
            raise OSError(t("浏览器正在使用中，等这一步做完再清除。"))
        self.close()
        if self.profile.is_symlink():
            self.profile.unlink()
        elif self.profile.exists():
            shutil.rmtree(self.profile)

    # ---- 忙和闲 -----------------------------------------------------------------

    def began(self) -> None:
        with self._lock:
            self._busy += 1
            self._last_used = self.clock()
            # 一串连着的动作（点、输入、回车……）中间只空闲几百毫秒：不为每一下都刷新页面，
            # 只在真正"忙起来"（之前已经闲了一阵、页面上显示的是闲着）时说一声
            changed = self._busy == 1 and not self._shown_busy
            self._shown_busy = True
            if self._calm is not None:
                self._calm.cancel()
                self._calm = None
        if changed:
            self._changed()

    def ended(self) -> None:
        with self._lock:
            self._busy = max(0, self._busy - 1)
            self._last_used = self.clock()
            if self._busy == 0 and self._calm is None:
                self._calm = threading.Timer(CALM_SECONDS, self._settle)
                self._calm.daemon = True
                self._calm.start()

    def _settle(self) -> None:
        """动作做完、又安静了 CALM_SECONDS 秒：页面上的指示从"正在操作"换回"开着"。"""
        with self._lock:
            self._calm = None
            if self._busy or not self._shown_busy:
                return
            self._shown_busy = False
        self._changed()

    def _watch_idle(self) -> None:
        while True:
            time.sleep(IDLE_CHECK_SECONDS)
            self.check_idle()
            with self._lock:
                if self._chrome is None:
                    return

    def check_idle(self) -> bool:
        """闲置太久就关掉。返回关了没有（测试直接调用它，不用真的等十分钟）。"""
        with self._lock:
            idle = self._chrome is not None and not self._busy and self.clock() - self._last_used > BROWSER_IDLE_MINUTES * 60
        if idle:
            self.close()
        return idle

    def _changed(self) -> None:
        if self.on_change is not None:
            with contextlib.suppress(Exception):  # 刷新页面出了错，不能连累浏览器本身
                self.on_change()

    def status(self) -> dict:
        """页面上要显示的：开没开、在不在忙、用的是哪个 Chrome、配置文件夹在哪儿。"""
        executable = self.executable()
        with self._lock:
            running = self._chrome is not None and not self._chrome.connection.closed
            busy = self._busy > 0
        return {
            "enabled": self.enabled,
            "running": running,
            "busy": busy or self._shown_busy,
            "headless": self.headless,
            "headless_setting": bool(self.settings.browser.get("headless")),
            "headless_forced": self.headless_only or chrome.needs_headless(),
            "chrome": str(executable) if executable else "",
            "path": str(self.settings.browser.get("path") or ""),
            "profile": str(self.profile),
            "profile_exists": self.profile.exists(),
            "error": self.error,
            # 下面三个只给页面上的说明用（规矩只写在 config.py 一处，页面不另记一份）
            "shot_days": BROWSER_SHOT_DAYS,  # 截图的大图留几天
            "idle_minutes": BROWSER_IDLE_MINUTES,  # 闲置多久自动关掉
            "shot_tokens": BROWSER_VIEWPORT[0] * BROWSER_VIEWPORT[1] // 750,  # 一张截图大约多少个 token（Claude 的算法：宽×高÷750）
        }

    # ---- Chrome 主动说的事（在读线程里，要快） ----------------------------------------

    def _owner(self, target: str) -> BrowserSession | None:
        return next((session for session in list(self._sessions) if session.owns(target)), None)

    def _target_created(self, params: dict, _session: str) -> None:
        info = params.get("targetInfo") or {}
        opener = info.get("openerId")
        if info.get("type") != "page" or not opener:
            return
        owner = self._owner(opener)
        if owner is not None:
            owner.adopt(info["targetId"])

    def _target_destroyed(self, params: dict, _session: str) -> None:
        target = params.get("targetId", "")
        owner = self._owner(target)
        if owner is not None:
            owner.lost(target)

    def _download(self, params: dict, _session: str) -> None:
        owner = self._owner(params.get("frameId", ""))
        if owner is not None:
            owner.downloaded(str(params.get("url", "")))

    def _dialog(self, params: dict, cdp_session: str) -> None:
        kind = str(params.get("type", "alert"))
        message = " ".join(str(params.get("message", "")).split())[:200]
        owner = next((session for session in list(self._sessions) if session.owns_session(cdp_session)), None)
        if owner is not None:
            owner.note(f"页面弹出了一个对话框（{kind}）：“{message}”。智狐已经替你取消了它。")
        connection = self.connection_if_running()
        if connection is None:
            return
        # beforeunload（"确定要离开吗"）点"离开"，不然跳转会一直卡住；别的都点"取消"
        accept = kind in ("beforeunload", "alert")

        def answer() -> None:
            with contextlib.suppress(CDPError):
                connection.call("Page.handleJavaScriptDialog", {"accept": accept}, session=cdp_session, timeout=5)

        threading.Thread(target=answer, daemon=True).start()  # 读线程里不能等 Chrome 的回答（见 Connection.on）
