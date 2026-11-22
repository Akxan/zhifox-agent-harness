"""模型：换模型面板要的目录，和设置里的换模型、API 密钥、同步模型列表、价格。

  POST /api/model/catalog  换模型面板的目录（page_state，一行，在 server.py 的 _route 里）
  POST /api/model          换模型（select_model；能不能接着聊由 App.select_model 和 providers.continues 定）
  POST /api/key            填写或删除一家的 API 密钥（填了顺便同步一次模型列表）
  POST /api/models         同步模型列表
  POST /api/price          填写现在用的这个模型的价格（_valid_price）

目录本身由 harness/model_catalog.py 算（能选哪些模型、能做什么、多少钱、换过去会怎样，规矩只在那里算一份）。
page_state 只做一件事：把里面给人看的话换成页面认得的样子（webui/ui.py 的 said：译好的文字，加上原文和填进去的值），
页面换语言时照着原文在原地重译，不用再问一遍服务器。

为什么不放进每次的状态里：OpenAI 的模型列表有上百个，每个模型再带几个标签，每调用一次模型都发一遍不划算。
面板打开时取一次；开着的时候状态变了（换了模型、填了密钥、同步了列表、这段对话聊起来了），页面再取一次。

ModelActions 混进 WebApp（见 webapp.py）：只有方法，用到的状态都在 WebApp.__init__ 里。
_provider_state 是状态里 providers 的一家（密钥只有"有没有"和最后 4 位）。换模型、填密钥拿对话的锁，同步模型、改价格不拿。

route_* 由 server.py 的 Handler._route 调用（写法和约定见 server.py 开头）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .. import model_catalog
from ..i18n import t
from ..providers import PROVIDERS, ProviderError, caps, continues, find, has_key, hint
from ..settings import valid_api_key
from .ui import said
from .webapp_types import WebAppPart

if TYPE_CHECKING:
    from .server import Handler
    from .webapp import WebApp


def page_state(app: Any) -> dict:
    found = model_catalog.catalog(app)
    return {
        "provider": found.provider,
        "model": found.model,
        "started": found.started,
        "persona": None if found.persona is None else {"name": found.persona.name, "model": found.persona.model},
        "groups": [
            {
                "id": group.id,
                "label": group.label,
                "has_key": group.has_key,
                "key_hint": group.key_hint,  # 只有最后 4 位
                "synced": group.synced,
                "switch": group.switch,
                "custom": group.custom,  # 自定义接口（见 harness/endpoints/）
                "where": group.where,  # 它在哪儿：local / lan / remote；内置的几家是空的
                "models": [
                    {
                        "id": model.id,
                        "current": model.current,
                        "tags": [said(tag) for tag in model.tags],
                        "price": model.price,
                        "offpeak": model.offpeak,
                        "free": said(model.free) if model.free else None,  # 价格是 0 时写的那句话
                        "switch": model.switch,  # 和这一组不一样时才有
                    }
                    for model in group.models
                ],
            }
            for group in found.groups
        ],
    }


class ModelActions(WebAppPart):
    """换模型、密钥、同步模型列表、价格。"""

    def _provider_state(self, provider) -> dict:
        """状态里的一家。密钥只有"有没有"和最后 4 位。"""
        settings = self.app.settings
        # 能收哪些附件：页面据此决定附件按钮能选什么、不能选的给出说明。文本文件哪一家都能收。
        # 自定义接口按模型算：现在用的这一家写现在这个模型的，别的写"有没有哪个模型能"
        accepts = caps(provider, settings.model) if provider.id == settings.provider else provider
        return {
            "id": provider.id,
            "label": provider.label,
            "has_key": has_key(provider),
            "key_hint": hint(provider),
            "key_page": provider.key_page,
            "models": model_catalog.models_of(settings, provider.id, provider),  # 规矩在 model_catalog.models_of
            "vision": accepts.vision,
            "pdf": accepts.pdf,
            "custom": provider.custom,  # 自定义接口：设置里的"API 密钥"一节不列它（它有自己的一节）
        }

    # ---- 换模型、密钥、同步模型列表、价格 ------------------------------------
    # 换模型、填密钥会改动你正看着的那一段的助手，所以拿它的锁（见 _viewed_idle）：它在进行一轮时不能改。
    # 后台在跑的那几段不受影响（它们各有各的适配层；填的密钥从它们的下一次请求起生效，见 App.set_api_key）。
    # 同步模型列表、改价格不拿锁。返回值的约定和 send 一样：空字符串表示成功，否则是失败的原因。

    def select_model(self, provider_id: str, model: str) -> str:
        provider = find(provider_id)
        if provider is None:
            return t("这个模型接口已经不在了，可能刚被删掉。")
        if not has_key(provider):
            return t("还没有设置 {provider} 的 API 密钥", provider=provider.label)
        with self._viewed_idle() as free:
            if not free:
                return self._busy()
            # 换到另一家要开始新对话，也就是离开当前会话（同一个自定义接口上，能不能用工具不一样的两个模型之间换也是，见 providers.continues）
            same_provider = provider_id == self.app.agent.backend.provider
            if not continues(self.app.agent.backend, provider, model):
                blocked = self._before_leaving(self.sessions.viewed, f"model:{provider_id}:{model}")
                if blocked:
                    return blocked
            else:
                # 同一家：这段会话接着用新模型，要存一次盘。它正在聊天通道里跑一轮的话，两边会抢着写同一个文件（见 claims.py）
                held = self._held_elsewhere(self.app.conversation.id)
                if held:
                    return held
            continued = self.app.select_model(provider_id, model)
            if not continued:
                self._left(self.sessions.viewed)
                self.ui.reset()  # 换了一家：历史格式不通用，页面也清空
            label = provider.label
            if continued:
                self.ui.info(t("已换成 {provider} · {model}。对话继续。", provider=label, model=model))
            elif same_provider:
                self.ui.info(t("已换成 {provider} · {model}。它能不能用工具和刚才的模型不一样，开始新对话。", provider=label, model=model))
            else:
                self.ui.info(t("已换成 {provider} · {model}。换了一家，开始新对话。", provider=label, model=model))
            self._show_warnings()
            if continued:
                self._save()  # 这段会话以后用新模型，记下来
            self.publish_state()
        return ""

    def set_api_key(self, provider_id: str, key: str) -> tuple[str, str]:
        """保存或删除一家的 API 密钥。返回（失败原因，提醒）。

        保存成功后顺便同步一次模型列表；如果同步失败（比如 key 不对），key 仍然保存了，
        原因放在"提醒"里告诉用户。
        """
        warning = ""
        with self._viewed_idle() as free:
            if not free:
                return self._busy(), ""
            self.app.set_api_key(provider_id, key)
            if key:
                try:
                    self.app.sync_models(provider_id)
                except ProviderError as exc:
                    warning = t("key 已保存，但同步模型列表失败：{error}", error=exc)
            else:
                self.app.settings.set_models(provider_id, None)
            self.publish_state()
        return "", warning

    def sync_models(self, provider_id: str) -> str:
        try:
            self.app.sync_models(provider_id)
        except ProviderError as exc:
            return str(exc)
        self.publish_state()
        return ""

    def set_price(self, model: str, price: dict | None) -> None:
        """设置里"价格"一节改的是现在用的这个模型：自定义接口上的记进 endpoints.json，内置的记进 settings.json（见 App.set_price）。"""
        self.app.set_price(self.app.settings.provider, model, price)
        self.publish_state()


def route_model(handler: Handler, web: WebApp, body: dict) -> None:
    provider, model = body.get("provider"), body.get("model")
    if find(provider) is None or not isinstance(model, str) or not 0 < len(model.strip()) <= 200:
        handler._send_json(400, {"error": t("参数不正确")})
    else:
        handler._finish(web.select_model(provider, model.strip()))


def route_key(handler: Handler, web: WebApp, body: dict) -> None:
    provider, key = body.get("provider"), body.get("key")
    if provider not in PROVIDERS or not isinstance(key, str):
        handler._send_json(400, {"error": t("参数不正确")})
    elif key.strip() and not valid_api_key(key.strip()):
        handler._send_json(400, {"error": t("这不像是一个 API 密钥：里面不能有空格、换行或中文")})
    else:
        error, warning = web.set_api_key(provider, key.strip())
        handler._finish(error, warning=warning)


def route_models(handler: Handler, web: WebApp, body: dict) -> None:
    provider = body.get("provider")
    if find(provider) is None:
        handler._send_json(400, {"error": t("参数不正确")})
    else:
        handler._finish(web.sync_models(provider))


def route_price(handler: Handler, web: WebApp, body: dict) -> None:
    model, price = body.get("model"), body.get("price")
    if not isinstance(model, str) or not model or not _valid_price(price):
        handler._send_json(400, {"error": t("价格必须是不小于 0 的数字，输入和输出两项必填")})
    else:
        web.set_price(model, price)
        handler._send_json(200, {"ok": True})


def _valid_price(price: object) -> bool:
    """价格要么是 None（删除自己填的价格），要么是一组不小于 0 的数字。"""
    if price is None:
        return True
    if not isinstance(price, dict) or not {"input", "output"} <= set(price) <= {"input", "output", "cache_read"}:
        return False
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 100_000 for v in price.values())
