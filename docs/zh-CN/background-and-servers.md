[English](../en/background-and-servers.md) | **简体中文**

[智狐](../../README.zh-CN.md) › [文档](../../README.zh-CN.md#文档) › 后台服务和服务器

# 后台服务和服务器

没开终端的时候也让智狐一直开着：在自己的电脑上当后台服务，或者在服务器上用 `harness serve`。

## 在后台一直运行

用 `uv run harness --web` 开起来的智狐，是那个终端的子进程：终端关了，或者开它的 Claude Code 会话结束了，它也跟着退出，
网页打不开，聊天机器人也没人应。想让它一直开着，就把它交给操作系统里本来就管这件事的程序，也就是"服务管家"：
macOS 上是 **launchd**，Linux 上是 **systemd**，Windows 上是 **任务计划程序**。一条命令就够了：

```bash
uv run harness service install     # 装上并启动：以后每次登录都自动启动，出错退出后自己重启
uv run harness service open        # 打开它的网页（带口令的地址替你读出来）
uv run harness service status      # 在不在运行、进程编号、端口、几个文件夹、日志在哪儿、最近的警告和错误
uv run harness service logs -f     # 跟着看日志（Ctrl-C 不看了）；-n 200 多看几行
uv run harness service stop        # 现在停下（下次登录时还会自动启动）
uv run harness service start       # 再开起来
uv run harness service restart     # 重启，比如改了 .env、升级了智狐之后
uv run harness service uninstall   # 停下，以后登录时也不再自动启动（数据和日志都留着）
```

clone 下来的，在项目文件夹里运行它们；一行命令装上的，去掉前面的 `uv run`，在哪儿运行都行（`harness service install`）。
服务用的就是运行智狐的那个 Python（clone 里的 `.venv`，或者 uv 给这个工具建的环境），所以 `uv tool upgrade harness-assistant`
之后运行 `harness service restart` 就换上了新版本，不用重装服务。`install` 可以加 `--no-web`（只运行聊天通道和定时任务，不开网页）、`--port 9000`（默认 8765；
8765 被占用时自动换一个空闲的）和 `--workspace /某个/文件夹`（助手的工作目录，默认是现在所在的目录）。
`install` 重复运行也没关系：设置没变就什么都不动，变了就停下旧的、换上新的。网页上的 **设置 → 后台运行** 会显示智狐现在是哪种开法；
跟着终端的时候，那里有装成后台服务的命令，可以直接复制。

**从手动开的智狐换过来。** 一个数据目录同一时间只能给一个智狐用（终端界面也算，它也存会话：在那里输入 `/exit`），
所以先把手动开的那个关掉，再装：

```bash
# 在运行着 `uv run harness --web` 的那个终端里按 Ctrl-C。
# 是在 Claude Code 的会话里开的？结束那个会话；或者找到它的进程，让它停下：
pgrep -fl "harness --web"          # 打印出进程编号
kill -INT <进程编号>               # 和按 Ctrl-C 一样：智狐存好盘再退出
cd /path/to/zhifox-agent-harness
uv run harness service install
uv run harness service open
```

数据目录还被占着的话，`install` 不装，并且说清楚是谁占着（哪一种智狐、进程编号），免得装上一个一开起来就退出、
每隔十秒又被重新开一次的服务。反过来也一样：服务开着的时候，你在终端里运行 `uv run harness --web`，它会告诉你智狐正作为后台服务运行，
用 `uv run harness service open` 打开它的页面，想在终端里另开一个就先 `uv run harness service stop`。

Windows 上，手动开的智狐在它的 PowerShell 窗口里按 Ctrl-C 关掉；找不到那个窗口的话，这样找到它：
`Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object CommandLine -like '*harness*' | Select-Object ProcessId, CommandLine`。

### Windows 上：任务计划程序

`uv run harness service install` 在任务计划程序里建一个叫 `ZhiFox-你的用户名` 的任务（是隐藏的任务：在**查看 → 显示隐藏的任务**里才看得到）。
你登录时启动，以你的身份运行，不保存密码、不要管理员权限，不限运行时间。上面那几条命令用法都一样。

- **出错退出后重启。** 任务计划程序自己的"失败时重新启动"只在任务*没能启动*时才管用，程序出错退出它是不管的。
  所以任务里运行的是一个小小的看护程序（[service/supervise.py](../../harness/service/supervise.py)，用 `pythonw.exe`，不弹黑色的窗口），
  由它来运行智狐：出错退出后等 10 秒再开；退出码是 2（设置有问题，重启也没用，看日志）时不再开。任务计划程序的"失败时重新启动"留着当第二道保险。
- **好好地停下。** 在任务计划程序里"结束"任务，进程会被直接结束。`uv run harness service stop` 不这样：它在数据目录里放一个"请停下"的标记，
  看护程序关上智狐的输入，智狐停下正在做的事、存好盘再退出。30 秒还没停下，才退一步去结束任务。
- **日志** 在 `data\logs\service.log`，每个文件 1 MB，留三个旧的（`.1` 到 `.3`），和 macOS 上一样。`uv run harness service logs -f`
  跟着看，不需要 `tail`。
- **环境变量。** 任务没法设环境变量，所以数据目录、`.env` 的位置作为参数交给看护程序（只有路径，从来没有密钥）。
  密钥和别的系统上一样，智狐启动时从 `.env` 里读。路径里不能有 `%`（任务计划程序会把 `%名字%` 展开）。
- **什么时候停。** 它以你的身份运行，所以你注销时它就停了；注销、关机时它来不及好好地收尾。

### launchd 和 systemd 是怎么让它一直开着的

两个服务管家的做法一样：每个程序一个小小的设置文件，照着它开、照着它看着：

| | macOS：launchd | Linux：systemd |
|---|---|---|
| 设置文件 | `~/Library/LaunchAgents/io.github.akxan.zhifox.plist`，plist（XML 写的） | `~/.config/systemd/user/zhifox.service`，unit（INI 的写法） |
| 登录时启动 | `RunAtLoad`：launchd 读到这个文件就开起来，而每次登录它都会读这个文件夹 | `WantedBy=default.target` 加上 `systemctl --user enable`：跟着你的登录会话开起来 |
| 出错后重启 | `KeepAlive` 里的 `SuccessfulExit: false`：只要不是以退出码 0 退出的，就再开 | `Restart=on-failure`：同样的规矩 |
| 两次重启之间 | `ThrottleInterval`：10 秒 | `RestartSec=10` |
| 日志 | `StandardOutPath` 和 `StandardErrorPath`：`data/logs/service.log` | journald：`journalctl --user -u zhifox` |
| 智狐用到的命令 | `launchctl bootstrap gui/<uid> <plist>`、`bootout`、`kickstart -k`、`list`（macOS 10.10 以前是 `load`、`unload`） | `systemctl --user daemon-reload`、`enable --now`、`stop`、`restart`、`disable --now`、`show` |

"只有出错退出才重启"正是 `stop` 能停得下来的原因：收到 SIGTERM 时，智狐把手上的事收好尾、存好盘，以退出码 0 退出，
服务管家就不会马上又把它开起来；崩溃了、被 `kill -9` 了、出错退出了，才会被重新开起来。退出码 2 的意思是设置有问题
（比如 `channels.json` 读不出来），重启也没用，所以 systemd 不再重试（`RestartPreventExitStatus=2`），`status` 会说清楚每个退出码是怎么回事。
LaunchAgent 在你登录着的时候运行：登录时开起来，不是开机时。Mac 睡着的时候智狐也睡着，醒来以后接着收那段时间里的消息。

项目在 `/Users/you/zhifox` 时写出来的 plist（这里排得紧凑一些；真正的文件一行一个键或者值，按字母排）：

```xml
<dict>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HARNESS_DATA_DIR</key> <string>/Users/you/zhifox/data</string>
    <key>HARNESS_ENV_FILE</key> <string>/Users/you/zhifox/.env</string>
    <key>LANG</key>             <string>zh_CN.UTF-8</string>
    <key>PATH</key>             <string>（装的时候你终端里的 PATH）</string>
    <key>PYTHONUTF8</key>       <string>1</string>
  </dict>
  <key>KeepAlive</key>         <dict><key>SuccessfulExit</key><false/></dict>
  <key>Label</key>             <string>io.github.akxan.zhifox</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/you/zhifox/.venv/bin/python</string> <string>-P</string> <string>-m</string> <string>harness</string>
    <string>--workspace</string> <string>/Users/you/zhifox</string>
    <string>serve</string> <string>--web</string> <string>--service</string> <string>launchd</string>
  </array>
  <key>RunAtLoad</key>         <true/>
  <key>StandardErrorPath</key> <string>/Users/you/zhifox/data/logs/service.log</string>
  <key>StandardOutPath</key>   <string>/Users/you/zhifox/data/logs/service.log</string>
  <key>ThrottleInterval</key>  <integer>10</integer>
  <key>WorkingDirectory</key>  <string>/Users/you/zhifox</string>
</dict>
```

项目在 `/home/you/zhifox` 时写出来的 unit：

```ini
[Unit]
Description=ZhiFox (智狐)
Documentation=https://github.com/Akxan/zhifox-agent-harness#run-it-in-the-background

[Service]
Type=simple
WorkingDirectory=/home/you/zhifox
ExecStart="/home/you/zhifox/.venv/bin/python" "-P" "-m" "harness" "--workspace" "/home/you/zhifox" "serve" "--web" "--service" "systemd"
Environment="HARNESS_DATA_DIR=/home/you/zhifox/data"
Environment="HARNESS_ENV_FILE=/home/you/zhifox/.env"
Environment="LANG=zh_CN.UTF-8"
Environment="PATH=…"
Environment="PYTHONUTF8=1"
Restart=on-failure
RestartSec=10
RestartPreventExitStatus=2
KillMode=mixed
TimeoutStopSec=30

[Install]
WantedBy=default.target
```

- **运行的是什么。** 项目自己的虚拟环境：`.venv/bin/python -P -m harness … serve --web`。启动时不经过 `uv`，所以登录的时候
  不会去同步、下载什么。`-P` 让 Python 找模块时不看工作目录：你的工作目录里要是放着一个 `json.py`，它顶替不了标准库里的那个。
  每个参数单独一项，不经过 shell，路径里有空格也没事。`--service` 告诉智狐"你是后台服务"：数据目录的锁上记着这一点，
  网页上会显示出来，macOS 上日志由智狐自己写进文件。`KillMode=mixed` 是停下时先只给智狐发 SIGTERM，由它自己停下它开的命令、存好盘。
- **这两个文件里没有密钥。** 只有路径和开关，权限 644（launchd 和 systemd 都要求这样）。API 密钥留在 `.env` 里
  （或者 `HARNESS_ENV_FILE` 指的那个文件），智狐启动时自己读；机器人的 token 留在 `data/channels.json` 里。
- **不继承你终端里的环境变量。** launchd 和 systemd 开智狐的时候，你的 `.zshrc`、`.bashrc` 都没有运行过。服务里只有数据目录、
  `.env` 的位置，以及装的时候的 `PATH` 和 `LANG`；别的（`HTTPS_PROXY`、`HARNESS_MODEL`、只 `export` 过的 API 密钥）都写进 `.env`。
  `install` 会列出只在终端里设置了、`.env` 里没有的那些，只说名字，从不打印值。
- **网页和它的口令。** 网页照样只监听 127.0.0.1。后台服务的口令存在 `data/.web-token` 里（只有你的账号能读，导出时不带），
  重启前后不变：更新代码、`harness service restart` 以后，开着的标签页刷新一下就能接着用。口令防的是这台电脑上的别的账号、
  和你浏览器里别的网站；用你的账号运行的程序本来就读得到 `data/`，口令放在旁边并没有多给它什么。卸载服务时删掉口令，
  下次装上换一个新的（也可以删掉这个文件再重启）。在终端里开的 `harness --web` 照旧每次启动换一个。
  没有终端可以打印，地址就写在 `data/.web-address` 里（只有你的账号能读，退出时删掉），从不写进日志。
  `uv run harness service open` 读它、打开浏览器，打印出来的地址不带口令；`open --print` 打印完整的地址，给另一台机器上的浏览器用（走 SSH 隧道）。
- **日志。** macOS 上是 `data/logs/service.log`，每行带着日期。写到 1 MB 智狐就换一个新文件，旧的留三份（`service.log.1` 到 `.3`），
  所以日志不会超过 4 MB 左右；launchd 自己从来不管它多大。Linux 上在 journald 里：`journalctl --user -u zhifox -f`。
  两边都可以用 `status` 看最近几条警告和错误。API 密钥和机器人的 token 都换成 `<hidden>`，消息的内容从来不写进日志。
- **macOS 和受保护的文件夹。** 项目或者工作目录在"文稿""桌面""下载"或者 iCloud 云盘里的话，macOS 可能会在第一次时问你
  要不要允许 python 访问这个文件夹（点"好"），也可能直接拦下（日志里会出现 `Operation not permitted`）。`install` 和 `status`
  都会提醒。最省事的办法是把项目放到别处，比如 `~/zhifox`；另一个办法是在 **系统设置 → 隐私与安全性 → 完整磁盘访问权限** 里
  加上 `status` 给出的那个 python，不过这样一来，用这个 python 运行的所有程序都能读你全部的文件。
- **Linux 上没人登录的时候。** 默认情况下，你退出登录时 systemd 会把你这个账号的服务一起停掉。想让智狐没人登录时也开着、
  开机就开起来：`sudo loginctl enable-linger $USER`。`status` 会告诉你开了没有。服务器上用一个专门的账号的话，
  也可以用下面[部署到服务器](#部署到服务器)里的系统服务。

## 部署到服务器

智狐可以住在一台 Linux 服务器上，听你的机器人吩咐（"磁盘还剩多少？""今天早上以来 nginx 的日志里有没有报错？"
"把 worker 重启一下"）。服务器上要有 Python 3.11 以上的版本和 [uv](https://docs.astral.sh/uv/)，能用 HTTPS 访问模型厂商的接口和
你用的聊天平台（`api.telegram.org`，`discord.com` 和 `gateway.discord.gg`，`slack.com` 和 `wss-*.slack.com`，`open.feishu.cn`，
`api.dingtalk.com`，`api.bot.qq.com`）；不需要开放任何入站的端口。

```bash
git clone https://github.com/Akxan/zhifox-agent-harness.git ~/zhifox && cd ~/zhifox
uv sync
echo 'ANTHROPIC_API_KEY=sk-ant-…' >> .env && chmod 600 .env    # 或者别家的密钥
uv run harness channels setup telegram     # 粘贴机器人的 token（输入时不显示），或者先设好 TELEGRAM_BOT_TOKEN
uv run harness channels pair telegram      # 打印一个 6 位配对码；把它发给你的机器人，等到显示"配对成功"
uv run harness channels approval telegram readonly    # ask | readonly | edits | all（默认是 ask）
uv run harness commands add "npm test"     # 可选：你自己加的、在 readonly 那一档里不用问的命令
sudo apt install bubblewrap                # 推荐：命令的沙箱（别的发行版用 dnf / pacman）
uv run harness sandbox level workspace     # 命令只在工作目录里运行，不能联网
uv run harness sandbox check               # 看看这台服务器上沙箱挡住了什么、放过了什么
uv run harness channels status
```

同样的命令也认 `discord`、`slack`、`feishu`、`dingtalk`、`qq`。`setup` 会印出这个平台的步骤，一项一项地问（密钥输入时不显示）；
也可以先放在环境变量里（`DISCORD_BOT_TOKEN`，`SLACK_BOT_TOKEN` 和 `SLACK_APP_TOKEN`，`FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`，
`DINGTALK_CLIENT_ID` 和 `DINGTALK_CLIENT_SECRET`，`QQ_APP_ID` 和 `QQ_APP_SECRET`），或者从标准输入传进来，一行一项。
飞书和钉钉要先装它们的 SDK：`uv sync --extra feishu --extra dingtalk`（以后运行 `uv sync` 时也带上同样的 `--extra`，
不然它们会被卸掉）。

然后把它作为服务运行。如果你是用自己的账号登录这台服务器的，`uv run harness service install` 会替你装好 systemd 的
*用户服务*（见上面的"在后台一直运行"；再加上 `sudo loginctl enable-linger $USER`，退出登录以后它也照样开着）。
要用一个专门的、没有特权的账号运行，就写一个系统服务，配置文件是 `/etc/systemd/system/zhifox.service`：

```ini
[Unit]
Description=ZhiFox (chat channels and scheduled tasks)
After=network-online.target
Wants=network-online.target

[Service]
User=zhifox
# 工作目录：助手读写文件、执行命令的地方。要想清楚了再选
WorkingDirectory=/srv/app
ExecStart=/home/zhifox/.local/bin/uv run --project /home/zhifox/zhifox harness serve
# 各家的 API 密钥。智狐自己也会读项目里的 .env；机器人的 token 在 channels.json 里，不在这里
EnvironmentFile=/home/zhifox/zhifox/.env
Environment=HARNESS_DATA_DIR=/home/zhifox/zhifox-data
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now zhifox
journalctl -u zhifox -f        # 看日志；加上 `-p warning` 只看警告和错误
```

- **工作目录。** 助手的文件工具和 shell 命令都从这里开始，所以聊天里说的"这个文件夹"指的就是它。可以用 `WorkingDirectory=` 设，
  也可以先 `cd` 过去，或者用 `harness --workspace /srv/app serve`。
- **数据。** `HARNESS_DATA_DIR`（默认是项目里的 `data/`）里放着会话、记忆、任务和 `channels.json`。
  一个数据文件夹同一时间只能给一个智狐用。服务运行的时候，`harness channels …` 这些命令照样能用：它们在文件锁的保护下改
  `channels.json`，服务两秒之内就会发现。服务开着的时候配对，配对码由服务来收；没开着的话，命令自己连上 Telegram，
  一直等到配对成功。
- **日志。** 写在 stderr 上的普通文字：输出的不是终端时不带颜色代码；在 journald 下每行前面加 `<6>` / `<4>` / `<3>`
  这样的级别标记，`journalctl -p` 才能按级别筛。API 密钥和机器人的 token 都换成 `<hidden>`，消息的内容从来不写进日志。
- **停止。** `systemctl stop` 发出 SIGTERM：智狐不再接收消息，停下正在进行的对话和任务运行（每个工具调用照样补上结果），
  存好会话（以及 Telegram 的消息读到了哪里），以退出码 0 退出。数据文件夹已经有一个智狐在用、或者端口被占用时，退出码是 1；
  `channels.json` 读不出来时是 2（修好或者删掉这个文件；`Restart=on-failure` 只会一遍一遍地重试）。
- **服务器上的网页界面。** `harness serve --web` 会把网页界面也开起来，但仍然只监听 127.0.0.1：它能在这台机器上执行命令，
  所以永远不对外开放。要用的话走 SSH 隧道：`ssh -L 8765:127.0.0.1:8765 你的服务器`，然后打开带口令的那个地址。
  没有终端的时候，这个地址不写进日志（能读日志的人都能拿它来用）；它在 `$HARNESS_DATA_DIR/.web-address` 里，
  只有你的账号能读。服务器上没有文件管理器，所以 **在访达中显示** 按钮改成告诉你文件的完整路径。
- **网络连不上？** 设置 `HTTPS_PROXY`：HTTPS 请求和手写的 WebSocket 客户端（Discord、Slack、QQ）都会走它（用 CONNECT 的
  HTTP 代理）。Telegram 还可以把 `TELEGRAM_API_BASE` 指向一个自己搭的 Bot API 服务器。飞书 SDK 的长连接不走代理。
- **更新。** `cd ~/zhifox && git pull && uv sync && sudo systemctl restart zhifox`（用了飞书、钉钉的话，`uv sync` 带上
  `--extra feishu` / `--extra dingtalk`）。数据和 `.env` 都不会被动到。

远程运维的安全模型：只为配对过的那个账号服务（陌生人得不到回复，群聊不理，配对码 10 分钟后或者猜错 5 次就作废）；
默认的确认方式是 **每次确认**，每次都先给你看完整的命令和工作目录，你再点"允许"；**自动允许只读命令** 只放开只是看的命令；
**全部自动允许** 会先提醒你，
而且要一个通道一个通道地有意去选；把[沙箱](sandbox-and-hooks.md#命令的沙箱命令只碰得到该碰的地方)设成"只在工作目录里"的话，
就算是放行了的命令，也只碰得到工作目录，读不到智狐的数据、`.env` 和 `~/.ssh`，连不上网，要在沙箱之外运行一条，
每次都在聊天里问你（选了"全部自动允许"的通道干脆不行）；同一个聊天里一次只跑一轮，`/stop` 能停下它，连同正在运行的命令；
机器人的 token 和应用的密钥只存在 `channels.json` 里（权限 600），从不发给浏览器或模型，导出时遮住，日志和出错信息里都换掉，
助手的文件工具在任何模式下都打不开这个文件；shell 命令的环境里看不到 `TELEGRAM_BOT_TOKEN`、别的平台存密钥的那几个环境变量
和各家的 API 密钥。你的聊天账号成了这台服务器的一把钥匙，所以要保护好它（开两步验证），并且给这个服务用一个专门的、没有特权的用户。
