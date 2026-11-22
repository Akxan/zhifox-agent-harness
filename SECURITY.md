# Security policy

**English** | [简体中文](#简体中文)

ZhiFox is a personal assistant that runs on your own computer, under your own account. With your approval it runs
commands, changes files, opens websites and, if you turn it on, operates your desktop. So the line between "the model
asked and you said yes" and "it happened without asking" matters a lot, and reports that move that line are very
welcome.

## Supported versions

ZhiFox has no numbered releases yet. Fixes land on the `main` branch, and only the latest `main` is supported: update
with `git pull` and `uv sync` before reporting, and say which commit you tested.

## Reporting a vulnerability

Please **don't open a public issue** for a security problem. Report it privately through GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability):
on this repository, open the **Security and quality** tab (in the **⋯** menu if you don't see it) and click
**Report a vulnerability**. The report and the discussion stay private between you and the maintainer until a fix is
published.

A useful report says:

- the commit you tested, your operating system and which interface (web UI, terminal, `harness serve`, a chat channel);
- the steps to reproduce, and what happened compared with what should have happened;
- what an attacker gains, and what they need first (a web page you visit, a file the assistant reads, a message in a chat…).

Please use made-up keys and the local fake servers to reproduce: `uv run python tests/demo_web.py` starts the web UI
with a fake model and a temporary data folder, and costs nothing. Never include a real API key, bot token or someone
else's data in a report. Give the maintainer a reasonable time to fix the problem before you talk about it publicly.

## Security model in brief

These are the safeguards ZhiFox promises. A way around any of them is a vulnerability.

- **The web UI is for this computer only.** The server listens on `127.0.0.1` only. Every request must carry the token
  from the page address and pass Host and Origin checks, so other websites open in your browser and other accounts on
  the computer can't drive it. The page loads nothing from outside, its scripts run under a nonce-based Content
  Security Policy, and text from the model or a tool is inserted as text, never parsed as HTML. See
  [One loop, two interfaces](README.md#one-loop-two-interfaces) and, for the background service's token,
  [How launchd and systemd keep it running](README.md#how-launchd-and-systemd-keep-it-running).
- **You are asked first.** By default every command and every file write waits for your approval. The looser modes
  only decide when not to ask; they never open anything else: `.env` is off-limits in every mode, reading ZhiFox's own
  `data/` asks in every mode except Allow everything, the read-only check is deliberately conservative, and tasks the
  assistant creates always ask. Your hooks can make a step stricter, never looser. See
  [Permissions](README.md#permissions) and [Hooks](README.md#hooks-your-own-scripts-at-fixed-moments).
- **An optional sandbox for commands**, as a second safeguard (macOS `sandbox-exec`, Linux bubblewrap). When a level is
  on, a command can't reach `data/` or `.env`, and where the sandbox can't work ZhiFox refuses to run the command rather
  than running it outside. See [Sandbox for commands](README.md#sandbox-for-commands-what-a-command-can-reach).
- **Secrets in tool results are masked** before they reach the model, the history, the page, your hooks or the run
  trace. This is a safety net, not a guarantee: a key the model deliberately rewrote won't be recognized. See
  [Masking secrets in tool results](README.md#masking-secrets-in-tool-results).
- **Keys don't reach the browser.** API keys, bot tokens and MCP secrets are never sent to the page (it shows only the
  last 4 characters); commands, MCP servers and hooks don't get the model API keys in their environment; exports mask
  them. See [Data and privacy](README.md#data-and-privacy).
- **Chat channels answer their paired owner only**; messages from anyone else and from group chats are ignored. See
  [Chat channels](README.md#chat-channels-talking-to-zhifox-from-your-chat-app).
- **Browser use and computer use have their own rules**: ZhiFox's Chrome is driven over a pipe with no debugging port,
  passwords and card details always ask, downloads are refused, and computer use is off until you accept its risks. See
  [Browser use](README.md#browser-use-a-chrome-of-its-own) and
  [Computer use](README.md#computer-use-your-whole-desktop-advanced).

## What is not a vulnerability

- **Anything that follows from a choice you made**: what happens after you choose Allow everything (`/auto all`, the
  web UI's mode button, a task or chat channel set to it), or after you approve a step yourself, even if a web page or
  a file misled the model into proposing it. The approval is the boundary.
- **Prompt injection on its own.** A page or file that talks the model into *asking* for something harmful is a known
  limit of every agent; that's why you are asked. It becomes a vulnerability when the step then runs without asking,
  or reaches something a safeguard above says it can't.
- **Attacks that need code already running as your account**, or physical access to the computer. Such a program can
  read `data/` and `.env` directly.
- **Exposing the web UI yourself**: a reverse proxy, port forwarding or any change that makes it listen beyond
  `127.0.0.1`. To reach it from another machine, use an SSH tunnel.
- **What you install or connect**: MCP servers, skills, custom agents, hooks and custom model endpoints run with the
  access you give them.
- **Limits that are documented**, such as no command sandbox on Windows, or a secret that masking doesn't recognize
  (please open an ordinary issue for the latter, with a made-up example).
- **Problems in a model provider or in a dependency itself**; please report those upstream. If ZhiFox uses a dependency
  in a way that makes it exploitable, that is in scope.

---

## 简体中文

智狐是运行在你自己电脑上、用你自己的账户运行的个人助手。经你同意，它会执行命令、改文件、打开网页，打开相应的选项后还会操作整台电脑。
所以"模型问了、你同意了"和"没问就做了"之间的那条线很要紧，能让这条线挪动的问题非常欢迎报告。

**支持的版本：** 还没有带编号的发布版。修复都在 `main` 分支上，只支持最新的 `main`。报告之前请先 `git pull`、`uv sync`，并写明你测的是哪个提交。

**怎么私下报告：** 安全问题请**不要开公开的 issue**，用 GitHub 的
[私下报告漏洞](https://docs.github.com/zh/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)：
在这个仓库的 **Security and quality** 标签页里（看不到的话在 **⋯** 菜单里）点 **Report a vulnerability**。修复发布之前，报告和讨论只有你和维护者看得到。
请写明测的提交、操作系统和界面（网页、终端、`harness serve`、聊天通道），复现的步骤，实际发生了什么、本该怎样，攻击者能得到什么、
事先需要什么（你打开的一个网页、助手读到的一个文件、聊天里的一条消息……）。复现时请用编造的密钥和本地的假服务器：
`uv run python tests/demo_web.py` 用假模型和临时的数据目录启动网页界面，不花钱。报告里不要放真的 API 密钥、机器人 token 或别人的数据。
公开讨论之前，请给维护者留出合理的修复时间。

**安全模型简述**（绕过其中任何一条都算漏洞）：

- **网页界面只给这台电脑用**：服务器只监听 `127.0.0.1`；每个请求都要带页面地址里的口令，并通过 Host 和 Origin 检查，浏览器里开着的别的网站、
  这台电脑上的别的账户都驱动不了它；页面不加载任何外部资源，脚本只在带 nonce 的内容安全策略下运行，模型和工具输出的文字只当作文字放进页面，
  从不当作 HTML 解析。见 [一个循环，两种界面](README.zh-CN.md#一个循环两种界面)，后台服务的口令见
  [launchd 和 systemd 是怎么让它一直开着的](README.zh-CN.md#launchd-和-systemd-是怎么让它一直开着的)。
- **先问你**：默认每条命令、每次改文件都等你确认。宽松的几档只决定什么时候可以不问，不会放开别的：`.env` 在任何一档都碰不了，读智狐自己的
  `data/` 除了"全部自动允许"都要问，"只读的命令"的判断故意很保守，助手自己添加的定时任务一律要问；你的钩子只能更严，不能放宽。
  见 [权限规则](README.zh-CN.md#权限规则) 和 [你的钩子](README.zh-CN.md#你的钩子在固定的时刻运行你自己的脚本)。
- **可选的命令沙箱**是第二道关卡（macOS 的 `sandbox-exec`、Linux 的 bubblewrap）：开着的时候命令碰不到 `data/` 和 `.env`；
  这台电脑上用不了沙箱时直接拒绝，绝不悄悄在沙箱之外运行。见 [命令的沙箱](README.zh-CN.md#命令的沙箱命令只碰得到该碰的地方)。
- **工具结果里的密钥会被遮住**，然后才交给模型、写进历史、显示在页面上、交给你的钩子、写进运行记录。这是一张安全网，不是保证：模型故意改写过的密钥认不出来。
  见 [遮住工具结果里的密钥](README.zh-CN.md#遮住工具结果里的密钥)。
- **密钥不进浏览器**：API 密钥、机器人 token、MCP 的密钥从不发给页面（只显示最后 4 位）；命令、MCP 服务器和钩子的环境里没有模型的密钥；
  导出时遮住。见 [数据存在哪里](README.zh-CN.md#数据存在哪里)。
- **聊天通道只听配对过的主人**，别人的消息和群聊一律不理。浏览器和操作整台电脑也有自己的规矩：智狐的 Chrome 只走管道、不开调试端口，
  密码和银行卡每次都问，下载一律拒绝；操作整台电脑在你确认过风险之前一直关着。见
  [浏览器](README.zh-CN.md#浏览器智狐自己的一个-chrome) 和 [操作整台电脑](README.zh-CN.md#操作整台电脑看你的屏幕动你的鼠标键盘进阶)。

**什么不算漏洞：**

- **你自己的选择带来的结果**：选了"全部自动允许"（`/auto all`、网页上的那个按钮、设成这一档的定时任务或聊天通道）之后发生的事，
  或者你自己点了允许的那一步，哪怕是网页或文件误导了模型才提出来的。确认就是那道关卡。
- **单纯的提示词注入**：网页或文件说动模型去"请求"做一件有害的事，是所有 agent 都有的局限，所以才要问你。要是那一步没问就做了，
  或者碰到了上面说碰不到的东西，那就算漏洞。
- **需要已经有程序以你的账户运行**，或者能直接碰到这台电脑的攻击：这样的程序本来就能直接读 `data/` 和 `.env`。
- **你自己把网页界面暴露出去**：反向代理、端口转发，或者改成监听 `127.0.0.1` 以外的地址。要从别的电脑访问，请用 SSH 隧道。
- **你自己装上、接入的东西**：MCP 服务器、Skill、自定义 Agent、钩子、自定义模型接口，它们能做的就是你给它们的权限。
- **文档里写明的局限**，比如 Windows 上没有命令的沙箱，或者某种密钥的格式没有被遮住（后者请开一个普通的 issue，用编造的例子）。
- **模型服务商或依赖本身的问题**，请报告给上游。要是智狐用某个依赖的方式让它可以被利用，那就算。
