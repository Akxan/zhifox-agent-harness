[English](../en/sandbox-and-hooks.md) | **简体中文**

[智狐](../../README.zh-CN.md) › [文档](../../README.zh-CN.md#文档) › 沙箱、遮密钥和钩子

# 沙箱、遮密钥和钩子

在[工具和权限](tools-and-permissions.md)里那些确认之外的几道保险：命令的操作系统沙箱、遮住工具结果里冒出来的密钥，以及在固定的时刻运行你自己的脚本的钩子。

## 沙箱

命令的沙箱默认关着；它做什么、为什么这样做，见下面的[命令的沙箱：命令只碰得到该碰的地方](#命令的沙箱命令只碰得到该碰的地方)。
打开的办法：**设置 → 沙箱**，给你的对话选一档：**只在工作目录里**（不能联网；在手机上指挥智狐时用它）或者 **工作目录 + 联网**。
这一节里写着这台电脑用的是哪一种沙箱（macOS 的 `sandbox-exec`、Linux 的 bubblewrap）；这里用不了的话，写着为什么、怎么修，
这时还选了一档的话，提醒变成红字：修好、或者改回"关"之前，每一条命令都会被拒绝执行。**管理路径** 打开"额外允许的路径"的列表，
命令可以读（或者读写）这些地方，比如加上 `~/.nvm`，沙箱里就能用 nvm 装的 `node`。改了从下一条命令起算数。
定时任务和聊天通道跟着它走，除非你在任务的编辑器、通道的向导里另外选了一档。

开着的时候，每条命令的确认卡片上多一行，写着它在哪一档里运行。命令因为被沙箱挡住而失败时，助手会被告知；
它确实需要在沙箱之外运行这一条的话，会单独弹出一张带警告的卡片，每次都问。在服务器上，同样的设置在命令行里：
`harness sandbox`、`harness sandbox level workspace`、`harness sandbox allow <路径> [--write]`、`harness sandbox forget <路径>`、
`harness sandbox check` 和 `harness channels sandbox <平台> <档位>`。

## 命令的沙箱：命令只碰得到该碰的地方

`run_command` 以你的身份执行每一条命令，所以一条命令碰得到你碰得到的一切：家目录、SSH 密钥、别的项目、网络。
第一道关卡是确认卡片（见[权限规则](tools-and-permissions.md#权限规则)）：每条命令先给你看，你点了"允许"才执行。可是这道关卡会被放宽
（"自动允许只读命令""全部自动允许"），会在手机上的聊天通道里被匆匆点过去，写命令的模型还可能被它刚读过的网页、
文件里藏着的一句话带偏（提示注入）。沙箱是另一道、各自独立的关卡：就算一条命令已经被放行了，它运行的时候也只碰得到该碰的地方。
两道关卡谁也不替代谁：放宽了"问不问"，沙箱照样挡着；开了沙箱，该问的一句都不少。它是可选的，**默认关着**。
代码在 [harness/sandbox/](../../harness/sandbox/)，一个文件讲一件事：

| 文件 | 做什么 |
|---|---|
| [policy.py](../../harness/sandbox/policy.py) | 有哪几档，一条命令能读、能写、永远碰不到哪些路径。两个系统照着的是这同一份 |
| [session.py](../../harness/sandbox/session.py) | 一次运行的沙箱：现在用哪一档、这次运行能不能请你放行一条命令、它自己的 HOME 和临时文件夹、确认卡片上和写给模型的说明里怎么说 |
| [macos.py](../../harness/sandbox/macos.py) | macOS：给 `sandbox-exec` 的 SBPL 配置 |
| [linux.py](../../harness/sandbox/linux.py) | Linux：给 bubblewrap（`bwrap`）的参数 |
| [windows.py](../../harness/sandbox/windows.py) | Windows：为什么暂时没有 |
| [backend.py](../../harness/sandbox/backend.py) | 这台电脑用哪一种；唯一看操作系统是哪一种的地方 |
| [webui/sandbox_api.py](../../harness/webui/sandbox_api.py) | **设置 → 沙箱**：选哪一档、额外允许的路径 |

**什么是沙箱。** 操作系统给一个进程（和它开的所有子进程）划一个圈：圈外的文件读不到、写不了，网络连不上。
守着这个圈的是操作系统的内核，不是智狐，要紧的就在这里。智狐也可以试着读每一条命令、猜它会碰到什么，
可这种检查总有办法绕过去：换个写法，写成一个脚本再运行它，`python -c`，base64……内核守的规矩，不管命令最后实际做了什么都算数。

**三档。** 在 **设置 → 沙箱** 里选。它是你的对话用的那一档，也是定时任务、聊天通道默认跟着的那一档：

| 档位 | 一条命令能做什么 |
|---|---|
| **关**（默认） | 和以前一样，只靠"要不要先问你"这一道关卡 |
| **只在工作目录里**（远程使用时推荐） | 能读写工作目录；能读运行程序要用的系统目录；家目录里别的东西、别的硬盘、`/tmp` 都碰不到；完全不能联网，连 `127.0.0.1` 也不行 |
| **工作目录 + 联网** | 和上一档一样，只是能联网：下载、`git pull`、调用接口 |

细一点说，两档里都是这样：

- **能读写：** 工作目录；这次运行自己的 HOME 和临时文件夹（见下面）；你加的"可读写"的路径。
- **只能读：** 运行程序要用的系统目录（每个系统各列各的：macOS 上是 `/System`、`/usr`、`/bin`、`/Library`、`/Applications`、
  `/private/etc`、`/opt/homebrew`……，Linux 上是 `/usr`、`/etc`、`/opt`、`/nix`……），以及你加的"只读"的路径。
- **永远碰不到，在工作目录里面也一样：** 智狐自己的数据目录（`data/`：以前的会话、设置、记忆、机器人的 token）、
  存着各家 API key 的 `.env`，以及文件工具保护的那几个文件（`mcp.json`、`channels.json`、`commands.json`）。
  还有家目录里放密钥的地方：`~/.ssh`、`~/.gnupg`、`~/.aws`、`~/.azure`、`~/.kube`、`~/.docker`、`~/.netrc`、
  `~/.git-credentials`、`~/.npmrc`、`~/.pypirc`、`~/.config/gh`、`~/.config/gcloud`、`~/.password-store`、钥匙串和钥匙圈。
  后面这些，除非你把这个地方本身（或者它里面的东西）单独加进"额外允许的路径"，否则照样挡着。为什么要单独列：
  工作目录可能就是家目录（在服务器上 `harness serve --workspace ~`），或者你把整个家目录加了进来；`~/.ssh` 照样挡着。
- **能写的地方里，也只能读：** 智狐自己的程序（`harness/`、`.venv`、`pyproject.toml`、`uv.lock`、`.git`、`.python-version`，
  以及它正在用的 Python 环境），和开终端、登录时会自动运行的文件（`~/.bashrc`、`~/.zshrc`、`~/.profile` 这一类，
  `~/.config/fish`、`~/.config/systemd`、`~/.config/autostart`、`~/Library/LaunchAgents`）。默认情况下工作目录就是项目目录，
  要是不挡，沙箱里的命令就能改掉 `harness/tools/shell.py`，下次智狐启动时，改过的代码就在沙箱之外运行了。
  改过的 `~/.zshrc` 也一样：你下次打开终端时它就在沙箱之外运行。
- **别的地方**都碰不到：家目录里别的东西、别的用户、别的硬盘、`/tmp`。

**为什么 HOME 换成一个空的文件夹。** 命令照样以你的身份运行，git、npm、pip 这些程序启动时会去读家目录里的设置
（`~/.gitconfig`、`~/.npmrc`），往那里写缓存（`~/.npm`、`~/.cache`）。家目录读不到时，有的程序直接退出：
git 会停在 `fatal: unable to access '/Users/你/.gitconfig': Operation not permitted`。所以每一次运行
（你的对话、定时任务的一次运行、一个聊天）有自己的、空的 HOME 和 TMPDIR，在系统的临时文件夹里，这次运行里的命令共用，
运行结束就删掉。git 照常能用（只是没有你全局设置的名字和邮箱），npm、pip 往那里下载缓存。命令里的 `~` 指的是这个文件夹，
所以要碰你加进来的地方，写完整的路径。指向真正家目录的那几个环境变量（`XDG_CONFIG_HOME`、`XDG_CACHE_HOME` 等 `XDG_*`，
还有 `SSH_AUTH_SOCK`）会被去掉。还有一处：`uv run` 会把智狐自己的 `.venv/bin` 放在 `PATH` 的最前面，在沙箱里它又被拿掉了。
那是智狐的程序，不是你的工具，而且它背后的 Python 多半装在家目录里（`~/.local/share/uv`），沙箱里根本启动不了；
拿掉以后，`python3` 用的是系统的或者 Homebrew 的那个。

**localhost 也算联网。** 在"只在工作目录里"这一档，连 `127.0.0.1` 也连不上。数据库、Docker、这台电脑上别的服务、
智狐自己的网页都在那里监听；能和它们说话的命令，就能让它们去做沙箱不让它自己做的事。Unix 套接字（Docker 的套接字、ssh-agent）
在 macOS 上哪一档都连不上；在 Linux 上它们本来就不在沙箱里。

**额外允许的路径。** **设置 → 沙箱 → 管理路径** 里加"只读"或者"可读写"的路径：从 `/` 或者 `~` 开始的完整路径，
最多 50 条（`SANDBOX_MAX_PATHS`），每条最长 1000 个字（`SANDBOX_PATH_CHARS`）。装在家目录里的程序（nvm、pyenv、
cargo 或 uv 装的工具）在沙箱里默认读不到：把它的文件夹加成只读就行，比如 `~/.nvm`。和别的存在本地的东西一样，
每一行都有"在访达中显示""编辑""删除"（删除先在原地确认）。`data/` 和 `.env` 加不进来：加了也不会生效。

**定时任务和聊天通道。** 每个"让助手去做"的任务（在任务的编辑器里）、每个聊天通道（在向导的最后一步）都有自己的一档：
默认是 **跟随设置**，也可以选 **关**、**只在工作目录里**、**工作目录 + 联网**。助手替你添加的任务一律是"跟随设置"，
它的工具也没法改：一个任务要不要在沙箱之外运行，只能由你决定。你的对话跟着设置走，改了从下一条命令起算数。
通道的那一档每条命令之前重新读一次；任务的一次运行从开始时就定下自己的那一档（选了"跟随设置"的，每条命令之前看一眼设置）。
子 Agent 用的就是叫它的那一方的沙箱：只会更少，不会更多。

**macOS：`sandbox-exec`。** macOS 自己就有一套沙箱（Seatbelt）：App Store 里的程序、Safari、系统的很多服务都关在里面，
那些服务用的配置就在 `/System/Library/Sandbox/Profiles`。`sandbox-exec` 让一条命令（和它开的所有子进程）在一份配置里运行，
配置用 SBPL（Sandbox Profile Language）写。智狐每条命令现写一份：

```
(version 1)
(deny default)                 ; 先什么都不许，再一样一样放开
(import "system.sb")           ; 一个程序起码要能做的：加载系统的库、读 /dev/null、查时区……
(allow process-fork) (allow process-exec)
(allow signal (target same-sandbox))            ; 只能给沙箱里的进程发信号
(allow file-read-metadata)                      ; 看文件在不在、多大，从不读内容
(allow file-read* file-map-executable (subpath (param "SYSTEM_0")) …)              ; 系统目录
(allow file-read* file-write* file-map-executable (subpath (param "WRITE_0")) …)   ; 工作目录、HOME、临时文件夹
(deny file-write* (subpath (param "READONLY_0")) …)                                ; 智狐自己的程序……
(deny file-read* file-write* (subpath (param "DENY_0")) …)                         ; data/、.env、~/.ssh……
```

不许的规则写在最后：同一个操作碰上好几条规则时，后面的说了算。没写到的一律不许：打开别的程序（`open -a`）、
用 Apple Events 控制别的程序（`osascript -e 'tell application …'`）、钥匙串、Unix 套接字。在"工作目录 + 联网"那一档，
多几行放开 IP 连接（包括 127.0.0.1）和查域名。路径从不写进配置的文字里：每一个都作为参数交过去（`-D WRITE_0=/那个/路径`），
配置里写的是 `(param "WRITE_0")`，所以文件名里有引号、括号、反斜杠、换行都改不了规则，也就没有"转义没写对"这回事。
苹果在 `sandbox-exec` 的说明（man page）里把它标成了"过时"，但它在现在的 macOS 上照样能用，苹果自己的服务也还在用同一套机制。
哪一版 macOS 真的去掉了它，智狐会说沙箱用不了、拒绝执行命令（见下面）。在沙箱里，被挡住的操作得到 `Operation not permitted`，
不能联网时看到的是 `Could not resolve host` 或者 `Couldn't connect to server`。带特殊权限（setuid）的程序，比如 `sudo`、`ps`，
在沙箱里运行不了。`/tmp` 也写不了：它是所有程序共用的（ssh-agent 的套接字就在那里），请用 `$TMPDIR`。
`sandbox-exec` 能不能用，智狐建一个什么都不做的沙箱试一下，结果记一分钟（`SANDBOX_PROBE_TTL`）；
比如智狐自己已经在别的沙箱里运行时，这一下就会失败。

**Linux：bubblewrap（`bwrap`）。** Linux 内核有"命名空间"（namespace）：一个进程可以有它自己看到的文件系统、
自己的进程编号、自己的网络。容器（Docker、Flatpak）就是这么做的，bubblewrap 是 Flatpak 用的那个小程序，不需要 root。
它从一个空的文件系统开始，只把点了名的地方挂进去（`--ro-bind` 只读，`--bind` 能读写），别的地方在沙箱里根本不存在。
参数是这样的：`--unshare-pid`（自己的进程编号：看不到、也杀不掉外面的任何进程，包括智狐自己）；"只在工作目录里"那一档有
`--unshare-net`（自己的一套网络，里面只有一个它自己的、空的 localhost：外网连不上，这台电脑上监听 127.0.0.1 的服务也连不上，
沙箱里自己开的服务，沙箱里照样能连）；`--die-with-parent`；自己的 `/proc`、一个最小的 `/dev`、一个空的 `/tmp`；
系统目录只读（`/bin`、`/lib` 这些指向 `/usr` 的，照样做成链接）；"工作目录 + 联网"那一档把真正的 `/etc/resolv.conf`
（常常是指向 `/run` 里 systemd-resolved 的链接）挂进来，才查得了域名。不许碰的文件夹盖上一个空的、只读的 `tmpfs`，
文件换成 `/dev/null`，最后挂上去，盖住前面的。能写的地方里还没有 `.env` 的，也照样盖上：不然命令可以新建一个，
智狐下次启动时会读它（bwrap 会在那个位置留下一个空文件当挂载点，空的 `.env` 和没有是一回事）。
在沙箱里，看不到的地方是 `No such file or directory`，只能读的地方是 `Read-only file system`，盖住的文件夹（比如 `data/`）
是一个空的、只读的文件夹；连这台电脑上的服务得到的是 `Connection refused`：沙箱自己的 localhost 上什么都没有在听。
bwrap 不是系统自带的，智狐也从来不替你装：没装的话沙箱"用不了"，并给出你那个发行版的安装命令：

```bash
sudo apt install bubblewrap       # Ubuntu、Debian
sudo dnf install bubblewrap       # Fedora
sudo pacman -S bubblewrap         # Arch
```

装好以后不用重启智狐。装了却建不了沙箱的话，智狐把 bwrap 自己的报错给你看。常见的原因是系统不许普通用户建"用户命名空间"：
Ubuntu 24.04 起由 AppArmor 限制（`kernel.apparmor_restrict_unprivileged_userns`），Debian 上看 `kernel.unprivileged_userns_clone`。

**Windows：暂时没有**，这是想清楚以后的决定，不是忘了。不加新依赖能做到的几种办法都评估过（细节写在
[windows.py](../../harness/sandbox/windows.py)）：作业对象（智狐已经用它保证结束一条命令时连孙进程一起结束）能限制进程数、内存、
CPU 时间，管不了文件和网络；受限令牌、低完整性级别写不了你的大部分文件，却照样能读（`~/.ssh` 也能读），照样能联网；
AppContainer 最接近（读不了你的文件、不能联网），可要让它读写工作目录，就得改你磁盘上那个文件夹的权限（ACL），
PowerShell 在里面很多东西用不了，Python 的 `subprocess` 也没法在里面启动进程；Windows 沙盒（Windows Sandbox）是一台虚拟机，
只有专业版、企业版有，启动要好几秒，还看不到工作目录。所以在 Windows 上沙箱会说"用不了"；想要沙箱，可以在 WSL
（Windows 里的 Linux）里运行智狐，再在那里装 bubblewrap。别的系统（FreeBSD……）也没有沙箱。

**用不了就不运行，绝不悄悄退回去。** 实际用的那一档不是"关"、这台电脑上却建不了沙箱的时候（没装 bwrap、Windows、
`sandbox-exec` 出错），每一条命令都在弹出确认卡片之前就被拒绝。模型拿到原因（和所有工具结果一样是中文），
并被告知要把怎么修告诉你，或者请你把这一档改成"关"；设置里的提醒变成红色，任务的编辑器和通道的向导里也写着。
命令绝不会悄悄在沙箱之外运行：你以为它在沙箱里，其实不在，比没有沙箱更糟。

**你看得到的。** 沙箱开着的时候，每条命令的确认卡片多一行，写着它在哪一档里运行、意味着什么（能读写工作目录；能不能联网）。
默认开着、而某个任务或通道选了"关"时，它的卡片上也写明。从来没开过沙箱的人，看到的卡片和以前一模一样。
功能清单（和终端的 `/tools`）里，"执行命令"那一行写着命令在哪一档里运行，或者说这台电脑上用不了沙箱、命令会被拒绝；
它的几类操作里多了"在沙箱之外运行一条命令"（每次都问，或者根本不许），旁边有一个"沙箱"按钮，直接打开设置里的那一节。

**放行一条命令。** 一条确实需要的命令被沙箱挡住时，模型可以加上 `unsandboxed: true` 再执行一次。这一条命令：

- **每次都问你，不管选了哪一档，"全部自动允许"也一样。** 它有自己的一张卡片，带着警告：这条命令能读写你能读写的一切
  （家目录、密钥、别的硬盘），也能联网；还写着这次运行平时用的是哪一档。卡片上没有"不想每次都问？"，也没有任何"不再问"：
  每一条都单独看；
- **在你的对话里（网页和终端）可以请你放行**：你正看着；
- **在定时任务、聊天通道里，只有这次运行自己的那一档不是"全部自动允许"时才能请你放行。** 选了"全部自动允许"，
  就是你说过"别问我"，多半也没人会来回答：问了，要么让它一直等着，要么让你习惯不看就点。所以直接拒绝，告诉模型换一个在沙箱里
  就能做的办法，或者请你改这个任务、这个通道的沙箱。别的几档本来就会问你，所以这一条也照样问；
- 沙箱关着时没有作用：这条命令和别的命令一样照常问。

为什么这样定：第二道关卡最要紧的是"被网页里的一句话带偏的模型，自己出不了沙箱"。出去的门只有一扇：人在一张写着
这条命令的卡片上点"允许"；在你说过"别问我"的运行里，干脆没有这扇门。`unsandboxed` 这个参数一直写在 `run_command` 的说明书里，
沙箱开着、关着都在，因为一段对话里工具的说明书不能变（见[循环](architecture.md#循环)）；对话进行中开关沙箱，变的只是命令怎么运行。

**被挡住以后。** 在沙箱里运行的命令失败了、输出里有被挡住的常见迹象（`Operation not permitted`、`Read-only file system`、
Linux 上的 `No such file or directory`、`Could not resolve host`……）时，结果后面会补一段给模型看的说明（和所有工具结果一样是中文）：
这条命令是在沙箱里、哪一档里运行的，这一档能做什么，接下来可以怎么办（改用工作目录里的文件，或者在允许的地方用
`unsandboxed: true` 再试一次）。

**沙箱挡不住的。**

- **工作目录本身。** 在里面删文件、覆盖文件，沙箱不管；管这个的是"要不要先问你"和文件工具的备份。
- **留下来的东西。** 命令写进工作目录的脚本、git 的钩子和设置（`.git/hooks`、`.git/config`）、`package.json` 里的 scripts、
  Makefile，你以后自己运行它们时，照样以你的身份、在沙箱之外运行。
- **能联网的那一档。** 在"工作目录 + 联网"里，工作目录里的东西可以被发到任何地方。
- **你放行的。** 你允许在沙箱之外运行的那一条，以及沙箱选了"关"的任务和通道。
- **操作系统自己的漏洞。** 圈是内核守的，内核有漏洞时也守不住。
- **macOS 上的文件名和大小。** 命令照样能知道一个文件在不在、有多大（为了让程序能展开路径，`file-read-metadata` 哪儿都放开了），
  从来读不到里面是什么。
- **资源。** 沙箱不限制 CPU、内存和磁盘空间，失控的命令照样能把它们用光（命令的超时和"停止"照样管用）。

**命令行上也能设**（服务器上没有网页时正好用）：

```bash
uv run harness sandbox                     # 现在的情况：哪一档、这台电脑上的沙箱、额外允许的路径
uv run harness sandbox level workspace     # off | workspace | network
uv run harness sandbox allow ~/.nvm        # 加一条只读的路径；加上 --write 是可读写
uv run harness sandbox forget ~/.nvm
uv run harness sandbox check               # 在沙箱里跑几个无害的小试验，告诉你哪些挡住了、哪些放过去了
uv run harness channels sandbox telegram workspace   # 一个通道的：default | off | workspace | network
```

在 PowerShell 里也是同样的命令。存在哪儿：哪一档和额外允许的路径在 `data/settings.json`（`"sandbox": {"level", "paths"}`），
任务的那一档跟着任务存在 `data/tasks.json`，通道的在 `data/channels.json`。网页的接口
（[harness/webui/sandbox_api.py](../../harness/webui/sandbox_api.py)）和别的接口一样都要口令：`POST /api/sandbox/level`、
`/api/sandbox/add`、`/api/sandbox/edit`、`/api/sandbox/delete` 和 `/api/sandbox/reveal`（只显示列表里已有的那几条），
`/api/channel/sandbox`，以及 `/api/task/save` 里的 `sandbox`。能调的数在 [harness/config.py](../../harness/config.py)：
`SANDBOX_MAX_PATHS`（50）、`SANDBOX_PATH_CHARS`（1000）、`SANDBOX_PROBE_SECONDS`（10）、`SANDBOX_PROBE_TTL`（60）。

## 遮住工具结果里的密钥

命令的输出、助手读到的文件、MCP 服务器和网页交回来的东西里，常常一不小心就夹着一把密钥：`cat config.yaml`、`env`、
`git remote -v` 的地址里带着令牌、日志里有一行 `Authorization: Bearer …`。它一旦进了对话，就会发给模型厂商、写进会话文件、
显示在页面和聊天里、跟着导出，而且历史只在末尾追加，进去了就收不回来。所以智狐在结果产生的那一刻就把它换掉：
在它放进结果、交给你的 `PostToolUse` 钩子、到 [harness/agent.py](../../harness/agent.py) 的 `tool_ended` 记录点之前。
模型、历史、存下来的会话、页面、聊天通道、运行记录、你的钩子看到的都是遮好的那一份；历史里前面的内容一个字都不动。做这件事的只有一个文件
[harness/redact.py](../../harness/redact.py)，认三类：

1. **各家密钥的固定格式**：Anthropic、OpenAI、DeepSeek、Gemini/Google、GitHub、Slack、Telegram 和 Discord 的机器人令牌、
   AWS 的访问密钥、Stripe 的正式密钥、JWT、PEM 私钥。导出（[harness/export.py](../../harness/export.py)）和 MCP 的设置页认密钥
   用的是同一张表：密钥长什么样只写在一处。
2. **已经知道的密钥的值**（按值比较，值从不写进日志）：`tools/shell.SECRET_ENV_VARS` 里的变量，环境变量和 `.env` 里
   名字像密钥的（`…_TOKEN`、`…_SECRET`、`…_PASSWORD`、`…_API_KEY`），自定义模型接口的密钥，日志里藏起来的机器人令牌。
   记号上写的是哪个变量（`‹已遮住：OPENAI_API_KEY 的值›`），不写值。
3. **跟在提示词后面、看起来随机的一串**：`password=`、`token:`、`"api_key": "…"`、`--token …`、`Authorization: Bearer …`，
   以及 `scheme://用户:密码@主机` 里的密码。要够长、字母和数字都有、信息量（熵）够高才算
   （[harness/config.py](../../harness/config.py) 里的 `REDACT_MIN_VALUE_CHARS`、`REDACT_MIN_ENTROPY`）。

密钥换成 `‹已遮住：疑似 OpenAI 密钥›`，结果末尾再附一句话，告诉模型遮了几处、不要把记号写回去。记号是写给模型看的，
和别的工具结果一样保持中文。运行记录在每次工具调用上记着遮了几处（`redacted`，时间线和终端 `/trace` 里写"遮住了 2 处密钥"）。

宁可漏，不要错，因为遮错了会把正常的活儿弄坏。git 的提交号、UUID 不遮，一大段 base64（`REDACT_DATA_RUN`，200 个字符）
和 `data:` 地址（比如图片的内容）不遮，像代码的（`request.headers.get`、`process.env.API_KEY`）、占位符
（`${API_KEY}`、`<your-key>`、`****`）、翻页用的令牌（`next_page_token`）也不遮。改文件的工具不肯把记号写回文件里
（`create`、`str_replace`、`insert` 要写的内容里有文件里本来没有的记号）：那会把真正的密钥换成记号。它会请模型只改别的那几行。

这是一层兜底，不是防线：模型要是存心绕开（把密钥换个编码再打印），认不出来。真正挡住密钥的仍然是 `.env` 和 `data/`
碰不到、命令看不到各家的密钥。默认开着；**设置 → 遮住密钥**可以关掉（关之前问一次），存在 `data/settings.json` 里是
`"redact_secrets": false`（`POST /api/redact {"enabled": …}`）。终端、定时任务、聊天通道、子 Agent 用的都是这同一个开关。

## 你的钩子：在固定的时刻运行你自己的脚本

确认卡片让你一步一步地决定做不做。有些规矩你想一次写好、每次自动照办："不许删 `~/Documents` 里的东西""改完 Python 文件就跑一遍格式化"
"每轮答完往日志里记一笔""定时任务跑完发个通知"。**钩子**就是你写的一个小脚本：智狐到了某个固定的时刻运行它，听它怎么说。
做法照着 [Claude Code 的 hooks](https://code.claude.com/docs/en/hooks)：同样的事件名、同样的 matcher 概念、同样从标准输入交一份 JSON、
同样用退出码和标准输出里的 JSON 回答。Claude Code 的钩子一般只要改一下工具的名字（`run_command`、`str_replace_based_edit_tool`，
而不是 `Bash`、`Edit`）就能用。

| 事件 | 什么时候 | 钩子能做什么 |
|---|---|---|
| `PreToolUse` | 一个工具马上要执行，**已经过了确认这一关**（你点了允许，或者这一档本来不用问） | 拦下它：这次调用照样有结果，模型看到"这次调用被用户的钩子拦下了……"和你写的原因。或者要你再确认一次（哪一档都问）。不能改参数，也不能让一个本来要问的操作不问 |
| `PostToolUse` | 工具做完了 | 在这一条工具结果后面接一小段话交给模型（最多 `HOOK_NOTE_CHARS`，2000 字）。它看到的结果和模型将看到的一模一样 |
| `UserPromptSubmit` | 你发了一条消息、它进历史之前 | 拦下它（原因给你看，什么都不发、不花钱），或者给这条消息附一段说明交给模型 |
| `Stop` | 一轮答完了（答完、被截断、被拒绝、步数用完） | 只是通知：跑格式化、记日志、发通知。你点了停止、出错了、消息被钩子拦下的那一轮不跑 |

**约定。** 钩子从标准输入收到一个 JSON 对象：`hook_event_name`、`hook_name`、`session_id`、`cwd`、`permission_mode`（`ask` / `readonly` /
`edits` / `all`）、`source`（`chat`、`task` 或 `channel`）、`provider`、`model`、`agent_type`（只有子 Agent 里才有）、`test`（只有试运行才有）；
工具的两个事件多 `tool_name`、`tool_group`（属于哪种功能，和功能清单一样）、`tool_input`、`tool_use_id`、`mcp_server`，
`run_command` 还多一个 `command_cwd`（命令在哪个目录里运行，之前的 `cd` 可能改过它；`cwd` 照旧是工作目录）；
`PostToolUse` 再多 `tool_response`、`is_error`、`duration_ms`；`UserPromptSubmit` 多 `prompt`、`attachments`；`Stop` 多
`last_assistant_message`、`ended`、`stop_hook_active`（永远是 false）。然后：

- **退出码 0**：成功。标准输出可以是一个 JSON 对象，字段和 Claude Code 的一样：`{"decision": "block", "reason": "…"}`、
  `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny" | "ask", "permissionDecisionReason": "…"}}`、
  `{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "…"}}`（`UserPromptSubmit` 也一样），
  以及给你看一句话的 `{"systemMessage": "…"}`。不是 JSON 的输出只有 `UserPromptSubmit` 当成附给模型的说明。
  `"allow"` 不认：钩子只能让确认更严，不能放宽；`updatedInput`、`updatedToolOutput`、`continue: false` 也不认，试运行时会告诉你。
- **退出码 2**：拦下，标准错误就是原因（`PostToolUse` 的接在结果后面，`Stop` 的给你看）。
- **别的退出码**、超时（默认 `HOOK_TIMEOUT` 30 秒，最长 `HOOK_MAX_TIMEOUT`）、程序启动不了：钩子自己出错了。提醒你一句，照常继续；
  除非这个钩子写了 `"onFailure": "block"`，那么 `PreToolUse` 和 `UserPromptSubmit` 上的出错当成拦下：管安全的钩子坏了的时候宁可不做。

几个钩子对上同一个时刻时，按文件里的顺序一个接一个地跑（Claude Code 是同时跑），第一个拦下的说了算。
[同一步里的几次读取一起做](tools-and-permissions.md#同一步里的几次读取一起做)的时候，钩子也从不同时跑：有 `PreToolUse` 钩子要看的调用单独跑，
`PostToolUse` 每个调用跑一次，在它的结果出来以后、按调用的顺序。
每跑一次钩子，[运行记录](architecture.md#看懂一轮对话运行记录)里就有一段。

**在哪儿跑。** 你的对话、定时任务、聊天通道，默认三处都跑；`runsIn` 可以改小。为什么没人看着的运行里也默认跑：
钩子只能让事情更严，多跑不会让哪里变得不安全；反过来，一个管安全的钩子要是在定时任务里悄悄不算数，正好是没人看着、最需要它的时候。
子 Agent 的工具调用也跑工具前后的两个钩子（带着 `agent_type`）；它的"一轮"不是你发的消息，所以不跑 `UserPromptSubmit` 和 `Stop`。
"重新生成"不再跑 `UserPromptSubmit`：那条消息原样留在历史里。

**历史仍然只在末尾追加。** 钩子的话只放进此刻正在生成的那条工具结果、那条用户消息里，包成 `<hook_note name="…">` 或
`<hook_context name="…">`；历史里已有的内容一个字都不改，系统提示词和工具清单也不变。

**安全。** 保存一个钩子，就是让智狐在你的电脑上运行这个命令，所以它和本地的 MCP 服务器守同样的规矩：
只有带着这次运行的口令的页面（**扩展 → 钩子**）或者命令行（`harness hooks …`）能保存；命令在工作目录里用参数列表启动，从不经过 shell
（`command` 里有空格又没有 `args` 的会被退回，并说明要怎么写）；环境里去掉了各家模型的密钥和机器人的 token，引用它们的
（`$ANTHROPIC_API_KEY`、`${…}`、`%…%`、`$env:…`）保存时退回、运行前再查一遍；模型没有任何能添加或修改钩子的工具，
`data/hooks.json` 和 `.env`、`mcp.json` 一样在文件工具的 protected 名单里；文件权限 600、导出时遮住，页面上的参数里像令牌的部分也遮住。
每次运行的钩子各是各的：对话里点"停止"只结束对话自己的钩子，停下一个任务只结束那个任务的。
[命令的沙箱](#命令的沙箱命令只碰得到该碰的地方)开着的时候，钩子和这次运行的命令关在同一个沙箱里、同一档：脚本是你写的，可它读到的 JSON 里有模型给的参数、
工具的输出，写得不小心的脚本（拿参数去拼一条命令）就会把手借给被网页带偏的模型。钩子自己的程序和脚本（写成绝对路径的）在里面可以读；
工作目录之外的别的地方（写日志、联网发通知），要在沙箱的设置里加上路径，或者选"工作目录 + 联网"。沙箱开着、这台电脑上却用不了的话，
钩子根本不运行（算出错，`onFailure` 照常起作用），绝不悄悄在沙箱之外运行。

```json
{
  "hooks": {
    "no-rm": {"event": "PreToolUse", "matcher": "shell", "command": "python3", "args": ["/Users/me/hooks/no_rm.py"],
              "timeout": 10, "onFailure": "block"},
    "fmt":   {"event": "PostToolUse", "matcher": "files", "command": "python3", "args": ["scripts/format.py"]},
    "done":  {"event": "Stop", "command": "python3", "args": ["/Users/me/hooks/notify.py"], "runsIn": ["task"]}
  }
}
```

matcher 写功能的名字（`shell`、`files`、`browser`、`mcp`……，和功能清单一样）、工具的真名（`run_command`、`browser.left_click`、
`mcp__github__create_issue`）或者带 `*` 的通配（`mcp__github__*`），用 `|` 隔开；空的或 `*` 是全部工具。不认正则表达式：够用，
也写不出会卡死的正则。Claude Code 的设置按事件分组，这里摊平成"名字 → 一个钩子"，页面上才能一个个地开关、编辑、试运行。命令行里：

```bash
uv run harness hooks add no-rm --event PreToolUse --matcher shell --on-failure block -- python3 /Users/me/hooks/no_rm.py
uv run harness hooks list            # 还有 enable / disable / remove / test 名字
```

```powershell
uv run harness hooks add no-rm --event PreToolUse --matcher shell --on-failure block -- python C:\Users\me\hooks\no_rm.py
```
