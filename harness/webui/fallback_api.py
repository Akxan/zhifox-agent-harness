"""设置面板里"备用模型"那一节背后的部分（POST /api/fallbacks）。

备用模型是什么、为什么只能选同一家的，见 harness/fallback.py。这里只做两件事：
  - state：放进页面状态里的样子：{"max": 每一家最多几个, "chosen": {提供方: [模型, …]}}（按顺序，没设的提供方不出现）
  - save：页面交回一家排好的名单。只认这一家现在能选的模型（和换模型面板同一份名单，见 model_catalog.models_of），
    最多 config.FALLBACK_MAX_MODELS 个，不能重复。空的名单表示这一家不用备用模型

改备用模型不碰正在进行的对话（只影响下一次"模型用不了"的时候挑谁），所以不用拿对话的锁，对话进行中也能改。
"""

from __future__ import annotations

from typing import Any

from ..config import FALLBACK_MAX_MODELS
from ..i18n import t
from ..model_catalog import models_of
from ..providers import find


def state(app: Any) -> dict:
    return {"max": FALLBACK_MAX_MODELS, "chosen": {provider: list(models) for provider, models in app.settings.fallbacks.items()}}


def save(app: Any, body: dict) -> tuple[int, dict]:
    """交回（HTTP 状态码，回答）。出错的回答里是给人看的话。"""
    provider, models = body.get("provider"), body.get("models")
    found = find(provider)  # 内置的四家，或者一个自定义接口（见 providers.find）
    if found is None or not isinstance(models, list) or not all(isinstance(model, str) for model in models):
        return 400, {"error": t("参数不正确")}
    if len(models) > FALLBACK_MAX_MODELS:
        return 400, {"error": t("每一家最多选 {n} 个备用模型。", n=FALLBACK_MAX_MODELS)}
    if len(set(models)) != len(models):
        return 400, {"error": t("同一个模型只能选一次。")}
    offered = models_of(app.settings, found.id, found)  # 和换模型的面板同一份名单（自定义接口的在它自己的设置里）
    if any(model not in offered for model in models):
        return 400, {"error": t("只能选 {provider} 的模型：换到另一家要开始新的对话。", provider=found.label)}
    app.settings.set_fallbacks(provider, models)
    return 200, {"ok": True, "fallbacks": state(app)}
