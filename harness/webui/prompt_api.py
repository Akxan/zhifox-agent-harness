from __future__ import annotations

from typing import TYPE_CHECKING

from .. import prompt as prompts
from ..config import MAX_PROMPT_CHARS
from ..i18n import t

if TYPE_CHECKING:
    from .server import Handler
    from .webapp import WebApp


class PromptActions:
    # ---- 提示词 ------------------------------------------------------------

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

        和改设置一样拿对话的锁：当前这段还没开始聊的话，要换掉它的适配层，不能和正在进行的一轮撞上。
        """
        if not self._turn.acquire(blocking=False):
            return self._busy(), False
        try:
            try:
                applied = self.app.set_prompt(text)
            except prompts.PromptError as exc:
                return str(exc), False
            except OSError as exc:
                return t("没能保存提示词：{error}", error=exc.strerror or exc), False
            self.publish_state()
        finally:
            self._turn.release()
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
