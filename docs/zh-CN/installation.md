[English](../en/installation.md) | **简体中文**

[智狐](../../README.zh-CN.md) › [文档](../../README.zh-CN.md#文档) › 安装和更新

# 安装和更新

安装的命令在[快速开始](../../README.zh-CN.md#快速开始)里。这一页是其余的部分：一行命令装上的智狐怎么更新和卸载、怎么装某一个发布版、数据和密钥放在哪儿、怎么知道有新版本，以及在别的目录里启动它。

## 一行命令装上：更多说明

更新和卸载（包名是 `harness-assistant`，装上的命令是 `harness`）：

```bash
uv tool upgrade harness-assistant      # 更新到 GitHub 上的最新版
uv tool uninstall harness-assistant    # 卸掉程序；你的数据文件夹原样留着
```

装成了[后台服务](background-and-servers.md#在后台一直运行)的话，更新以后运行一次 `harness service restart`。
智狐也可以在有新版本时告诉你，并且告诉你这几行里哪一行适合你的装法：见[更新](#更新)（默认关着）。

**装某一个发布版。** [快速开始](../../README.zh-CN.md#一行命令装上)里那几行装的是 `main` 上最新的代码。每一个[发布版](https://github.com/Akxan/zhifox-agent-harness/releases)还附着打好的安装包，
装的就是那一版，不需要 Git，macOS、Linux、Windows 都一样：

```bash
uv tool install https://github.com/Akxan/zhifox-agent-harness/releases/download/v0.2.0/harness_assistant-0.2.0-py3-none-any.whl
```

有 Git 的话，`uv tool install git+https://github.com/Akxan/zhifox-agent-harness@v0.2.0` 从那一版的 tag 装，结果一样。
这两种装法都会停在那一版（`uv tool upgrade` 会说没有可更新的）：要换到别的发布版，运行那一版的这一行并加上 `--force`。
`harness --version` 显示你装的是哪一版（从仓库里运行时还显示是哪个提交）。

**数据和密钥在哪儿。** 这样装的时候，程序在 uv 自己的文件夹里，`uv tool upgrade` 和 `uv tool uninstall` 会替换、删掉它。
所以会话、记忆、设置放在它外面、你这个账号自己的一个文件夹里，API 密钥（`.env`）也在这个文件夹里：

| 系统 | 数据文件夹（`.env` 在里面） |
|---|---|
| macOS | `~/Library/Application Support/ZhiFox/` |
| Linux | `$XDG_DATA_HOME/zhifox/`，默认是 `~/.local/share/zhifox/` |
| Windows | `%LOCALAPPDATA%\ZhiFox\`，比如 `C:\Users\你\AppData\Local\ZhiFox\` |

这个文件夹只有你的账号能读，和 clone 时的 `data/` 一样（见[数据存在哪里](data-and-privacy.md)）。填密钥最省事的办法是网页界面的
**设置**，它会写进这里的 `.env`；也可以自己建这个文件，写上 `ANTHROPIC_API_KEY=…` 这样的行（见 [.env.example](../../.env.example)）。
`.env` 放在数据文件夹里，并不会因此更容易被碰到：助手的文件工具打不开它，命令的沙箱碰不到它，**导出全部数据**也不带它，
和 clone 时一样。`HARNESS_DATA_DIR`、`HARNESS_ENV_FILE` 照样能把它们换到别处。

为什么是这几个位置：Windows 上用的是"本地"的 AppData，不是"漫游"的那个：漫游的配置文件每次登录、注销都会把整个文件夹同步一遍，
而智狐的数据（附件、浏览器的配置文件夹、留底）不小，有一部分还只属于这台电脑。Linux 上 `.env` 留在数据文件夹里，不放进 `~/.config`：
很多人把 `~/.config` 整个放在公开的 dotfiles 仓库里。

**备份。** **设置 → 导出全部数据** 会把数据文件夹打成一个不含密钥的 zip（见[备份和恢复](data-and-privacy.md#备份和恢复)）。直接复制整个文件夹也行，
但那一份里有 `.env`，要自己收好。

**和 clone 的方式有什么不同：**

- 命令前面不写 `uv run`：`harness --web`、`harness service install`、`harness channels setup telegram`。
  这几页说明里写 `uv run harness …` 的地方，换成 `harness …`。
- 用不着[在别的目录里使用](#在别的目录里使用)里的 `zhifox` 快捷命令：`harness` 本来就在哪个文件夹里都能用，你在哪个文件夹里启动它，
  那里就是助手的工作目录。
- 数据文件夹是上面表里的那个，不是项目里的 `data/`：同一台电脑上 clone 的和装上的两个智狐，会话和密钥不共用。
  想从 clone 的搬过来：先退出智狐，把 clone 里 `data/` 文件夹里面的东西和 `.env` 拷进那个文件夹
  （或者把 `HARNESS_DATA_DIR`、`HARNESS_ENV_FILE` 指向 clone 里的那两样）。
- 飞书和钉钉要另外装它们的 SDK，办法是带上 extra 重新装一次：
  `uv tool install --force "harness-assistant[feishu,dingtalk] @ git+https://github.com/Akxan/zhifox-agent-harness"`
  （网页上和 `harness channels setup` 会照你已经装了的，给出完整的那一行）。
- [评测](evals.md)要在 clone 下来的仓库里跑：任务和演习用的假服务器都在仓库里，不在装好的包里。`harness eval` 会这样告诉你。

## 更新

怎么更新看你是怎么装的：从仓库里运行的，在项目文件夹里先 `git pull`、再 `uv sync`；一行命令装上的，见[一行命令装上](#一行命令装上更多说明)。
已经开着的智狐在重启之前用的还是旧代码（后台服务用 `harness service restart` 重启）。

**有新版本时告诉你（默认关着）。** 智狐可以每天看一次有没有新的正式版本。你打开之前它一直关着：在**设置 → 检查新版本**里打开
"有新版本时告诉我"这个开关，或者在命令行里：

```bash
harness updates on       # 打开，并且马上查一次（从仓库里运行是 uv run harness updates on）
harness updates          # 上一次查到了什么（和 harness updates status 一样）；不联网
harness updates check    # 现在就问 GitHub 一次
harness updates off
```

PowerShell 里也是这几行。它到底发了什么：最多每 24 小时（上一次没查成的话隔 3 小时）一个请求，

```
GET https://api.github.com/repos/Akxan/zhifox-agent-harness/releases/latest
```

不登录、不带 cookie，User-Agent 只写 `ZhiFox`（不写版本号、不写系统）。GitHub 看得到的是你的 IP 地址和请求的时间。
别的什么都不发：不发你用的版本、设置、会话和密钥，也没有任何编号能把两次检查连起来。这是智狐唯一一个自己发出去的请求；
别的请求都是因为你叫它做了什么（发一条消息、点"同步模型"、连上一个聊天通道）。所以你不说，它就不开。
草稿和预发布不算；打开的时候马上查一次。

谁来查：网页界面和 `harness serve`，在一个后台线程里。终端界面启动时只看上一次的结果；开着这个设置、上一次的结果又超过了一天，
就在后台查一次，不耽误你打字，查到新版本在你下一次输入之前说一声。`harness doctor` 只显示上一次查到的，永远不联网。
结果存在数据文件夹里的 `updates.json`（什么时候查的、最新的正式版本、上一次没查成的原因）。它只是一份缓存：
**导出全部数据**不带它，恢复备份也不写它，删掉了也没关系。

知道有新版本以后，右上角的齿轮上多一个小圆点，**设置 → 检查新版本**里写着是哪一版、它的发布页，以及按*你这一种*装法怎么升级：

| 智狐是怎么装的 | 让你运行什么 |
|---|---|
| 从仓库里运行（`git clone`） | 在项目文件夹里先 `git pull`，再 `uv sync`。没有 `.git` 的文件夹（下载的 zip）会让你去下载新版本的源代码 |
| `uv tool install git+https://…`，没写版本 | `uv tool upgrade harness-assistant` |
| `uv tool install git+https://…@v0.2.0`，钉在了某一版上 | `uv tool install --force git+https://github.com/Akxan/zhifox-agent-harness@v0.3.0` |
| 发布页上的 wheel、zip，或者看不出来 | `uv tool install --force https://github.com/Akxan/zhifox-agent-harness/releases/download/v0.3.0/harness_assistant-0.3.0-py3-none-any.whl`，哪种装法都能用。看不出你是怎么装的时候，会明说看不出来，并且提一句也可以用 `uv tool upgrade` |

它是看 `direct_url.json` 分出来的：uv 和 pip 从一个地址装包的时候都会写这个标准的文件（PEP 610）。每条命令单独一行，
不用 `&&` 连起来（Windows PowerShell 5 不认它），所以 zsh、bash 和 PowerShell 里都一样。作为后台服务运行的，还会加上
`harness service restart`。在 Windows 上，它会让你先停下开着的智狐（`harness service stop`，装好以后 `harness service start`）：
Windows 换不掉一个正在运行的程序的文件。

没查成的时候（没有网、超时、GitHub 对不登录的请求每个 IP 每小时只给 60 次、回答看不懂），智狐在日志里写一行，
设置里写没能检查和原因，别的什么都不耽误；隔 3 小时再试。规矩都在 [harness/updates.py](../../harness/updates.py) 里。
问的地址只能用环境变量 `HARNESS_UPDATE_API` 改（只认 `https://`，明文的 `http://` 只认这台电脑自己），测试和演示把它指向本地的假服务器：
在 `uv run python tests/demo_web.py` 里打开这个开关，不联网就能看到"有新版本"的样子（`--release 0.9.0` 指定假服务器说的是哪一版；
不写的话，比你现在的高一个小版本）。

## 在别的目录里使用

助手的"工作目录"是你启动它时所在的目录。一行命令装上的 `harness` 本来就在哪个文件夹里都能用：`cd` 到那里运行 `harness`
（或者 `harness --workspace /某个/文件夹`）就行。下面说的是 clone 下来的情况。想让它在别的目录里工作，先 `cd` 到那里，再运行：

```bash
uv run --project "/这个项目的完整路径" harness
```

密钥和数据照样用项目自己的 `.env` 和 `data/`。想在哪儿都只敲一个 `zhifox`，在 shell 的启动文件里加一个快捷方式：

```bash
# macOS 和 Linux：写进 ~/.zshrc（或者 ~/.bashrc），然后另开一个终端
alias zhifox='uv run --project "/这个项目的完整路径/zhifox-agent-harness" harness'
```

```powershell
# Windows：写进 PowerShell 的配置文件。`notepad $PROFILE` 打开它（还没有的话先运行
# `New-Item -ItemType File -Path $PROFILE -Force`），然后另开一个 PowerShell 窗口
function zhifox { uv run --project "C:\这个项目的完整路径\zhifox-agent-harness" harness @args }
```

之后在哪个文件夹里都能用 `zhifox`、`zhifox --web`、`zhifox service status`（`@args` 把后面的参数原样传过去）。
