from __future__ import annotations

from .browser import urls
from .i18n import t
from .tools.fetch import FetchFacts

# ---- 读网页 --------------------------------------------------------------------


def review_fetch(facts: FetchFacts) -> str | None:
    """读一个网页要不要问你（规矩见文件开头）：这段对话里第一次读一个网站之前问。读不了的（不是 http(s)、写法古怪……）不问：
    工具会直接把原因告诉模型（Tool.check），什么都不会发出去。卡片上是完整的网址：带出去的就是它。"""
    if facts.refusal or not facts.url or not facts.new_site:
        return None
    return t(
        "读一个网页（这个网址会原样发给那个网站）：\n\n    {url}\n\n这段对话里还没读过 {site} 的网页。网址里的每一段（包括 ? 后面的）那个网站都看得到。"
        "允许以后，这段对话里再读这个网站就不再问。",
        url=urls.shown(facts.url, urls.MAX_URL_CHARS),
        site=facts.site,
    )
