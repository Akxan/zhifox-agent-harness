"""网页服务器 · 发页面、网页应用的清单和图标（Handler 的一组方法，见 server.py 开头）。

  PAGE、MARKDOWN   启动时拼一次的页面（page/__init__.py）和回复的 Markdown 渲染器（markdown.js）。演示（tests/demo_web.py）
                   每次刷新都重新拼，换的就是这个模块上的 PAGE、MARKDOWN
  _send_page       每次发页面时填一个新的 nonce、口令、翻译表和渲染器；内容安全策略（CSP）只放行带这个 nonce 的脚本和样式，
                   为装成应用多放的两样由 manifest.policy 写；浏览器收 gzip 就压缩（为什么不怕 BREACH，见里面）
  _send_manifest、_send_icon  网页应用的清单（地址里带着口令，不进缓存）和自带的几张图标（只认 manifest.ICONS 里一字不差的名字）
  accepts_gzip     Accept-Encoding 里有没有 gzip

为什么单独一个文件：这几样都是 GET，发的是智狐自己的东西（页面、图标），不是你的数据；安全策略的说明又长，和路由表挤在一起不好读。
把你的数据（附件、截图）的字节发给浏览器的接口不在这里，在 server.py 的 _route，和 _send 挨着。
"""

from __future__ import annotations

import gzip
import re
import secrets
from pathlib import Path
from urllib.parse import quote

from .. import i18n
from ..config import PAGE_GZIP_LEVEL
from ..safe import to_json
from . import manifest as web_manifest
from .page import assemble as assemble_page
from .server_types import HandlerPart

# 页面由 page/ 里的几块拼成（骨架、样式、脚本，见 page/__init__.py），启动时拼一次。
# 拼好的仍然是一个自给自足的 HTML，里面留着 __NONCE__ 这几处，每次发页面时填（见下面的 _send_page）
PAGE = assemble_page()
# 回复里的 Markdown 怎么画，单独写在 markdown.js 里（一个文件只讲一件事，也方便用 Node 单独测试）。
# 发页面时把它原样填进页面里那个带 nonce 的 <script>，所以不用为它另开一个地址，也不用放宽安全策略。
MARKDOWN = (Path(__file__).parent / "markdown.js").read_text(encoding="utf-8")


class HandlerPage(HandlerPart):
    # ---- 页面、清单和图标 ------------------------------------------------------

    def _send_page(self) -> None:
        # nonce 是一个一次性的随机数：只有带着它的 <script> 和 <style> 才会被浏览器执行，
        # 这样即使页面里混进了别的脚本（比如模型输出里夹带的），浏览器也不会运行。
        nonce = secrets.token_urlsafe(16)
        # 图片和清单这两项由 manifest.policy 写：img-src 除了 data: 只多了自带的那几张图标的地址，manifest-src 只有清单那一个地址
        # （为什么要放行、为什么这样就够窄，见那里）。Host 已经在 _allowed 里查过，只能是 127.0.0.1:端口 或 localhost:端口
        policy = (
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; "
            f"{web_manifest.policy('http://' + self.headers.get('Host', ''))}; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        # 翻译表和 Markdown 渲染器也在这时塞进页面（翻译表放在一段 JSON 数据里，浏览器不会执行）。
        # 口令填进头上清单和图标的地址里：只是把打开这一页的地址里已有的东西再写一遍，quote 之后只剩字母、数字和 %-_.~。
        # 几处一次填完（re.sub 不会回头再扫一遍填进去的内容）：翻译表里万一出现 "__NONCE__"，
        # 或者 nonce 里碰巧有 "__I18N__"，都不会被当成要填的地方。
        fills = {
            "__NONCE__": nonce,
            "__TOKEN__": quote(self.server.web.token, safe=""),
            "__I18N__": i18n.page_payload(self.server.web.app.settings.language),
            "__MARKDOWN__": MARKDOWN,
        }
        page = re.sub("__NONCE__|__TOKEN__|__I18N__|__MARKDOWN__", lambda match: fills[match.group(0)], PAGE)
        headers = {"Content-Security-Policy": policy, "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY", "Vary": "Accept-Encoding"}
        body = page.encode()
        # 浏览器说收 gzip 的话压缩一下：一百多万字节变成三分之一左右，远程打开（手机、隧道）时快得多。
        # 页面里有口令。压缩以后的长度会随内容变，有一类攻击（BREACH）就是往同一个回答里塞进自己选的文字、
        # 看长度的变化一点点猜出秘密；它要求回答里回显请求带来的东西。这一页只填 nonce、口令、翻译表和 Markdown 渲染器，
        # 一个字都不回显请求（地址、请求头、表单），所以不受影响。以后要是往页面里填请求带来的内容，先想好这一条
        if accepts_gzip(self.headers.get("Accept-Encoding", "")):
            body = gzip.compress(body, PAGE_GZIP_LEVEL)
            headers["Content-Encoding"] = "gzip"
        self._send(200, "text/html; charset=utf-8", body, headers)

    def _send_manifest(self) -> None:
        """网页应用的清单（见 manifest.py）。里面的地址带着口令，所以和页面一样不进缓存。"""
        self._send(
            200,
            "application/manifest+json; charset=utf-8",
            to_json(web_manifest.manifest(self.server.web.token), readable=True).encode(),
            {"Content-Security-Policy": "default-src 'none'; sandbox"},
        )

    def _send_icon(self, name: str) -> None:
        """一张图标。name 是地址里 /icons/ 后面的那一段，原样拿去查表（不解码、不拼路径），
        不是 manifest.ICONS 里一字不差的文件名就是 404。内容是程序自带的固定的几张图，不是你的数据，
        所以让浏览器缓存一天（地址里带着口令，换了口令就是另一个地址）；类型写死成 image/png，不让浏览器猜。"""
        data = web_manifest.icon_bytes(name)
        if data is None:
            self._send_json(404, {"error": "not found"})
            return
        self._send(
            200,
            "image/png",
            data,
            {"Cache-Control": "private, max-age=86400", "Content-Security-Policy": "default-src 'none'; sandbox"},
        )


def accepts_gzip(header: str) -> bool:
    """Accept-Encoding 里有没有 gzip（或者 *），并且没写 q=0。"gzip, deflate, br"、"gzip;q=0.5" 都算；"gzip;q=0" 不算。"""
    accepted = False
    for item in header.split(","):
        name, _, params = item.strip().partition(";")
        name = name.strip().lower()
        if name not in ("gzip", "x-gzip", "*"):
            continue
        quality = 1.0
        for param in params.split(";"):
            key, _, value = param.strip().partition("=")
            if key.strip().lower() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0  # 写坏了的当成不要
        if name in ("gzip", "x-gzip"):
            return quality > 0  # 明说了 gzip 的，以它为准
        accepted = quality > 0
    return accepted
