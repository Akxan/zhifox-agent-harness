from __future__ import annotations

from typing import TYPE_CHECKING

from .. import export, i18n
from ..budget import valid_budget
from ..config import LEDGER_CHART_DAYS
from ..i18n import t
from ..permissions import MODES

if TYPE_CHECKING:
    from .server import Handler
    from .webapp import WebApp


class SettingsActions:
    def set_budget(self, amount: float | None) -> None:
        """记下每月预算（None 表示不设）。只是提醒用，所以不用拿对话的锁，对话进行中也可以改。"""
        self.app.settings.set_budget(amount)
        self.publish_state()  # 状态里带着预算，超了没有也在这时重新算一次（见 App.spending）

    def set_redact(self, enabled: bool) -> None:
        """开、关"遮住工具结果里的密钥"（见 redact.py）。每次遮之前现看设置，所以不用拿对话的锁：正在跑的这一轮，
        下一个工具的结果就按新的来。"""
        self.app.settings.set_redact(enabled)
        self.publish_state()

    def spend_report(self) -> dict:
        """"花费明细"窗口要的全部数字。只读内存里的汇总，对话进行中也可以看。"""
        return self.app.ledger.report(self.app.agent.clock().date(), LEDGER_CHART_DAYS)

    def export_info(self) -> dict:
        """导出之前给页面看的：数据文件夹在哪、要导出的有多大、多少个文件。"""
        config = self.app.config
        work = export.plan(config.data_dir, [config.env_file])
        return {"bytes": work.size, "files": work.count, "path": str(config.data_dir)}

    def set_language(self, language: str) -> str:
        """换界面语言。成功返回空字符串，否则返回原因。

        不刷新页面：记下新的选择、发一次新的状态，页面收到后在原地把文字换掉（见 page/scripts/language.js 的 applyLanguage）。
        也不用拿对话的锁：对话进行中也可以换。之后程序发给页面的话（提示、确认卡片上的说明）都用新的语言。
        """
        i18n.choose(language)
        self.channels.relabel()  # 聊天软件里命令菜单上的说明，也换成新的语言
        try:
            self.app.settings.set_language(language)
        except OSError as exc:
            self.publish_state()  # 这一次照样换过去，只是下次启动还是原来的语言
            return t("界面已经换过去了，但没能记下这个选择（{error}），下次启动时还是原来的语言。", error=exc)
        self.publish_state()
        return ""

    def set_approval(self, mode: str) -> None:
        """更改"要不要每次都问"。不用拿对话的锁：对话进行中也可以改，比如看到第五张确认卡片时决定全部放开。

        已经显示出来、正在等答复的那一张卡片不受影响，还是要点；从下一次需要确认的操作开始按新的来。
        这个选择只记在内存里，下次启动回到"每次都问"（原因见 permissions.py）。
        """
        self.app.agent.permissions.mode = mode
        self.publish_state()


def route_language(handler: Handler, web: WebApp, body: dict) -> None:
    language = body.get("language")
    if not isinstance(language, str) or language not in i18n.LANGUAGES:
        handler._send_json(400, {"error": t("参数不正确")})
    else:
        handler._finish("", warning=web.set_language(language))


def route_approval(handler: Handler, web: WebApp, body: dict) -> None:
    mode = body.get("mode")
    if not isinstance(mode, str) or mode not in MODES:
        handler._send_json(400, {"error": t("参数不正确")})
    else:
        web.set_approval(mode)
        handler._send_json(200, {"ok": True})


def route_budget(handler: Handler, web: WebApp, body: dict) -> None:
    amount = body.get("amount")
    if "amount" not in body or not valid_budget(amount):  # 不设预算要明说（null），不能是忘了带
        handler._send_json(400, {"error": t("预算要填一个大于 0 的数（美元）")})
    else:
        web.set_budget(None if amount is None else float(amount))
        # 带上存完以后的花费：设的预算比这个月已经花的还少时，页面当场就能说清楚（提示框会被设置面板挡住）
        handler._send_json(200, {"ok": True, "spend": web.app.spending()})


def route_redact(handler: Handler, web: WebApp, body: dict) -> None:
    if not isinstance(body.get("enabled"), bool):
        handler._send_json(400, {"error": t("要说清楚开还是关（enabled 是 true 或 false）")})
    else:
        web.set_redact(body["enabled"])
        handler._send_json(200, {"ok": True})
