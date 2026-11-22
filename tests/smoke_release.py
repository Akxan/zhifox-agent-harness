"""发布前的试装：在一个只装了这个安装包（wheel 或源码包）的干净环境里，看智狐装得全不全、能不能启动。

    uv run --isolated --no-project --with dist/harness_assistant-0.2.0-py3-none-any.whl tests/smoke_release.py 0.2.0

.github/workflows/release.yml 打包以后在 Ubuntu、macOS、Windows 上各跑一次（wheel 和源码包都跑），哪一样不对就不发布。查两件事：
  - 打包时漏没漏东西：仓库里 harness/ 下的每一个文件（git 记着的那些：页面的各块、翻译表、图标、Agent 的示例……）
    装好的包里都要有。运行时读的文件只能放在 harness/ 里，因为 wheel 只带它（CLAUDE.md"两种运行方式"）。
  - 装好的程序能不能启动：`harness --version`、`--help`、`harness doctor --json` 都要正常回答，网页能拼出来。
不联网、不花钱；数据目录和 .env 换成临时目录，不碰这台电脑上真的那份。

小心别测到仓库里的那份：`python 脚本` 只把脚本所在的 tests/ 放进 sys.path，所以这里 import 到的是装好的包（开头会核对）；
另开的进程用 `-I` 并且在临时目录里运行，因为 `python -m` 会把当前目录放进 sys.path，在仓库根目录下就会用上仓库里的 harness/。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def fail(message: str) -> None:
    print(f"FAILED: {message}", file=sys.stderr)
    sys.exit(1)


def run(command: list[str], env: dict[str, str], cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, env=env, cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)


def main(expected: str) -> None:
    import harness
    from harness import config, version
    from harness.webui import manifest
    from harness.webui.page import assemble

    package = Path(harness.__file__).resolve().parent
    if package.is_relative_to(ROOT):
        fail(f"imported the repository's own harness/ ({package}), not the installed package")
    if config.FROM_SOURCE:
        fail("the installed package thinks it is running from a clone (config.FROM_SOURCE)")
    if version.version() != expected:
        fail(f"installed version is {version.version()!r}, expected {expected!r}")
    print(f"installed {expected} at {package}")

    # 1. 打包时没漏东西：git 记着的 harness/ 下的每个文件，装好的包里都有
    listed = subprocess.run(["git", "ls-files", "-z", "harness"], cwd=ROOT, capture_output=True, check=True).stdout
    files = [name for name in listed.decode("utf-8").split("\0") if name]
    missing = [name for name in files if not (package / Path(name).relative_to("harness")).is_file()]
    if not files or missing:
        fail(f"{len(missing)} of {len(files)} files under harness/ are missing from the package: {missing[:10]}")
    print(f"all {len(files)} files under harness/ are in the package")

    # 2. 网页拼得出来，图标都在
    page = assemble()
    if "__NONCE__" not in page or len(page) < 100_000:
        fail(f"the assembled web page looks wrong ({len(page)} characters)")
    absent = [icon.name for icon in manifest.ICONS if not manifest.icon_bytes(icon.name)]
    if absent:
        fail(f"icons missing: {absent}")
    print(f"web page assembles ({len(page) // 1024} KB), {len(manifest.ICONS)} icons")

    # 3. 装好的命令能启动（数据目录、.env 换成临时的）
    with tempfile.TemporaryDirectory() as scratch:
        env = {**os.environ, "HARNESS_DATA_DIR": str(Path(scratch) / "data"), "HARNESS_ENV_FILE": str(Path(scratch) / ".env")}
        command = shutil.which("harness", path=str(Path(sys.executable).parent))
        if not command:
            fail(f"the harness command isn't next to {sys.executable}")
        assert command is not None  # 给 mypy 看：fail 不会返回
        shown = run([command, "--version"], env, scratch)
        if shown.returncode != 0 or expected not in shown.stdout or "commit" in shown.stdout or "提交" in shown.stdout:
            fail(f"harness --version: exit {shown.returncode}, {shown.stdout!r} {shown.stderr!r}")
        print(f"harness --version → {shown.stdout.strip()}")
        helped = run([sys.executable, "-I", "-m", "harness", "--help"], env, scratch)
        if helped.returncode != 0 or "usage: harness" not in helped.stdout:
            fail(f"harness --help: exit {helped.returncode}, {helped.stderr!r}")
        doctor = run([sys.executable, "-I", "-m", "harness", "doctor", "--json"], env, scratch)
        try:
            report = json.loads(doctor.stdout)
        except ValueError:
            fail(f"harness doctor --json didn't print JSON: exit {doctor.returncode}, {doctor.stdout[:500]!r} {doctor.stderr[:500]!r}")
        if report.get("app", {}).get("version") != expected:
            fail(f"harness doctor reports version {report.get('app')!r}")
        print(f"harness doctor --json → {len(report.get('sections', []))} sections, exit {doctor.returncode} (problems are expected on a bare machine)")
    print("smoke test passed")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: smoke_release.py X.Y.Z", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])
