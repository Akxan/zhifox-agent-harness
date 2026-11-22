"""你的钩子（hooks）：在几个固定的时刻，运行你自己配置的命令。

为什么要有它：确认卡片让你一次次地决定"这一步做不做"，但有些规矩你想一次写好、每次自动照办——
"不许删 ~/Documents 里的东西""改完 Python 文件就跑一遍格式化""每轮答完往我的日志里记一笔""定时任务跑完发个通知"。
这些写成一个小脚本，挂在对应的时刻上，智狐到了那个时刻就运行它，看它怎么说。

这套做法照着 Claude Code 的 hooks（https://code.claude.com/docs/en/hooks）：同样的事件名、同样的 matcher 概念、
同样通过标准输入交一份 JSON、同样用退出码和标准输出的 JSON 回答。写过 Claude Code 钩子的人，脚本几乎不用改
（工具的名字不一样：这里是 run_command、str_replace_based_edit_tool……，而不是 Bash、Edit）。

## 四个时刻（事件）

  PreToolUse        一个工具马上要执行的时候：已经过了确认这一关（你点了允许，或者这一档本来就不用问），还没真的做。
                    钩子可以拦下它（这次调用照样有结果，模型看到"这次调用被用户的钩子拦下了……"和原因），
                    也可以要你再确认一次（哪一档都问）；不能改它的参数，也不能让一个本来要问你的操作不问。
  PostToolUse       工具做完了。钩子可以补一小段话，接在这次的工具结果后面交给模型（最多 config.HOOK_NOTE_CHARS 个字）。
                    补的话是在这条结果刚生成、还没放进历史的时候接上的：历史里已有的内容一个字都不改（只在末尾追加的规矩）。
                    结果先经过别的处理（洗掉坏字符、以后的输出脱敏），钩子看到的就是模型将看到的那一份。
  UserPromptSubmit  你发了一条消息、它进历史之前。钩子可以拦下它（原因给你看，消息不发给模型、不花钱），
                    也可以附一段说明交给模型（和"当前时间""可用的 Skill"一样附在这条消息里，见 Agent.notes）。
                    "重新生成"不再跑它：那条消息原样留在历史里，没有重新发。
  Stop              一轮答完了（答完、被截断、被拒绝、步数用完）。只是通知：拿来跑格式化、记日志、发通知。
                    你点了停止、出错了、消息被钩子拦下的那一轮不跑。

子 Agent（见 agents/）的工具调用也跑 PreToolUse、PostToolUse（JSON 里多一项 agent_type）；它的"一轮"不是你发的消息，
也不是你在等的那个回答，所以不跑 UserPromptSubmit 和 Stop。

## 在哪儿跑：你的对话、定时任务、聊天通道都跑，默认三处都算

每个钩子有 runsIn（chat / task / channel，默认三个都有）。为什么默认在没人看着的运行里也跑：
钩子只能让事情更严（拦下、再问你一次），不能放宽任何一条确认的规矩，所以多跑不会让哪里变得不安全；
反过来，一个管安全的钩子（"不许 rm -rf"）要是在定时任务里悄悄不算数，那正好是没人看着、最需要它的时候。
只想在某一处跑的（比如"任务跑完发通知"只要 task），把 runsIn 改小就行；JSON 里也有 source，脚本自己也能分。

## 交给钩子的 JSON（标准输入）

    {
      "hook_event_name": "PreToolUse",      事件
      "hook_name": "no-rm",                 这个钩子的名字（智狐自己的）
      "session_id": "20261122-…",          会话的编号（定时任务、聊天通道的也是它们那段会话的；还没存过的可能是空的）
      "cwd": "/Users/me/work",              工作目录。钩子就在这里运行
      "permission_mode": "ask",             这次运行的确认档位：ask / readonly / edits / all
      "source": "chat",                     chat 你的对话 / task 定时任务 / channel 聊天通道
      "provider": "anthropic", "model": "claude-opus-5-5",
      "agent_type": "资料研究员",            只有子 Agent 的工具调用才有
      "test": true,                         只有页面上"试运行"时才有

      PreToolUse、PostToolUse 多这几项：
      "tool_name": "run_command",           工具的真名（工具包的成员写成 browser.left_click）
      "tool_group": "shell",                属于哪种功能（和信息栏"智狐能做的事"是同一张表）
      "tool_input": {"command": "ls"},      模型给的参数，原样
      "tool_use_id": "toolu_…",
      "mcp_server": "github",               只有 MCP 的工具才有

      PostToolUse 再多：
      "tool_response": "…",                 工具的结果（交给模型的那段文字）
      "is_error": false,                    这次是不是出错了（Claude Code 把出错的另算一个事件，这里合在一起）
      "duration_ms": 12

      UserPromptSubmit 多：
      "prompt": "你写的话",
      "attachments": [{"name": "a.png", "kind": "image", "media_type": "image/png", "size": 1234}]

      Stop 多：
      "last_assistant_message": "这一轮最后的回答",
      "ended": "done",                      done / truncated / refused / steps
      "stop_hook_active": false             永远是 false（Stop 在这里只是通知，不会让这一轮接着做）
    }

另外环境变量里有 HARNESS_HOOK_EVENT、HARNESS_HOOK_NAME、HARNESS_PROJECT_DIR（工作目录，相当于 Claude Code 的 CLAUDE_PROJECT_DIR）。

## 钩子怎么回答（退出码和标准输出，见 protocol.py）

  退出码 0   成功。标准输出可以是一个 JSON 对象（字段名和 Claude Code 的一样）：
               {"decision": "block", "reason": "…"}                      拦下（PreToolUse、UserPromptSubmit）；
                                                                         PostToolUse 的 reason 接在结果后面
               {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                  "permissionDecision": "deny" | "ask",
                  "permissionDecisionReason": "…"}}                      拦下 / 再问你一次。"allow" 不会放宽，当它没说
               {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                  "additionalContext": "…"}}                             接在结果后面（UserPromptSubmit 的附在消息里）
               {"systemMessage": "…"}                                    给你看的一句话（任何事件）
             不是 JSON 的输出：UserPromptSubmit 的当成附给模型的说明，别的事件不看
  退出码 2   拦下，标准错误就是原因（PostToolUse 的接在结果后面，Stop 的给你看）
  别的       钩子自己出错了：提醒你一句，照常继续。超时（默认 config.HOOK_TIMEOUT 秒）、启动不了也一样。
             onFailure 设成 block 的，PreToolUse 和 UserPromptSubmit 上当成拦下（管安全的钩子坏了时宁可不做）

几个钩子对上同一个时刻时，按文件里的顺序一个接一个地跑（Claude Code 是同时跑），第一个拦下的说了算，后面的不再跑。

## 安全：钩子就是在你的电脑上运行命令，所以和 stdio 的 MCP 服务器守同样的规矩

  - 只有你能添加和修改：网页上带口令的页面（扩展 → 钩子）、命令行的 harness hooks，或者你自己改 data/hooks.json。
    模型没有任何能添加、修改钩子的工具；这个文件和 .env、mcp.json 一样在文件工具的 protected 名单里，任何模式下都碰不了；
  - 命令用参数列表启动，从不经过 shell（runner.py）；
  - 这次运行的命令沙箱（sandbox/）开着的话，钩子和 run_command 关在同一个沙箱里、同一档（runner.jailed）：
    钩子自己的程序和脚本可以读；沙箱开着却用不了时钩子不运行，绝不悄悄在沙箱之外运行；
  - 环境里去掉各家模型的密钥和聊天机器人的 token；配置里引用它们（$ANTHROPIC_API_KEY、${…}、%…%、$env:…）的，保存时就退回，
    运行前再查一遍（config.model_key_references）；
  - 参数里可能有令牌：文件权限 600、导出时遮住（export.MASKED_FILES）、页面上只显示遮住以后的样子；
  - 钩子只能让确认更严，不能放宽：拦下、再问你一次可以；"allow"、改参数、改掉工具的结果都不认；
  - 在运行记录（trace/）里每跑一个钩子画一段：跑了多久、怎么说的，你看得到它做了什么。

文件：config.py（data/hooks.json 的格式、检查、读写）、runner.py（启动一个命令、收输出、超时）、
protocol.py（退出码和输出怎么解释）、engine.py（这个时刻跑哪几个、怎么照办）、cli.py（harness hooks）。
"""

from .config import EVENTS, HookConfig, HookError
from .engine import HookRun, Hooks

NO_HOOKS = HookRun(None)  # 没接上钩子的助手（测试里自己组装的 Agent）用它：每个方法都什么都不做

__all__ = ["EVENTS", "NO_HOOKS", "HookConfig", "HookError", "HookRun", "Hooks"]
