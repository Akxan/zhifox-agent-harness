"""重新画网页应用的图标（harness/webui/icons/ 里的几张 PNG）：从 docs/assets/logo.svg 出发，用无界面的 Chrome 画。

    uv run python tests/make_icons.py                    # 重画，写进 harness/webui/icons/
    uv run python tests/make_icons.py --preview 某个文件夹  # 再存一张对照图（preview.png）：各种尺寸、浅色和深色的底、
                                                         # 标签页和程序坞里的样子，看一眼就知道画得好不好

画哪几张、多大、有没有底色，写在 harness/webui/manifest.py 的 ICONS 里（只写在那一处）。改了标志或者 ICONS，就运行一次，
看一眼对照图，再把新的 PNG 一起提交。程序运行时只读这几个文件，不会再画。

为什么用 Chrome：智狐自己就有一个手写的 CDP 客户端（harness/browser/），页面测试也用它，不用为了画几张图再装一个
SVG 渲染库。每张图在页面里画到一块正好那么大的 canvas 上（SVG 的宽高直接写成目标尺寸，浏览器按矢量画，不是先画小了再放大），
读出每个像素（getImageData），再由这里写成 PNG（zlib 最高压缩，每行挑最省地方的过滤方式）。
放在 tests/ 里，因为它和 demo_web.py 一样是开发时用的工具，不随程序运行。
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import struct
import sys
import tempfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.browser import chrome
from harness.webui.manifest import FOLDER, ICONS, TILE_COLOR, Icon

LOGO = ROOT / "docs" / "assets" / "logo.svg"

# 标志的画布是 32×32，狐狸连同描边大约占 x 1.5–30.5、y 1.3–30.5。有底色的图标要让狐狸整个落在 maskable 的安全区里
# （中间一个半径为边长 40% 的圆，见 https://www.w3.org/TR/appmanifest/#icon-masks）。离中心 (16, 16) 最远的是两只耳朵尖，
# 连描边约 18.3 个单位；再留一点余地，按 18.6 算
FOX_REACH = 18.6
SAFE_RADIUS = 0.4
# 不超过这么大的图标去掉眼睛里的两个高光：16 像素时它们不到半个像素，画不出亮点，只会把眼睛弄成一团灰；
# 去掉以后眼睛是两个清楚的深色方点，一眼看得出是一张脸。32 像素起高光有一个像素大，留着更有神
NO_HIGHLIGHTS_UP_TO = 16
HIGHLIGHTS = re.compile(r'<g fill="#ffecd2">.*?</g>', flags=re.DOTALL)  # logo.svg 里最后那一组：两个浅色的小圆


def logo_body() -> str:
    """logo.svg 里 <svg> 里面的那些（渐变和几条路径），去掉 <title>。"""
    text = LOGO.read_text(encoding="utf-8")
    inner = re.search(r"<svg\b[^>]*>(.*)</svg>", text, flags=re.DOTALL)
    if inner is None:
        raise SystemExit(f"{LOGO} 不是一个 SVG")
    body = re.sub(r"<title>.*?</title>", "", inner.group(1), flags=re.DOTALL).strip()
    if len(HIGHLIGHTS.findall(body)) != 1:
        raise SystemExit(f"{LOGO} 里找不到（或者不止一组）眼睛的高光 {HIGHLIGHTS.pattern}：标志改过的话，先改这里")
    return body


def drawing(icon: Icon, body: str) -> str:
    """一张图标的 SVG：宽高就是目标尺寸，浏览器直接按这个大小画。"""
    n = icon.size
    if n <= NO_HIGHLIGHTS_UP_TO:
        body = HIGHLIGHTS.sub("", body)
    if not icon.tile:
        # 透明底：标志原样铺满（它自己四周留着一圈描边的余地）
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{n}" height="{n}" viewBox="0 0 32 32">{body}</svg>'
    # 底色铺满整张；狐狸缩到安全区里，居中
    scale = SAFE_RADIUS * n / FOX_REACH
    side = 32 * scale
    offset = (n - side) / 2
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{n}" height="{n}" viewBox="0 0 {n} {n}">'
        f'<rect width="{n}" height="{n}" fill="{TILE_COLOR}"/>'
        f'<svg x="{offset:.3f}" y="{offset:.3f}" width="{side:.3f}" height="{side:.3f}" viewBox="0 0 32 32">{body}</svg>'
        "</svg>"
    )


# 在页面里画：SVG 先变成一张图，画到 canvas 上，读出 RGBA 的像素（没有预乘透明度，正好是 PNG 要的）
RASTERIZE = """
(async (svg, size) => {
  const image = new Image();
  image.src = 'data:image/svg+xml;base64,' + svg;
  await image.decode();
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = size;
  const context = canvas.getContext('2d');
  context.drawImage(image, 0, 0, size, size);
  const pixels = context.getImageData(0, 0, size, size).data;
  let binary = '';
  for (let at = 0; at < pixels.length; at += 0x8000) binary += String.fromCharCode.apply(null, pixels.subarray(at, at + 0x8000));
  return btoa(binary);
})
"""


class Painter:
    """一个无界面的 Chrome，加一个空白的标签页。"""

    def __init__(self, profile: Path):
        executable = chrome.find_chrome()
        if executable is None:
            raise SystemExit("找不到 Chrome（可以用环境变量 HARNESS_CHROME 指定它的路径）")
        self.process = chrome.launch(executable, profile, headless=True, extra_args=["--force-device-scale-factor=1"])
        connection = self.process.connection
        target = connection.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        self.session = connection.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        self.connection = connection

    def call(self, method: str, params: dict, timeout: float = 30) -> dict:
        return self.connection.call(method, params, session=self.session, timeout=timeout)

    def evaluate(self, expression: str):
        reply = self.call("Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True})
        if "exceptionDetails" in reply:
            raise SystemExit(f"Chrome 里出错了：{reply['exceptionDetails']}")
        return reply["result"]["value"]

    def rasterize(self, svg: str, size: int) -> bytes:
        encoded = base64.b64encode(svg.encode()).decode()
        return base64.b64decode(self.evaluate(f"({RASTERIZE})({json.dumps(encoded)}, {size})"))

    def screenshot(self, html: str, width: int, height: int) -> bytes:
        self.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
        frame = self.call("Page.getFrameTree", {})["frameTree"]["frame"]["id"]
        self.call("Page.setDocumentContent", {"frameId": frame, "html": html})
        self.evaluate("Promise.all([...document.images].map((image) => image.decode()))")
        return base64.b64decode(self.call("Page.captureScreenshot", {"format": "png"})["data"])

    def close(self) -> None:
        self.process.close()


def png(rgba: bytes, size: int) -> bytes:
    """把 RGBA 像素写成 PNG。每一行试五种过滤方式，挑"字节加起来最小"的那种（PNG 规范推荐的办法），再用 zlib 最高一档压缩。"""
    stride = size * 4
    previous = bytes(stride)
    rows = []
    for y in range(size):
        row = rgba[y * stride:(y + 1) * stride]
        rows.append(_best_filter(row, previous))
        previous = row
    raw = b"".join(rows)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8 位、RGBA、标准压缩、标准过滤、不隔行
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def _best_filter(row: bytes, up: bytes) -> bytes:
    def left(i: int) -> int:
        return row[i - 4] if i >= 4 else 0

    def corner(i: int) -> int:
        return up[i - 4] if i >= 4 else 0

    def paeth(a: int, b: int, c: int) -> int:
        p = a + b - c
        pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
        return a if pa <= pb and pa <= pc else b if pb <= pc else c

    n = len(row)
    candidates = [
        bytes([0]) + row,
        bytes([1]) + bytes((row[i] - left(i)) & 255 for i in range(n)),
        bytes([2]) + bytes((row[i] - up[i]) & 255 for i in range(n)),
        bytes([3]) + bytes((row[i] - (left(i) + up[i]) // 2) & 255 for i in range(n)),
        bytes([4]) + bytes((row[i] - paeth(left(i), up[i], corner(i))) & 255 for i in range(n)),
    ]
    # 把每个字节当成有符号数加绝对值：越接近 0，zlib 越好压
    return min(candidates, key=lambda line: sum(value if value < 128 else 256 - value for value in line[1:]))


PREVIEW_SIZE = (1200, 720)
PREVIEW_STYLE = (
    "body{margin:0;font:12px system-ui;display:grid;grid-template-columns:1fr 1fr}"
    "section{padding:20px;display:flex;flex-direction:column;gap:18px}"
    ".light{background:#eeece7;color:#16171b}.dark{background:#1d1e23;color:#edece8}"
    ".row{display:flex;gap:18px;align-items:flex-end;flex-wrap:wrap}"
    "figure{margin:0;display:flex;flex-direction:column;gap:6px;align-items:center}"
    ".pixels{image-rendering:pixelated}"
    ".tab{display:flex;align-items:center;gap:8px;padding:8px 14px;border-radius:10px 10px 0 0;width:max-content;font-size:13px}"
    ".light .tab{background:#fff}.dark .tab{background:#35363a}"
    ".dock{display:flex;gap:14px;padding:10px 14px;border-radius:22px;width:max-content;align-items:center}"
    ".light .dock{background:rgba(255,255,255,.75);box-shadow:0 0 0 1px rgba(0,0,0,.08)}.dark .dock{background:#3a3a40}"
    ".squircle{border-radius:22.37%;box-shadow:0 1px 4px rgba(0,0,0,.3)}"
)


def preview_page(images: dict[str, bytes]) -> str:
    """对照图，左边浅色、右边深色的底：16 和 32 像素的原大和放大（放大时不平滑，看得清每个像素）；大图标；
    标签页里的样子；程序坞里的样子（maskable 那张裁成圆角方块，macOS Sonoma 以后的 Chrome 就这么用它，
    透明底的那张原样放着，apple-touch-icon 是 Safari 的"添加到程序坞"用的）。"""

    def img(name: str, shown: int, pixels: bool = False) -> str:
        data = base64.b64encode(images[name]).decode()
        return f'<img src="data:image/png;base64,{data}" width="{shown}" height="{shown}"{" class=pixels" if pixels else ""}>'

    def figure(inner: str, caption: str) -> str:
        return f"<figure>{inner}<figcaption>{caption}</figcaption></figure>"

    small = "".join(
        figure(img(icon.name, 128, pixels=True) + img(icon.name, icon.size), f"{icon.name}（放大 {128 // icon.size} 倍、原大）")
        for icon in ICONS if icon.size <= 32
    )
    large = "".join(figure(img(icon.name, 112), f"{icon.name} · {icon.purpose}") for icon in ICONS if icon.size > 32)
    tab = f'<div class="tab">{img("icon-16.png", 16)}<span>智狐 · 16</span>{img("icon-32.png", 16)}<span>32 画成 16（高分屏）</span></div>'
    dock = (
        '<div class="dock">'
        + img("icon-maskable-512.png", 72).replace("<img", '<img class="squircle"', 1)
        + img("apple-touch-icon.png", 72).replace("<img", '<img class="squircle"', 1)
        + img("icon-512.png", 72)
        + "</div>"
    )
    half = f'<div class="row">{small}</div><div class="row">{large}</div>{tab}{dock}'
    return (
        f"<!doctype html><meta charset=utf-8><style>{PREVIEW_STYLE}</style>"
        f'<section class="light">{half}</section><section class="dark">{half}</section>'
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="从 docs/assets/logo.svg 重画网页应用的图标")
    parser.add_argument("--preview", type=Path, help="另外存一张对照图 preview.png 到这个文件夹")
    options = parser.parse_args()
    body = logo_body()
    images: dict[str, bytes] = {}
    with tempfile.TemporaryDirectory(prefix="zhifox-icons-") as profile:
        painter = Painter(Path(profile))
        try:
            for icon in ICONS:
                images[icon.name] = png(painter.rasterize(drawing(icon, body), icon.size), icon.size)
            if options.preview:
                options.preview.mkdir(parents=True, exist_ok=True)
                shot = painter.screenshot(preview_page(images), *PREVIEW_SIZE)
                (options.preview / "preview.png").write_bytes(shot)
                print(f"对照图：{options.preview / 'preview.png'}")
        finally:
            painter.close()
    FOLDER.mkdir(exist_ok=True)
    for name, data in images.items():
        (FOLDER / name).write_bytes(data)
        print(f"{FOLDER.relative_to(ROOT) / name}  {len(data):>6} 字节")


if __name__ == "__main__":
    main()
