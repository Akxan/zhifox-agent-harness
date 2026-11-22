"""设置里的"提示词"：读出、保存、恢复内置的、预览（模板和占位符见 ../prompt.py）。

  POST /api/prompt           读出现在用的（一行，在 server.py 的 _route 里：prompt_info）
  POST /api/prompt/save      保存改过的
  POST /api/prompt/reset     恢复内置的
  POST /api/prompt/preview   还没保存的模板换上占位符是什么样子（只是算一下）

PromptActions 混进 WebApp（见 webapp.py）：只有方法，用到的状态都在 WebApp.__init__ 里。
保存拿你正看着的那一段的锁：它还没开始聊的话要换掉它的适配层，不能和正在进行的一轮撞上。
后台在跑的那几段早就开始了，照旧用它们开始时的那版（一段对话里提示词不变，见 ../prompt.py）。

route_* 由 server.py 的 Handler._route 调用（写法和约定见 server.py 开头）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import prompt as prompts
from ..config import MAX_PROMPT_CHARS
from ..i18n import t
from .webapp_types import WebAppPart

if TYPE_CHECKING:
    from .server import Handler
    from .webapp import WebApp


class PromptActions(WebAppPart):
    """提示词：读出、保存。"""

    def prompt_info(self) -> dict:
        config = self.app.config
        template = prompts.load_template(config)
        web = config.web and self.app.provider.web  # 预览按现在选的这一家来填"能不能联网"
        return {
            "text": template,
            "default": prompts.DEFAULT_TEMPLATE,
            "custom": template != prompts.DEFAULT_TEMPLATE,
            "preview": prompts.render(template, config, web),
            "placeholders": [{"name": name, "value": value} for name, value in prompts.placeholder_values(config, web).items()],
        }

    def save_prompt(self, text: str | None) -> tuple[str, bool]:
        """保存（None 表示恢复内置的）提示词。返回（失败原因，当前这段是不是也用上了）。

        和改设置一样拿你正看着的那一段的锁：它还没开始聊的话，要换掉它的适配层，不能和正在进行的一轮撞上。
        """
        with self._viewed_idle() as free:
            if not free:
                return self._busy(), False
            try:
                applied = self.app.set_prompt(text)
            except prompts.PromptError as exc:
                return str(exc), False
            except OSError as exc:
                return t("没能保存提示词：{error}", error=exc.strerror or exc), False
            self.publish_state()
        return "", applied


def route_save(handler: Handler, web: WebApp, path: str, body: dict) -> None:
    text = body.get("text") if path.endswith("save") else None
    if path.endswith("save") and not isinstance(text, str):
        handler._send_json(400, {"error": t("参数不正确")})
    else:
        error, applied = web.save_prompt(text)
        if error:
            handler._send_json(409, {"error": error})
        else:
            handler._send_json(200, {"ok": True, "applied": applied})


def route_preview(handler: Handler, web: WebApp, body: dict) -> None:
    # 还没保存的模板换上占位符是什么样子。只是算一下，不改任何东西
    text = body.get("text")
    if not isinstance(text, str) or len(text) > MAX_PROMPT_CHARS:
        handler._send_json(400, {"error": t("参数不正确")})
    else:
        config = web.app.config
        handler._send_json(200, {"ok": True, "preview": prompts.render(text, config, config.web and web.app.provider.web)})
