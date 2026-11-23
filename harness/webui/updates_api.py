"""设置里"检查新版本"一节背后的接口（规矩全在 ../updates.py，这里只是接到网页上）。

  POST /api/updates         打开、关上：{"enabled": true / false}。打开时马上在后台问一次 GitHub，结果随状态发过来
  POST /api/updates/check   马上问一次（只在打开着的时候；同样在后台问，不让这个请求等着 GitHub）

状态里的 updates（见 state）：
  enabled      打开了没有
  current      现在这一版（0.2.0）
  checking     正在问
  checked_at   上一次问的时刻（秒，time.time()；从没问过是 null）。页面照着界面的语言写成"几点几分"
  problem      上一次没问成的原因（said 的样子：换语言时页面就地重译）；问成了是 null
  latest       有新版本时：{version, published, page}（page 是这一版的发布页，由程序照着 tag 拼，只指向 github.com 上的这个仓库）；
               没有新版本、或者关着的时候是 null
  upgrade      有新版本时怎么升级（../updates.py 的 upgrade）：lead、commands（一条一行）、in_project、before、notes，话都是 said 的样子

后台线程（Updater.start / stop）由 lifecycle.py 开和关，和定时任务、聊天通道一样；测试里的 WebApp 从来不 start，
要问的时候打开设置，check_soon 另开一个线程问完就走。这些都不拿对话的锁：问的是 GitHub，不碰对话。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..i18n import t
from ..updates import SERVICE, TERMINAL, UNKNOWN, Release, Updater, upgrade
from .ui import said

if TYPE_CHECKING:
    from .webapp import WebApp

# WebApp.service（智狐是怎么开着的，见 webapp.py）→ 升级完怎样才用得上新版本（updates.upgrade 的 running）
_RUNNING = {"launchd": SERVICE, "systemd": SERVICE, "taskscheduler": SERVICE, "terminal": TERMINAL}


class UpdatesAPI:
    def __init__(self, web: WebApp):
        self.web = web
        # 状态变了（开始问、问完了、打开、关上）就发一次状态。publish_state 每次现找：测试会换掉它
        self.updater = Updater(web.app.config, web.app.settings, on_change=lambda: web.publish_state())

    def state(self) -> dict:
        status = self.updater.status()
        found = status.newer
        return {
            "enabled": status.enabled,
            "current": status.current,
            "checking": status.checking,
            "checked_at": status.checked_at,
            "problem": said(status.problem) if status.problem else None,
            "latest": {"version": found.version, "published": found.published, "page": found.page} if found else None,
            "upgrade": self._upgrade(found) if found else None,
        }

    def _upgrade(self, found: Release) -> dict:
        plan = upgrade(found, _RUNNING.get(self.web.service or "", UNKNOWN))
        return {
            "lead": said(plan.lead),
            "commands": plan.commands,
            "in_project": plan.in_project,
            "before": [said(text) for text in plan.before],
            "notes": [said(text) for text in plan.notes],
        }

    def route(self, action: str, body: dict) -> tuple[int, dict]:
        if action == "":
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                return 400, {"error": t("要说清楚开还是关（enabled 是 true 或 false）")}
            try:
                self.updater.set_enabled(enabled)
            except OSError as exc:
                return 409, {"error": t("没能保存设置：{error}", error=str(exc))}
            return 200, {"ok": True}
        if action == "check":
            if not self.updater.enabled:
                return 409, {"error": t("先打开“检查新版本”。")}
            self.updater.check_soon()
            return 200, {"ok": True}
        return 404, {"error": t("没有这个操作")}
