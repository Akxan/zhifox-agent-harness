from __future__ import annotations

import textwrap
from dataclasses import dataclass

from ..i18n import t
from ..tasks import retell
from .base import Prompt
from .session_turn import logger
from .session_types import SessionPart


@dataclass
class Waiting:
    """一个发到聊天里、还没答复的确认请求。"""

    id: int  # WebUI 里确认请求的编号，按钮上带着它
    summary: str  # 要做的事（Permissions.review 写的那段话，带着原文的 Message：页面、/status 用 retell 按现在的界面语言写）
    handle: str = ""  # 聊天里那条消息的编号（发出去以后才有）
    by: str = ""  # 谁答复的：chat（在聊天里）或 web（在网页上）
    # 发送队列记的（见 outbox.py）：waiting 是没发到聊天里、留着等连上了补发；failed 是发不出去、不再试了。
    # 这一轮一直在等它，页面上照着写"还没发到聊天里"，网页上照样能答。problem 是为什么（t() 写的）
    delivery: str = ""
    problem: object = ""


class SessionConfirm(SessionPart):
    # ---- 确认请求 --------------------------------------------------------------

    def _asked(self, confirm_id: int, description: str, agent: str = "") -> None:
        """agent 要你确认一个操作（ChannelUI 拿着它自己的锁时调用的：这里只排队，不等）。
        description 是带着原文的那句话（t() 给的 Message，见 ui.py）：卡片、页面上的"在等你批准"、/status 都照着它按界面语言写。
        agent 是哪个子 Agent 在问：消息上写"某某想要"而不是"助手想要"，你知道是谁要动手。"""
        waiting = Waiting(confirm_id, description)
        self.waiting = waiting
        workspace = self.router.app.config.workspace

        def prompt() -> Prompt:
            # 轮到它发的时候才写（连不上平台时它会排着等，见 outbox.py）：按那一刻的界面语言，照着原文重译（retell）
            first, _, rest = retell(description).partition("\n")
            return Prompt(
                id=confirm_id,
                title=(t("「{agent}」想要", agent=agent) if agent else t("助手想要")) + first,
                detail=_dedent(rest),
                where=t("工作目录：{path}", path=workspace),
                allow=t("允许"),
                deny=t("拒绝"),
                hint=t("回复“允许”或“拒绝”。"),
            )

        self.router.ask(self.platform, self.chat, prompt, waiting)
        self.router.changed()
        logger.info("%s: 等你批准（第 %d 号确认）", self.key, confirm_id)

    def _settled(self, confirm_id: int, allowed: bool, stopped: bool) -> None:
        waiting = self.waiting
        if waiting is None or waiting.id != confirm_id:
            return
        self.waiting = None
        if stopped:
            outcome = t("已中断")
        elif waiting.by == "web":
            outcome = t("已在网页上允许") if allowed else t("已在网页上拒绝")
        else:
            outcome = t("已允许") if allowed else t("已拒绝")
        self.router.settle(self.platform, self.chat, waiting, outcome)
        self.router.changed()


def _dedent(text: str) -> str:
    """去掉每行开头共同的缩进和首尾的空行（Permissions.review 写的说明里，命令和文件内容是缩进了四格的）。"""
    return textwrap.dedent(text).strip("\n")
