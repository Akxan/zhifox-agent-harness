[English](../en/windows.md) | **简体中文**

[智狐](../../README.zh-CN.md) › [文档](../../README.zh-CN.md#文档) › 在 Windows 上使用

# 在 Windows 上使用

智狐能在 Windows 上运行。每次推送都会在 GitHub 的 `windows-latest`（Windows Server）上和 Ubuntu、macOS 一起跑一遍测试；
还没有在 Windows 10、11 的桌面电脑上手动用过，那里要是有不一样的地方，请告诉我们。只是想用的话，一行命令装上
（见[一行命令装上](../../README.zh-CN.md#一行命令装上)；数据和密钥放在 `%LOCALAPPDATA%\ZhiFox\`）：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # 装 uv，装好后另开一个 PowerShell 窗口
winget install --id Git.Git -e                                                        # 还没有 Git 的话先装上（uv 用它下载）
uv tool install git+https://github.com/Akxan/zhifox-agent-harness
harness --web
```

（不想装 Git 的话，装[发布版](https://github.com/Akxan/zhifox-agent-harness/releases)附着的安装包（见[一行命令装上](installation.md#一行命令装上更多说明)里的"装某一个发布版"），
或者用 `uv tool install https://github.com/Akxan/zhifox-agent-harness/archive/refs/heads/main.zip` 装最新的代码。）
想改代码的话，改成 clone 下来：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # 装 uv，装好后另开一个 PowerShell 窗口
git clone https://github.com/Akxan/zhifox-agent-harness.git
cd zhifox-agent-harness
uv sync
uv run harness --web
```

（没装 Git 的话，从 GitHub 下载 ZIP 解压也行。）别的用法和另外两个系统一样，不同的是这几处：

| | 在 Windows 上 |
|---|---|
| 快捷键 | Mac 上写 ⌘ 的地方都是 Ctrl，⌥ 是 Alt（见下表）。按打开页面的那台电脑（浏览器）判断 |
| "在访达中显示" | **在文件资源管理器中显示**（`explorer /select,`）；Linux 上是"在文件管理器中显示"。按智狐所在的那台电脑判断，因为窗口是在那台电脑上打开的 |
| 执行命令（`run_command`） | 在 **PowerShell** 里运行：装了 PowerShell 7（`pwsh`）就用它，没装就用系统自带的 Windows PowerShell 5.1。工具说明和系统提示词里写明是哪一种，模型才会写 `Get-ChildItem` 而不是 `ls -la`。不运行你的 `$PROFILE`，执行策略照你的设置，输出按 UTF-8 读；"停止"或者超时会连同它启动的所有程序一起结束（Windows 的作业对象）。目录会从一条命令接到下一条（`Set-Location` 会保留，见[命令记得自己在哪个目录](tools-and-permissions.md#命令记得自己在哪个目录)） |
| **自动允许只读命令** | 暂时一条命令都不放过：只读的判断认得的是 bash，不是 PowerShell，所以每条命令照样问，你自己加的"不用问的命令"也不算（"要不要先问你"的窗口、那个清单窗口和能力清单里都写着）。这一档读工作目录之外的文件照样不问 |
| 只有你自己能读 | Windows 没有 600、700 这种权限。智狐把 `data\` 和 `.env` 从上一层继承来的权限去掉，只留你自己的账号、SYSTEM 和管理员（见[数据存在哪里](data-and-privacy.md)） |
| 浏览器 | Chrome；没装 Chrome 时用每台 Windows 10、11 都有的 Microsoft Edge。照样只走管道、不开端口（见[浏览器：智狐自己的一个 Chrome](browser-and-computer-use.md#浏览器智狐自己的一个-chrome)） |
| 后台服务 | 用 **任务计划程序**，不是 launchd、systemd：命令还是那几条 `uv run harness service …`（见[在后台一直运行](background-and-servers.md#在后台一直运行)） |
| 操作整台电脑 | 暂时用不了。设置和能力清单里写着"Windows 上暂时不支持" |

网页界面的快捷键：

| 做什么 | macOS | Windows 和 Linux |
|---|---|---|
| 发送 / 换行 | Enter / Shift+Enter | Enter / Shift+Enter |
| 搜索会话 | ⌘F 或 ⌘K | Ctrl+F 或 Ctrl+K |
| 设置 | ⌘, | Ctrl+, |
| 换模型 | ⌘. | Ctrl+. |
| 编辑窗口里保存（记忆、任务、Skill、Agent、MCP 服务器、提示词） | ⌘Enter | Ctrl+Enter |
| 查看器里缩放图片 | ⌘+ / ⌘− / ⌘0，⌘ 加滚轮 | Ctrl++ / Ctrl+− / Ctrl+0，Ctrl 加滚轮 |
| 收起面板、关上窗口（有没保存的修改时先问一次） | Esc | Esc |

Windows 上暂时还不支持的：

- 操作整台电脑（还没有 Windows 上截图、动鼠标键盘的那一套）。
- 命令的沙箱（见[命令的沙箱](sandbox-and-hooks.md#命令的沙箱命令只碰得到该碰的地方)）：Windows 上暂时没有可靠的办法，打开了沙箱的话每条命令都会被拒绝，
  页面上写着为什么。想用的话，在 WSL 里运行智狐，再装上 bubblewrap。
- 自动允许只读命令，见上面。
- 注销、关机时好好地收尾：Windows 会直接结束进程，正在进行的那一轮可能存不上。在终端里按 Ctrl-C、Ctrl+Break，
  以及 `uv run harness service stop`，都会好好地停下；直接关掉终端窗口不会。
- 后台服务在你注销时会停下（它以你的身份运行，不保存密码）。
