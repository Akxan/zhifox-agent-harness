"""网页上"设置 → 浏览器"和"浏览器正在被智狐控制"那条提示背后的接口：/api/browser/……。

  POST /api/browser/settings   开、关浏览器，要不要窗口（无界面运行），Chrome 在哪儿
  POST /api/browser/stop       停下所有正在进行的浏览器动作，关掉浏览器（提示条上的"停止"）
  POST /api/browser/reveal     在文件管理器里显示浏览器的配置文件夹（data/browser/profile）
  POST /api/browser/clear      清除配置文件夹：在这个浏览器里登录过的网站、缓存、历史记录都没了（页面上先确认一次）
  POST /api/browser/shot       一张截图的大图（点开工具卡片上的小图时）。浏览器只给编号，读哪个文件由服务器定

和别的"返回文件内容"的接口（/api/media/file）一样：编号只能是 64 个十六进制字符，拼不出任何路径；
回答的类型固定是 image/jpeg，带 Content-Disposition: attachment、nosniff 和 sandbox，见 server.py 的 _route（发文件字节的几个接口都在那里）。

开关、路径这些只是设置，不拿对话的锁：对话进行中也能关浏览器（关了以后，这段对话里再用浏览器的工具，会回答"浏览器关着"）。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ..browser.shots import SHOT_ID
from ..i18n import t
from ..safe import private_dir
from .ui import said

if TYPE_CHECKING:
    from .server import WebApp

MAX_PATH_CHARS = 1000


class BrowserAPI:
    def __init__(self, web: WebApp, reveal: Callable[[Path], None]):
        self.web = web
        self.manager = web.app.browser
        self._reveal = reveal

    def state(self) -> dict:
        """状态里的浏览器：开没开、在不在运行、忙不忙、找到的 Chrome、配置文件夹。出错的原因带着原文，换语言时页面能重译。"""
        status = self.manager.status()
        status["error"] = said(status["error"]) if status["error"] else None
        return status

    def route(self, action: str, body: dict) -> tuple[int, dict]:
        if action == "settings":
            return self._settings(body)
        if action == "stop":
            self.manager.stop_all()
            return 200, {"ok": True}
        if action == "reveal":
            return self._reveal_profile()
        if action == "clear":
            try:
                self.manager.clear_profile()
            except OSError as exc:
                return 409, {"error": t("没能清除：{error}", error=str(exc))}
            self.web.publish_state()
            return 200, {"ok": True}
        return 404, {"error": t("没有这个操作")}

    def shot(self, shot_id: object) -> bytes | None:
        """一张截图的大图。编号不对、过期删掉了，返回 None。"""
        if not isinstance(shot_id, str) or not SHOT_ID.fullmatch(shot_id):
            return None
        return self.manager.shots.read(shot_id)

    def _settings(self, body: dict) -> tuple[int, dict]:
        changes: dict = {}
        for name in ("enabled", "headless"):
            if name in body:
                if not isinstance(body[name], bool):
                    return 400, {"error": t("参数不正确")}
                changes[name] = body[name]
        if "path" in body:
            path = body["path"]
            if not isinstance(path, str) or len(path) > MAX_PATH_CHARS or any(not char.isprintable() for char in path):
                return 400, {"error": t("参数不正确")}
            changes["path"] = path.strip()
        if not changes:
            return 400, {"error": t("参数不正确")}
        app = self.web.app
        before = dict(app.settings.browser)
        try:
            app.settings.set_browser(changes)
        except OSError as exc:
            return 409, {"error": t("没能保存设置：{error}", error=str(exc))}
        after = app.settings.browser
        if before != after and (not after["enabled"] or before["headless"] != after["headless"] or before["path"] != after["path"]):
            # 关掉了、换了窗口模式或者换了 Chrome：现在开着的那个关掉，下次用的时候按新的设置打开
            self.manager.close()
        warning = ""
        if before["enabled"] != after["enabled"]:
            started = self.web.refresh_tools()
            if started:
                warning = t("这段对话已经开始了，浏览器的工具从下一段新对话开始生效。") if after["enabled"] else t("这段对话里浏览器的工具还在，但再用会告诉助手“浏览器关着”。")
        found = self.manager.executable()
        if after["enabled"] and found is None:
            warning = t("找不到 Chrome。请装一个 Chrome，或者在下面填上它的路径。")
        self.web.publish_state()
        return 200, {"ok": True, "warning": warning}

    def _reveal_profile(self) -> tuple[int, dict]:
        target = self.manager.profile
        try:
            private_dir(target)  # 还没用过浏览器的话，先建好这个文件夹，打开的才是它
            self._reveal(target)
        except OSError as exc:
            return 409, {"error": t("没能打开文件管理器（{error}）。文件在：{path}", error=exc, path=target)}
        return 200, {"ok": True, "path": str(target)}
