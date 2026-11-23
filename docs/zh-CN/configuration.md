[English](../en/configuration.md) | **简体中文**

[智狐](../../README.zh-CN.md) › [文档](../../README.zh-CN.md#文档) › 配置和排查问题

# 配置和排查问题

## 配置

写在 `.env` 里，都是可选的：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HARNESS_MAX_SEARCHES` | `5` | 每条消息最多联网搜索几次、读几次网页（Claude 的读网页，和别家模型用的智狐自己的 `web_fetch`），`0` 表示不限制 |
| `HARNESS_WEB` | `1` | 设为 `0` 完全关闭 Anthropic 服务器上的联网搜索。这时 Claude 改用智狐自己的 `web_fetch`（在 **设置 → 读网页** 里能关掉） |
| `HARNESS_EFFORT` | `medium` | Claude 思考的力度：`low` / `medium` / `high` / `xhigh` / `max` |
| `HARNESS_DATA_DIR` | 项目里的 `data/`；一行命令装上的，是你这个账号的那个文件夹（见[一行命令装上](installation.md#一行命令装上更多说明)） | 记忆、待办、设置等数据存放的目录 |
| `HARNESS_PROVIDER`、`HARNESS_MODEL` | `anthropic`、`claude-opus-5-5` | 还没在界面上选过模型时用哪一个。选过之后以界面上的选择为准 |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | 用你的 OpenAI 密钥转语音时发到哪里（OpenAI 的 SDK 发对话请求也认它）。只能是 `https://`，或者连这台电脑自己的明文 `http://` |
| `HARNESS_CHROME` | 自动查找 | 浏览器用哪个 Chrome（或者 Chromium、Edge、Brave）的完整路径。"设置 → 浏览器"里填了路径的话，以那里为准 |
| `HARNESS_UPDATE_API` | `https://api.github.com` | [检查新版本](installation.md#更新)去哪儿问最新的发布版（测试和演示把它指向本地的假服务器）。只认 `https://`，明文的 `http://` 只认这台电脑自己 |

## 出了问题：先运行 harness doctor

哪里不对劲的时候，先运行自检。它把智狐在这台电脑上的情况放在一屏里；想告诉我们你的电脑上有什么不一样，这也是最快的办法，
Windows 上尤其如此（Windows 只在 GitHub 的测试机上跑过，还没在真的桌面电脑上用过）。在智狐的文件夹里：

```bash
# macOS 和 Linux（zsh、bash）
uv run harness doctor                       # 给人看的表格
uv run harness doctor --json > doctor.json  # 给程序看，或者找人帮你看时当附件发过去
echo $?                                     # 有一项有问题是 1，都没问题是 0
```

```powershell
# Windows（PowerShell）
uv run harness doctor
uv run harness doctor --json | Out-File -Encoding utf8 doctor.json
$LASTEXITCODE
```

终端界面里输入 `/doctor` 也一样。每一行是三种状态之一：**好**、**有问题**、**没用上**（没配置的功能不算问题）。
有问题的都跟着一句怎么修，按你的 shell 写：macOS 上是 zsh 的，Linux 上是 bash 的，Windows 上是 PowerShell 的。
比如数据目录别人也能读：macOS、Linux 上给的是 `chmod 700 '…/data'`，Windows 上给的是智狐自己收紧权限时用的那条
`icacls '…\data' /inheritance:r /grant:r '*S-1-5-21-…:(OI)(CI)F' …`。

| 一节 | 看什么 |
|---|---|
| 环境 | Python 的版本和路径（是不是项目里的 `.venv`）、智狐的版本和 git 提交、有没有新版本（只看上一次[检查新版本](installation.md#更新)存下的结果，不联网）、系统、PATH 里有没有 `uv` |
| 目录 | 工作目录能不能写；`data/` 和 `.env` 是不是只有你自己的账号能读：macOS、Linux 上看 700 和 600（还要属于你，用 `sudo` 运行过一次的话会变成 root 的），Windows 上看访问控制列表（用 `icacls /save` 读，从不改） |
| 占用 | 数据目录现在是不是被另一个智狐占着（网页界面、`harness serve`、后台服务、`channels pair`），进程编号是多少 |
| 密钥 | 哪几家有密钥、从哪儿来（`.env`、环境变量、Anthropic 的登录信息）；环境变量设成了空的、把 `.env` 里的密钥盖住了；环境变量里的和 `.env` 里的不一样；现在选的模型；自定义接口 |
| 这台电脑上能不能用 | 找不找得到 Chrome；命令的沙箱在这台电脑上能不能用（以及谁要用它：对话、哪个定时任务、哪个聊天通道）；操作整台电脑：后端，以及 macOS 上"录屏与系统录音""辅助功能"两项权限（只做不弹窗的预检）；后台服务：装没装、在不在跑、是不是出错退出了、有没有只在终端里设置的密钥 |
| 扩展 | MCP 服务器、聊天通道、钩子、定时任务各有几个、开着几个；要运行的程序没装的 MCP 服务器和钩子（`npx`、`uvx`……）；没填全、没配对、缺 SDK 的聊天通道；读不出来的任务文件 |
| 网络 | 代理和改过的接口地址（只写协议、主机和端口）、指向外面的明文 `http://` 地址；联网搜索开没开 |

它只看不改：不改任何文件、权限和设置，不启动任何东西（MCP 服务器、Chrome、截图都不碰），也不发任何网络请求。
密钥只说有没有，不写出密钥，连最后几位也不写；`--json` 的结果里没有密钥、口令和带口令的网址，但有你的路径（路径里一般有你的用户名）。
