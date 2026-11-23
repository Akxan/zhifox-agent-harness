[English](../en/models.md) | **简体中文**

[智狐](../../README.zh-CN.md) › [文档](../../README.zh-CN.md#文档) › 模型和厂商

# 模型和厂商

## 支持的模型

| 厂商 | 在哪里申请 API 密钥 | `.env` 里的变量名 | 验证情况 |
|---|---|---|---|
| Claude (Anthropic) | [platform.claude.com](https://platform.claude.com) | `ANTHROPIC_API_KEY` | 用真实 API 实测过 |
| GPT (OpenAI) | [platform.openai.com](https://platform.openai.com) | `OPENAI_API_KEY` | 没有实测 |
| Gemini (Google) | [aistudio.google.com](https://aistudio.google.com) | `GEMINI_API_KEY` | 没有实测 |
| DeepSeek | [platform.deepseek.com](https://platform.deepseek.com) | `DEEPSEEK_API_KEY` | 实测过：保存密钥、同步模型、带文本附件的对话 |

GPT 和 Gemini 是对照各自的官方文档写的，并用本地的假服务器测试过，但还没有用真实的密钥测过。
第一次使用时如果报错，多半是那一家的接口有文档里没写到的细节。

各家的 API 都按用量计费，和它们聊天产品的订阅是分开的，一般需要先充值。
密钥可以在网页界面的设置里填，也可以把 `.env.example` 复制成 `.env` 后直接写在里面，两种方式存的是同一个地方。

## 本地模型和自己的网关（Ollama、LM Studio、vLLM、llama.cpp）

任何支持 OpenAI Chat Completions 格式的服务器，都可以在**设置 → 自定义模型接口**里加进来：这台电脑上跑的模型、
内网里的一台显卡机器，或者公司自己的网关。本机和内网的接口，对话不出你的网络，也不花钱（换模型的面板里写着
"本地，不计费"）。每个接口在换模型的面板里是单独的一组。

**Ollama，一步接上**：Ollama 开着（`ollama serve`，或者开着它的桌面程序）时，点**添加接口**，选**Ollama**
（`http://localhost:11434/v1`），点**测试连接**，再点**保存**。智狐会列出你拉下来的模型，并问 Ollama 每个模型能做什么
（能不能用工具、看不看得懂图片、上下文多大）。

**LM Studio，一步接上**：在 LM Studio 的"开发者"一页点**启动服务器**（Start Server），然后添加接口、选**LM Studio**
（`http://localhost:1234/v1`），**测试连接**、**保存**。

vLLM（`http://localhost:8000/v1`）和 llama.cpp 的 `llama-server`（`http://localhost:8080/v1`）也一样。
API 密钥可以不填；你的服务器或者网关要的话再填。

**怎么挑本地模型**：读写文件、执行命令、记待办，都要模型支持工具调用（function calling）。Qwen 3、Llama 3.1 以上、
Mistral、GPT-OSS 支持；小的、老的模型（Llama 2、Gemma 2、Phi-3）多半不行。用不了工具的模型只能用文字回答：
服务器第一次拒绝带工具的请求时，智狐把这个模型标成**不能用工具**，在对话里说一声，"智狐能做的事"里每一项都写着用不了和原因。
vLLM 要加 `--enable-auto-tool-choice --tool-call-parser …`、llama.cpp 要加 `--jinja` 才能用工具。
智狐自己的系统提示词和工具说明书就要好几千个 token，所以上下文要给够：Ollama 默认只给 4096，更长的提示会被它悄悄截掉开头；
启动它之前设 `OLLAMA_CONTEXT_LENGTH=32768`（或者更大；PowerShell 里写 `$env:OLLAMA_CONTEXT_LENGTH=32768`），并在接口的设置里把这个模型的上下文上限填成同样的数，
智狐靠这个数决定什么时候压缩对话。

安全上的规矩：外面的地址必须是 `https://`；明文的 `http://` 只许这台电脑（`localhost`、`127.x`、`::1`）和内网的地址
（`10.x`、`172.16–31.x`、`192.168.x`、Tailscale 用的 `100.64.x`、`*.local`、`*.internal`、没有点的单个名字）。
所有请求都从智狐的程序发出去，页面从来不直接连这个地址。密钥存在 `data/endpoints.json`（只有你的账号能读、导出时遮住、
页面上只看得到最后 4 位），不进环境变量，所以命令和 MCP 服务器都看不到它。内置几家的密钥和设置（`OPENAI_API_KEY`、
`OPENAI_ORG_ID`、`OPENAI_CUSTOM_HEADERS`……）永远不会发给自定义接口；重定向不跟；改了接口的服务器地址，要重新填一次密钥。
Agent 文件和别的程序用 `custom-<编号>/<模型名>` 指定接口上的模型，比如 `custom-ollama/qwen3:8b`。

## 一个循环，多家模型

[循环](architecture.md#循环)里那段历史是 Claude 的格式。别家的格式不一样，但循环（`agent.py`）并不关心，
它只要求每一家的适配层都能做这几件事（定义在 [harness/providers/base.py](../../harness/providers/base.py)）：

| 方法 | 作用 |
|---|---|
| `add_user` | 往历史里追加一条用户消息 |
| `step` | 调用一次模型，边收边显示，把回复追加进历史，并告诉循环模型想调用哪些工具 |
| `add_tool_results` | 把工具的执行结果追加进历史 |
| `mark` / `rollback` | 记下历史的位置 / 退回到那个位置（出错或被拒绝时撤销这一轮） |

适配层只有两个：[claude.py](../../harness/providers/claude.py) 用 Anthropic 的原生接口；
[openai_compat.py](../../harness/providers/openai_compat.py) 用 OpenAI 定下的 Chat Completions 格式，
GPT、Gemini、DeepSeek 都支持这套格式，只是服务器地址不同，所以一份代码服务三家。两者的主要差别：

| | Claude | GPT、Gemini、DeepSeek |
|---|---|---|
| 系统提示词 | 单独的参数 | 历史里的第一条消息 |
| 工具调用 | 回复里的一个"内容块" | 助手消息上的 `tool_calls` 字段，参数是一段 JSON 文本 |
| 工具结果 | 所有结果合在一条消息里 | 每个结果单独一条消息 |
| 联网搜索 | 有，在 Anthropic 的服务器上执行 | 没有。智狐自己的 `web_fetch` 能按网址读网页，但不能搜索（见[读网页](browser-and-computer-use.md#读网页不用浏览器web_fetch)） |
| 对话太长时 | 由 API 在服务器上把较早的内容压缩成摘要 | 由智狐请模型写摘要，换掉较早的内容（见下一节） |
| 缓存 | 由我们在请求里标记 | 各家自动处理 |

要接入另一家支持 OpenAI 格式的厂商，在 [harness/providers/\_\_init\_\_.py](../../harness/providers/__init__.py) 的 `PROVIDERS` 里加一项就够了。

自定义接口（见[本地模型和自己的网关](#本地模型和自己的网关ollamalm-studiovllmllamacpp)）用的也是这个适配层，有三处不同。
一个模型能做什么（看图、工具、PDF、上下文多大）按模型记，不按家记：同一个 Ollama 上可能一个模型能用工具、旁边一个不能。
只做了一部分这套格式的服务器，退一步就行：服务器不认 `stream_options`，就不再要用量；这段对话还没用过工具时服务器拒绝了工具，
这段对话就不带工具接着聊（被拒绝的那次请求服务器根本没处理，所以历史一个字都没改）。两样都记进 `data/endpoints.json`，
下一段对话一开始就按对的办法发。最后，同一个接口上能不能用工具不一样的两个模型之间换，要开始新的一段：一段对话的工具清单从来不变。

## 对话太长了怎么办：压缩历史

每一轮都要把整段历史重发一遍，所以对话越长，每一轮越贵；长到超过模型一次能读的上限（上下文窗口），
请求就会被拒绝，这段对话就聊不下去了。办法是压缩：把较早的内容换成一份摘要，只留最近几轮的原文。

Claude 的压缩由 Anthropic 的 API 在服务器上完成（请求里带着 `context_management`，见 [claude.py](../../harness/providers/claude.py)），
我们这边的历史仍然只追加。OpenAI 格式的接口没有这个功能，所以 GPT、Gemini、DeepSeek 的压缩由智狐自己做，
代码在 [harness/providers/compaction.py](../../harness/providers/compaction.py)：

1. **什么时候压缩。** 每次请求回来，用量里写着这一次输入和输出一共多少 token，也就是这段对话现在占了多大的上下文。
   它超过模型上限的 70%，下一次请求之前就先压缩。剩下的 30% 要装得下下一条消息、工具的结果和模型的回复。
2. **切在哪里。** 切口只落在两轮之间，也就是你的某一条消息前面。工具调用和它的结果总在同一轮里，所以永远在切口的同一边，
   历史里不会出现"有调用、没结果"的情况（API 会拒绝这样的请求）。最近两轮（正在进行的这一轮也算）原样保留。
3. **请谁写摘要。** 就是正在对话的那个模型。切口之前的那一段原样发过去，末尾加一条指令请它写摘要（指令写在 `compaction.py` 里）。
   原样发是为了命中缓存：这一段之前每一轮都发过，开头一字不差，按低得多的缓存价计费。
   这次调用的用量和平时一样记下来，算进估算的费用里。
4. **换成什么。** 系统提示词，一条装着摘要的用户消息和一条助手的确认（这样历史仍然是你一句、它一句；有的模型不接受连着两条用户消息），
   再接上最近几轮的原文。被压缩的那一段里如果有附件，原件不再保留，只留一句话说你发过哪些文件。
   之后又太长时再压缩一次，上一份摘要会被合进新的摘要里。

这是"历史只在末尾追加"这条规矩唯一的例外，所以边界划得很清楚：只有 OpenAI 格式的适配层这样做；
只换切口之前的部分，系统提示词和最近几轮一个字不动；只在两次请求之间做。历史因此变短了，`mark` 记下的位置也跟着换算，
出错时撤回这一轮，撤掉的仍然正好是这一轮。代价是一次缓存失效：压缩之后的第一次请求开头变了，要按原价付一次，
之后新的开头又会被缓存起来。换来的是历史不再无限变长。

界面上会出现两句提示：一句说正在请模型写摘要，做完后另一句告诉你之后每一轮大约少发多少 token。
写摘要失败了（比如网络断了、服务器太忙），提示里会说明原因，这一轮照常进行，下一轮再试。
保存的会话里，`messages`（发给模型的那一份）是压缩过的，重新打开时接着摘要聊；`events`（页面上显示过的那一份）一点不少，
所以重放和搜索看到的仍然是完整的对话。

能调的数都在 [harness/config.py](../../harness/config.py) 里：`ENABLE_OPENAI_COMPACTION`（改成 `False` 就不压缩）、
`CONTEXT_WINDOWS`（各个模型的上限，DeepSeek 的两个模型是 100 万 token）、`DEFAULT_CONTEXT_WINDOW`
（不在表里的模型按 12.8 万 token 算，宁可早一点压缩）、`COMPACTION_THRESHOLD`（0.7）、`COMPACTION_KEEP_TURNS`（2）。

## 模型暂时用不了：备用模型

有时候请求失败和你没关系：厂商的服务器过载了（Anthropic 回答 529 `overloaded_error`）、他们那边出错了（5xx）、
或者短时间里请求太多了（429）。SDK 自己会等一下再重试两次；还是不行的话，以前这一轮就停下来报错。
换个模型也没用的错误（密钥不对、请求写错了、OpenAI 账号的额度用完了 `insufficient_quota`）照旧停下。

**会怎样。** 在**设置 → 备用模型**里，可以按顺序选最多三个**同一家**的别的模型（比如 Claude Opus 5.5 → Claude Sonnet 5.5
→ Claude Fable 5.2）。一次请求因为上面这几种原因失败时，循环把一模一样的请求发给这一轮还没试过的第一个备用模型，
在对话里说一声（"Opus 5.5 暂时不可用，这一轮接下来改用 Sonnet 5.5。"），这一轮剩下的步骤都用它。
下一轮又从你选的那个开始（它可能已经好了），这段会话记着的也还是你选的那个。默认一个都没有：换模型会改变回答的质量和价格，
换不换、换成哪个由你决定。定时任务、聊天通道、子 Agent 用的是同一份设置。

**为什么只能选同一家的。** 另一家的模型接不上这段对话：历史的格式不一样（Claude 的内容块不是 OpenAI 那种消息），
系统提示词不一样（"能联网"那一行只写给 Claude），工具也不一样（Claude 用 Anthropic 定义的浏览器、电脑工具包，
别家用我们自己写的 `browser_*`、`computer_*` 替身）。一轮做到一半换过去，等于扔掉这一轮已经做过的工具调用和结果，
还悄悄换了提示词和工具，[只在末尾追加](architecture.md#循环)的规矩不允许。同一家之内，`App.select_model` 本来就允许对话中途换模型：
历史、系统提示词、工具一字不变，只有请求里的 `model` 变了。换不换得过去，和在换模型的面板上换是同一条规矩
（`providers.continues`：同一家，用不用得了工具也一样，只有自定义接口会碰到），再加一条：这段对话开始时定下的工具包它得用得了
（`Backend.can_switch_to`），用不了的跳过不选。自定义接口的备用模型，是同一个接口上的别的模型。

**Claude 思考内容的签名怎么办？** Claude 的思考块带着签名，签名里记着是哪个模型写的。换请求里的 `model` 不算改了历史，
所以换模型从不让请求失败；新模型读不了的思考块，API 会在它看到之前去掉，不计费。智狐照样把历史原样发过去
（自己去删反而会出错）。所以 Opus 5.5 换成 Sonnet 5.5 时，Sonnet 做的那几步看不到 Opus 之前的思考（Sonnet 5.5 读不了
Opus 5.5 的），但请求照常成功；下一轮换回 Opus 5.5，它读得了 Sonnet 5.5 留下的（在 Claude API 上），什么都不丢。

**在哪儿看得到。** 对话里的那一句；[运行记录](architecture.md#看懂一轮对话运行记录)里，失败的那次请求标成出错、写着原因，用备用模型发的每一次标着
"备用"和它替的是哪个；[花费账本](cost.md)按实际回答的那个模型记钱。失败的那次什么都没留在历史里（两家的适配层都是收完整个回复才追加）；
已经显示在页面上的半截文字留着，和工具参数不是合法 JSON 时的重试一样。规矩在 [harness/fallback.py](../../harness/fallback.py)，
循环里的那一段是 [harness/agent.py](../../harness/agent.py) 的 `Agent._request`。
