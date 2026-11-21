"""后台服务：让智狐登录时自动启动、出错退出后自己重启，不再跟着某一个终端（或者 Claude Code 的会话）一起停下。

    uv run harness service install      装上并启动（以后每次登录都会自动启动）
    uv run harness service status       在不在运行、网页在哪个端口、日志在哪儿、最近的错误
    uv run harness service open         在浏览器里打开它的网页（地址里的口令每次启动都换，所以用这条命令来开）
    uv run harness service logs [-f]    看日志
    uv run harness service stop | start | restart
    uv run harness service uninstall    停下，并且不再自动启动

为什么要交给系统：你在终端里运行 `uv run harness --web`，它是这个终端的子进程。终端关了、Claude Code 的会话结束了，
它收到挂断的信号就跟着退出了，网页打不开，Telegram 里的机器人也没人应。操作系统本来就有一个"一直开着的程序"的管家：

    macOS   launchd   每个程序一份 plist（XML 写的设置），放在 ~/Library/LaunchAgents/ 里。登录时 launchd 读这个文件夹，
                      把里面的程序一个个开起来；RunAtLoad 是"读到就开"，KeepAlive 是"退出了再开"
    Linux   systemd   每个程序一份 unit（INI 写的设置），用户自己的放在 ~/.config/systemd/user/ 里。
                      enable 是"登录时开"，Restart=on-failure 是"出错退出了再开"
    Windows 任务计划程序  每个任务一份 XML 写的设置，schtasks /Create 交给它。LogonTrigger 是"登录时开"；
                      "出错退出了再开"和"好好地停下"它不会，由我们自己的 supervise.py 来做

它们开的就是 `harness serve --web`（见 serve.py）：定时任务、聊天通道、网页界面，和你在终端里开的是同一套。

这一组文件：
    base.py       结果、运行系统命令（launchctl、systemctl、schtasks 都经过这里，测试换成假的）、服务的状态
    plan.py       服务要怎么运行（用哪个 Python、工作目录、数据目录、开不开网页），装之前的检查和提醒
    launchd.py    macOS：写 plist、launchctl 的几条命令、读状态
    systemd.py    Linux：写 unit、systemctl 的几条命令、读状态
    windows.py    Windows：写任务的 XML、schtasks 的几条命令、读状态、好好地停下
    supervise.py  Windows：任务计划程序开的是它，它开智狐、出错退出了再开、有人请它停下时让智狐收尾
    logs.py       看日志、挑出最近的错误
    cli.py        harness service 这一组命令：怎么问、怎么打印

安全上守的几条：plist、unit 和任务的 XML 里都没有密钥（API key 留在 .env 里，服务启动时自己读；网页的口令每次启动随机生成，
写在只有你能读的 data/.web-address 里，不进日志、不进服务的设置）；命令一律用参数列表，不经过 shell；
网页仍然只监听 127.0.0.1。
"""

from __future__ import annotations
