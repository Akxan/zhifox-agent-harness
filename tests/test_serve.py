"""无界面模式（`harness serve`）、命令行里设置聊天通道（`harness channels ...`），以及运行日志。

在服务器上，智狐作为一个服务一直开着（systemd 管着它），没有浏览器：定时任务到点自己跑，Telegram 里的消息
由它收。这里测的是"开得起来、该跑的都在跑、收到 SIGTERM 能好好地停下"，以及没有浏览器时怎么在命令行里
连机器人、配对、换确认方式。日志要能放心交给 journald：没有颜色的控制字符，绝不出现 token。

对着的都是本地的假服务器（假 Telegram、假 Claude API），数据目录在临时文件夹里，不花钱。
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import anthropic
import pytest
from fake_api import FakeAPI, reply, text
from fake_telegram import TOKEN, FakeTelegram
from test_channels import BOT, OWNER, OWNER_NAME, paired_store, stop_later, until
from test_webui import Browser

from harness import cli, log
from harness.channels import cli as channels_cli
from harness.channels.store import ChannelStore
from harness.providers import PROVIDERS
from harness.serve import EXIT_BUSY, EXIT_CONFIG, EXIT_OK, run_serve, serve
from harness.tasks import TaskStore
from harness.webui.server import claim_data_dir

ROOT = Path(__file__).resolve().parent.parent
HINT = "…" + TOKEN[-4:]  # 页面和命令行上只显示 token 的最后 4 位


@pytest.fixture
def fake():
    telegram = FakeTelegram(max_poll=0.2).start()
    yield telegram
    stop_later(telegram.stop)


@pytest.fixture
def quiet_logs():
    """log.setup 会改掉 harness 这个 logger 的输出位置（还关掉了往上传）：测完原样放回去，别的测试的 caplog 才收得到。"""
    root = logging.getLogger("harness")
    saved = (list(root.handlers), root.level, root.propagate)
    with log._LOCK:
        secrets = set(log._SECRETS)
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in saved[0]:
        root.addHandler(handler)
    root.setLevel(saved[1])
    root.propagate = saved[2]
    with log._LOCK:
        log._SECRETS.clear()
        log._SECRETS.update(secrets)


def factory_for(api: FakeAPI):
    return lambda provider: anthropic.Anthropic(api_key=os.environ.get(provider.key_env) or "missing", base_url=api.base_url, max_retries=0)


# ---- serve()：在一个线程里跑，用一个 Event 当作"收到了退出信号" ----------------------------------


def test_serve_runs_the_channels_and_the_scheduler_until_stopped(make_config, monkeypatch, fake, caplog):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    caplog.set_level(logging.INFO, logger="harness")
    api = FakeAPI([reply(text("磁盘还剩 42%"))]).start()
    config = make_config(telegram_api=fake.base_url)
    paired_store(config.channels_file)
    # 一个提醒：服务关着的时候就该响了（建它时的钟拨回两天前）。服务一开起来，调度器先补上错过的
    tasks = TaskStore(config.tasks_file, clock=lambda: time.time() - 2 * 86400)
    tasks.add({"title": "喝水", "kind": "reminder", "prompt": "起来喝杯水", "schedule": {"type": "interval", "minutes": 60}})

    stop = threading.Event()
    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(serve(config, stop, client_factory=factory_for(api))), daemon=True)
    thread.start()
    try:
        until(lambda: "getUpdates" in fake.calls, what="开始收 Telegram 的消息")
        (reminder,) = until(lambda: TaskStore(config.tasks_file).snapshot()[1], what="补上错过的提醒")
        assert reminder["title"] == "喝水" and reminder["catch_up"] is True

        sent = len(fake.sent)
        fake.user_says("磁盘还剩多少？")
        fake.wait_sent(lambda message: "磁盘还剩 42%" in message["plain"], after=sent)
    finally:
        stop.set()
        thread.join(15)
    assert result == [EXIT_OK]

    # 退出之前存好了盘：聊天里的那一段会话在会话列表里，标着 Telegram
    (saved,) = [json.loads(path.read_text(encoding="utf-8")) for path in config.conversations_dir.glob("*.json")]
    assert saved["channel"] == "telegram" and saved["title"] == "磁盘还剩多少？"
    messages = [record.getMessage() for record in caplog.records]
    assert any("已作为服务启动" in message for message in messages) and "再见。" in messages
    assert not any(TOKEN in message for message in messages)
    api.stop()


def test_serve_explains_what_is_missing(make_config, fake, caplog):
    """没有密钥、通道还没配对：照样开着，但在日志里说清楚要做什么。"""
    caplog.set_level(logging.INFO, logger="harness")
    config = make_config(telegram_api=fake.base_url)
    store = ChannelStore(config.channels_file)
    store.connect("telegram", {"token": TOKEN}, BOT)
    stop = threading.Event()
    stop.set()  # 一开起来就退出
    assert serve(config, stop) == EXIT_OK
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert any(f"还没有设置 {PROVIDERS['anthropic'].label} 的 API 密钥" in message for message in warnings)
    assert any("还没有配对" in message and "harness channels pair telegram" in message for message in warnings)


def test_serve_refuses_a_broken_channels_file(make_config, caplog):
    config = make_config()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.channels_file.write_text("{坏了", encoding="utf-8")
    assert serve(config, threading.Event()) == EXIT_CONFIG  # 重启也没用：退出码 2，等你修好
    assert config.channels_file.read_text(encoding="utf-8") == "{坏了"
    assert any(str(config.channels_file) in record.getMessage() for record in caplog.records if record.levelno == logging.ERROR)


def test_serve_with_the_web_page_listens_only_on_this_machine(make_config, fake, caplog):
    caplog.set_level(logging.INFO, logger="harness")
    config = make_config(telegram_api=fake.base_url)
    stop = threading.Event()
    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(serve(config, stop, web=True, port=0)), daemon=True)
    thread.start()
    address = config.data_dir / ".web-address"
    try:
        # 输出不是终端（交给 journald）：带口令的地址不写进日志（能读日志的人就能连上网页、执行命令），
        # 写在数据目录里一个只有自己能读的文件里
        url = until(lambda: address.is_file() and address.read_text(encoding="utf-8").strip(), what="网页的地址")
        assert url.startswith("http://127.0.0.1:")  # 只监听本机，从别的机器上要用 SSH 隧道
        assert stat.S_IMODE(address.stat().st_mode) == 0o600
        assert any("ssh -L" in record.getMessage() for record in caplog.records)
        assert not any("?token=" in record.getMessage() for record in caplog.records)
        Browser(url).wait_synced()
    finally:
        stop.set()
        thread.join(15)
    assert result == [EXIT_OK]
    assert not address.exists()  # 退出时删掉：口令只在这一次运行里有用


def test_serve_reports_a_busy_port(make_config, fake):
    config = make_config(telegram_api=fake.base_url)
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        assert serve(config, threading.Event(), web=True, port=taken.getsockname()[1]) == EXIT_BUSY


def test_run_serve_refuses_a_data_folder_that_is_in_use(make_config, quiet_logs):
    config = make_config()
    lock = claim_data_dir(config.data_dir)  # 已经有一个智狐在用这个数据目录
    try:
        assert run_serve(config) == EXIT_BUSY
    finally:
        lock.close()


# ---- 真的进程、真的 SIGTERM ------------------------------------------------------------


def start_service(tmp_path, fake, **extra_env) -> subprocess.Popen:
    """像 systemd 那样启动 `python -m harness serve`：输出不是终端，密钥文件、数据目录、Telegram 的地址都由环境变量指定。"""
    workspace = tmp_path / "server-work"
    workspace.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "HARNESS_DATA_DIR": str(tmp_path / "server-data"),
        "HARNESS_ENV_FILE": str(tmp_path / "no-such.env"),
        "TELEGRAM_API_BASE": fake.base_url,
        "NO_PROXY": "*",
        "no_proxy": "*",
        "HOME": str(tmp_path / "home"),  # 别读到这台电脑上真正的东西
        "PYTHONPATH": str(ROOT),
        **extra_env,
    }
    if "JOURNAL_STREAM" not in extra_env:
        env.pop("JOURNAL_STREAM", None)
    return subprocess.Popen(
        [sys.executable, "-m", "harness", "serve"], cwd=workspace, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def stop_service(process: subprocess.Popen, fake) -> tuple[str, str]:
    try:
        until(lambda: "getUpdates" in fake.calls or process.poll() is not None, timeout=30, what="服务开始收消息")
        assert process.poll() is None, process.communicate()
        process.send_signal(signal.SIGTERM)  # systemctl stop
        out, err = process.communicate(timeout=15)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    return out, err


@pytest.mark.parametrize("journal", [False, True])
def test_sigterm_stops_the_service_cleanly(tmp_path, fake, journal):
    paired_store(tmp_path / "server-data" / "channels.json")
    process = start_service(tmp_path, fake, **({"JOURNAL_STREAM": "8:12345"} if journal else {}))
    started = time.monotonic()
    out, err = stop_service(process, fake)
    assert process.returncode == 0, err
    assert time.monotonic() - started < 30
    assert "\x1b[" not in err  # 输出不是终端：不带颜色的控制字符
    assert TOKEN not in err and TOKEN not in out and TOKEN.split(":", 1)[1] not in err
    lines = [line for line in err.splitlines() if line.strip()]
    assert any("已作为服务启动" in line for line in lines) and lines[-1].endswith("再见。")
    if journal:
        # journald 下每一行开头是级别标记：journalctl -p warning 就能只看警告
        assert all(re.match(r"<[0-7]>", line) for line in lines), lines
        assert any(line.startswith("<4>") and "API 密钥" in line for line in lines)
        assert any(line.startswith("<6>") and "再见" in line for line in lines)
    else:
        assert all(re.match(r"\d\d:\d\d:\d\d (info|warning|error) ", line) for line in lines), lines


def test_sigterm_stops_the_web_ui_cleanly_too(tmp_path):
    # `harness --web` 以前只在 Ctrl-C（SIGINT）时收尾存盘：普通的 kill（SIGTERM）会直接把它杀掉，
    # 正在进行的一轮、还没写完的会话就丢了，数据目录的锁也来不及好好放开。现在两种信号走同一条收尾的路
    workspace = tmp_path / "web-work"
    workspace.mkdir()
    env = {
        **os.environ,
        "HARNESS_DATA_DIR": str(tmp_path / "web-data"),
        "HARNESS_ENV_FILE": str(tmp_path / "no-such.env"),
        "HOME": str(tmp_path / "home"),
        "PYTHONPATH": str(ROOT),
        "PYTHONUNBUFFERED": "1",  # 输出接的是管道：不加的话 print 攒在缓冲区里，这里等不到那个地址
        "NO_PROXY": "*",
        "no_proxy": "*",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "harness", "--web", "--no-browser", "--port", "0"], cwd=workspace, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        seen = []
        deadline = time.monotonic() + 30
        for line in process.stdout:  # 等它打印出地址：说明已经开始服务了
            seen.append(line)
            if "?token=" in line or time.monotonic() > deadline:
                break
        assert process.poll() is None, "".join(seen)
        process.send_signal(signal.SIGTERM)
        out, _ = process.communicate(timeout=15)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert process.returncode == 0, out
    assert out.rstrip().endswith("再见。"), out


# ---- harness channels ...（命令行）-----------------------------------------------------


@pytest.fixture
def harness_cli(tmp_path, monkeypatch, capsys, fake, quiet_logs):
    """像在服务器的终端里那样运行 `harness ...`：数据目录在临时文件夹里，Telegram 是本地的假服务器。"""
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "cli-data"))
    monkeypatch.setenv("TELEGRAM_API_BASE", fake.base_url)

    def run_cli(*argv: str) -> tuple[int, str, str]:
        capsys.readouterr()
        code = cli.main(list(argv))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    run_cli.store = lambda: ChannelStore(tmp_path / "cli-data" / "channels.json")
    return run_cli


class Terminal(io.StringIO):
    """假装标准输入是终端：这时 token 只能用不回显的方式问（getpass），不能从标准输入读一行。"""

    def isatty(self) -> bool:
        return True

    def readline(self, *args) -> str:
        raise AssertionError("在终端里不该用回显的方式读 token")


def test_setup_reads_the_token_from_the_environment(harness_cli, monkeypatch, fake):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    code, out, err = harness_cli("channels", "setup", "telegram")
    assert code == 0, err
    assert f"已连接机器人 @zhifox_test_bot（密钥 {HINT}）" in out
    assert "下一步：运行 harness channels pair telegram" in out
    assert TOKEN not in out + err and fake.calls == ["getMe"]
    assert harness_cli.store().get("telegram")["credentials"].get("token", "") == TOKEN


def test_setup_asks_for_the_token_without_echo(harness_cli, monkeypatch):
    asked = []

    def fake_getpass(prompt: str = "") -> str:
        asked.append(prompt)
        return f"  {TOKEN} "

    monkeypatch.setattr("getpass.getpass", fake_getpass)
    monkeypatch.setattr(sys, "stdin", Terminal())
    code, out, err = harness_cli("channels", "setup", "telegram")
    assert code == 0, err
    assert len(asked) == 1 and "不会显示出来" in asked[0]  # 用的是不回显的输入
    assert "@BotFather" in out and HINT in out and TOKEN not in out + err


def test_setup_reads_a_piped_token(harness_cli, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(TOKEN + "\n"))  # echo $TOKEN | harness channels setup telegram
    code, out, err = harness_cli("channels", "setup", "telegram")
    assert code == 0 and HINT in out and TOKEN not in out + err


def test_setup_explains_a_bad_token(harness_cli, monkeypatch, fake):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not-a-token\n"))
    code, out, err = harness_cli("channels", "setup", "telegram")
    assert code == 1 and "不像是一个 Telegram 机器人的 token" in err and fake.calls == []
    rejected = "987654321:" + "Z" * 35
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", rejected)
    code, out, err = harness_cli("channels", "setup", "telegram")
    assert code == 1 and "不认这个 token" in err and rejected not in out + err
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
    code, out, err = harness_cli("channels", "setup", "telegram")
    assert code == 1 and "没有收到 token" in err
    assert not harness_cli.store().get("telegram")["credentials"].get("token", "")


def test_commands_need_a_bot_first(harness_cli):
    for argv in (("pair", "telegram"), ("approval", "telegram", "all"), ("enable", "telegram"), ("disable", "telegram")):
        code, out, err = harness_cli("channels", *argv)
        assert code == 1 and "还没有连接 Telegram：先运行 harness channels setup telegram" in err, argv
    code, out, err = harness_cli("channels", "status")
    assert code == 0 and "没有在运行" in out and "还没有设置。运行 harness channels setup telegram" in out


def test_pair_without_waiting_prints_a_code(harness_cli):
    harness_cli.store().connect("telegram", {"token": TOKEN}, BOT)
    code, out, err = harness_cli("channels", "pair", "telegram", "--no-wait")
    assert code == 0, err
    secret = harness_cli.store().pairing("telegram")["secret"]
    assert f"配对码：{secret[:3]} {secret[3:]}（10 分钟内有效）" in out
    assert f"https://t.me/zhifox_test_bot?start={secret}" in out  # 点开就替你把配对码发过去


def test_pair_waits_for_the_running_service(harness_cli, monkeypatch):
    """智狐作为服务开着：由它去收你发的配对码，命令行只是等着看配对成了没有（两个程序对着同一个文件）。"""
    harness_cli.store().connect("telegram", {"token": TOKEN}, BOT)
    monkeypatch.setattr(channels_cli, "_running", lambda config: True)

    def the_service_receives_the_code() -> None:
        service = harness_cli.store()
        code = until(lambda: service.refresh() is not None and service.pairing("telegram"), what="命令行生成的配对码")["secret"]
        assert service.attempt("telegram", OWNER, OWNER_NAME, code) == "paired"

    helper = threading.Thread(target=the_service_receives_the_code, daemon=True)
    helper.start()
    code, out, err = harness_cli("channels", "pair", "telegram")
    helper.join(5)
    assert code == 0, err
    assert "正在运行，由它来收你发的配对码" in out and f"配对成功：{OWNER_NAME}" in out


def test_pair_listens_by_itself_when_nothing_is_running(harness_cli, fake):
    """智狐没开着：命令行自己临时连上 Telegram 收配对码，配好就断开。"""
    harness_cli.store().connect("telegram", {"token": TOKEN}, BOT)

    def you_send_the_code() -> None:
        code = until(lambda: harness_cli.store().pairing("telegram"), what="配对码")["secret"]
        until(lambda: "getUpdates" in fake.calls, what="命令行开始收消息")
        fake.user_says(f"/start {code}")

    helper = threading.Thread(target=you_send_the_code, daemon=True)
    helper.start()
    code, out, err = harness_cli("channels", "pair", "telegram")
    helper.join(5)
    assert code == 0, err
    assert "正在等你发配对码" in out and f"配对成功：{OWNER_NAME}" in out
    assert any("配对成功" in message["plain"] for message in fake.sent)  # 聊天里也回了一句
    assert harness_cli.store().get("telegram")["owner"]["id"] == OWNER
    lock = claim_data_dir(harness_cli.store().path.parent)
    assert lock is not None  # 配好以后放开了数据目录，服务可以启动了
    lock.close()


def test_approval_all_comes_with_a_warning(harness_cli):
    harness_cli.store().connect("telegram", {"token": TOKEN}, BOT)
    code, out, err = harness_cli("channels", "approval", "telegram", "all")
    assert code == 0 and "Telegram 的确认方式改成了：全部自动允许" in out, err
    assert "确认是你和模型之间唯一的一道关卡" in out
    assert harness_cli.store().get("telegram")["approval"] == "all"
    code, out, err = harness_cli("channels", "approval", "telegram", "ask")
    assert code == 0 and "唯一的一道关卡" not in out, err
    with pytest.raises(SystemExit):
        harness_cli("channels", "approval", "telegram", "yolo")  # argparse 拦下：只有三档


def test_status_enable_disable_and_remove(harness_cli, monkeypatch):
    store = paired_store(harness_cli.store().path)
    store.start_pairing("telegram")
    store.attempt("telegram", "999", "Stranger", "你好")  # 配对期间陌生人说了一句：提醒过一次，不算忽略
    store.attempt("telegram", "999", "Stranger", "在吗")
    code, out, err = harness_cli("channels", "status")
    assert code == 0, err
    assert f"机器人：@zhifox_test_bot（密钥 {HINT}）" in out and TOKEN not in out
    assert f"主人：{OWNER_NAME}" in out and "正在配对：配对码" in out and "确认方式：每次都问" in out
    assert "忽略了 1 条陌生人的消息" in out and "已打开" in out

    code, out, err = harness_cli("channels", "disable", "telegram")
    assert code == 0 and "Telegram 已关闭。" in out and harness_cli.store().get("telegram")["enabled"] is False
    code, out, err = harness_cli("channels", "enable", "telegram")
    assert code == 0 and "Telegram 已打开。" in out and harness_cli.store().get("telegram")["enabled"] is True

    monkeypatch.setattr("builtins.input", lambda question: "n")  # 问一遍：不删
    code, out, err = harness_cli("channels", "remove", "telegram")
    assert code == 1 and harness_cli.store().get("telegram")["credentials"].get("token", "") == TOKEN
    code, out, err = harness_cli("channels", "remove", "telegram", "--yes")
    assert code == 0 and "已删除 Telegram 的设置" in out and harness_cli.store().known() == []


def test_a_broken_settings_file_stops_every_command(harness_cli):
    path = harness_cli.store().path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2", encoding="utf-8")
    for argv in (("status",), ("pair", "telegram", "--no-wait"), ("approval", "telegram", "ask")):
        code, _out, err = harness_cli("channels", *argv)
        assert code == 2 and str(path) in err, argv
    assert path.read_text(encoding="utf-8") == "[1, 2"


def test_the_workspace_must_exist(harness_cli, tmp_path):
    code, _out, err = harness_cli("--workspace", str(tmp_path / "nowhere"), "channels", "status")
    assert code == 2 and "工作目录不存在" in err


# ---- 运行日志 ----------------------------------------------------------------------


def test_registered_secrets_are_hidden_in_the_log(quiet_logs):
    log.hide("short")  # 太短的不算密钥：不然日志里所有的这几个字母都会被换掉
    log.hide(None)
    log.hide("supersecret-1")
    log.hide("supersecret-1-and-more")
    assert log.redact("short supersecret-1-and-more / supersecret-1") == "short <hidden> / <hidden>"  # 长的先换，不会只换掉一半


def test_the_log_has_no_colors_when_it_is_not_a_terminal(quiet_logs, monkeypatch):
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)  # 环境变量里的 token：setup 时就登记成要遮住的
    stream = io.StringIO()
    log.setup(stream)
    logger = logging.getLogger("harness.test")
    logger.warning("请求 https://api.telegram.org/bot%s/getMe 失败", TOKEN)
    try:
        raise RuntimeError(f"连接 bot{TOKEN} 时出错")
    except RuntimeError:
        logger.exception("出错了")
    output = stream.getvalue()
    assert "\x1b[" not in output and TOKEN not in output
    first = output.splitlines()[0]
    assert re.fullmatch(r"\d\d:\d\d:\d\d warning 请求 https://api.telegram.org/bot<hidden>/getMe 失败", first), first
    assert "RuntimeError: 连接 bot<hidden> 时出错" in output


def test_the_log_uses_journald_levels(quiet_logs, monkeypatch):
    monkeypatch.setenv("JOURNAL_STREAM", "8:12345")
    stream = io.StringIO()
    log.setup(stream)
    logger = logging.getLogger("harness.test")
    logger.info("开始")
    logger.warning("小心")
    try:
        raise ValueError("第一行\n第二行")
    except ValueError:
        logger.exception("坏了")
    lines = stream.getvalue().splitlines()
    assert lines[:2] == ["<6>开始", "<4>小心"]
    assert len(lines) == 3 and lines[2].startswith("<3>坏了 | Traceback")  # 异常的几行合成一行，每一行都带着级别


def test_the_log_is_colored_on_a_terminal(quiet_logs, monkeypatch):
    class Screen(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setenv("JOURNAL_STREAM", "8:12345")  # 在终端里看的时候，不管有没有 journald，都按终端的样子写
    monkeypatch.delenv("NO_COLOR", raising=False)
    screen = Screen()
    log.setup(screen)
    logging.getLogger("harness.test").warning("黄色的警告")
    assert "\x1b[33m黄色的警告" in screen.getvalue()

    monkeypatch.setenv("NO_COLOR", "1")  # 不想要颜色的人可以关掉（https://no-color.org）
    plain = Screen()
    log.setup(plain)
    logging.getLogger("harness.test").warning("没有颜色")
    assert "\x1b[" not in plain.getvalue() and "warning 没有颜色" in plain.getvalue()
