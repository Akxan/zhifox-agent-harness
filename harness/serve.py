"""无界面模式：`harness serve`。在服务器上作为服务一直开着，跑定时任务和聊天通道，不开浏览器。

在服务器上，你多半是从手机上的 Telegram 跟它说话："看看磁盘还剩多少""nginx 的日志里最近有什么错"。
这时候不需要网页，也没有显示器：

    systemd ──启动──► harness serve ──► 定时任务（到点自己跑）
                                    └─► 聊天通道（Telegram：长轮询，不需要公网地址）
          ◄──SIGTERM（systemctl stop / 重启）── 停下正在进行的、存好盘，再退出

和网页界面（`harness --web`）用的是同一套东西（WebApp、lifecycle.py），只是不开 HTTP 服务器；
加上 --web 也会开着网页界面，但它仍然只监听 127.0.0.1（谁都连不上，只有这台机器自己）。
想在自己的电脑上看，用 SSH 隧道：ssh -L 8765:127.0.0.1:8765 你的服务器，然后打开终端里打印的地址。
绝不监听 0.0.0.0：网页界面能在这台机器上执行命令，不能让网上的任何人都能连。

日志写到标准错误（见 log.py）：在 journald 下带级别标记、不带颜色，`journalctl -u zhifox -f` 就能看。
退出码：0 正常退出；1 已经有一个在运行，或者端口被占用；2 设置有问题（比如 channels.json 坏了），重启也没用，
要先修好（systemd 的 Restart=on-failure 会一直重试，所以在日志里写清楚要怎么修）。

--service launchd / systemd：由 `harness service install` 写进服务的配置里（见 service/），自己不用加。它让这里知道
自己是"后台服务"：数据目录的锁上记着 service（另开一个的时候，提示里会说"它正作为后台服务运行"，见 instance.py），
网页的设置里显示出来；launchd 下日志写进 data/logs/service.log，写满了换新的（launchd 自己不管日志的大小）。
"""

from __future__ import annotations

import logging
import secrets
import signal
import sys
import threading
from collections.abc import Callable
from typing import Any

from . import instance, log
from .app import build_app
from .channels.platforms import ORDER, label
from .config import APP_NAME, Config
from .i18n import t
from .providers import has_key
from .safe import write_file
from .webui import lifecycle
from .webui.server import SHUTDOWN_WAIT_SECONDS, WebApp, claim_data_dir, create_server
from .webui.ui import WebUI

logger = logging.getLogger("harness.serve")

EXIT_OK, EXIT_BUSY, EXIT_CONFIG = 0, 1, 2
SERVICES = ("launchd", "systemd")  # --service 能写的几种（见开头的说明）


def setup_logs(config: Config, service: str | None) -> None:
    """日志写到哪儿：launchd 下写进数据目录里的日志文件（按大小换新的），别的时候写到标准错误。"""
    if service != "launchd":
        log.setup()
        return
    try:
        log.setup(file=config.service_log, own_stdio=True)
    except OSError as exc:  # 日志文件开不了（文件夹没有权限……）：退回标准错误，launchd 还是会把它写进 plist 里那个文件
        log.setup()
        logger.warning("%s", t("打不开日志文件 {path}：{error}", path=config.service_log, error=exc))


def run_serve(config: Config, web: bool = False, port: int | None = None, service: str | None = None) -> int:
    """`harness serve` 的入口：占住数据目录，接上 SIGTERM，一直运行到收到退出信号。"""
    setup_logs(config, service)
    lock = claim_data_dir(config.data_dir, "service" if service else "serve")
    if lock is None:
        for line in instance.busy_lines(config.data_dir):  # 说清楚是谁占着、要怎么办
            logger.error("%s", line)
        return EXIT_BUSY
    stop = threading.Event()
    previous = {}
    for number in (signal.SIGTERM, signal.SIGINT):
        previous[number] = signal.signal(number, lambda *_: stop.set())  # 信号处理只立个旗子，收尾在下面做
    try:
        return serve(config, stop, web=web, port=port, service=service)
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        lock.close()


def serve(
    config: Config, stop: threading.Event, web: bool = False, port: int | None = None,
    client_factory: Callable[..., Any] | None = None, service: str | None = None,
) -> int:
    """运行到 stop 被设置为止。测试直接调用它（不用真的发信号）。"""
    ui = WebUI()
    app = build_app(config, ui) if client_factory is None else build_app(config, ui, client_factory=client_factory)
    app.browser.headless_only = True  # 服务器上没有屏幕：浏览器（设置里开着的话）一律无界面运行
    server = None
    if web:
        try:
            server, url = create_server(app, ui, port, service=service)  # 只监听 127.0.0.1（见 server.create_server）
        except OSError as exc:
            logger.error("%s", t("无法在端口 {port} 上启动：{error}", port=port, error=exc))
            return EXIT_BUSY
        webapp = server.web
    else:
        webapp = WebApp(app, ui, secrets.token_urlsafe(16), service)  # service：设置里显示"作为后台服务运行"（见 WebApp.service）
    store = webapp.channels.store
    if store.problem:
        logger.error("%s", store.problem)
        return EXIT_CONFIG

    logger.info("%s", t("{name}已作为服务启动。工作目录：{workspace}，数据目录：{data}", name=APP_NAME, workspace=config.workspace, data=config.data_dir))
    provider = app.provider
    if has_key(provider):
        logger.info("%s", t("模型：{model}", model=f"{provider.label} · {app.settings.model}"))
    else:
        logger.warning("%s", t("还没有设置 {provider} 的 API 密钥：聊天和定时任务都会失败。把它写进 .env（或者 HARNESS_ENV_FILE 指的文件）再重启。", provider=provider.label))
    if app.tasks.problem:
        logger.warning("%s", app.tasks.problem)
    configured = [platform for platform in ORDER if store.configured(platform)]
    if not configured:
        logger.warning("%s", t("还没有设置任何聊天通道。运行 harness channels setup telegram（或者 discord、slack、feishu、dingtalk、qq）设置一个。"))
    for platform in configured:
        entry = store.get(platform)
        if not entry["enabled"]:
            logger.info("%s", t("{platform}：已关闭", platform=label(platform)))
        elif entry["owner"] is None:
            logger.warning("%s", t("{platform}：还没有配对，谁的消息都不会处理。运行 harness channels pair {id} 配对。", platform=label(platform), id=platform))
    address = config.web_address_file
    if server is not None:
        threading.Thread(target=server.serve_forever, daemon=True, name="web").start()
        port = server.server_address[1]
        if service:
            logger.info("%s", t("网页界面在 127.0.0.1 的 {port} 端口上（只有这台电脑能打开）。打开它：uv run harness service open", port=port))
        else:
            logger.info("%s", t("网页界面只在这台机器上能打开（127.0.0.1）。在自己的电脑上看：ssh -L {port}:127.0.0.1:{port} 这台服务器，然后打开带口令的地址。", port=port))
        if sys.stderr.isatty() and not service:
            logger.info("    %s", url)
        else:
            # 日志会进 journald（或者 launchd 下的日志文件），同一台机器上能读日志的人（adm、systemd-journal 组）就能拿到口令。
            # 所以不写进日志，写进数据目录里一个只有你的账号能读的文件，退出时删掉。harness service open 读的就是它
            write_file(address, url + "\n")
            logger.info("%s", t("带口令的地址写在 {path}（只有这个账号能读）：cat 它就能看到。", path=address))

    lifecycle.start(webapp)
    try:
        while not stop.wait(1.0):
            pass
    finally:
        logger.info("%s", t("正在退出：停下正在进行的、存好盘……"))
        lifecycle.stop(webapp, SHUTDOWN_WAIT_SECONDS)
        if server is not None:
            server.shutdown()
            server.server_close()
            address.unlink(missing_ok=True)
        logger.info("%s", t("再见。"))
    return EXIT_OK
