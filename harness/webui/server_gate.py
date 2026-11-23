from __future__ import annotations

import json
import secrets

from ..i18n import t
from ..safe import clean
from .server_types import HandlerPart
from .skills_api import MAX_UPLOAD_BODY_BYTES

MAX_BODY_BYTES = 1_000_000
# 发消息的请求可以带附件（base64 编码后比原文件大三分之一），所以单独放宽到这么多。别的请求都是几行设置，1MB 足够
MAX_MESSAGE_BODY_BYTES = 64_000_000
MAX_DISCARD_BYTES = 16_000_000  # 请求体太大时，最多读掉这么多再回答（见 Handler._read_json）


class HandlerGate(HandlerPart):
    # ---- 安全检查 ----------------------------------------------------------

    def _allowed(self, supplied_token: str) -> bool:
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origin = self.headers.get("Origin")
        ok = (
            self.headers.get("Host") in hosts
            and (origin is None or origin in {f"http://{host}" for host in hosts})
            and secrets.compare_digest(supplied_token.encode(), self.server.web.token.encode())
        )
        if not ok:
            if self.command == "POST":
                # 先把（不大的）请求体读掉再回答。Windows 上关掉一个还有没读完的数据的连接，系统会直接发 RST，
                # 对方连这个 403 都收不到，只看到"连接被中止"（CI 的 Windows 上真出过）。只读不大的，读的量有上限
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                if 0 < length <= MAX_BODY_BYTES:
                    self._discard(length, MAX_BODY_BYTES)
            # 作为后台服务运行时口令重启前后不变（见 token.py），过期多半是重装过服务、或者是终端里开的那一份换了口令
            self._send_text(403, t("链接无效或已经过期。请使用终端里显示的完整地址重新打开。") + "\n" + t("智狐作为后台服务运行时，在终端里运行 uv run harness service open 重新打开。"))
        return ok

    def _read_json(self, limit: int) -> dict | None:
        """读出请求体并解析成字典。请求体超过 limit 个字节时回答 413，返回 None。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= limit:
            if limit == MAX_UPLOAD_BODY_BYTES:
                self._discard(length, limit + MAX_DISCARD_BYTES)
                self._send_json(413, {"error": t("上传的内容太大了：一次最多约 20 MB。")})
            elif limit > MAX_BODY_BYTES:
                # 发消息的请求：上限本来就大，多读掉的量也跟着放宽，不然页面还是只能看到"连接断开"
                self._discard(length, limit + MAX_DISCARD_BYTES)
                self._send_json(413, {"error": t("请求太大：一条消息连同附件最多发送约 64MB 的内容")})
            else:
                self._discard(length, MAX_DISCARD_BYTES)
                self._send_json(413, {"error": t("请求太大：一次最多发送约 1MB 的内容")})
            return None
        try:
            # clean：浏览器送来的文字（消息、标题……）在进门的地方统一洗一遍，见 safe.py
            body = clean(json.loads(self.rfile.read(length) or b"{}"))
        except (ValueError, RecursionError):  # RecursionError：嵌套了上千层的 JSON
            body = None
        if not isinstance(body, dict):
            self._send_json(400, {"error": t("请求格式不正确")})
            return None
        return body

    def _discard(self, length: int, most: int) -> None:
        """把一个不打算处理的请求体读掉扔了，最多读 most 个字节。

        不读的话，浏览器还在往这边发数据，连接就被我们关掉了：它来不及读到我们的回答，只会报"连接断开"。
        读掉再回答，页面才能把"请求太大"显示出来。读的量设了上限，免得被一个巨大的请求拖住。
        """
        remaining = min(max(length, 0), most)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)
