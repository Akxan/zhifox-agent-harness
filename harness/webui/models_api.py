"""换模型面板要的目录（POST /api/model/catalog）。

目录本身由 harness/model_catalog.py 算（能选哪些模型、能做什么、多少钱、换过去会怎样，规矩只在那里算一份）。
这里只做一件事：把里面给人看的话换成页面认得的样子（webui/ui.py 的 said：译好的文字，加上原文和填进去的值），
页面换语言时照着原文在原地重译，不用再问一遍服务器。

为什么不放进每次的状态里：OpenAI 的模型列表有上百个，每个模型再带几个标签，每调用一次模型都发一遍不划算。
面板打开时取一次；开着的时候状态变了（换了模型、填了密钥、同步了列表、这段对话聊起来了），页面再取一次。
"""

from __future__ import annotations

from typing import Any

from .. import model_catalog
from .ui import said


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
