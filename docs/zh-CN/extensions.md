[English](../en/extensions.md) | **简体中文**

[智狐](../../README.zh-CN.md) › [文档](../../README.zh-CN.md#文档) › 扩展：MCP、Skill 和自定义 Agent

# 扩展：MCP、Skill 和自定义 Agent

## MCP：别人写好的工具

上面这些工具都写在这个程序里。[MCP](https://modelcontextprotocol.io)（Model Context Protocol）是一个公开的协议，
管的是其余的情况：任何人都可以把一组工具（读写 GitHub、查数据库、操作浏览器……）做成一个"MCP 服务器"，
任何支持 MCP 的助手（"客户端"）都能连上它，把这些工具交给自己的模型用。智狐是一个客户端，
在 [harness/mcp/](../../harness/mcp/) 里手写，没有用官方的 SDK，一个文件管一件事：

| 文件 | 做什么 |
|---|---|
| [jsonrpc.py](../../harness/mcp/jsonrpc.py) | 消息的信封：请求、回答、通知（JSON-RPC 2.0），以及怎么等一个回答、等多久 |
| [stdio.py](../../harness/mcp/stdio.py) | 本地服务器：启动那个程序，消息一行一条走它的标准输入和输出，它的 stderr 留着当日志 |
| [http.py](../../harness/mcp/http.py) | 远程服务器：每条消息是一次 HTTP POST，回答是一个 JSON，或者一串服务器推送事件 |
| [client.py](../../harness/mcp/client.py) | 协议本身：弄清对面说的是哪一代协议，一页一页列出它的工具，调用它们 |
| [bridge.py](../../harness/mcp/bridge.py) | MCP 的工具 ↔ 智狐的工具：名字、参数格式、结果 |
| [manager.py](../../harness/mcp/manager.py) | 服务器的启动、停止、重启和状态，以及"一段对话里工具不变"的快照 |
| [config.py](../../harness/mcp/config.py)、[importers.py](../../harness/mcp/importers.py) | `data/mcp.json`、给页面看时遮住密钥、从其他工具导入（见"从其他工具导入"） |

一次调用走的路：

```
模型："我要调用 mcp__github__search_issues，参数是 {...}"
  └─► agent.py        和别的工具一样：先过权限这一关（默认每次都问你），再执行
        └─► manager.py      找到 github 这个服务器的连接
              └─► client.py      包成一个 tools/call 请求
                    └─► stdio.py / http.py   写进服务器的 stdin，或者 POST 到它的网址
                    ◄── 服务器的回答
        ◄── bridge.py       把回答里的文字、图片整理成交给模型的结果
```

**两代协议。** 到 2025-11-25 这一版为止，连接要先"握手"：客户端发 `initialize`，带上自己支持的版本；
服务器回答它选定的版本和能力；客户端再发一条 `notifications/initialized` 确认，这一来一回定下的东西在整个连接里都算数。
从 2026-07-28 起，MCP 是"无状态"的：没有握手，每个请求都在 `params._meta` 里自己带着协议版本、客户端是谁、
有哪些能力，服务器不用记得上一个请求。外面两种服务器都有，所以智狐两种都会说：先按新的规矩问一句 `server/discover`。
回答了，或者回了一个只有新一代才有的错误（比如"不支持这个版本"），就是新一代；回了别的错误，或者干脆不理，
就是老一代，改走握手。有的本地服务器启动很慢（`npx` 第一次要下载），所以等几秒没有回音，就把 `initialize` 也发出去，
两个谁先成功按谁来。

**一段对话里工具是"冻结"的。** 每次请求都从工具说明书开始，而 Claude 的缓存认的就是请求的开头：
对话进行到一半改了一个工具，整段对话的缓存就作废了，Claude 之前的思考内容也对不上它前面的内容了。
可是 MCP 服务器随时可能连上、断开、改变它的工具。所以一段对话发出第一条消息的那一刻，
智狐把当时已经连上的服务器的工具拍一张快照，这段对话从头到尾只用这张快照。之后才连上或者有变化的服务器，
从下一段新对话开始才用得上，在那之前，它在 MCP 一页里的那一行标着"新对话生效"。
快照里的某个服务器后来不在了，调用它的工具会得到一个说清楚原因的错误，模型会换个办法。

**名字、参数格式和结果的翻译。** 各家模型要求工具名最长 64 个字符，只能用字母、数字、`_` 和 `-`；
两个服务器还可能都有一个叫 `search` 的工具。所以智狐把它叫做 `mcp__服务器__工具`，别的字符换成 `_`，
太长的截短并加一小段哈希，保证不重名；说明的开头写明它来自哪个服务器。参数格式原样交过去，只去掉各家不接受的：
Claude 不接受最外层的 `anyOf`，Gemini 不认识 `$ref` 和 `$schema`，所以给 OpenAI 格式的几家时把引用就地展开。
结果里，文字交给模型；图片在模型看得懂图片时作为图片交过去；嵌入的资源变成文字；服务器只给了结构化结果、
没给文字时，把它写成 JSON；`isError` 会把这个结果标成出错，模型可以据此自己改正。

**停止、超时和崩溃。** 点"停止"会取消正在进行的调用：stdio 上发一条 `notifications/cancelled`；
HTTP 上挂断那个请求（对老一代的服务器也会发那条通知）。一次调用超过 5 分钟也会这样取消
（可以在 `data/mcp.json` 里按服务器用 `"timeout": 秒数` 改）。本地服务器崩溃了会自动重启，10 分钟内最多 3 次。
服务器通知"工具列表变了"（`notifications/tools/list_changed`）时，智狐在后台重新取一遍。

## Skill 是怎么工作的

模型什么都懂一点，但不知道你希望某一类事怎么做。每次都把要求重新说一遍很累，全写进系统提示词又会让每一次请求
都背着所有的要求。Skill 就是为这个准备的：一个文件夹，里面的 `SKILL.md` 写着做这一类事的步骤，旁边可以带着
脚本、参考资料和模板。格式是开放的 [Agent Skills 标准](https://agentskills.io/specification)，Claude、Claude Code
和越来越多别家的工具都用它，所以在那边写好的 Skill 可以原样装进来：

```
weekly-report/
├── SKILL.md          ---
│                     name: weekly-report          小写字母、数字和连字符，和文件夹同名
│                     description: 整理每周的      做什么、什么时候用（最多 1024 个字符）
│                       工作周报。用户说……时使用
│                     ---
│                     （下面是做法，用 Markdown 写）
├── scripts/          可选：模型可以运行的脚本
└── references/       可选：需要时才读的参考资料
```

分三层、用到才拿（progressive disclosure）：一、每个启用着的 Skill 的名字和说明一直在模型眼前，每个约 100 个 token，
所以装几十个也不会让对话变贵；二、请求和某个说明对得上时，模型用 `skill` 工具读出完整的 `SKILL.md`；
三、别的文件和脚本，只有说明里要求、确实需要时才读、才运行。

这份列表放在哪里，为什么不放系统提示词：一段对话里，系统提示词和工具说明书必须一字不差（缓存靠它；Claude 之前的
思考内容也和它绑在一起），可你随时可能装上、停用一个 Skill。所以列表放在用户消息里，和当前时间一样：
一段对话的第一条消息带着一个 `<skills>` 块，写着当时有哪些 Skill；之后每条消息发出之前，和模型最近一次看到的那份比一比，
变了就把新的一份附在这条消息前面。"模型最近一次看到的那份"直接从对话历史里找，不另外记，所以打开旧会话、
某一轮出错被撤回，都不用额外处理。唯一的例外是 Claude 的自动压缩：压缩之前的内容不再发给模型，那里面的列表也就不算看到过。
Claude Code 也是这样做的：它把 Skill 列表作为附加信息放进消息里，而不是写死在系统提示词里。
"怎么用 Skill"也写在这个块和 `skill` 工具的说明里，而不是系统提示词里：系统提示词你可以在设置里改，
改过的提示词不该一声不响地让 Skill 失效。定时任务每次运行的第一条消息里也带着这份列表。
见 [harness/skills/catalog.py](../../harness/skills/catalog.py)。

读和运行：`skill` 工具只能读，只能读启用着的 Skill，路径一层一层地检查，出不了那个 Skill 的文件夹，也不跟着链接走，
所以读 Skill 不用问你，和记忆工具一样。Skill 里的脚本由模型用 `run_command` 运行，和别的命令一样每次都问。
Skill 不能替你放开这一关：Claude Code 把 `allowed-tools` 当作"用这些工具不用再问"，智狐只把它显示出来。
Skill 里的 `${CLAUDE_SKILL_DIR}` 会换成这个 Skill 真实的文件夹，所以用到它的 Claude Code Skill 照样能用。
标了 `disable-model-invocation: true` 的 Skill 不列给模型（作者的意思是"只在用户点名时用"），你点名要用它时，模型照样读得到。

安装：装、改、删 Skill 只能由你在网页上做，模型做不了。智狐有自己的文件夹 `data/skills/`，不会自己去读别的工具的：
"从其他工具导入"只有在你点了之后，才去看 `~/.claude/skills/`、`~/.agents/skills/`、`~/.codex/skills/`、`.github/skills/`
这些文档里写明的地方（见"从其他工具导入"），你勾选了的才复制过来，之后两边各改各的，互不影响。从外面来的东西，写进去之前都先给你看（名字、说明、文件、来源），并且一律当作不可信的：

- 网址只认 https，跳转也只跟到 https；下载有超时，最多 20 MB。GitHub 上的文件夹链接（`…/tree/main/skills/pdf`）
  会从 `codeload.github.com` 下载那个分支的压缩包（一个请求，不占 GitHub API 每小时的额度），再从里面取出那个文件夹；
  `SKILL.md` 的链接装的是它所在的整个文件夹。
- 压缩包里只要有一项是绝对路径、带 `..`、是符号链接、设备文件或管道，或者是加密的，就整个不装；文件个数（1000 个）、
  解开之后的总大小（50 MB）都有上限，只差大小写的同名文件（在 macOS 上会互相覆盖）也不装。文件先写进一个隐藏的文件夹，
  最后一步改名换上去，装到一半出错不会留下半个 Skill。
- 和已有的 Skill 同名时，问你改名（`SKILL.md` 里的 `name` 跟着改成新文件夹的名字）还是替换。

frontmatter 的解析：`SKILL.md` 开头有几行 YAML。没有为此加一个 YAML 库，[harness/skills/frontmatter.py](../../harness/skills/frontmatter.py)
只认标准用到的那一小部分（普通的、带引号的、多行的值，一层的 `metadata` 表，列表），别的写法都明确报错并给出行号：
锚点和别名（可以拿来做指数膨胀）、标签（有的库会照着它构造任意对象），以及没加引号的值里出现"冒号加空格"
（正规的 YAML 解析器同样会报错，Claude Code 遇到这种文件会当作什么字段都没写）。值一律当作文字，`version: 1.0`
读出来就是 `"1.0"`，不是数字。标准之外的字段（比如 Claude Code 的 `argument-hint`、`hooks`）原样留在文件里。

## 自定义 Agent：子 Agent 和角色

Skill 教的是"某一类事怎么做"。**Agent** 则是另一个助手：有它自己的设定、自己的工具、自己的模型。每个 Agent 是
`data/agents/` 里的一个 Markdown 文件，格式和 [Claude Code 的 subagent](https://code.claude.com/docs/en/sub-agents)
一样，那边写好的可以原样导入：

```markdown
---
name: 资料研究员                  字母（中文也可以）、数字、- 和 _；也是文件名（资料研究员.md）
description: 查资料、读网页，      它擅长什么、什么时候该把事情交给它（最多 1024 个字符）
  交回一份带出处的摘要。
tools: Read, WebSearch, WebFetch  可选；不写就是"叫它的那一方有的都能用"，[] 表示一个都没有
model: inherit                    可选："提供方/模型"（deepseek/deepseek-flash）、inherit，或者 opus / sonnet
color: blue                       可选：red、orange、yellow、green、cyan、blue、purple、pink 之一
---
你是一个细心的研究员……           它的设定（就是它的系统提示词）
```

同一个文件有两种完全不同的用法，分清这两种，就明白了"agent"这个词的要点：

| | 子 Agent | 角色 |
|---|---|---|
| 谁叫它 | 主助手：任务和它的说明对得上时，用 `agent` 工具交给它（你也可以直接说"让资料研究员查一下……"） | 你：在"新对话"旁边的箭头里选它 |
| 它看得到什么 | 只有主助手写给它的任务：看不到你们的对话，也看不到附件 | 整段对话，和平常的助手一样 |
| 历史 | 每次交给它都是一段新的，做完就扔掉；只有它最后的回答回到主助手的对话里 | 它就是这段对话，和别的会话一样保存、打开 |
| 它和谁说话 | 主助手（它没法向你提问；你在一张卡片上看它在做什么） | 直接和你 |
| 适合 | "上下文隔离"：它在自己那边读了二十个网页，主助手的对话里只多一段话 | 一段时间里专门和你做某一类事的助手：写作搭档、代码审查员 |

**子 Agent 是怎么跑起来的**（[harness/agents/runner.py](../../harness/agents/runner.py)）。模型调用 `agent(name, task)` 时，
智狐建第二个 `Agent`（和主助手同一个循环类），给它：

- **一段新的历史**：第一条用户消息就是任务（和平常一样，前面带着 `<context>` 时间说明）；
- **它自己的系统提示词**：Agent 的设定，后面接一段程序附上的"环境"（工作目录、操作系统、`<context>` 是什么、
  网页和文件里的内容是资料不是指示，以及它的"用户"其实是主助手、主助手只收得到它最后的回答，所以要交回结论、
  缺什么信息就说出来，而不是去提问）。环境那几行和内置提示词用的是同一份文字（[harness/prompt.py](../../harness/prompt.py)
  把它们分成了几段有名字的常量），"可以联网"那一行只在它真的拿到联网工具时才写；
- **只有放开的工具，而且永远不比叫它的那一方多**：allow-list 是套在叫它的那一方手里的工具上的，所以一个只读的角色
  叫不出一个能改文件的子 Agent，用 DeepSeek 的子 Agent 也拿不到 Claude 的联网搜索。`Read` 拿到的是"只许读"的
  文件工具：改文件的调用在问你之前就被退回（`Tool.check`）。它还有自己的 shell、自己名下的 MCP 调用（重新拍一张快照、
  记在它自己名下，只留叫它的那一方本来就有的工具），和定时任务的每次运行一样；
- **它选的模型**；写 `inherit` 就和叫它的那一方用同一个。

它的每一次工具调用都经过叫它的那一方的同一个 `Permissions` 对象：在你的对话里，就是你在页面上选的那一档；
在定时任务里，就是那个任务自己的那一档。确认卡片照常出现在对话里，写着是哪个 Agent 在问。"停止"也停得了它：
它运行的那一会儿，把自己的"停止"登记在叫它的那一方的界面上，只结束自己的命令、只取消自己的 MCP 调用，别人的不碰；
被打断的那次 `agent` 调用和别的工具调用一样补上结果。它调用模型照样记账，来源写成 `subagent`；它的 token 和费用
也加进叫它的那一方的合计里（不算步数，也不算上下文的大小）。它最多调用 `AGENT_MAX_STEPS` 次模型（25 次）；
子 Agent 默认不能再往下交，除非你把 [harness/config.py](../../harness/config.py) 里的 `AGENT_MAX_DEPTH`（1）调大。
主助手收到的工具结果，是子 Agent 最后的回答，加上一句"它做了什么"（调用了几次模型、用了哪些工具）。

**为什么主助手的对话又便宜又稳定。** `agent` 工具一直都在（哪怕一个 Agent 都还没有），它的说明书里也不写有哪些 Agent；
Agent 的列表放在用户消息里的 `<agents>` 块中，和 `<skills>` 块的做法一模一样（[harness/agents/catalog.py](../../harness/agents/catalog.py)，
两者共用 [harness/catalog.py](../../harness/catalog.py)）。所以主助手的系统提示词和工具说明书在一轮又一轮之间、在交出任务的前后都一字不差，
缓存照样命中。

**角色。** 用一个 Agent 开始一段对话，这段对话换掉三样东西：系统提示词（它的设定，可以用和你自己的提示词一样的
`{workspace}` 这类占位符，后面同样接上程序附上的环境说明）、工具（它的 allow-list 套在平常那一套上）、模型（它写了的话；
这也会成为"现在用的模型"，和打开一段旧会话一样）。会话文件里记着是哪个 Agent，左边的会话列表里有一个带颜色的小标记，
顶上的标题前面写着它的名字。角色在一段对话里不能换（系统提示词和工具不能变），想换就开一段新的；换到另一家的模型时，
新的一段照样是这个角色。重新打开这段会话时，按这个 Agent 现在的文件重新生成提示词（和内置的提示词一样）；
它已经被删掉的话，会用内置的提示词接着聊，并且告诉你一声。

**管理 Agent。** "扩展 → Agent"列出它们；每一个都可以用表单编辑（名字、说明、从同步到的模型列表里选模型、颜色、
按"内置的 / Skill / MCP"分组的工具清单、设定），也可以直接改整个 Markdown 文件，两种写法都由和 `SKILL.md` 同一个解析器检查
（[harness/skills/frontmatter.py](../../harness/skills/frontmatter.py)）。按表单保存时只改你改过的那几项，注释和智狐不用的字段
（Claude Code 的 `permissionMode`、`hooks`……）原样留着。Claude Code 的工具名照样认：`Write`、`MultiEdit` 算 `Edit`，
`Grep`、`Glob`、`LS` 算 `Read`，`Task` 算 `Agent`；智狐没有的名字原样保留、列出来，但会被忽略。智狐自己的 `Browser`
给的是浏览器的全部工具，前提是叫它的那一方手里有（浏览器开着）；这时子 Agent 用的是叫它的那一方的标签页，确认也跟着那一方。
`Computer` 同样给的是操作电脑的全部工具（叫它的那一方手里有的话）；子 Agent 和叫它的那一方共用同一个屏幕的 session，
所以叫它的那一方要问的时候它也问（卡片上写着是哪个 Agent），你在这段对话里选过的截图不再问、点击和打字不再问，对它也算数。"从其他工具导入"
只在你点它的时候才去读 Claude Code、GitHub Copilot、Gemini CLI、OpenCode、Codex 这些工具的 Agent，写法不一样的先换成智狐的写法，
只复制你勾选的（见"从其他工具导入"）。三个示例（只读文件加联网搜索的资料研究员、
只读的代码审查员、不用任何工具的写作助手）点一下就能加进来，默认一个都不装。模型没有任何能新建或修改 Agent 的工具。

## 从其他工具导入

你可能已经在 Claude Code、Cursor、VS Code、Codex、Gemini CLI 或者别的 AI 工具里配好了 MCP 服务器、写好了 Skill 和 Agent。
Skill、MCP、Agent 三页各有一个"从其他工具导入"：点了才去读这些工具的文档里写明的位置，列出找到了什么，
你勾选的复制进智狐自己的文件夹。`~` 开头的在你的家目录里，别的在工作目录里（项目级）：

| 工具 | MCP 服务器 | Skill | Agent |
|---|---|---|---|
| [Claude Code](https://code.claude.com/docs/en/mcp) | `~/.claude.json`（你自己的，和这个项目的那一项）、`.mcp.json` | `~/.claude/skills`、`.claude/skills` | `~/.claude/agents`、`.claude/agents`（原样复制） |
| [Claude Desktop](https://modelcontextprotocol.io/docs/develop/connect-local-servers) | `claude_desktop_config.json`（macOS `~/Library/Application Support/Claude/`，Windows `%APPDATA%\Claude\`，Linux `~/.config/Claude/`） | | |
| [Cursor](https://cursor.com/docs/context/mcp) | `~/.cursor/mcp.json`、`.cursor/mcp.json` | `~/.cursor/skills`、`.cursor/skills` | `~/.cursor/agents`、`.cursor/agents` |
| [VS Code](https://code.visualstudio.com/docs/agents/reference/mcp-configuration) | 用户的 `mcp.json`（Code 和 Code - Insiders）、`settings.json` 里的 `mcp.servers`、`.vscode/mcp.json` | | |
| [GitHub Copilot](https://docs.github.com/en/copilot/reference/custom-agents-configuration) | `~/.copilot/mcp-config.json` | `~/.copilot/skills`、`.github/skills` | `~/.copilot/agents`、`.github/agents`（`*.agent.md`） |
| [Codex](https://learn.chatgpt.com/docs/extend/mcp) | `~/.codex/config.toml`、`.codex/config.toml`（TOML） | `~/.codex/skills`、`.codex/skills` | `~/.codex/agents`、`.codex/agents`（TOML） |
| [Gemini CLI](https://geminicli.com/docs/tools/mcp-server/) | `~/.gemini/settings.json`、`.gemini/settings.json` | `~/.gemini/skills`、`.gemini/skills` | `~/.gemini/agents`、`.gemini/agents` |
| [Agent Skills](https://agentskills.io/client-implementation/adding-skills-support)（很多工具共用的文件夹） | | `~/.agents/skills`、`.agents/skills` | |
| [Windsurf](https://docs.devin.ai/windsurf/plugins/cascade/mcp) | `~/.codeium/windsurf/mcp_config.json`、`~/.config/devin/mcp_config.json`、`.devin/mcp_config.json` | `~/.config/devin/skills`、`.windsurf/skills`、`.devin/skills` | |
| [Cline](https://docs.cline.bot/mcp/mcp-overview) | `~/.cline/data/settings/cline_mcp_settings.json`，以及它以前的两个位置 | | |
| [Roo Code](https://roocodeinc.github.io/Roo-Code/features/mcp/using-mcp-in-roo) | VS Code 的 globalStorage 里的 `mcp_settings.json`、`.roo/mcp.json` | `~/.roo/skills`、`.roo/skills` | |
| [Continue](https://docs.continue.dev/customize/deep-dives/mcp) | `~/.continue/config.yaml`（YAML）、`~/.continue/config.json`、`~/.continue/mcpServers/`、`.continue/mcpServers/` | | |
| [Zed](https://zed.dev/docs/ai/mcp) | `settings.json` 里的 `context_servers`、`.zed/settings.json` | | |
| [OpenCode](https://opencode.ai/docs/mcp-servers/) | `~/.config/opencode/opencode.json(c)`、`opencode.json(c)`、`.opencode/opencode.json(c)` | `~/.config/opencode/skills`、`.opencode/skills` | `~/.config/opencode/agents`、`.opencode/agents` |
| [Goose](https://goose-docs.ai/docs/guides/config-files) | `~/.config/goose/config.yaml`（YAML；Windows 上在 `%APPDATA%\Block\goose\config\`） | `.goose/skills` | |
| [Qwen Code](https://qwenlm.github.io/qwen-code-docs/en/users/features/mcp/) | `~/.qwen/settings.json`、`.qwen/settings.json` | `~/.qwen/skills`、`.qwen/skills` | `~/.qwen/agents`、`.qwen/agents` |
| [Kiro](https://kiro.dev/docs/mcp/configuration/) | `~/.kiro/settings/mcp.json`、`.kiro/settings/mcp.json` | `~/.kiro/skills`、`.kiro/skills` | `~/.kiro/agents`、`.kiro/agents`（JSON 或 Markdown） |
| [Amazon Q Developer](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/command-line-mcp-configuration.html) | `~/.aws/amazonq/mcp.json`、`.amazonq/mcp.json` | | `~/.aws/amazonq/cli-agents`、`.amazonq/cli-agents`（JSON） |
| [Junie](https://junie.jetbrains.com/docs/junie-cli-mcp-configuration.html) | `~/.junie/mcp/mcp.json`、`.junie/mcp/mcp.json` | `~/.junie/skills`、`.junie/skills` | |
| [Factory](https://docs.factory.com/cli/configuration/custom-droids) | | `~/.factory/skills`、`.factory/skills` | `~/.factory/droids`、`.factory/droids` |
| [Amp](https://ampcode.com/docs/customize/skills) | | `~/.config/amp/skills`、`~/.config/agents/skills` | |
| [Warp](https://docs.warp.dev/agents/capabilities/mcp) | `~/.warp/.mcp.json`、`.warp/.mcp.json`、`~/.agents/.mcp.json`、`.agents/.mcp.json` | | |
| [LM Studio](https://lmstudio.ai/docs/app/mcp) | `~/.lmstudio/mcp.json` | | |

打开的是你点它的那一页。顶上是总览：找到了东西的工具一个一行，后面写着三样各有几个；点这一样的数字，滚到那个工具的那一组，
点另外两样，换到那一页去导入。每一组的标题写着哪个工具、哪一级（你自己的还是这个项目的）、哪个文件；读不出来的文件写明原因
（比如"不是合法的 JSON：第 4 行第 3 列"），别的文件照常列出来。最下面收着一栏"没找到的"：这一样智狐认得、但没有找到的工具，
去过哪些地方，和它们的官方文档。

各家的写法不一样，所以检查之前（检查的规矩和你自己添加时一样），[harness/imports/](../../harness/imports/) 先把它换成智狐的写法，
换了什么写在那一项上：

- **MCP**：VS Code 的 `servers`、Zed 的 `context_servers`、Codex 的 TOML、Goose 的 YAML `extensions`、Continue 的 YAML 列表、
  OpenCode 写成列表的 `command`，都换成 Claude Desktop 的写法。各家引用环境变量的写法（`${env:X}`、`$X`、`{env:X}`、
  `${{ secrets.X }}`、Codex 的 `bearer_token_env_var`、Goose 的 `env_keys`）换成智狐的 `${X}`，启动时从环境变量里取；
  `${workspaceFolder}` 这类编辑器的变量直接填好；只有那个工具做得到的（VS Code 的 `${input:…}`、`envFile`）标出来，
  请你导入后编辑。别的工具里"不用问"的设置（`autoApprove`、`alwaysAllow`、`trust`，以及克隆来的 `.mcp.json` 里智狐自己的
  `approval`）一律不搬：导入的服务器每次调用前都先问你。智狐用不上的设置列成"没有搬过来"，不悄悄留在配置里。
  引用了智狐自己的模型密钥（`${ANTHROPIC_API_KEY}`）的服务器列出来但勾不上，导入时服务器也会再拒绝一次。
- **Skill** 各家都是同一种带 `SKILL.md` 的文件夹，复制时和上传一样当作不可信的：里面的链接、隐藏文件不要，路径、大小、个数都有上限。
- **Agent**：Claude Code 的文件原样复制。别家的只写智狐用得上的几项：工具名换成智狐的叫法（`execute`、`run_shell_command` → `Bash`，
  `read_file` → `Read`，`服务器/*`、`@服务器` → `mcp__服务器`……），智狐没有的工具不搬，"只读""不许执行命令"照样不放开，
  认不出来的模型改成"和叫它的那一方一样"，Codex 的 `developer_instructions`、Kiro 的 `prompt`（包括指向旁边文件的 `file://`）
  成为正文。换过来的 Agent 能用的工具只会更少，不会更多。

不打开这一页什么都不读，什么都不运行，除了智狐自己的数据目录什么都不写。页面送回来的只有"哪个来源的哪个名字"，
导入时服务器重新读一遍文件。环境变量和请求头的值不会发到浏览器，只有最后 4 位。加一个工具，是在
[registry.py](../../harness/imports/registry.py) 里写一个函数；它的写法是新的，再在 [mcp_shapes.py](../../harness/imports/mcp_shapes.py)
或 [agent_formats.py](../../harness/imports/agent_formats.py) 里加一种读法。YAML 用的是一个小而安全的子集解析器
（[yaml_lite.py](../../harness/imports/yaml_lite.py)），没有加新的依赖：锚点、别名和标签一律拒绝。
