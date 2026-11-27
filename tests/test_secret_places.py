"""存着密钥的地方（harness/secret_places.py）：只读命令的判断和命令的沙箱认的是同一份名单。

  - 只读的命令（commands/paths.py）：读这些地方，哪怕只是 cat，也照常问；文件工具在"只读"那一档读它们也问。
    名字太普通的（token、config.json、settings.xml）只在家目录下的那一处才算，在工作目录里照样不用问。
  - 沙箱（sandbox/policy.py）：在任何一档都碰不到家目录里的这些地方；Linux 上不存在的那些怎么办（不在你的家目录里留空文件）。
  - 两边对得上：名单里的每一处，两边都认（以前两边各写一份，对不上：只读的判断认 ~/.pgpass、~/.vault-token，沙箱却不挡）。

家目录一律是临时目录，不碰这台电脑真正的家目录。真的沙箱里的那一关（git 照常能用、读不到 ~/.gitconfig）在 sandbox_cases.py。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from harness import secret_places
from harness.commands import Places, Unclear, classify
from harness.permissions import WHY_READONLY, Permissions
from harness.sandbox import linux
from harness.sandbox.policy import HOME_SECRETS, build, real
from harness.tools.files import TOOL_NAME as FILE_TOOL
from harness.tools.files import FileEditor

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="命令的沙箱只在 macOS、Linux 上有（Windows 上没有沙箱，见 harness/sandbox/windows.py）")


@pytest.fixture
def spot(tmp_path):
    """一个工作目录和一个假的家目录：家目录里真有几样（.gitconfig 里带着 token），别的只看路径、不用真的存在。"""
    root = tmp_path.resolve()  # Windows 上临时目录可能写成短名字（RUNNER~1），那种写法本身就要问
    workspace, home = root / "ws", root / "home"
    (workspace / "src").mkdir(parents=True)
    (home / ".m2" / "repository").mkdir(parents=True)
    (home / ".cache" / "huggingface").mkdir(parents=True)
    (home / ".gitconfig").write_text('[url "https://x-token@example.invalid/"]\n\tinsteadOf = https://example.invalid/\n', encoding="utf-8")
    (home / ".cache" / "huggingface" / "token").write_text("hf_fake\n", encoding="utf-8")
    (home / "notes.txt").write_text("普通的文件\n", encoding="utf-8")
    return Places(workspace, home=home), workspace, home


# ---- 只读的命令 -------------------------------------------------------------------

ASKS = [
    # git：url.<地址>.insteadOf 里常常写着带 token 的地址，http.extraHeader 里是整个认证头（git-config(1)）
    "cat ~/.gitconfig",
    "cat ~/.config/git/config",
    "cat ~/.config/git/credentials",
    "cat .gitconfig",
    # Yarn 的 npmAuthToken、pip 的带 token 的 index-url：项目里的那一份也算
    "cat ~/.yarnrc.yml",
    "cat .yarnrc.yml",
    "cat ~/.yarnrc",
    "cat ~/.config/pip/pip.conf",
    "cat ~/.pip/pip.conf",
    "cat '{HOME}/Library/Application Support/pip/pip.conf'",
    "cat venv/pip.conf",
    "cat pip.ini",
    # Maven、Gradle
    "cat ~/.m2/settings.xml",
    "cat ~/.m2/settings-security.xml",
    "cat ~/.gradle/gradle.properties",
    # 云存储、Terraform、rclone
    "cat ~/.boto",
    "cat ~/.s3cfg",
    "cat ~/.terraformrc",
    "cat terraform.rc",
    "cat ~/.config/rclone/rclone.conf",
    "cat rclone.conf",
    # 各家的命令行工具
    "ls ~/.config/op",
    "ls ~/.op",
    "cat ~/.oci/config",
    "cat ~/.config/doctl/config.yaml",
    "cat '{HOME}/Library/Application Support/doctl/config.yaml'",
    "cat ~/.composer/auth.json",
    "cat ~/.config/composer/config.json",
    "cat auth.json",
    "cat ~/.codex/auth.json",
    "cat ~/.codex/config.toml",
    "cat ~/.kaggle/kaggle.json",
    "cat ~/.cache/huggingface/token",
    "cat ~/.cache/huggingface/stored_tokens",
    "cat ~/.config/github-copilot/apps.json",
    "cat ~/.config/ngrok/ngrok.yml",
    "cat '{HOME}/Library/Application Support/ngrok/ngrok.yml'",
    "cat ~/.ngrok2/ngrok.yml",
    "cat ~/.fly/config.yml",
    "ls ~/.cloudflared",
    "cat ~/.wrangler/config/default.toml",
    "cat ~/.config/.wrangler/config/default.toml",
    "cat ~/.mc/config.json",
    "cat ~/.cargo/credentials.toml",
    "cat ~/.gem/credentials",
    # 以前就认的（名单搬到 secret_places.py 以后照样认）
    "ls ~/.local/share/keyrings",
    "ls ~/Library/Keychains",
    # 会把整个文件夹读一遍的：里面有上面这些地方
    "grep -r TOKEN ~/.cache",
]

# 名字太普通：只有家目录下的那一处才算，别的地方照样是"只是看"
STILL_FINE = [
    "cat src/config.json",
    "cat token",
    "cat settings.xml",
    "cat gradle.properties",
    "cat .codex/notes.md",
    "cat .m2/repository/a.pom",
    "cat ~/.m2/repository/a.pom",
    "cat ~/notes.txt",
    "ls ~/.cache/pip",
]


def _fill(command: str, home: Path) -> str:
    return command.replace("{HOME}", str(home))


@pytest.mark.parametrize("command", ASKS)
def test_reading_a_place_that_holds_secrets_asks(spot, command):
    places, _, home = spot
    verdict = classify(_fill(command, home), places)
    assert not verdict.readonly, (command, verdict)


@pytest.mark.parametrize("command", STILL_FINE)
def test_ordinary_names_elsewhere_still_just_look(spot, command):
    places, _, home = spot
    assert classify(_fill(command, home), places).readonly, command


def test_the_file_tool_asks_in_the_readonly_mode(tmp_path, monkeypatch):
    # Permissions 的 Places 用的是 os.path.expanduser("~")：换成临时的家目录（Windows 上 conftest 已经换了 USERPROFILE）
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    (home / ".m2").mkdir(parents=True)
    (home / ".m2" / "settings.xml").write_text("<settings><servers><server><password>x</password></server></servers></settings>\n", encoding="utf-8")
    (home / ".gitconfig").write_text("[http]\n\textraHeader = Authorization: Bearer x\n", encoding="utf-8")
    (home / "notes.txt").write_text("普通的文件\n", encoding="utf-8")
    workspace = tmp_path / "work"
    (workspace / "data").mkdir(parents=True)
    editor = FileEditor(workspace, workspace / "data" / "backups", protected=[workspace / ".env"])
    permissions = Permissions(editor, private=[workspace / "data"])
    permissions.mode = "readonly"
    for place in (".gitconfig", ".m2/settings.xml", ".kaggle/kaggle.json", ".config/git/credentials"):
        assert permissions.allowance(FILE_TOOL, {"command": "view", "path": str(home / place)}) is None, place
    assert permissions.allowance(FILE_TOOL, {"command": "view", "path": str(home / "notes.txt")}) == WHY_READONLY


# ---- 命令的沙箱 --------------------------------------------------------------------


def _policy(tmp_path: Path, extra: list[dict] | None = None, protected: list[Path] | None = None):
    workspace, home, scratch = tmp_path / "work", tmp_path / "home", tmp_path / "scratch"
    (workspace / "data").mkdir(parents=True, exist_ok=True)
    home.mkdir(exist_ok=True)
    for place in (scratch / "home", scratch / "tmp"):
        place.mkdir(parents=True, exist_ok=True)
    found = build(
        level="workspace", workspace=workspace, extra=extra or [], private=[workspace / "data"],
        protected=protected if protected is not None else [workspace / ".env"],
        home=scratch / "home", temp=scratch / "tmp", real_home=home,
    )
    return found, workspace, home


NEW_IN_THE_SANDBOX = (
    ".gitconfig", ".config/git", ".yarnrc.yml", ".config/pip/pip.conf", ".m2/settings.xml", ".m2/settings-security.xml",
    ".gradle/gradle.properties", ".boto", ".s3cfg", ".terraformrc", ".config/rclone", ".config/op", ".oci", ".config/doctl",
    ".codex", ".composer", ".kaggle", ".cache/huggingface/token", ".config/github-copilot", ".config/ngrok", ".fly",
    ".cloudflared", ".wrangler/config", ".mc", ".cargo/credentials.toml", ".gem/credentials", ".pgpass", ".vault-token",
)


def test_the_sandbox_never_reaches_them(tmp_path):
    found, _, home = _policy(tmp_path)
    for place in NEW_IN_THE_SANDBOX:
        assert real(home) / place in found.deny, place


def test_a_symlinked_secret_place_is_denied_where_it_really_is(tmp_path):
    # 用 stow 这类工具管 dotfiles 时，~/.gitconfig 是指向别处的链接。macOS 的沙箱按真实路径比，只挡链接本身等于没挡
    home, dotfiles = tmp_path / "home", tmp_path / "dotfiles"
    home.mkdir()
    dotfiles.mkdir()
    (dotfiles / "gitconfig").write_text("[user]\n")
    try:
        (home / ".gitconfig").symlink_to(dotfiles / "gitconfig")
    except OSError:
        pytest.skip("这台电脑上建不了符号链接")
    found, _, _ = _policy(tmp_path)
    assert real(home) / ".gitconfig" in found.deny and real(dotfiles / "gitconfig") in found.deny


# ---- 两边是同一份 ------------------------------------------------------------------


def test_both_sides_use_the_same_list(tmp_path):
    found, workspace, home = _policy(tmp_path)
    places = Places(workspace, home=home)
    assert HOME_SECRETS == secret_places.home()
    for place in secret_places.home():
        assert real(home) / place in found.deny, place  # 沙箱挡着
        for written in (str(home / place), f"~/{place}"):
            with pytest.raises(Unclear):
                places.check(written)  # 只读的命令也认
    # Windows 用户文件夹里的那几处：只读的判断按 Windows 的规矩看时认（Windows 上没有沙箱）
    windows = Places(workspace, home=home, windows=True)
    for place in secret_places.home(windows=True):
        with pytest.raises(Unclear):
            windows.check(str(home / place))


def test_the_list_is_written_the_same_way_everywhere():
    for place in (*secret_places.home(windows=True), *secret_places.FOLDERS, *secret_places.WINDOWS_FOLDERS):
        assert place and not place.startswith(("/", "~")) and "\\" not in place and ".." not in place.split("/"), place
        assert place == place.strip() and "//" not in place, place
    everything = [*secret_places.HOME, *secret_places.HOME_ONLY, *secret_places.WINDOWS_HOME]
    assert len(everything) == len(set(everything)), "同一处写了两遍"
    assert set(secret_places.guarded()) <= set(secret_places.home())
    assert set(secret_places.folders()) <= set(secret_places.home())


def test_no_place_is_inside_another():
    # Linux 上永远碰不到的文件夹盖上一个空的、只读的 tmpfs（见 linux.py）。里面还有一处要盖的话，bwrap 要在只读的 tmpfs 里
    # 建挂载点，建不了，整条命令就运行不了。所以名单里不许有一处在另一处里面（policy.build 也会把这样的去掉，这里先不写出来）
    places = [Path(place) for place in secret_places.home()]
    for one in places:
        for other in places:
            assert one == other or not one.is_relative_to(other), (one, other)


# ---- Linux：不存在的那些 ------------------------------------------------------------


def _mounts(args: list[str], flag: str) -> list[tuple[str, str]]:
    return [(args[index + 1], args[index + 2]) for index, arg in enumerate(args) if arg == flag]


@POSIX_ONLY
def test_bwrap_leaves_nothing_behind_in_your_home(tmp_path):
    # 把整个家目录加成可读写的（和 harness serve --workspace ~ 一样，你真正的家目录在沙箱里能写）
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[user]\n")
    (home / ".aws").mkdir()
    found, _, _ = _policy(tmp_path, extra=[{"path": str(home), "write": True}])
    args = linux.arguments(found)
    root = real(home)
    # 存在的：文件换成 /dev/null，文件夹盖上空的只读的
    assert ("/dev/null", str(root / ".gitconfig")) in _mounts(args, "--ro-bind")
    assert str(root / ".aws") in [args[index + 1] for index, arg in enumerate(args) if arg == "--tmpfs"]
    # 不存在的：不盖。盖上的话 bwrap 会在你真正的家目录里留一个只读的空文件当挂载点（以后 npm login 写不了 .npmrc，
    # 空的 settings.xml 让 Maven 报错，~/.docker 的位置上是个文件，docker login 就建不了那个文件夹）。里面本来就没有东西可读
    for place in (".npmrc", ".netrc", ".m2/settings.xml", ".docker", ".kaggle", "Library/Application Support/doctl"):
        assert str(root / place) not in args, place
    # 只有 ~/.ssh 不存在也要盖上：新建出来就能让别人登进这台电脑（authorized_keys）。盖的是一个空的文件夹，不是文件
    assert ("/dev/null", str(root / ".ssh")) not in _mounts(args, "--ro-bind")
    at = args.index(str(root / ".ssh"))
    assert args[at - 1] == "--tmpfs" and args[at + 1 : at + 3] == ["--remount-ro", str(root / ".ssh")]


@POSIX_ONLY
def test_bwrap_never_mounts_inside_a_folder_it_covered(tmp_path):
    # 真的配置里保护的文件（mcp.json、channels.json……）就在数据目录里。数据目录盖上只读的 tmpfs 以后，
    # 再往里面盖 /dev/null，bwrap 要在只读的 tmpfs 里建挂载点，建不了，每条命令都运行不了
    workspace = tmp_path / "work"
    (workspace / "data").mkdir(parents=True)
    (workspace / "data" / "mcp.json").write_text("{}")
    protected = [workspace / ".env", workspace / "data" / "mcp.json", workspace / "data" / "channels.json"]
    found, _, _ = _policy(tmp_path, protected=protected)
    data = real(workspace / "data")
    assert data in found.deny and not any(path != data and path.is_relative_to(data) for path in found.deny)
    args = linux.arguments(found)
    assert str(data / "mcp.json") not in args and str(data / "channels.json") not in args


@POSIX_ONLY
def test_bwrap_covers_a_missing_data_folder_with_a_folder(tmp_path):
    found, workspace, _ = _policy(tmp_path)
    (workspace / "data").rmdir()
    args = linux.arguments(found)
    data = str(real(workspace / "data"))
    assert ("/dev/null", data) not in _mounts(args, "--ro-bind")  # 不然那里是个文件，智狐下次启动时建不了数据目录
    assert args[args.index(data) - 1] == "--tmpfs"
