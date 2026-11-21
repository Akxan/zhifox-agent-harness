"""真的 Linux 后端（x11.py），在一块虚拟的屏幕上跑一遍：截图、换算坐标、移动鼠标、打字、按键。

只用这里自己开的 Xvfb（一块只在内存里的 X11 屏幕，谁也看不见），从不用你正在用的那块屏幕：
命令都带着 DISPLAY=:<这里开的那一块>（X11Backend 自己带，不改这个测试进程的环境）。
需要 Linux、Xvfb、xdotool 和 ImageMagick（GitHub Actions 的 ubuntu 上会装好，见 .github/workflows/tests.yml），
别的地方（比如 macOS）一律跳过。CI 里设了 ZHIFOX_REQUIRE_XVFB=1：缺了什么就算失败，不会悄悄地被跳过。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fake_desktop import install
from test_desktop_backends import jpeg_size

from harness.desktop import session as session_module
from harness.desktop.backend import Shot
from harness.desktop.frame import Box, Frame, Screen
from harness.desktop.manager import DesktopManager
from harness.desktop.x11 import X11Backend
from harness.settings import COMPUTER_DEFAULTS

WIDTH, HEIGHT = 1920, 1080


def _missing() -> list[str]:
    if not sys.platform.startswith("linux"):
        return [f"Linux（这里是 {sys.platform}）"]
    missing = [name for name in ("Xvfb", "xdotool") if shutil.which(name) is None]
    if shutil.which("import") is None and shutil.which("scrot") is None:
        missing.append("ImageMagick 的 import（或者 scrot）")
    if shutil.which("convert") is None and shutil.which("magick") is None:
        missing.append("ImageMagick 的 convert")
    return missing


def _free_display() -> int:
    for number in range(91, 140):
        if not Path(f"/tmp/.X{number}-lock").exists() and not Path(f"/tmp/.X11-unix/X{number}").exists():
            return number
    raise RuntimeError("找不到空着的 X 屏幕编号")


@pytest.fixture(scope="module")
def display():
    missing = _missing()
    if missing:
        reason = "在虚拟屏幕上测真的 Linux 后端需要：" + "、".join(missing)
        if os.environ.get("ZHIFOX_REQUIRE_XVFB"):
            pytest.fail(reason)
        pytest.skip(reason)
    number = _free_display()
    process = subprocess.Popen(
        ["Xvfb", f":{number}", "-screen", "0", f"{WIDTH}x{HEIGHT}x24", "-nolisten", "tcp"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    name = f":{number}"
    deadline = time.monotonic() + 15
    while True:  # 等它起来：xdotool 能问出屏幕多大，就是好了
        ready = subprocess.run(["xdotool", "getdisplaygeometry"], env={**os.environ, "DISPLAY": name}, capture_output=True, check=False)
        if ready.returncode == 0:
            break
        if process.poll() is not None or time.monotonic() > deadline:
            process.kill()
            pytest.fail(f"Xvfb 没有起来（退出码 {process.returncode}）")
        time.sleep(0.1)
    yield name
    process.terminate()
    try:
        process.wait(5)
    except subprocess.TimeoutExpired:
        process.kill()


@pytest.fixture
def backend(display) -> X11Backend:
    return X11Backend(display)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(session_module, "DESKTOP_SETTLE_SECONDS", 0.05)


def test_the_virtual_screen_size(backend):
    assert backend.screen() == Screen(WIDTH, HEIGHT, WIDTH, HEIGHT)
    assert backend.problem("look") == "" and backend.problem("input") == ""
    assert backend.checks() == {"xdotool": True, "capture": True, "convert": True}


def test_captures_are_jpegs_of_the_asked_sizes(backend):
    full, thumb = backend.capture([Shot(1429, 804, 70), Shot(320, 180, 50)])
    assert jpeg_size(full) == (1429, 804) and jpeg_size(thumb) == (320, 180)
    zoomed = backend.capture([Shot(400, 200, 70)], region=Box(10, 10, 200, 100))[0]
    assert jpeg_size(zoomed) == (400, 200)


def test_the_pointer_moves_where_it_is_told(backend):
    backend.mouse("move", 300, 200)
    assert backend.cursor() == (300.0, 200.0)
    backend.mouse("down", 310, 210)
    backend.mouse("drag", 400, 260)
    backend.mouse("up", 400, 260)
    assert backend.cursor() == (400.0, 260.0)


def test_a_session_maps_screenshot_coordinates_onto_the_real_screen(backend, tmp_path):
    manager = DesktopManager(tmp_path, SimpleNamespace(computer={**COMPUTER_DEFAULTS, "enabled": True}))
    install(manager, backend)
    session = manager.session("你的对话")
    shot = session.run("screenshot")
    assert jpeg_size(shot.image) == (1429, 804) and shot.thumb.startswith("data:image/jpeg;base64,")
    assert manager.shots.read(shot.shot) == shot.image
    frame = Frame.of(backend.screen())
    session.run("click", point=(714, 402), button="left", count=1, modifiers=())
    assert backend.cursor() == tuple(float(value) for value in frame.to_screen(714, 402)) == (959.0, 540.0)
    session.run("click", point=(100, 100), button="right", count=1, modifiers=("shift",))
    session.run("scroll", point=None, direction="down", amount=2, modifiers=())
    session.run("move", point=(1428, 803))
    assert backend.cursor() == (1919.0, 1079.0)  # 截图右下角的那个像素，就是屏幕右下角的那一点
    assert session.run("cursor").text == "X=1428, Y=803"
    session.run("drag", start=(10, 10), end=(200, 150), modifiers=())
    session.run("type", text="hello 智狐")
    session.run("key", text="ctrl+a BackSpace", repeat=1)
    session.run("hold", text="shift", duration=0.1)
    zoom = session.run("zoom", region=[0, 0, 400, 300])
    assert jpeg_size(zoom.image)[0] <= 1429
    session.close()
