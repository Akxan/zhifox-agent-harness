"""网页上"设置 → 操作整台电脑"和"智狐正在控制这台电脑"那条提示背后的接口：/api/computer/……。

  POST /api/computer/settings   开、关（打开要带着 acknowledge: true，页面上先讲清楚风险、你勾了"我明白"才发）、
                                定时任务和聊天通道能不能用、不许碰的程序
  POST /api/computer/stop       停下正在操作电脑的那个运行（提示条上的"停止"）。它这一轮里不能再动电脑
  POST /api/computer/trust      这段对话里不再问（确认卡片上多出来的那个按钮）：{"what": "screenshots" 截图 / "actions" 点击、打字,
                                "confirm": 卡片的编号}。只认你正在看的这段对话里、正在等你答复的那张卡片，而且那张卡片上给了这个选择；
                                别的一律拒绝（409）。换一段对话就从头再问；定时任务、聊天通道的运行根本打不开（见 DesktopSession.trust）
  POST /api/computer/untrust    收回上面那一条（设置里的"恢复每一步都问"）：这段对话里截图、点击打字又都先问你
  POST /api/computer/permission 在 macOS 上打开系统设置里对应的那一页（screen 屏幕录制 / input 辅助功能）。只是打开那一页给你看，
                                不改任何设置；页面只能给这两个固定的词，地址由服务器定，命令用参数列表、不经过 shell
  POST /api/computer/shot       一张截图的大图（点开工具卡片上的小图时）。和 /api/browser/shot 一样只收编号，见 server.py

开关这些只是设置，不拿对话的锁：对话进行中也能关掉（关了以后，这段对话里再用这些工具，会回答"关着"）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

from ..browser.shots import SHOT_ID
from ..desktop.session import TRUSTS
from ..i18n import t
from ..providers import PROVIDERS
from ..settings import MAX_APP_NAME_CHARS, MAX_BLOCKED_APPS
from ..tools.shell import SECRET_ENV_VARS

if TYPE_CHECKING:
    from .server import WebApp

# macOS 系统设置里那两页的地址（"隐私与安全性"下面）。只打开，不改
PANES = {
    "screen": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    "input": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
}


class ComputerAPI:
    def __init__(self, web: WebApp, opener=subprocess.Popen):
        self.web = web
        self.manager = web.app.desktop
        self._open = opener  # 测试换成只记一笔的

    def state(self) -> dict:
        """状态里的这一节（manager.status），加上：这段对话里截图、点击打字问不问、每一家模型能不能用、要不要打开系统设置的按钮。"""
        status = self.manager.status()
        desktop = self.web.app.chat_desktop
        status["trusted"] = desktop.trust_screenshots
        status["trusted_actions"] = desktop.trust_actions
        # 每一家怎么用：toolset 是 Anthropic 专门训练过的电脑工具包；standin 是智狐自己写的替身工具；none 是用不了（看不了图片）
        status["providers"] = {
            provider.id: "toolset" if provider.kind == "anthropic" else "standin" if provider.vision else "none" for provider in PROVIDERS.values()
        }
        status["can_open_settings"] = sys.platform == "darwin"
        return status

    def route(self, action: str, body: dict) -> tuple[int, dict]:
        if action == "settings":
            return self._settings(body)
        if action == "stop":
            self.manager.stop_all()
            return 200, {"ok": True}
        if action == "trust":
            return self._trust(body.get("what"), body.get("confirm"))
        if action == "untrust":
            # 收回这段对话里的"不再问"（设置里的那个按钮）。只会更严，所以不用对着哪张卡片、什么时候都能点
            self.web.app.chat_desktop.untrust()
            self.web.publish_state()
            return 200, {"ok": True}
        if action == "permission":
            return self._open_pane(body.get("pane"))
        return 404, {"error": t("没有这个操作")}

    def _trust(self, what: object, confirm_id: object) -> tuple[int, dict]:
        """确认卡片上的"允许，这段对话里……不再问"：先到这里打开，页面再答复那张卡片（/api/confirm）。"""
        if not isinstance(what, str) or what not in TRUSTS.values():
            return 400, {"error": t("参数不正确")}
        if not self.manager.enabled:
            return 409, {"error": t("“操作整台电脑”现在是关着的。")}
        if not self.web.ui.offered(confirm_id, what) or not self.web.app.chat_desktop.trust(what):
            return 409, {"error": t("这张确认卡片已经处理过了，或者上面没有这个选择。")}
        self.web.publish_state()
        return 200, {"ok": True}

    def shot(self, shot_id: object) -> bytes | None:
        if not isinstance(shot_id, str) or not SHOT_ID.fullmatch(shot_id):
            return None
        return self.manager.shots.read(shot_id)

    def _settings(self, body: dict) -> tuple[int, dict]:
        changes: dict = {}
        for name in ("enabled", "unattended"):
            if name in body:
                if not isinstance(body[name], bool):
                    return 400, {"error": t("参数不正确")}
                changes[name] = body[name]
        if changes.get("enabled") and not self.manager.enabled and body.get("acknowledge") is not True:
            # 打开它是一件要想清楚的事：页面上先把风险讲清楚，你勾了"我明白"才会带着 acknowledge 发过来
            return 400, {"error": t("打开之前要先确认你明白这样做的风险。")}
        if "blocked" in body:
            blocked = body["blocked"]
            ok = isinstance(blocked, list) and len(blocked) <= MAX_BLOCKED_APPS and all(
                isinstance(name, str) and 0 < len(name.strip()) <= MAX_APP_NAME_CHARS and all(char.isprintable() for char in name) for name in blocked
            )
            if not ok:
                return 400, {"error": t("程序的名字不对：每个最多 {chars} 个字，最多 {count} 个。", chars=MAX_APP_NAME_CHARS, count=MAX_BLOCKED_APPS)}
            changes["blocked"] = blocked
        if not changes:
            return 400, {"error": t("参数不正确")}
        app = self.web.app
        before = dict(app.settings.computer)
        try:
            app.settings.set_computer(changes)
        except OSError as exc:
            return 409, {"error": t("没能保存设置：{error}", error=str(exc))}
        after = app.settings.computer
        warning = ""
        if before["enabled"] != after["enabled"]:
            if not after["enabled"]:
                self.manager.stop_all()  # 关掉了：正在做的那一步马上停下
            started = self.web.refresh_tools()
            if started:
                warning = t("这段对话已经开始了，操作电脑的工具从下一段新对话开始生效。") if after["enabled"] else t("这段对话里操作电脑的工具还在，但再用会告诉助手“关着”。")
        self.web.publish_state()
        return 200, {"ok": True, "warning": warning}

    def _open_pane(self, pane: object) -> tuple[int, dict]:
        if sys.platform != "darwin" or not isinstance(pane, str) or pane not in PANES:
            return 400, {"error": t("参数不正确")}
        env = {name: value for name, value in os.environ.items() if name not in SECRET_ENV_VARS}
        try:
            self._open(["/usr/bin/open", PANES[pane]], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
        except OSError as exc:
            return 409, {"error": t("没能打开系统设置（{error}）。", error=exc)}
        return 200, {"ok": True}
