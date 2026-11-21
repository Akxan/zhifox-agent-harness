"""临时：CI 的 Windows 上看真的 Chrome 在哪种情况下回答得慢（调完删掉）。"""

import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import winjob  # noqa: E402
from harness.browser import chrome  # noqa: E402
from harness.browser.manager import BrowserManager  # noqa: E402


def attempt(name, env=None, job=True, manager=False):
    saved = dict(os.environ)
    real_job = winjob.Job
    try:
        if env:
            os.environ.update(env)
        if not job:
            class NoJob:
                handle = None

                def __init__(self, *a, **k):
                    pass

                def add(self, p):
                    return False

                def terminate(self):
                    pass

                def close(self):
                    pass
            winjob.Job = NoJob
        folder = Path(tempfile.mkdtemp(prefix="dbg-"))
        start = time.monotonic()
        if manager:
            m = BrowserManager(folder, SimpleNamespace(browser={"enabled": True, "headless": True, "path": ""}))
            try:
                m.connect()
                print(name, "OK", round(time.monotonic() - start, 1), "s", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(name, "FAIL", round(time.monotonic() - start, 1), "s", exc, flush=True)
            m.close()
            return
        exe = chrome.find_chrome()
        c = chrome.launch(exe, folder / "profile", True)
        try:
            c.connection.call("Browser.getVersion", timeout=40)
            print(name, "OK", round(time.monotonic() - start, 1), "s", "alive", c.alive(), flush=True)
        except Exception as exc:  # noqa: BLE001
            print(name, "FAIL", round(time.monotonic() - start, 1), "s", exc, "alive", c.alive(), flush=True)
        c.close()
    finally:
        winjob.Job = real_job
        os.environ.clear()
        os.environ.update(saved)


print("chrome:", chrome.find_chrome(), "threads", threading.active_count(), flush=True)
home = Path(tempfile.mkdtemp(prefix="home-"))
attempt("plain")
attempt("nojob", job=False)
attempt("userprofile-missing", env={"USERPROFILE": str(home / "nope")})
attempt("userprofile-exists", env={"USERPROFILE": str(home)})
attempt("noproxy", env={"NO_PROXY": "*", "no_proxy": "*"})
attempt("manager")
attempt("manager-up-missing", env={"USERPROFILE": str(home / "nope2")}, manager=True)
attempt("manager-plain", manager=True)
