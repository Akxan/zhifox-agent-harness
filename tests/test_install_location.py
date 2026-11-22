"""两种运行方式下数据和密钥放在哪儿（见 harness/install.py）：从仓库里运行（git clone + uv sync），还是装成了工具（uv tool install）。

  - 怎么分：包的上一层有没有我们自己的 pyproject.toml、是不是叫 site-packages；
  - 装成工具时三个系统各自的默认位置（macOS 的 Application Support、Linux 的 XDG_DATA_HOME、Windows 的 LOCALAPPDATA）；
  - 从仓库里运行时一个字都不变：data/ 和 .env 还在仓库里；
  - HARNESS_DATA_DIR、HARNESS_ENV_FILE 永远优先；
  - .env 放进数据目录以后照样碰不到：文件工具按路径拒绝、导出时跳过、沙箱里不让碰；
  - 新建的数据目录、.env 所在的文件夹只有自己的账号能进；
  - 装成工具时说给你听的命令是 harness（不是 uv run harness），评测说清楚为什么没有任务。

"装成了工具"是用 monkeypatch 模拟的（换掉 config.FROM_SOURCE、machine.system、环境变量），不真的装：
真的装一次（uv build + uv tool install 到临时目录）是手动做的，见交回的报告。
load_config 读 .env 的那一步（load_env_file）在这里一律换成只记下路径、不读文件：没设 HARNESS_ENV_FILE 时，
从仓库里运行的默认位置就是仓库里真正的 .env，测试不能读到它（CLAUDE.md："不要读出 .env 的内容"）。
"""

from __future__ import annotations

import argparse
import ast
import io
import os
import tomllib
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from modes import has_mode

from harness import config as config_module
from harness import export, install, instance, machine
from harness.channels import platforms
from harness.config import PROJECT_ROOT, Config, load_config
from harness.evals import cli as eval_cli
from harness.sandbox import policy
from harness.service import plan as plan_module
from harness.settings import save_api_key
from harness.tools.base import ToolError
from harness.tools.files import FileEditor

KEY = "sk-ant-api03-" + "x" * 40  # 编造的密钥：只用来看它有没有被带出去


def pyproject(folder: Path, name: str = install.PROJECT_NAME) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "0.1.0"\n', encoding="utf-8")
    return folder


@pytest.fixture
def env_reads(monkeypatch):
    """load_config 想读的 .env：只记下路径，不真的读（见开头的说明）。"""
    seen: list[Path] = []
    monkeypatch.setattr(config_module, "load_env_file", lambda path: seen.append(path))
    return seen


@pytest.fixture
def tool_mode(monkeypatch, tmp_path):
    """假装是装成了工具：config 认为不是从仓库里运行，两个环境变量都没设，三个系统的"用户文件夹"都指到临时目录里。
    返回一个函数：给它一个系统（mac / windows / linux），这台"电脑"就是那种系统。"""
    monkeypatch.setattr(config_module, "FROM_SOURCE", False)
    monkeypatch.delenv("HARNESS_DATA_DIR", raising=False)
    monkeypatch.delenv("HARNESS_ENV_FILE", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)  # 不看这台电脑真正的 %LOCALAPPDATA%
    home = tmp_path / "home"

    def use(system: str) -> Path:
        monkeypatch.setattr(machine, "system", lambda: system)
        return home

    return use


# ---- 怎么分 ----------------------------------------------------------------------


def test_this_checkout_runs_from_source():
    # 测试就是在仓库里跑的：照旧是 uv run harness，数据和密钥照旧在仓库里
    assert install.from_source(PROJECT_ROOT) is True
    assert config_module.FROM_SOURCE is True
    assert config_module.COMMAND == "uv run harness" and config_module.REPAIR == "uv sync"


def test_our_own_pyproject_means_a_checkout(tmp_path):
    assert install.from_source(pyproject(tmp_path / "zhifox-agent-harness")) is True


@pytest.mark.parametrize("name", ["site-packages", "dist-packages", "Site-Packages"])
def test_a_package_inside_site_packages_is_installed_even_with_a_pyproject(tmp_path, name):
    # 装好的包总在 site-packages（Debian 的系统 Python 是 dist-packages；Windows 上不分大小写）里：
    # 哪怕那里不知怎么有一份写着我们名字的 pyproject.toml，也不能把数据写进安装目录
    assert install.from_source(pyproject(tmp_path / "lib" / "python3.11" / name)) is False


def test_no_pyproject_or_someone_elses_or_a_broken_one_means_installed(tmp_path):
    assert install.from_source(tmp_path / "nothing-here") is False
    assert install.from_source(pyproject(tmp_path / "their-project", name="their-app")) is False  # 有人把 harness/ 抄进了自己的项目
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "pyproject.toml").write_text("[project\nname = ", encoding="utf-8")
    assert install.from_source(broken) is False
    (broken / "pyproject.toml").write_bytes(b"\xff\xfe\x00not utf-8")
    assert install.from_source(broken) is False


# ---- 装成工具时的默认位置 ------------------------------------------------------------


def test_macos_uses_application_support(tmp_path):
    home = tmp_path / "home"
    assert install.user_dir(machine.MAC, {}, home) == home / "Library" / "Application Support" / "ZhiFox"
    # XDG 的变量在 macOS 上不算数（有人为了别的工具设了它）
    assert install.user_dir(machine.MAC, {"XDG_DATA_HOME": str(tmp_path / "xdg")}, home) == home / "Library" / "Application Support" / "ZhiFox"


def test_linux_follows_xdg_data_home(tmp_path):
    home = tmp_path / "home"
    assert install.user_dir(machine.LINUX, {}, home) == home / ".local" / "share" / "zhifox"
    assert install.user_dir(machine.LINUX, {"XDG_DATA_HOME": "/srv/me/data"}, home) == Path("/srv/me/data") / "zhifox"
    # XDG 规范：空的、不是绝对路径的，都当作没设
    assert install.user_dir(machine.LINUX, {"XDG_DATA_HOME": ""}, home) == home / ".local" / "share" / "zhifox"
    assert install.user_dir(machine.LINUX, {"XDG_DATA_HOME": "relative/data"}, home) == home / ".local" / "share" / "zhifox"


def test_windows_uses_local_not_roaming_appdata(tmp_path):
    home = tmp_path / "home"
    local, roaming = r"C:\Users\me\AppData\Local", r"C:\Users\me\AppData\Roaming"
    # 只看 LOCALAPPDATA：漫游的 APPDATA 会在登录、注销时整个同步，数据太大，里面也有只属于这台电脑的东西（见 install.py）
    assert install.user_dir(machine.WINDOWS, {"LOCALAPPDATA": local, "APPDATA": roaming}, home) == Path(local) / "ZhiFox"
    assert install.user_dir(machine.WINDOWS, {"APPDATA": roaming}, home) == home / "AppData" / "Local" / "ZhiFox"
    assert install.user_dir(machine.WINDOWS, {"LOCALAPPDATA": r"AppData\Local"}, home) == home / "AppData" / "Local" / "ZhiFox"


def test_the_folder_name_does_not_follow_the_display_name(monkeypatch, tmp_path):
    # 给人看的英文名改了，存数据的文件夹不能跟着搬家（不然用户的会话、记忆就"不见了"）
    monkeypatch.setattr(config_module, "APP_NAME_LATIN", "SomethingElse")
    assert install.user_dir(machine.MAC, {}, tmp_path).name == "ZhiFox"


@pytest.mark.parametrize("system", [machine.MAC, machine.LINUX, machine.WINDOWS])
def test_installed_data_and_env_go_to_the_users_folder(tool_mode, env_reads, system):
    home = tool_mode(system)
    expected = {
        machine.MAC: home / "Library" / "Application Support" / "ZhiFox",
        machine.LINUX: home / ".local" / "share" / "zhifox",
        machine.WINDOWS: home / "AppData" / "Local" / "ZhiFox",
    }[system]
    config = load_config()
    assert config.data_dir == expected.resolve()
    assert config.env_file == (expected / ".env").resolve()  # .env 和数据放在一起（理由见 install.py）
    assert env_reads == [config.env_file]
    # 不在 site-packages 里：升级、卸载时不会被删掉
    assert "site-packages" not in config.data_dir.parts
    # load_config 只算路径，不建文件夹
    assert not expected.exists()


def test_linux_honours_xdg_data_home_through_load_config(tool_mode, env_reads, monkeypatch):
    tool_mode(machine.LINUX)
    # 写成 Linux 的绝对路径（在 Windows 上跑这个测试时也是：XDG 只按 Linux 的规矩认）。load_config 只算路径，不会去建它
    monkeypatch.setenv("XDG_DATA_HOME", "/srv/xdg-data")
    config = load_config()
    assert config.data_dir == Path("/srv/xdg-data/zhifox").resolve()
    assert config.env_file == Path("/srv/xdg-data/zhifox/.env").resolve()


# ---- 环境变量优先；从仓库里运行时一字不变 ----------------------------------------------


@pytest.mark.parametrize("source", [True, False], ids=["from-source", "installed"])
def test_environment_variables_win_in_both_modes(monkeypatch, env_reads, tmp_path, source):
    monkeypatch.setattr(config_module, "FROM_SOURCE", source)
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "my-data"))
    monkeypatch.setenv("HARNESS_ENV_FILE", str(tmp_path / "secrets" / "zhifox.env"))
    config = load_config()
    assert config.data_dir == (tmp_path / "my-data").resolve()
    assert config.env_file == (tmp_path / "secrets" / "zhifox.env").resolve()


@pytest.mark.parametrize("source", [True, False], ids=["from-source", "installed"])
def test_only_the_data_dir_variable_leaves_env_at_its_default(monkeypatch, env_reads, tmp_path, source):
    # 两个变量各管各的：只换数据目录时，.env 还在默认的位置（从仓库里运行时一直是这样）
    monkeypatch.setattr(config_module, "FROM_SOURCE", source)
    monkeypatch.setattr(machine, "system", lambda: machine.LINUX)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("HARNESS_ENV_FILE", raising=False)
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "my-data"))
    config = load_config()
    assert config.data_dir == (tmp_path / "my-data").resolve()
    default = PROJECT_ROOT / ".env" if source else tmp_path / "home" / ".local" / "share" / "zhifox" / ".env"
    assert config.env_file == default.resolve()


def test_from_source_the_data_and_env_stay_in_the_project(monkeypatch, env_reads):
    # 用户现在的 data/ 和 .env 不能挪：没设环境变量时，还是仓库里的那两样
    monkeypatch.setattr(config_module, "FROM_SOURCE", True)
    monkeypatch.delenv("HARNESS_DATA_DIR", raising=False)
    monkeypatch.delenv("HARNESS_ENV_FILE", raising=False)
    config = load_config()
    assert config.data_dir == (PROJECT_ROOT / "data").resolve()
    assert config.env_file == (PROJECT_ROOT / ".env").resolve()
    assert env_reads == [config.env_file]  # 只记下了路径，没有读它
    assert install.default_data_dir(PROJECT_ROOT, True) == PROJECT_ROOT / "data"
    assert install.default_env_file(PROJECT_ROOT, True) == PROJECT_ROOT / ".env"


# ---- .env 在数据目录里也碰不到 ----------------------------------------------------------


@pytest.fixture
def installed_config(tmp_path) -> Config:
    """装成工具时的样子：数据目录在"Application Support"里，.env 就在数据目录里，里面有一个编造的密钥。"""
    data_dir = tmp_path / "home" / "Library" / "Application Support" / "ZhiFox"
    data_dir.mkdir(parents=True)
    workspace = tmp_path / "work"
    workspace.mkdir()
    env_file = data_dir / ".env"
    env_file.write_text(f"ANTHROPIC_API_KEY={KEY}\n", encoding="utf-8")
    return Config(effort="medium", max_tokens=1000, max_steps=5, workspace=workspace, data_dir=data_dir, env_file=env_file, web=True)


def test_the_file_tool_refuses_the_env_inside_the_data_dir(installed_config):
    config = installed_config
    files = FileEditor(config.workspace, config.backup_dir, protected=config.protected_files)
    for raw in (str(config.env_file), str(config.workspace / ".." / "home" / "Library" / "Application Support" / "ZhiFox" / ".env")):
        with pytest.raises(ToolError, match="密钥"):
            files.run({"command": "view", "path": raw})
        with pytest.raises(ToolError, match="密钥"):
            files.run({"command": "create", "path": raw, "file_text": "ANTHROPIC_BASE_URL=https://evil.example\n"})
    assert config.env_file.read_text(encoding="utf-8") == f"ANTHROPIC_API_KEY={KEY}\n"


def test_export_skips_the_env_inside_the_data_dir(installed_config):
    config = installed_config
    (config.data_dir / "todos.json").write_text("[]", encoding="utf-8")
    work = export.plan(config.data_dir, [config.env_file])
    assert [relative for _, relative in work.files] == ["todos.json"]
    assert (".env", "存着密钥的文件") in work.skipped
    out = io.BytesIO()
    export.write_archive(config.data_dir, out, datetime(2026, 11, 22, 9, 30), [config.env_file])
    with zipfile.ZipFile(out) as archive:
        assert ".env" not in archive.namelist()
        assert all(KEY.encode() not in archive.read(name) for name in archive.namelist())
        assert "这个文件从不导出" in archive.read(export.NOTE).decode()


def test_export_skips_a_key_file_with_any_name_by_its_path(installed_config):
    # 按路径认，不只按名字：HARNESS_ENV_FILE 指向数据目录里一个不叫 .env 的文件，照样不导出
    config = installed_config
    keys = config.data_dir / "keys.txt"
    keys.write_text(f"ANTHROPIC_API_KEY={KEY}\n", encoding="utf-8")
    work = export.plan(config.data_dir, [keys])
    assert "keys.txt" not in [relative for _, relative in work.files]
    assert ("keys.txt", "存着密钥的文件") in work.skipped


def test_the_sandbox_denies_the_env_inside_the_data_dir(installed_config, tmp_path):
    config = installed_config
    home = tmp_path / "home"
    found = policy.build(
        "workspace", config.workspace, [{"path": str(config.env_file), "write": True}], private=[config.data_dir],
        protected=config.protected_files, home=tmp_path / "run-home", temp=tmp_path / "run-temp", real_home=home,
    )
    env = policy.real(config.env_file)
    assert policy.inside(env, found.deny)
    assert not policy.inside(env, found.write) and not policy.inside(env, found.read)  # 设置里加了它也不算


# ---- 新建的文件夹只有自己能进 --------------------------------------------------------------


def test_claiming_a_new_data_dir_creates_it_private(tmp_path):
    data_dir = tmp_path / "fresh-home" / ".local" / "share" / "zhifox"
    handle = instance.claim(data_dir, "web")
    try:
        assert handle is not None and data_dir.is_dir()
        assert has_mode(data_dir, 0o700)
        assert has_mode(data_dir.parent, 0o700)  # 沿途新建的上级（~/.local/share）也一样，XDG 规范也是这么要求的
    finally:
        handle.close()


def test_claiming_an_existing_data_dir_does_not_touch_its_permissions(tmp_path):
    # 从仓库里运行时 data/ 早就在了：照旧，不去改它
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o755)
    data_dir.chmod(0o755)
    handle = instance.claim(data_dir, "web")
    try:
        assert has_mode(data_dir, 0o755)
    finally:
        handle.close()


def test_saving_a_key_creates_the_env_folder_private(tmp_path, monkeypatch):
    env_file = tmp_path / "home" / "Library" / "Application Support" / "ZhiFox" / ".env"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    save_api_key(env_file, "ANTHROPIC_API_KEY", KEY)
    assert env_file.read_text(encoding="utf-8") == f"ANTHROPIC_API_KEY={KEY}\n"
    assert has_mode(env_file.parent, 0o700) and has_mode(env_file, 0o600)


# ---- 说给你听的命令 --------------------------------------------------------------------


def test_installed_commands_drop_uv_run():
    assert install.command(True) == "uv run harness"
    assert install.command(False) == "harness"
    assert install.repair(True) == "uv sync"
    assert install.repair(False) == f"uv tool install --force git+{install.REPOSITORY}"


def test_busy_lines_name_the_installed_command(tmp_path, monkeypatch):
    monkeypatch.setattr(instance, "COMMAND", "harness")
    data_dir = tmp_path / "data"
    handle = instance.claim(data_dir, "service")
    try:
        lines = [str(line) for line in instance.busy_lines(data_dir)]
    finally:
        handle.close()
    assert any("harness service open" in line for line in lines)
    assert not any("uv run" in line for line in lines)


def test_installed_channel_sdks_are_added_by_reinstalling_with_extras(monkeypatch):
    monkeypatch.setattr(platforms, "FROM_SOURCE", False)
    monkeypatch.setattr(platforms, "sdk_ready", lambda platform: platform == "dingtalk")
    command = platforms.install_command("feishu")
    # 钉钉的 SDK 已经装了：重新装的时候一起写上，不然它会被卸掉
    assert command == f'uv tool install --force "harness-assistant[dingtalk,feishu] @ git+{install.REPOSITORY}"'
    message = str(platforms.missing_sdk("feishu"))
    assert command in message and "uv sync" not in message
    monkeypatch.setattr(platforms, "FROM_SOURCE", True)
    assert platforms.install_command("feishu") == "uv sync --extra feishu"


def test_installed_service_problems_say_how_to_reinstall(tmp_path, monkeypatch):
    workspace = tmp_path / "w"
    workspace.mkdir()
    plan = plan_module.Plan(python=str(tmp_path / "gone" / "python"), workspace=workspace, data_dir=tmp_path / "d", env_file=tmp_path / "d" / ".env")
    assert "uv sync" in plan_module.problem(plan)
    monkeypatch.setattr(plan_module, "FROM_SOURCE", False)
    monkeypatch.setattr(plan_module, "REPAIR", install.repair(False))
    message = plan_module.problem(plan)
    assert "uv tool install --force" in message and "uv sync" not in message


def test_installed_eval_explains_where_the_tasks_are(monkeypatch, tmp_path, capsys, make_config):
    monkeypatch.setattr(eval_cli, "FROM_SOURCE", False)
    monkeypatch.setattr(eval_cli, "EVAL_TASKS_DIR", tmp_path / "site-packages" / "evals" / "tasks")
    parser = argparse.ArgumentParser()
    eval_cli.add_parser(parser.add_subparsers(dest="command"))
    assert eval_cli.run(parser.parse_args(["eval", "tasks"]), make_config()) == 2
    err = capsys.readouterr().err
    assert "--tasks-dir" in err and "git clone" in err
    # 自己指定了任务的文件夹：照常读
    out = io.StringIO()
    assert eval_cli.run(parser.parse_args(["eval", "tasks", "--tasks-dir", str(PROJECT_ROOT / "evals" / "tasks")]), make_config(), out=out) == 0
    assert "共" in out.getvalue()


def test_the_wheel_ships_everything_the_program_reads():
    # 装成工具时只有 harness/ 这个包：运行时读的文件（网页、翻译表、图标）都得在包里面，不能在仓库根目录
    with open(PROJECT_ROOT / "pyproject.toml", "rb") as file:
        project = tomllib.load(file)
    assert project["project"]["name"] == install.PROJECT_NAME
    assert project["project"]["scripts"] == {"harness": "harness.cli:main"}
    assert project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["harness"]
    package = PROJECT_ROOT / "harness"
    for name in ("locales/en.json", "locales/es.json", "webui/markdown.js", "webui/page/index.html"):
        assert (package / name).is_file(), name


def test_no_runtime_path_points_outside_the_package_except_the_documented_ones():
    # PROJECT_ROOT 之外（仓库根目录）的东西，装成工具时都没有：只许出现在说明过的几处
    allowed = {"config.py", "evals/cli.py", "evals/dryrun.py", "sandbox/policy.py", "service/plan.py"}
    users = set()
    for file in (PROJECT_ROOT / "harness").rglob("*.py"):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        if any(isinstance(node, ast.Name) and node.id == "PROJECT_ROOT" for node in ast.walk(tree)):
            users.add(file.relative_to(PROJECT_ROOT / "harness").as_posix())
    assert users <= allowed, sorted(users - allowed)


def test_all_three_systems_have_an_answer(tmp_path):
    for system in (machine.MAC, machine.LINUX, machine.WINDOWS):
        folder = install.user_dir(system, {}, tmp_path)
        assert folder.is_relative_to(tmp_path) and folder.name.lower() == "zhifox"
    assert os.path.basename(install.ENV_NAME) == ".env"
