"""自定义 Agent：你定制的、擅长某一类事的助手。一个 Agent 就是 data/agents/ 里的一个 Markdown 文件
（格式和 Claude Code 的 subagent 一样，见 spec.py）：开头几行写它叫什么、擅长什么、能用哪些工具、用哪个模型，
下面是它的设定。

同一个文件有两种用法。两者的区别，是理解"agent"这个词的关键：

  子 Agent（subagent）
    主助手正在和你对话，遇到一件"要查很多资料、读很多文件，但只需要结论"的事，就用 agent 工具把它交给一个 Agent。
    这个 Agent 从一段空白的历史开始，只看得到主助手写给它的任务；它在自己的历史里读网页、读文件、调用工具，
    做完了只把结论交回来。所以主助手的上下文里只多了一段结论，没有那几十页原文——这叫"上下文隔离"。
    代价是：它不知道你们之前聊了什么（任务里要写清楚），你也看不到它的思路（只看到它调用了哪些工具和最后的结论）。
    主助手什么时候交、交给谁，由主助手自己判断（它在每条消息前面看得到 <agents> 目录，见 catalog.py），
    你也可以直接说"让资料研究员查一下……"。

  角色（persona）
    你在"新对话"旁边选一个 Agent，用它开始一段新对话。这段对话从头到尾就是它：系统提示词换成它的设定，
    工具只有它的 tools 放开的那些，模型用它选的那个。它直接和你说话，看得到整段对话。
    一段对话的角色不能中途换（系统提示词和工具在一段对话里不能变，原因见 providers/claude.py），想换就开一段新的。

一句话：子 Agent 是主助手"派出去办事的人"，角色是"你直接找的那个人"。

这个包里每个文件讲一件事：

  spec.py          文件的格式：每一项的要求、怎么检查、怎么从表单写出来
  tools.py         tools 一项的写法，和"按它从一套工具里挑出能用的"（永远只会更少）
  store.py         data/agents/：列出、读、新建、改、删
  claude_code.py   从 Claude Code 导入（复制一份过来）
  examples.py      内置的几个示例（点一下才加进来）
  catalog.py       告诉主助手有哪些 Agent 可以交给（放在用户消息里，和 Skill 的目录一样）
  prompt.py        一个 Agent 的系统提示词：它的设定，加上程序附上的环境说明
  runner.py        把一项任务交给子 Agent：新的历史、它的工具和模型、确认、停止、记账、交回结果

主助手用的 agent 工具在 tools/agent.py；用一个 Agent 开始一段对话，在 app.py 的 new_conversation。
"""

from .store import (
    AgentConflict,
    AgentError,
    AgentInvalid,
    AgentStore,
    BadRequest,
    CustomAgent,
)

__all__ = ["AgentConflict", "AgentError", "AgentInvalid", "AgentStore", "BadRequest", "CustomAgent"]
