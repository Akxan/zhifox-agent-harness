"""你自己加的"不用问的命令"（data/commands.json）的几道关（harness/commands/allowlist_guard.py）。

清单里的一条是前缀：命令开头几个词和它一样就算，后面跟什么参数都行。所以有三种口子（2026-11-25 安全复查找到的）：

  1. 运行器：加了 uv run、npx、python，就等于 uv run 任何命令、npx 任何包、python -c 任何代码都不问。
     现在运行器后面要紧跟着一个写死的名字（npx eslint、uv run pytest、python manage.py check）才加得进来。
  2. 内置已经放开了只读用法的程序（find、sed、grep……）：加进清单只会多放开内置规则不放的那些
     （find -exec、sed -i、走遍文件夹时不再看里面有没有 .env），所以加不进来。
     git 的几个子命令（config、submodule foreach、bisect run）和选项（--upload-pack、rebase -x……）一样。
  3. 对上以后，后面的参数能让它运行别的命令（make check --eval=…、git pull --upload-pack=…），
     或者指到工作目录之外已有的地方（npm test --prefix ../别的项目、make check -C ../x）：这一次就不算，照常问。

下面每一组先写成修之前会失败的样子（那时它们都加得进来、都算）。
"""

from __future__ import annotations

import json

import pytest
from conftest import symlinks_work

from harness.commands import Places, classify
from harness.commands.allowlist import CommandList, CommandListError, check_entry
from harness.commands.allowlist_guard import (
    NEVER,
    RUNNERS,
    RUNS,
    SPOILERS,
    refusal,
    spoiled,
)

# ---- 1. 运行器：要写明运行哪一个 -------------------------------------------------------


def refused(entry: str, says: str, why: str):
    return pytest.param(entry, says, id=why)


@pytest.mark.parametrize(
    "entry, says",
    [
        # 包运行器写到它为止：后面接什么包都行
        refused("npx", "后面要紧跟着写明运行哪一个", "npx 什么包都能下载、运行"),
        refused("pnpx", "后面要紧跟着写明运行哪一个", "pnpx 是 pnpm dlx"),
        refused("bunx", "后面要紧跟着写明运行哪一个", "bunx 是 bun x"),
        refused("uvx", "后面要紧跟着写明运行哪一个", "uvx 是 uv tool run"),
        refused("uv run", "后面要紧跟着写明运行哪一个", "uv run 任何命令"),
        refused("uv tool run", "后面要紧跟着写明运行哪一个", "uv tool run 任何包"),
        refused("pipx run", "后面要紧跟着写明运行哪一个", "pipx run 任何包"),
        refused("pnpm exec", "后面要紧跟着写明运行哪一个", "pnpm exec 任何命令"),
        refused("pnpm dlx", "后面要紧跟着写明运行哪一个", "pnpm dlx 任何包"),
        refused("yarn dlx", "后面要紧跟着写明运行哪一个", "yarn dlx 任何包"),
        refused("bun x", "后面要紧跟着写明运行哪一个", "bun x 任何包"),
        refused("bun run", "后面要紧跟着写明运行哪一个", "bun run 任何文件"),
        refused("deno run", "后面要紧跟着写明运行哪一个", "deno run 任何网址"),
        refused("poetry run", "后面要紧跟着写明运行哪一个", "poetry run 任何命令"),
        refused("pdm run", "后面要紧跟着写明运行哪一个", "pdm run 任何命令"),
        refused("hatch run", "后面要紧跟着写明运行哪一个", "hatch run 任何命令"),
        refused("conda run", "后面要紧跟着写明运行哪一个", "conda run 任何命令"),
        refused("mamba run", "后面要紧跟着写明运行哪一个", "mamba run 任何命令"),
        refused("pixi run", "后面要紧跟着写明运行哪一个", "pixi run 任何命令"),
        refused("go run", "后面要紧跟着写明运行哪一个", "go run 任何模块"),
        refused("bundle exec", "后面要紧跟着写明运行哪一个", "bundle exec 任何命令"),
        refused("docker run", "后面要紧跟着写明运行哪一个", "docker run 任何镜像"),
        # 后面先跟一个选项：选项能决定运行什么
        refused("npx -y eslint", "后面要紧跟着写明运行哪一个", "npx 后面先跟选项"),
        refused("npx --package=evil eslint", "后面要紧跟着写明运行哪一个", "npx --package 换成别的包"),
        refused("uvx --from evil ruff", "后面要紧跟着写明运行哪一个", "uvx --from 换成别的包"),
        refused("uv run --with evil pytest", "后面要紧跟着写明运行哪一个", "uv run --with 多装一个包"),
        refused("uv run -- python", "后面要紧跟着写明运行哪一个", "uv run -- 后面是任何命令"),
        refused("conda run -n base pytest", "后面要紧跟着写明运行哪一个", "conda run 后面先跟选项"),
        refused("deno run -A main.ts", "后面要紧跟着写明运行哪一个", "deno run 后面先跟权限的选项"),
        refused("docker run -v /:/host alpine", "后面要紧跟着写明运行哪一个", "docker run 挂上整个磁盘"),
        # 解释器（和以前一样，现在和包运行器是同一张表）
        refused("python", "后面要写明运行哪个脚本", "光写 python"),
        refused("python3.12 -c 'print(1)'", "后面要写明运行哪个脚本", "带版本号的 python"),
        refused("/usr/local/bin/python3.11 -c x", "后面要写明运行哪个脚本", "写了完整路径、带版本号的 python"),
        refused("PYTHON3 -c x", "后面要写明运行哪个脚本", "大写的 python（macOS 不分大小写）"),
        refused("python -m", "后面要写明运行哪个脚本", "python -m 后面没写模块"),
        refused("python -m -c", "后面要写明运行哪个脚本", "python -m 后面跟的是选项"),
        refused("pypy3 -c x", "后面要写明运行哪个脚本", "pypy"),
        refused("ts-node -e x", "后面要写明运行哪个脚本", "ts-node -e"),
        # 运行器后面写死的那个名字本身也是一条命令：照样检查
        refused("uv run python", "后面要写明运行哪个脚本", "uv run 后面是光写的 python"),
        refused("poetry run python -c x", "后面要写明运行哪个脚本", "poetry run python -c"),
        refused("uv run bash", "能运行任何命令", "uv run bash"),
        refused("npx node", "后面要写明运行哪个脚本", "npx 后面是光写的 node"),
        refused("python -m uv", "还没写到子命令", "python -m uv 就是 uv"),
        # 后面写什么都不行的
        refused("npm exec eslint", "在后面任何位置都认自己的选项", "npm exec 名字 --package=别的包"),
        refused("npm x eslint", "在后面任何位置都认自己的选项", "npm x 就是 npm exec"),
        refused("npm init vite", "create-名字", "npm init 名字 运行 create-名字 这个包"),
        refused("npm create vite", "create-名字", "npm create 就是 npm init"),
        refused("npm explore lodash", "它会运行后面写的任何命令", "npm explore 包 -- 命令"),
        refused("yarn exec eslint", "当成一段 shell 脚本", "yarn exec 运行一段 shell 脚本"),
        refused("bun exec 'echo hi'", "当成一段 shell 脚本", "bun exec 运行一段 shell 脚本"),
        refused("deno eval 'console.log(1)'", "一段要运行的代码", "deno eval"),
        refused("git config core.pager", "core.fsmonitor", "git config 写进去的设置以后会运行命令"),
        refused("git submodule foreach", "它会运行后面写的任何命令", "git submodule foreach"),
        refused("git bisect run", "它会运行后面写的任何命令", "git bisect run"),
        refused("docker exec web", "它会运行后面写的任何命令", "docker exec 在容器里运行任何命令"),
        refused("kubectl exec pod", "它会运行后面写的任何命令", "kubectl exec"),
        refused("lerna exec", "它会运行后面写的任何命令", "lerna exec -- 命令"),
        refused("yarn workspaces foreach", "它会运行后面写的任何命令", "yarn workspaces foreach"),
        refused("yarn workspace web exec", "当成一段 shell 脚本", "yarn workspace 名字 exec 就是 yarn exec"),
        refused("yarn workspace web", "还没写到子命令", "yarn workspace 名字 后面能接 exec"),
        # 还没写到子命令：后面接上 run、exec、config 就能运行任何东西
        refused("uv", "还没写到子命令", "光写 uv"),
        refused("npm", "还没写到子命令", "光写 npm"),
        refused("pnpm", "还没写到子命令", "光写 pnpm"),
        refused("yarn", "还没写到子命令", "光写 yarn"),
        refused("go", "还没写到子命令", "光写 go"),
        refused("poetry", "还没写到子命令", "光写 poetry"),
        refused("git", "还没写到子命令", "光写 git"),
        refused("docker", "还没写到子命令", "光写 docker"),
        refused("uv tool", "还没写到子命令", "uv tool 后面能接 run"),
        refused("git submodule", "还没写到子命令", "git submodule 后面能接 foreach"),
        refused("git bisect", "还没写到子命令", "git bisect 后面能接 run"),
        refused("docker compose", "还没写到子命令", "docker compose 后面能接 exec"),
        refused("uv --quiet sync", "还没写到子命令", "子命令前面写了选项"),
        refused("git -c core.pager=less log", "还没写到子命令", "git -c 能改任何设置"),
        refused("git -C ../x status", "还没写到子命令", "git -C 换到别的仓库"),
        refused("npm --prefix ../x test", "还没写到子命令", "npm 的选项写在子命令前面"),
        # 能运行任何命令的
        refused("caffeinate", "能运行任何命令", "caffeinate 命令"),
        refused("sandbox-exec -p x", "能运行任何命令", "sandbox-exec 命令"),
        refused("gtimeout 5", "能运行任何命令", "GNU 的 g 前缀"),
    ],
)
def test_runners_must_name_what_they_run(entry, says):
    with pytest.raises(CommandListError) as caught:
        check_entry(entry)
    assert says in str(caught.value), str(caught.value)


@pytest.mark.parametrize(
    "entry",
    [
        "npm test",
        "npm run build",
        "npm ci",
        "npx eslint",
        "npx eslint --fix",
        "npx tsc --noEmit",
        "uv run pytest",
        "uv run pytest -x",
        "uv run python -m pytest",
        "uv run ruff check",
        "uv sync",
        "uv tool run ruff",
        "uvx ruff check",
        "pipx run black",
        "pnpm test",
        "pnpm exec jest",
        "pnpm dlx create-vite",
        "yarn test",
        "yarn dlx eslint",
        "yarn workspace web test",
        "rake test",
        "just test",
        "bun test",
        "bun x vite",
        "bun run dev",
        "deno run main.ts",
        "deno task build",
        "deno test",
        "poetry run pytest",
        "pdm run test",
        "hatch run test",
        "conda run pytest",
        "pixi run test",
        "go run .",
        "go test ./...",
        "go build",
        "python manage.py check",
        "python -m pytest",
        "python3.12 -m pytest",
        "node scripts/build.js",
        "bundle exec rspec",
        "docker run alpine",
        "docker compose up",
        "docker ps",
        "git status",
        "git pull",
        "git push",
        "git rebase main",
        "git submodule update",
        "git bisect start",
        "make",
        "make check",
        "make -C build check",
        "cargo test",
        "tar -czf backup.tgz src",
        "./scripts/status.sh",
        "systemctl restart nginx",
        "kubectl apply -f k8s/",
    ],
)
def test_entries_that_name_what_they_run_are_fine(entry):
    assert check_entry(entry)
    assert refusal(check_entry(entry)) is None


# ---- 2. 内置已经放开了只读用法的程序 ------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        "find",
        "find . -name '*.py'",
        "/usr/bin/find .",
        "gfind .",  # Homebrew 装的 GNU 版本
        "sed",
        "sed -n 1p",
        "gsed -i",
        "awk '{print $1}'",
        "gawk '{print}'",
        "rg",
        "grep -r",  # 走遍文件夹时，内置的规则会先看里面有没有 .env；你加的命令不会
        "ls",
        "du -a",
        "tree",
        "diff -r",
        "sort",
        "uniq",
        "xxd",
        "file",
        "printf",
        "crontab",
        "journalctl",
        "cat",
        "ps",
    ],
)
def test_programs_the_builtin_rules_already_cover_cannot_be_added(entry):
    with pytest.raises(CommandListError) as caught:
        check_entry(entry)
    assert "已经内置" in str(caught.value), str(caught.value)


def test_a_listed_grep_would_have_read_the_key_file(tmp_path):
    """修之前的样子：grep 加进清单以后，grep -r 走遍工作目录（里面有 .env）也不问了。内置的规则会先看一遍文件夹，你加的命令不会。
    现在它根本加不进来；清单里原来就有的（别的程序写进去的）classify 也不认。"""
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-fake\n", encoding="utf-8")
    places = Places(tmp_path, protected=[tmp_path / ".env"])
    assert classify("grep -r sk- .", places).readonly is False  # 内置的：问
    assert classify("grep -r sk- .", places, listed=[["grep"]]).readonly is False
    assert classify("uv run anything", places, listed=[["uv", "run"]]).readonly is False
    with pytest.raises(CommandListError):
        check_entry("grep")


# ---- 选项能运行命令的：条目里不能有，对上以后后面有也不算 ------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        "git rebase -x make",
        "git rebase --exec=make main",
        "git clone -u x",
        "git clone --upload-pack=x repo",
        "git clone -c core.fsmonitor=x repo",
        "git fetch --upload-pack=x",
        "git push --receive-pack=x",
        "git ls-remote -u x",
        "git grep -Oless",
        "git difftool -x",
        "tar -czf x.tgz --to-command=sh",
        "tar --use-compress-program=x -cf x.tar src",
        "tar cIf x x.tar src",  # 老写法：第一个参数不带 -，是一串短选项
        "make check --eval=x",
        "go test -exec x",
        "cargo test --config x",
        "npm test --node-options=x",
        "rsync -av -e x",
        "rake test -e x",
    ],
)
def test_entries_with_options_that_run_commands_are_refused(entry):
    with pytest.raises(CommandListError) as caught:
        check_entry(entry)
    assert "这个选项能让它运行别的命令" in str(caught.value), str(caught.value)


# ---- 3. 对上以后：后面的参数 -----------------------------------------------------------

LISTED = [
    ["npm", "test"], ["make", "check"], ["cargo", "test"], ["go", "test"], ["tar", "-tzf"], ["rsync", "-av"], ["rake", "test"],
    ["just", "test"], ["mvn", "test"], ["uv", "run", "make", "check"],
    ["git", "pull"], ["git", "fetch"], ["git", "push"], ["git", "clone"], ["git", "ls-remote"], ["git", "rebase"],
    ["git", "grep"], ["git", "difftool"], ["git", "archive"],
]


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    """(places, fill)：工作目录 ws（src/、指向旁边项目的链接）、旁边的另一个项目 other（package.json、Makefile……）、主目录里的 proj。"""
    root = tmp_path_factory.mktemp("guard").resolve()
    ws, other, home = root / "ws", root / "other", root / "home"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "a.txt").write_text("a\n", encoding="utf-8")
    other.mkdir()
    for name in ("package.json", "Makefile", "Cargo.toml", "x.js"):
        (other / name).write_text("{}\n", encoding="utf-8")
    (home / "proj").mkdir(parents=True)
    if symlinks_work():
        (ws / "otherlink").symlink_to(other, target_is_directory=True)
    places = Places(ws, home=home, workspace=ws)

    def fill(command: str) -> str:
        return command.replace("{ROOT}", root.as_posix())

    return places, fill


def counts(command: str, readonly: bool, why: str):
    return pytest.param(command, readonly, id=why)


@pytest.mark.parametrize(
    "command, readonly",
    [
        counts("npm test", True, "完全一样"),
        counts("npm test --watch", True, "后面多一个选项"),
        counts("npm test -- --grep foo", True, "不存在的词不管（foo 不是路径）"),
        counts("npm test src", True, "工作目录里面的路径"),
        counts("npm test ./src/../src/a.txt", True, "绕一圈还在工作目录里"),
        counts("npm test --prefix ../nothing-here", True, "不存在的路径不管"),
        # 指到工作目录之外已有的地方：npm、make、cargo 会去运行那里的脚本
        counts("npm test --prefix ../other", False, "npm --prefix 别的项目"),
        counts("npm test --prefix=../other", False, "--prefix=值"),
        counts("npm test --prefix {ROOT}/other", False, "绝对路径"),
        counts("npm test --prefix ~/proj", False, "主目录里的另一个项目"),
        counts("npm test ..", False, "上一层"),
        counts("npm test ../*", False, "通配符展开出来的是工作目录之外"),
        counts("npm test --node-options='--require ../other/x.js'", False, "值里用空格隔开的路径"),
        counts("make check -C ../other", False, "make -C 别的文件夹"),
        counts("make check -C../other", False, "make -C 紧贴着写值"),
        counts("make check -sC../other", False, "写在一串短选项里"),
        counts("make check -f ../other/Makefile", False, "make -f 别的 Makefile"),
        counts("make check --file=../other/Makefile", False, "make --file=别的 Makefile"),
        counts("make check --include-dir=../other", False, "make 的 include 目录"),
        counts("cargo test --manifest-path ../other/Cargo.toml", False, "cargo --manifest-path 别的项目"),
        counts("cargo test --manifest-path=../other/Cargo.toml", False, "cargo --manifest-path=别的项目"),
        # 选项能运行命令的
        counts("make check --eval='$(shell id)'", False, "make --eval 能运行任何命令"),
        counts("make check --ev='$(shell id)'", False, "--eval 的缩写"),
        counts("make check -E x", False, "make -E 就是 --eval"),
        counts("make check CC=evil", False, "make 的变量写在命令行里，会换掉 Makefile 里的"),
        counts("make check .SHELLFLAGS=-c", False, "make 的 .SHELLFLAGS"),
        counts("go test -exec=x ./...", False, "go test -exec"),
        counts("go test --toolexec x ./...", False, "go 的选项一个横线、两个横线都行"),
        counts("go test -ldflags=-extld=x ./...", False, "go -ldflags 能换外部链接程序"),
        counts("go test -run TestA ./...", True, "go test -run 只是挑测试"),
        counts("cargo test --config build.rustc-wrapper=x", False, "cargo --config 能换编译器的外壳"),
        counts("npm test --node-options=--import=x", False, "npm --node-options"),
        counts("npm test --node-opt=x", False, "npm 认选项的缩写"),
        counts("npm test --script-shell=x", False, "npm --script-shell"),
        counts("git pull --upload-pack=x", False, "git pull --upload-pack"),
        counts("git pull --upload-p=x", False, "git 认长选项的缩写"),
        counts("git fetch --upload-pack x origin", False, "git fetch --upload-pack"),
        counts("git push --receive-pack=x", False, "git push --receive-pack"),
        counts("git push --exec=x", False, "git push --exec"),
        counts("git clone -u x repo", False, "git clone -u 是 upload-pack"),
        counts("git clone --config core.fsmonitor=x repo", False, "git clone --config"),
        counts("git clone -c core.hooksPath=x repo", False, "git clone -c"),
        counts("git ls-remote -u x origin", False, "git ls-remote -u"),
        counts("git rebase -x 'rm -rf ~' main", False, "git rebase -x"),
        counts("git rebase -ix x main", False, "写在一串短选项里的 -x"),
        counts("git rebase --exec=x main", False, "git rebase --exec"),
        counts("git grep -Oless foo", False, "git grep -O 紧贴着写翻页程序"),
        counts("git grep --open-files-in-pager=x foo", False, "git grep --open-files-in-pager"),
        counts("git difftool -x x", False, "git difftool -x"),
        counts("git difftool --extcmd=x", False, "git difftool --extcmd"),
        counts("git archive --remote=. --exec=x HEAD", False, "git archive --exec"),
        counts("git pull", True, "git pull"),
        counts("git pull origin main", True, "git pull 远程和分支"),
        counts("git push -u origin main", True, "git push 的 -u 是 --set-upstream"),
        counts("git fetch -u origin", True, "git fetch 的 -u 是 --update-head-ok"),
        counts("git rebase main", True, "git rebase 分支"),
        counts("git log -- --upload-pack", True, "清单里没有 git log：照内置的规则判断（-- 后面是路径）"),
        counts("tar -tzf a.tgz", True, "tar 列出压缩包"),
        counts("tar -tzf a.tgz --to-command=sh", False, "tar --to-command"),
        counts("tar -tzf a.tgz --to-com=sh", False, "--to-command 的缩写"),
        counts("tar -tzf a.tgz --use-compress-program=x", False, "tar --use-compress-program"),
        counts("tar -tzf a.tgz -I x", False, "tar -I"),
        counts("tar -tzf a.tgz --checkpoint-action=exec=x", False, "tar --checkpoint-action"),
        counts("tar -tzf host:a.tgz", False, "tar 把 主机:文件 当成另一台电脑上的（要用 rsh 去连）"),
        counts("rake test -e 'system(1)'", False, "rake -e 运行一段 Ruby"),
        counts("just test --shell x", False, "just --shell"),
        counts("just test name=x", False, "just 的变量"),
        counts("mvn test", True, "mvn test"),
        counts("mvn test exec:exec", False, "mvn 后面多一个插件:目标"),
        counts("uv run make check --eval=x", False, "运行器后面的 make 照样看 make 的选项"),
        counts("uv run make check -k", True, "运行器后面的 make check -k"),
        counts("rsync -av src/ dst/", True, "rsync 本地复制"),
        counts("rsync -av src/ dst/ -e x", False, "rsync -e 换远程 shell"),
        counts("rsync -av --rsync-path=x src/ host:dst/", False, "rsync --rsync-path"),
    ],
)
def test_what_follows_a_listed_command(site, command, readonly):
    places, fill = site
    verdict = classify(fill(command), places, listed=LISTED)
    assert verdict.readonly is readonly, verdict.reason or "被当成了你加的命令"


@pytest.mark.skipif(not symlinks_work(), reason="这台电脑上建不了符号链接")
def test_a_link_inside_the_workspace_to_another_project_is_elsewhere(site):
    places, _ = site
    assert classify("npm test --prefix otherlink", places, listed=LISTED).readonly is False
    assert classify("npm test otherlink/package.json", places, listed=LISTED).readonly is False


def test_spoiled_says_why(site):
    places, _ = site
    assert spoiled(["npm", "test"], ["npm", "test", "--watch"], places) == ""
    assert "工作目录之外" in spoiled(["npm", "test"], ["npm", "test", "--prefix", "../other"], places)
    assert "--upload-pack" in spoiled(["git", "pull"], ["git", "pull", "--upload-pack=x"], places)
    assert "变量" in spoiled(["make", "check"], ["make", "check", "CC=x"], places)


def test_the_entry_itself_is_not_checked_for_paths(site):
    """条目里写的路径是你自己写的（make -C ../shared check）：不因为它指到工作目录之外就不算，只看后面多出来的。"""
    places, _ = site
    listed = [["make", "-C", "../other", "check"]]
    assert classify("make -C ../other check", places, listed=listed).readonly is True
    assert classify("make -C ../other check -f ../other/Makefile", places, listed=listed).readonly is False


# ---- 清单文件里原来就有的、现在不许加的 ---------------------------------------------------


def test_old_entries_that_are_now_refused_are_left_out_and_said(tmp_path):
    path = tmp_path / "commands.json"
    path.write_text(json.dumps({"commands": ["npm test", "uv run", "find .", "npx", "git config"]}), encoding="utf-8")
    store = CommandList(path)
    assert store.entries() == ["npm test"]
    assert "4 条" in store.problem and "uv run" in store.problem and "find ." in store.problem


# ---- 表本身 -------------------------------------------------------------------------


def test_the_tables_are_written_in_the_form_they_are_compared_in():
    """表里的程序名、子命令都是比较时用的样子（小写、不带版本号），每张表互不重叠。"""
    sequences = [*RUNS, *NEVER]
    for sequence in sequences:
        assert sequence and all(word == word.casefold() and word.strip() == word and word for word in sequence), sequence
        assert not sequence[0][-1].isdigit(), sequence
    assert not set(RUNS) & set(NEVER)
    assert not {sequence[0] for sequence in sequences if len(sequence) == 1} & RUNNERS
    for key, spoils in SPOILERS.items():
        assert key and all(word == word.casefold() for word in key), key
        assert spoils.options or spoils.assignments or spoils.colon, key
        assert all(option.startswith("-") and option == option.strip() for option in spoils.options), key
