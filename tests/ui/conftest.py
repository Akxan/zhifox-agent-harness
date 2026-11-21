"""页面测试（tests/ui/）共用的东西：演示服务器、测试用的 Chrome、每个测试一个新标签页。

    uv run pytest -m ui            # 只跑页面测试
    uv run pytest -m "not ui"      # 跳过页面测试

整个测试会话只启动一次演示服务器（tests/demo_web.py，假模型、临时的数据目录、编造的密钥，不花钱）和一个
无界面的 Chrome。每个测试开始前，先把服务器上会被测试改动的设置恢复原样（语言、确认的档位、模型、当前对话），
再开一个新标签页（在自己的浏览器上下文里，像一个隐身窗口：外观这些存在浏览器里的设置都是空的），
所以测试之间互不影响，单独跑哪一个都行。

服务器是一个子进程：和你手动运行 demo_web.py 时完全一样，测的就是你手动看的那个页面。它的回复按
tests/demo_scripts.py 里的剧本走（第一条消息写着 [ui:名字]），和测试的先后顺序无关。

这台电脑上没有 Chrome 时整个跳过。CI 里设了 HARNESS_UI_REQUIRED=1：找不到 Chrome 就算失败，
免得页面测试悄悄地一个都没跑。

测试失败（或者页面里出了错）时，截图、控制台输出、页面的 HTML 存进 HARNESS_UI_ARTIFACTS 指定的文件夹
（不设的话在 pytest 的临时目录里，路径会打印出来），演示服务器的输出也存一份。CI 把这个文件夹传上去。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from harness.browser import chrome
from harness.tools.shell import SECRET_ENV_VARS

from .pilot import Browser, Page, until

HERE = Path(__file__).parent
DEMO = HERE.parent / "demo_web.py"
URL = re.compile(r"http://127\.0\.0\.1:(\d+)/\?token=([\w-]+)")


def pytest_collection_modifyitems(config, items):
    # 这个文件夹里的测试都打上 ui 标记：uv run pytest -m ui 只跑它们，-m "not ui" 跳过它们
    for item in items:
        if HERE in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.ui)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    # 把每个阶段的结果挂在测试上，page 收尾时才知道这个测试是不是失败了（失败了要存截图）
    outcome = yield
    report = outcome.get_result()
    setattr(item, "report_" + report.when, report)


class Demo:
    """作为子进程运行的演示服务器。"""

    def __init__(self, root: Path):
        env = {key: value for key, value in os.environ.items() if key not in SECRET_ENV_VARS and "proxy" not in key.lower()}
        env.update(NO_PROXY="*", no_proxy="*", PYTHONUNBUFFERED="1")
        command = [
            sys.executable, str(DEMO), "--port", "0", "--data", str(root),
            "--delay", "0.004",  # 逐字输出还是一小段一小段的（测试要看到它），只是快一些
            "--think", "0.05",
        ]
        self.process = subprocess.Popen(
            command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", start_new_session=True,
        )
        self.output: list[str] = []
        self._found = threading.Event()
        self.url = self.origin = self.token = ""
        threading.Thread(target=self._read, daemon=True).start()
        if not self._found.wait(90):
            self.stop()
            raise RuntimeError("演示服务器没能启动：\n" + "".join(self.output[-40:]))

    def _read(self) -> None:
        # 一直读它的输出：不读的话管道写满了，服务器会卡在 print 上
        for line in self.process.stdout:
            self.output.append(line)
            match = URL.search(line)
            if match and not self._found.is_set():
                self.url = match.group(0)
                self.origin = f"http://127.0.0.1:{match.group(1)}"
                self.token = match.group(2)
                self._found.set()

    def api(self, path: str, data: dict | None = None) -> tuple[int, dict]:
        """像页面那样调用一个接口（带着口令）。返回（状态码，回答）。"""
        request = urllib.request.Request(
            self.origin + path, data=json.dumps(data or {}).encode(), method="POST",
            headers={"Content-Type": "application/json", "X-Harness-Token": self.token},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def reset(self) -> None:
        """把测试会改动的服务器设置恢复原样，换到一段新对话。"""
        self.api("/api/stop")
        for path, data in (
            ("/api/language", {"language": "zh"}),
            ("/api/approval", {"mode": "ask"}),
            ("/api/model", {"provider": "anthropic", "model": "claude-opus-5-5"}),
        ):
            status, reply = self.api(path, data)
            assert status == 200, f"{path} 失败：{reply}"
        # 刚点了停止，这一轮可能还在收尾，"新对话"要等它结束才行
        until(lambda: self.api("/api/new")[0] == 200, timeout=15, what="开始新对话")

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)  # 和按 Ctrl-C 一样：演示自己收尾（关掉假服务器、它开的 Chrome）
            try:
                self.process.wait(15)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(5)


@pytest.fixture(scope="session")
def artifacts(tmp_path_factory) -> Path:
    folder = Path(os.environ.get("HARNESS_UI_ARTIFACTS") or tmp_path_factory.getbasetemp() / "ui-artifacts")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@pytest.fixture(scope="session")
def browser(tmp_path_factory):
    executable = chrome.find_chrome()
    if executable is None:
        if os.environ.get("HARNESS_UI_REQUIRED"):
            pytest.fail("HARNESS_UI_REQUIRED 设了，但是这台电脑上找不到 Chrome（可以用 HARNESS_CHROME 指定它的路径）")
        pytest.skip("这台电脑上没有 Chrome，跳过页面测试")
    started = Browser(executable, tmp_path_factory.mktemp("ui-chrome"))
    yield started
    started.close()


@pytest.fixture(scope="session")
def demo(tmp_path_factory, browser, artifacts):
    # 依赖 browser：没有 Chrome、整个跳过的时候，也就不用启动服务器了
    server = Demo(tmp_path_factory.mktemp("ui-demo"))
    yield server
    server.stop()
    (artifacts / "demo-server.log").write_text("".join(server.output), encoding="utf-8")


def _safe(name: str) -> str:
    return re.sub(r"[^\w.-]+", "_", name)[:120]


@pytest.fixture
def page(request, demo, browser, artifacts) -> Page:
    """一个新标签页，打开演示页面，等它亮相。测试结束时页面里不能有任何错误。"""
    demo.reset()
    opened = browser.new_page()
    opened.demo = demo  # 测试里要直接调接口时用：page.demo.api(...)
    try:
        opened.goto(demo.url)
        opened.wait("document.body.classList.contains('ready')", what="页面亮相")
        yield opened
    finally:
        report = getattr(request.node, "report_call", None)
        failed = report is None or report.failed
        errors = opened.unexpected_errors()
        if failed or errors:
            stem = artifacts / _safe(request.node.name)
            with contextlib.suppress(Exception):  # 页面都坏了的时候，能存多少存多少
                opened.screenshot(stem.with_suffix(".png"))
                stem.with_suffix(".html").write_text(opened.html(), encoding="utf-8")
            stem.with_suffix(".console.txt").write_text("\n".join(opened.console), encoding="utf-8")
            print(f"\n页面测试的截图和日志：{stem}.*")
        opened.close()
    if errors:
        pytest.fail("页面里出了错（脚本异常、console.error 或者被安全策略拦下的东西）：\n" + "\n".join(errors))
