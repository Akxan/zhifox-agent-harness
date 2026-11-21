"""从别的 AI 工具导入：MCP 服务器、Skill、Agent。

你可能已经在 Claude Code、Cursor、VS Code、Codex、Gemini CLI……里配好了 MCP 服务器、写好了 Skill 和 Agent。
这个包让智狐认得它们放在哪儿、写成什么样，你在页面上点"从其他工具导入"时，列给你勾选，勾上的复制一份过来。

为什么单独一个包，而不是分别塞进 mcp/、skills/、agents/：
  "哪个工具把什么放在哪儿"是一张表，一个工具常常三样都有（Cursor 有 MCP、Skill 和 Agent），
  按工具登记在一处，加一个新工具只改一个地方（registry.py）；读文件的几种格式（JSON、TOML、YAML）
  和"别家的写法 → 智狐的写法"的换算也是三样共用的。检查合不合格、装进来，仍然是各自包里原来的那一套：
  MCP 用 mcp/config.py 的 normalize，Skill 用 skills/bundle.py 和 store.py，Agent 用 agents/spec.py 和 store.py。

  places.py        这台电脑上各家的文件放在哪：家目录、各系统的配置目录（AppData、~/.config、~/Library/……）
  registry.py      登记表：每个工具一项（名字、官方文档、MCP / Skill / Agent 各在哪儿、用哪种读法）
  readers.py       读配置文件：JSON、带注释的 JSON、TOML、YAML；读不出来的给一句说清楚的原因
  yaml_lite.py     YAML 的一个小子集（Goose、Continue 的配置用到的那些写法），不加依赖
  refs.py          值里引用环境变量的各种写法（${env:X}、$X、{env:X}……）→ 智狐的 ${X}
  mcp_shapes.py    各家 MCP 配置的写法 → 智狐（Claude Desktop）的写法，以及改了什么、丢了什么
  agent_tools.py   各家 Agent 文件里工具的叫法 → 智狐的叫法（Read、Edit、Bash……）
  agent_formats.py 各家 Agent 文件 → 智狐的 Agent 文件，以及改了什么、丢了什么
  overview.py      每个工具找到了几样：页面顶上的总览，和"没找到的"

几条规矩（导入的这几个文件都在你的电脑上，但可能是别人写的，比如克隆下来的仓库里的 .mcp.json）：
  - 只有你打开导入页时才去读，只读不写，什么都不运行；勾了哪几个才复制哪几个，复制完和原来的文件再无关系。
  - 每一样都用智狐自己的检查再过一遍；读不出来的文件写明原因，别的照常列出来。
  - MCP 配置里的密钥不发给页面（只有最后 4 位）；引用智狐自己的模型密钥（${ANTHROPIC_API_KEY}）的一律不导入；
    别家设成"不用问"的工具，到了智狐照样每次都问。
  - Skill 照样走压缩包、链接的那套安全检查（skills/bundle.py），复制而不是链接。
"""
