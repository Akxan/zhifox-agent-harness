"""启动快不快：用不到模型的小命令不导入两家模型的 SDK（anthropic、openai）。

光是导入这两个 SDK 就要大半秒。harness --help、harness service status、harness hooks list 这些命令一个请求都不发，
却曾经要等它们导入完：tools/memory.py 在文件开头导入 anthropic.tools，providers/__init__.py 在文件开头导入两个 SDK，
而这两个包会被很多地方顺路带进来（比如 pricing.py 要 providers/base.py 里的 Usage）。现在 SDK 都在真正要连接模型的
函数里才导入（见那几个文件里"用到时才导入"的注释）。

为什么要在子进程里测：测试进程自己早就导入过 SDK 了（conftest 要用它连假服务器），sys.modules 里查不出什么。
子进程是一个干净的 Python，跑一遍命令的入口（cli.main），再看 sys.modules 里有没有这两个名字。
哪天有人在这条路上又在文件开头导入了 SDK，这里会失败，并且写出是哪一个命令。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SDKS = ["anthropic", "openai"]
# 每段子进程脚本的最后一行：导入了哪几个 SDK（前面空一行，和命令自己的输出分开）
LOADED = '[name for name in ("anthropic", "openai") if name in sys.modules]'

# 子进程里跑的：先把会碰到这台电脑的两样换成假的——沙箱只说"用不了"（不运行 sandbox-exec、bwrap），
# 后台服务只说"这个系统不支持"（不运行 launchctl、systemctl、schtasks），和 conftest 里的规矩一样；
# 然后跑命令的入口，最后一行写出导入了哪几个 SDK
RUN = """
import json, sys
from harness import cli
from harness.sandbox import backend as sandbox_backend
from harness.service import cli as service_cli

sandbox_backend.detect = lambda: sandbox_backend.Backend("fake", "假沙箱", "", lambda: "测试里不建沙箱", lambda policy, argv: list(argv), ())
service_cli.detect = lambda: service_cli.System(None, "测试里不装后台服务")
try:
    code = cli.main(sys.argv[1:])
except SystemExit as exc:
    code = exc.code
print("\\n" + json.dumps({"code": code, "loaded": LOADED}))
""".replace("LOADED", LOADED)

# 只是导入：上面那些命令在别的动作里、函数里才导入的模块（装服务时的三种服务管家、钩子和沙箱试运行要的
# app.sandbox_layout、钩子的名字要的 capabilities、读设置、查有没有密钥的登记表）
IMPORT = """
import importlib, json, sys
for name in sys.argv[1:]:
    importlib.import_module(name)
print("\\n" + json.dumps({"loaded": LOADED}))
""".replace("LOADED", LOADED)

REACHED = [
    "harness.cli",
    "harness.app",
    "harness.capabilities",
    "harness.providers",
    "harness.settings",
    "harness.tools.shell",
    "harness.service.launchd",
    "harness.service.systemd",
    "harness.service.windows",
    "harness.service.supervise",
    "harness.service.plan",
    "harness.service.logs",
    "harness.hooks.engine",
    "harness.hooks.runner",
    "harness.sandbox.session",
    "harness.channels.hub",
    "harness.evals.cli",
]

# 用不到模型的命令。名字是固定的（并行跑测试时每个进程收集到的要一样）
COMMANDS = {
    "help": ["--help"],
    "service-status": ["service", "status"],
    "service-logs-help": ["service", "logs", "--help"],
    "hooks-list": ["hooks", "list"],
    "sandbox-status": ["sandbox", "status"],
    "channels-status": ["channels", "status"],
    "commands-list": ["commands", "list"],
    "eval-tasks": ["eval", "tasks"],
}


def _python(tmp_path: Path, script: str, args: list[str]) -> tuple[dict, str]:
    """在一个干净的 Python 里跑 script：数据目录、密钥文件、家目录都是临时的，读不到这台电脑上真正的东西。"""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {name: value for name, value in os.environ.items() if not name.startswith("HARNESS_") and not name.endswith("_API_KEY") and name != "PYTHONPATH"}
    env.update({
        "HARNESS_DATA_DIR": str(tmp_path / "data"),
        "HARNESS_ENV_FILE": str(tmp_path / "no-such.env"),
        "HOME": str(home),
        "PYTHONPATH": str(ROOT),
        "PYTHONIOENCODING": "utf-8",
    })
    if os.name == "nt":
        env["USERPROFILE"] = str(home)  # Windows 上 Path.home() 看的是它
    done = subprocess.run(
        [sys.executable, "-P", "-c", script, *args], cwd=tmp_path, env=env, capture_output=True,
        text=True, encoding="utf-8", timeout=120, check=False,
    )
    output = done.stdout + done.stderr
    lines = done.stdout.strip().splitlines()
    assert lines, output
    return json.loads(lines[-1]), output


@pytest.mark.parametrize("argv", list(COMMANDS.values()), ids=list(COMMANDS))
def test_commands_that_need_no_model_do_not_import_the_sdks(tmp_path, argv):
    found, output = _python(tmp_path, RUN, argv)
    assert found["code"] in (0, 1, 2, 3, None), output  # 命令照常跑完（没装服务时 status 返回 3）
    assert found["loaded"] == [], f"harness {' '.join(argv)} 导入了 {found['loaded']}：\n{output}"


def test_the_modules_those_commands_reach_do_not_import_the_sdks(tmp_path):
    found, output = _python(tmp_path, IMPORT, REACHED)
    assert found["loaded"] == [], output


def test_the_sdks_are_still_imported_when_a_model_is_really_used(tmp_path):
    """反过来也要对：真的要连模型时（建客户端、建记忆工具），SDK 照样导入，不会因为挪了位置就用不了。"""
    script = """
import json, sys
from pathlib import Path
from harness.providers import PROVIDERS, make_client
from harness.tools.memory import make_memory_tool
make_client(PROVIDERS["anthropic"])  # 只是建客户端，不发请求
make_client(PROVIDERS["openai"])
tool = make_memory_tool(Path(sys.argv[1]))
print("\\n" + json.dumps({"loaded": LOADED, "memory": tool.spec.get("type")}))
""".replace("LOADED", LOADED)
    found, output = _python(tmp_path, script, [str(tmp_path / "data")])
    assert found["loaded"] == SDKS, output
    assert found["memory"] == "memory_20250818", output
