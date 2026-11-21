"""网页应用的清单（manifest）和图标：让书签、标签页、"创建快捷方式 / 安装成应用"和程序坞上显示智狐的小狐狸。

## 为什么要有

浏览器给一个网页做快捷方式、装成应用时，要一张够大的图标（至少 144 像素，最好 192 和 512），还要知道这个"应用"
叫什么、从哪个地址打开、用什么底色。这些写在一个小小的 JSON 文件里，就是 web app manifest，页面用
<link rel="manifest"> 指向它。以前页面只有一个内嵌的 SVG 小图标（data: 地址），Chrome 拿不到够大的图，
做出来的快捷方式就是一块灰底上一个"智"字。

## 怎么发，为什么不放宽安全的规矩

清单和图标是浏览器自己去取的，不经过页面的脚本，所以只能是服务器上的地址：

  GET /manifest.webmanifest?token=…   清单（这次运行的口令填在里面的每个地址上）
  GET /icons/<名字>?token=…           下面 ICONS 里列出的几张 PNG 之一

和页面一样要带口令、经过 Host 和 Origin 的检查（见 server.py 的 _allowed）。图标的名字只认 ICONS 里一字不差的那几个，
是查表，不是拼路径，所以请求里写什么都读不到别的文件。内容是程序自带的固定的几张图，不是你的数据。

清单里的地址都带着口令：浏览器以后从快捷方式打开智狐，用的就是 start_url。后台服务的口令重启前后不变（见 token.py），
所以快捷方式一直能用；在终端里开的 harness --web 每次换一个口令，旧的快捷方式会打不开，要重新做一个。
id 写死成 "/"，不带口令：浏览器按它认"是不是同一个应用"，换了口令也还是同一个。

页面的安全策略（CSP）原来是 default-src 'none' 加 img-src data:，清单和这几张图都取不到。放行的两项都是一个一个写死的
完整地址（见下面的 policy），不是 'self'，也不是整个 /icons/ 文件夹：
  - manifest-src 只有清单这一个地址。
  - img-src 除了 data: 只多了 ICONS 里那几张图的地址。本来以为浏览器自己取清单里的图标时不看页面的 CSP，实测 Chrome 157
    看（tests/ui/test_ui_install.py）：不放行，apple-touch-icon 和清单里的图标都被拦下，"找不到合适的图标"，装不成应用。
CSP 里带路径的来源只比路径（? 后面的口令不算），所以每张图一个来源就正好是那一张。
标签页上的小图标仍然是页面里内嵌的 SVG（data:）。模型输出里的图片照样不加载（markdown.js 从来不画 <img>），
就算哪里混进一个 <img>，能加载的也只是这几张自带的狐狸。

也试过另一种做法：清单里的图标直接写成 data: 地址，img-src 一项都不用加。Chrome 157 也认（装得成），但 apple-touch-icon
也得变成 data: 塞进每一次发出的页面里，Safari 认不认没法测；所以用的还是普通的地址，这是各家浏览器都认的写法。

## 图标是怎么来的

从 docs/assets/logo.svg（和页面里的 #i-fox 是同一幅画）用无界面的 Chrome 画一次，存进 icons/ 文件夹，提交进仓库。
改了标志就运行 `uv run python tests/make_icons.py` 重画。每张怎么画写在 ICONS 里：
  - 透明底的（purpose 是 any）：标签页、书签、Windows 和 Linux 的任务栏。狐狸自己有颜色，深浅两种底上都看得清。
  - 有底色的（maskable、apple-touch-icon）：系统会把它裁成自己的形状（macOS Sonoma 以后的程序坞、Android 是圆角方块或圆形，
    iPhone 的主屏幕是圆角方块），所以底色铺满整张，狐狸只画在中间那个半径 40% 的圆里（maskable 的"安全区"），怎么裁都裁不到它。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from ..config import APP_NAME

FOLDER = Path(__file__).parent / "icons"
MANIFEST_PATH = "/manifest.webmanifest"
ICON_PREFIX = "/icons/"

# 和 styles/tokens.css 里浅色外观的 --bg（窗口的底色）一样，tests/test_web_manifest.py 会对一遍。
# 清单只能写一种颜色，用默认的浅色；选了深色的话，页面打开以后由 appearance.js 改 <meta name="theme-color">
THEME_COLOR = "#eeece7"
# 有底色的那几张图标的底：狐狸脸上那种奶油色再淡一些的暖白，和默认的浅色外观、README 的封面图是一家人。
# 浅色和深色的程序坞上都试过（tests/make_icons.py --preview），像别的浅色图标一样清楚
TILE_COLOR = "#fff3e3"


@dataclass(frozen=True)
class Icon:
    name: str  # 文件名，也是地址里 /icons/ 后面的那一段
    size: int  # 边长（像素）
    purpose: str  # any：原样显示；maskable：系统会裁成自己的形状
    tile: bool  # 铺满底色（狐狸只占中间的安全区），还是透明底
    in_manifest: bool  # 写进清单（apple-touch-icon 只给 Safari 和 iPhone，页面头上单独引用）


ICONS = (
    Icon("icon-16.png", 16, "any", tile=False, in_manifest=True),
    Icon("icon-32.png", 32, "any", tile=False, in_manifest=True),
    Icon("apple-touch-icon.png", 180, "any", tile=True, in_manifest=False),
    Icon("icon-192.png", 192, "any", tile=False, in_manifest=True),
    Icon("icon-512.png", 512, "any", tile=False, in_manifest=True),
    Icon("icon-maskable-512.png", 512, "maskable", tile=True, in_manifest=True),
)
_BY_NAME = {icon.name: icon for icon in ICONS}
_CACHE: dict[str, bytes] = {}


def icon_bytes(name: str) -> bytes | None:
    """一张图标的内容。name 必须是 ICONS 里的一个文件名（查表，不拼路径）；不是的、文件不在了，返回 None。"""
    if name not in _BY_NAME:
        return None
    if name not in _CACHE:
        try:
            _CACHE[name] = (FOLDER / name).read_bytes()
        except OSError:
            return None
    return _CACHE[name]


def with_token(path: str, token: str) -> str:
    return f"{path}?token={quote(token, safe='')}"


def policy(origin: str) -> str:
    """页面的安全策略里图片和清单的两项。origin 是 http://127.0.0.1:端口 这样的（server.py 用查过的 Host 拼）。"""
    icons = " ".join(origin + ICON_PREFIX + icon.name for icon in ICONS)
    return f"img-src data: {icons}; manifest-src {origin}{MANIFEST_PATH}"


def manifest(token: str) -> dict:
    """发给浏览器的清单。地址都从网站的根开始写（/…），浏览器按清单自己的地址补全，用 127.0.0.1 还是 localhost 打开都对。"""
    return {
        "id": "/",
        "name": APP_NAME,
        "short_name": APP_NAME,
        "start_url": with_token("/", token),
        "scope": "/",
        "display": "standalone",
        "background_color": THEME_COLOR,
        "theme_color": THEME_COLOR,
        "icons": [
            {
                "src": with_token(ICON_PREFIX + icon.name, token),
                "sizes": f"{icon.size}x{icon.size}",
                "type": "image/png",
                "purpose": icon.purpose,
            }
            for icon in ICONS
            if icon.in_manifest
        ],
    }
