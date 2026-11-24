"""三套坐标的换算：模型看到的截图、屏幕上的点、截图原图里的像素。

  图上的像素    交给模型的那张截图（缩小过）。模型说的坐标、zoom 的区域、cursor_position 报告的位置，全按它算，
               zoom 之后也一样（文档规定的：放大的图不改变坐标系）
  屏幕的坐标    发鼠标事件用的。macOS 上是"点"（point），Linux（X11）上就是像素
  屏幕的像素    截图原图里真正的像素。Retina 屏幕上一个点是 2×2 个像素（density = 2）

截图缩到多大（fit）按屏幕的坐标算，不按原图的像素算：Retina 屏幕的原图大一倍，缩小以后看到的内容是一样的，
按点算还能让小屏幕（1280×800 点）原样交出去、坐标一比一。

为什么 x、y 各算各的比例：缩小的时候宽和高都要取整（1330.65 → 1330），两边的比例差一点点，
合在一起算的话，点到屏幕右下角时会差出一两个点。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import DESKTOP_MAX_EDGE, DESKTOP_MAX_PIXELS
from ..i18n import zh
from ..tools.base import ToolError

MIN_ZOOM = 4  # zoom 的区域最小这么多个像素（图上的）
MAX_ZOOM_SCALE = 4.0  # zoom 最多把原图放大这么多倍：再大只是把像素拉成方块，看不出更多东西


@dataclass(frozen=True)
class Screen:
    """屏幕的大小。width、height 是发鼠标事件用的坐标（点）；pixel_width、pixel_height 是截图原图的像素。"""

    width: int
    height: int
    pixel_width: int
    pixel_height: int

    @property
    def density(self) -> float:
        """一个点是几个像素（Retina 是 2，普通的屏幕是 1）。"""
        return self.pixel_width / self.width if self.width else 1.0


@dataclass(frozen=True)
class Box:
    """屏幕上的一块（屏幕的坐标）。"""

    x: int
    y: int
    width: int
    height: int


def fit(width: int, height: int, max_edge: int = DESKTOP_MAX_EDGE, max_pixels: int = DESKTOP_MAX_PIXELS) -> tuple[int, int]:
    """一张 width×height 的图缩到多大：长边不超过 max_edge、总像素不超过 max_pixels，不放大，宽高比不变。

    和文档"Size screenshots to fit image limits"里的 get_scale_factor 是同一个算法。
    """
    if width <= 0 or height <= 0:
        raise ValueError("屏幕的大小不对")
    scale = min(1.0, max_edge / max(width, height), math.sqrt(max_pixels / (width * height)))
    return max(1, math.floor(width * scale)), max(1, math.floor(height * scale))


@dataclass(frozen=True)
class Frame:
    """一张交给模型的截图和它背后的屏幕：模型说的坐标靠它换算。"""

    screen: Screen
    width: int  # 图的大小
    height: int

    @classmethod
    def of(cls, screen: Screen) -> Frame:
        width, height = fit(screen.width, screen.height)
        return cls(screen, width, height)

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    def to_screen(self, x: int, y: int) -> tuple[int, int]:
        """图上的一点 → 屏幕的坐标。不在图上的抛 ToolError（交给模型，文档建议这样回答越界的坐标）。"""
        if not (0 <= x < self.width and 0 <= y < self.height):
            # 交给模型的话用 zh()：工具卡片上按界面的语言显示（见 ../i18n.py 开头）
            raise ToolError(zh("坐标 ({x}, {y}) 不在截图里：截图是 {width}×{height}，左上角是 (0, 0)。", x=x, y=y, width=self.width, height=self.height))
        screen = self.screen
        return (
            min(screen.width - 1, round(x * screen.width / self.width)),
            min(screen.height - 1, round(y * screen.height / self.height)),
        )

    def to_image(self, x: float, y: float) -> tuple[int, int]:
        """屏幕的坐标 → 图上的一点（cursor_position 报告给模型的位置）。屏幕外面的按最近的边算。"""
        screen = self.screen
        image_x = round(x * self.width / screen.width) if screen.width else 0
        image_y = round(y * self.height / screen.height) if screen.height else 0
        return max(0, min(self.width - 1, image_x)), max(0, min(self.height - 1, image_y))

    def region(self, region: object) -> Box:
        """zoom 的区域 [x0, y0, x1, y1]（图上的像素）→ 屏幕上的一块。超出图的部分去掉。"""
        if not (isinstance(region, list) and len(region) == 4 and all(_number(value) for value in region)):
            raise ToolError(zh("region 要写成 [x0, y0, x1, y1] 四个数：左上角和右下角，按截图上的像素算。"))
        x0, y0, x1, y1 = (float(value) for value in region)
        x0, y0 = max(0.0, x0), max(0.0, y0)
        x1, y1 = min(float(self.width), x1), min(float(self.height), y1)
        if x1 - x0 < MIN_ZOOM or y1 - y0 < MIN_ZOOM:
            raise ToolError(zh("region 太小了，或者不在截图里（截图是 {width}×{height}）。右下角要在左上角的右下方。", width=self.width, height=self.height))
        screen = self.screen
        left, top = math.floor(x0 * screen.width / self.width), math.floor(y0 * screen.height / self.height)
        right, bottom = math.ceil(x1 * screen.width / self.width), math.ceil(y1 * screen.height / self.height)
        return Box(left, top, max(1, min(screen.width, right) - left), max(1, min(screen.height, bottom) - top))

    def zoom_size(self, box: Box) -> tuple[int, int]:
        """放大一块时交出去的图多大：原图里那一块的像素，放大或缩小到正好装进平时截图的大小（宽高比不变），最多放大 MAX_ZOOM_SCALE 倍。"""
        density = self.screen.density
        width, height = box.width * density, box.height * density
        scale = min(self.width / width, self.height / height, MAX_ZOOM_SCALE)
        return max(1, math.floor(width * scale)), max(1, math.floor(height * scale))


def thumb_size(width: int, height: int, thumb_width: int) -> tuple[int, int]:
    """小预览图的大小：宽 thumb_width（比原图还宽的话就用原图的宽），高按比例。"""
    if width <= thumb_width:
        return width, height
    return thumb_width, max(1, round(height * thumb_width / width))


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
