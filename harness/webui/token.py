"""网页界面的口令：后台服务重启前后保持不变。

网页的地址里带着一个口令（?token=…），每个请求都要带着它（见 server.py 的检查）。在终端里开的 `harness --web`
每次启动换一个新的：你看着终端，地址就打印在那儿。后台服务不一样：它会自己重启（出错了、更新代码以后
`harness service restart`），每换一个口令，你浏览器里开着的那一页就打不开了，得再运行一次 `harness service open`。
所以后台服务的口令存进数据目录里一个只有你的账号能读的文件（.web-token，权限 600），重启时接着用。

这样会不会不安全：口令防的是这台电脑上的别的账号、和你浏览器里别的网站（它们连得上 127.0.0.1，但不知道口令）。
同一个账号的程序本来就读得到 data/ 里的会话和设置，口令放在旁边并没有多给它什么。想换一个新的：
卸载服务（会删掉这个文件）再装上，或者删掉这个文件再 `harness service restart`。
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path

from ..config import WEB_TOKEN
from ..safe import write_file

_GOOD = re.compile(r"[A-Za-z0-9_-]{16,128}")  # secrets.token_urlsafe 写出来的样子


def stable_token(data_dir: Path) -> str:
    """数据目录里存着的那个口令；没有、读不了、被改坏了、不是普通文件（比如一个指到别处的链接）的话，新生成一个存进去。"""
    path = data_dir / WEB_TOKEN
    try:
        if path.is_file() and not path.is_symlink():
            saved = path.read_text(encoding="utf-8").strip()
            if _GOOD.fullmatch(saved):
                path.chmod(0o600)  # 万一被别的程序改宽了，收回来
                return saved
    except OSError:
        pass
    token = secrets.token_urlsafe(16)
    if path.is_symlink():
        path.unlink()  # 不顺着链接往别处写
    write_file(path, token + "\n")  # 先写临时文件再替换，权限 600（见 safe.py）
    return token


def forget_token(data_dir: Path) -> None:
    """删掉存着的口令（卸载服务时）：下次再装上，就是一个新的。"""
    (data_dir / WEB_TOKEN).unlink(missing_ok=True)
