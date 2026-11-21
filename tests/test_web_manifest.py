"""网页应用的清单和图标（harness/webui/manifest.py）：书签、标签页、"创建快捷方式 / 安装成应用"和程序坞上的小狐狸。

用户在 Chrome 里给智狐的页面做了一个快捷方式，得到的是一块灰底上一个"智"字：页面没有清单，也没有够大的图标。
这里查服务器那一边：清单和图标要带口令、查 Host 和 Origin，图标只认一张固定的名单（请求里写什么都拼不出别的路径），
响应头固定；页面头上引用了它们，安全策略只多放行了这几个地址。还查图标文件本身：尺寸对、透明底的四角是透明的、
有底色的那几张狐狸整个落在 maskable 的安全区里（系统怎么裁都裁不到它）。
在真的 Chrome 里取清单、判断能不能装成应用，见 tests/ui/test_ui_install.py。
"""

from __future__ import annotations

import http.client
import json
import re
import struct
import zlib
from pathlib import Path
from urllib.parse import unquote

import test_webui

from harness.webui import manifest as web_manifest
from harness.webui import page as page_module

# 借用 test_webui.py 里的 fixture：serve 起一个真的网页服务器，后面连着假的模型
serve = test_webui.serve

ROOT = Path(__file__).resolve().parent.parent
LOGO = ROOT / "docs" / "assets" / "logo.svg"
SAFE_RADIUS = 0.4  # maskable 图标的安全区：中心一个半径为边长 40% 的圆（W3C 的 manifest 规范）


def fetch(host: str, path: str, headers: dict | None = None) -> tuple[int, dict, bytes]:
    """发一个 GET，原样拿回字节（test_webui.request 会把回答当成文字解码，PNG 解不了）。"""
    connection = http.client.HTTPConnection(host, timeout=10)
    connection.request("GET", path, headers=headers or {})
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, dict(response.getheaders()), body


def directives(policy: str) -> dict[str, list[str]]:
    """把 CSP 拆成 {指令: [来源, …]}。"""
    parsed = {}
    for part in policy.split(";"):
        words = part.split()
        if words:
            parsed[words[0]] = words[1:]
    return parsed


# ---- 带口令、查来路 ----------------------------------------------------------


def test_the_manifest_and_icons_need_the_token_and_the_right_host(serve):
    s = serve()
    host, token = s.browser.host, s.browser.token
    port = host.split(":")[1]
    for path in (web_manifest.MANIFEST_PATH, "/icons/icon-512.png"):
        assert fetch(host, path)[0] == 403, path
        assert fetch(host, f"{path}?token=wrong")[0] == 403, path
        # 口令对，但访问时用的不是本机地址（DNS 重绑定），或者请求是从别的网站发起的
        assert fetch(host, f"{path}?token={token}", {"Host": "evil.example"})[0] == 403, path
        assert fetch(host, f"{path}?token={token}", {"Origin": "http://evil.example"})[0] == 403, path
        assert fetch(host, f"{path}?token={token}", {"Origin": f"http://127.0.0.1:{int(port) + 1}"})[0] == 403, path
        # 用 localhost 打开的页面一样能取
        assert fetch(host, f"{path}?token={token}", {"Host": f"localhost:{port}", "Origin": f"http://localhost:{port}"})[0] == 200
    assert s.api.requests == []


def test_only_the_listed_icons_are_served(serve):
    s = serve()
    host, token = s.browser.host, s.browser.token
    for icon in web_manifest.ICONS:
        status, headers, body = fetch(host, f"/icons/{icon.name}?token={token}")
        assert status == 200, icon.name
        assert body == (web_manifest.FOLDER / icon.name).read_bytes()
        # 类型写死，不让浏览器猜；万一被直接打开也什么都不运行；自带的图可以缓存一天，但只缓存在这一个浏览器里
        assert headers["Content-Type"] == "image/png"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Content-Security-Policy"] == "default-src 'none'; sandbox"
        assert headers["Cache-Control"] == "private, max-age=86400"
    # 名单以外的名字一律 404：查表，不拼路径，也不解码，所以 ..、%2F、大小写、多一个斜杠都没用
    for name in (
        "", "icon-16.png/", "ICON-16.PNG", "icon-16.png%00", "icon-16", "icon-1024.png",
        "../manifest.py", "..%2Fmanifest.py", "%2e%2e/server.py", "../../config.py", "../../../.env",
        "/etc/passwd", "%2Fetc%2Fpasswd", "./icon-16.png", "icons/icon-16.png", "icon-16.png\\..\\..\\.env",
    ):
        status, headers, body = fetch(host, f"/icons/{name}?token={token}")
        assert status == 404, name
        assert headers["Content-Type"].startswith("application/json") and b"PNG" not in body, name
    # 就算 icons 文件夹里多了一个文件（比如重画时留下的草稿），不在名单上也取不到
    assert web_manifest.icon_bytes("preview.png") is None
    assert web_manifest.icon_bytes("icon-16.png") == (web_manifest.FOLDER / "icon-16.png").read_bytes()


# ---- 清单 ----------------------------------------------------------------------


def test_the_manifest_describes_the_app(serve):
    s = serve()
    host, token = s.browser.host, s.browser.token
    status, headers, body = fetch(host, f"/manifest.webmanifest?token={token}")
    assert status == 200
    assert headers["Content-Type"] == "application/manifest+json; charset=utf-8"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store"  # 里面的地址带着口令，不进缓存
    assert headers["Content-Security-Policy"] == "default-src 'none'; sandbox"
    data = json.loads(body)
    assert data["name"] == data["short_name"] == "智狐"
    assert data["display"] == "standalone"
    # 从快捷方式打开时用的地址带着口令；id 不带：换了口令，浏览器也还认得是同一个应用
    assert data["start_url"] == f"/?token={token}"
    assert data["id"] == "/" and data["scope"] == "/"
    assert data["theme_color"] == data["background_color"] == web_manifest.THEME_COLOR
    icons = data["icons"]
    assert all(icon["src"].startswith("/icons/") and icon["src"].endswith(f".png?token={token}") for icon in icons)
    assert all(icon["type"] == "image/png" for icon in icons)
    by_purpose = {(icon["sizes"], icon["purpose"]) for icon in icons}
    # Chrome 要至少一张 144 像素以上的 any 图标才肯装；512 的 maskable 给 macOS 的程序坞、Android、ChromeOS
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable"), ("16x16", "any"), ("32x32", "any")} <= by_purpose
    # 清单里的每张图都取得到
    for icon in icons:
        status, headers, _ = fetch(host, icon["src"])
        assert status == 200 and headers["Content-Type"] == "image/png", icon


def test_the_token_is_escaped_in_every_address():
    # 口令照理只有字母、数字、- 和 _（token.py 的格式检查），但万一哪天换了生成的办法，地址也不能被它写坏
    data = web_manifest.manifest("a b&c/d?e#f")
    assert data["start_url"] == "/?token=a%20b%26c%2Fd%3Fe%23f"
    assert all(icon["src"].endswith("?token=a%20b%26c%2Fd%3Fe%23f") for icon in data["icons"])


# ---- 页面头上的引用和安全策略 ---------------------------------------------------------


def test_the_page_links_the_manifest_and_the_icons(serve):
    s = serve()
    host, token = s.browser.host, s.browser.token
    status, _, body = fetch(host, f"/?token={token}")
    assert status == 200
    page = body.decode()
    head = page.split("</head>")[0]
    assert "__TOKEN__" not in page
    assert page.count(token) == 2  # 口令只填进下面这两个地址里，别处（注释、脚本）一处都没有
    assert f'<link rel="manifest" href="/manifest.webmanifest?token={token}">' in head
    assert f'<link rel="apple-touch-icon" href="/icons/apple-touch-icon.png?token={token}">' in head
    assert '<link rel="icon" href="data:image/svg+xml,' in head
    assert f'<meta name="theme-color" content="{web_manifest.THEME_COLOR}">' in head
    # 引用的地址都取得到
    for path in re.findall(r'<link rel="(?:manifest|apple-touch-icon)" href="([^"]+)">', head):
        assert fetch(host, path)[0] == 200, path


def test_the_policy_lets_in_only_the_manifest_and_the_shipped_icons(serve):
    s = serve()
    host, token = s.browser.host, s.browser.token
    port = host.split(":")[1]
    for name in ("127.0.0.1", "localhost"):
        origin = f"http://{name}:{port}"
        status, headers, _ = fetch(host, f"/?token={token}", {"Host": f"{name}:{port}"})
        assert status == 200
        policy = directives(headers["Content-Security-Policy"])
        # 别的都和原来一样：什么都不许，脚本和样式只认 nonce，请求只发给自己
        assert policy["default-src"] == ["'none'"]
        assert len(policy["script-src"]) == 1 and policy["script-src"][0].startswith("'nonce-")
        assert len(policy["style-src"]) == 1 and policy["style-src"][0].startswith("'nonce-")
        assert policy["connect-src"] == ["'self'"]
        assert policy["frame-ancestors"] == policy["base-uri"] == policy["form-action"] == ["'none'"]
        # 清单只有这一个地址；图片除了 data: 只有自带的那几张，每张一个完整的地址（不是 'self'，也不是整个文件夹）
        assert policy["manifest-src"] == [f"{origin}/manifest.webmanifest"]
        assert policy["img-src"] == ["data:"] + [f"{origin}/icons/{icon.name}" for icon in web_manifest.ICONS]
        assert not any(source.endswith("/") or "*" in source or source == "'self'" for source in policy["img-src"])
        assert set(policy) == {"default-src", "script-src", "style-src", "connect-src", "img-src", "manifest-src",
                               "base-uri", "form-action", "frame-ancestors"}


# ---- 同一只狐狸 ------------------------------------------------------------------


def _paths(svg: str) -> list[str]:
    return re.findall(r'<path d="([^"]+)"', svg)


def test_the_favicon_and_the_page_logo_are_the_same_fox():
    # 标签页上的小图标（内嵌的 SVG）、页面里的 #i-fox 和 docs/assets/logo.svg（图标都从它画出来）是同一幅画：
    # 改了一处忘了另外两处，不会有任何报错，只会让几个地方的狐狸长得不一样
    logo = _paths(LOGO.read_text(encoding="utf-8"))
    assert len(logo) >= 4
    skeleton = (page_module.FOLDER / "index.html").read_text(encoding="utf-8")
    favicon = unquote(re.search(r'<link rel="icon" href="data:image/svg\+xml,([^"]+)">', skeleton).group(1))
    assert re.findall(r"<path d='([^']+)'", favicon) == logo
    symbol = re.search(r'<symbol id="i-fox".*?</symbol>', skeleton, flags=re.DOTALL).group(0)
    assert _paths(symbol) == logo


def test_the_theme_color_is_the_light_window_color():
    # 清单和页面头上的 theme-color 是浅色外观的窗口底色（--bg）；颜色本身只在 tokens.css 里定义
    tokens = (page_module.FOLDER / "styles" / "tokens.css").read_text(encoding="utf-8")
    light = re.search(r":root \{(.*?)\}", tokens, flags=re.DOTALL).group(1)
    assert re.search(r"--bg:\s*(#[0-9a-f]{6})", light).group(1) == web_manifest.THEME_COLOR


# ---- 图标文件本身 ------------------------------------------------------------------


def decode_png(data: bytes) -> tuple[int, int, list[bytes]]:
    """读出一张 8 位 RGBA、不隔行的 PNG（make_icons.py 写的就是这种）：（宽，高，每一行的像素）。"""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    at, idat, header = 8, b"", None
    while at < len(data):
        length, kind = struct.unpack(">I4s", data[at:at + 8])
        chunk = data[at + 8:at + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"IDAT":
            idat += chunk
        at += 12 + length
    width, height, depth, color, _, _, interlace = header
    assert (depth, color, interlace) == (8, 6, 0), "应该是 8 位 RGBA、不隔行"
    raw, stride = zlib.decompress(idat), width * 4
    rows, previous = [], bytearray(stride)
    for y in range(height):
        kind, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - 4] if i >= 4 else 0
            b = previous[i]
            c = previous[i - 4] if i >= 4 else 0
            if kind == 1:
                line[i] = (line[i] + a) & 255
            elif kind == 2:
                line[i] = (line[i] + b) & 255
            elif kind == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif kind == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(bytes(line))
        previous = line
    return width, height, rows


def test_the_icon_files_are_the_listed_sizes_and_keep_the_fox_safe():
    on_disk = sorted(path.name for path in web_manifest.FOLDER.iterdir())
    assert on_disk == sorted(icon.name for icon in web_manifest.ICONS), "icons/ 里的文件和 manifest.ICONS 对不上"
    tile = bytes.fromhex(web_manifest.TILE_COLOR[1:]) + b"\xff"
    for icon in web_manifest.ICONS:
        width, height, rows = decode_png((web_manifest.FOLDER / icon.name).read_bytes())
        n = icon.size
        assert (width, height) == (n, n), icon.name

        def pixel(x: int, y: int, rows: list[bytes] = rows) -> bytes:
            return rows[y][x * 4:x * 4 + 4]

        center = pixel(n // 2, n // 2)
        assert center[3] == 255 and center != tile, f"{icon.name} 的中间不是狐狸"
        if not icon.tile:
            # 透明底：四个角是透明的（不是一块方形的底）
            assert all(pixel(x, y)[3] == 0 for x in (0, n - 1) for y in (0, n - 1)), icon.name
            continue
        # 有底色的：整张不透明；中心半径 40% 的圆（maskable 的安全区）外面全是底色，狐狸一点都没出去
        radius = SAFE_RADIUS * n
        outside = [
            (x, y) for y in range(n) for x in range(n)
            if (x + 0.5 - n / 2) ** 2 + (y + 0.5 - n / 2) ** 2 > radius ** 2
        ]
        assert all(pixel(x, y)[3] == 255 for y in range(n) for x in range(n)), icon.name
        stray = [(x, y) for x, y in outside if pixel(x, y) != tile]
        assert stray == [], f"{icon.name} 的狐狸画出了安全区：{stray[:5]}"
