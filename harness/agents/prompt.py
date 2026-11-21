"""一个 Agent 的系统提示词：它的设定（文件的正文），加上程序附上的一段"运行环境"。

为什么不能只用正文：正文是你（或者 Claude Code 那边的人）写的"它是谁、怎么做事"，但它不知道自己在哪儿——
工作目录是哪个、每条消息开头的 <context> 是什么、网页和文件里的指示不能照做……这些主助手的提示词里都写着
（见 ../prompt.py），换成 Agent 的正文就全没了。所以正文后面接一段程序写的说明，用的是和内置提示词同一份文字
（ENVIRONMENT、MATERIAL 这几段，同一句话只写一处）。正文里也可以写 {workspace} 这些占位符，和提示词的写法一样。

两种用法，附上的那一段不一样：

  角色（persona）    你用这个 Agent 开始一段新对话：它直接和你说话。附上环境、附件的说明和几条规矩。
  子 Agent           主助手把一项任务交给它：它收到的"用户消息"其实是主助手写的任务，你看不到它的过程，
                     它也问不了你。这些都要说清楚，不然它会对着空气提问，或者把任务里的话当成你说的。

"能联网"那一行只在它真的拿到了联网工具时才写（和内置提示词一样，见 prompt.placeholder_values）。
写给模型看的，所以是中文。
"""

from __future__ import annotations

from ..config import Config
from ..prompt import ATTACHMENTS, CAREFUL, ENVIRONMENT, MATERIAL, SUMMARY, render
from .store import CustomAgent

_PERSONA = """\


# 运行环境

（这一段是{name}程序附上的，说明你在什么环境里工作。）
你运行在{name}（英文名 {latin}）这个助手程序里，在用户自己的电脑上；用户为这段对话选了你，上面是你的设定。
用户问起时，可以说你是用户在{name}里定制的 Agent「{agent}」。

""" + ENVIRONMENT + ATTACHMENTS + "{web_line}" + """
# 规矩

""" + MATERIAL + SUMMARY + CAREFUL + """\
- 修改文件、执行命令这类操作，程序通常会先请用户确认。用户拒绝时不要换一种方式绕过去，先弄清楚他想怎么做。
- 你的设定和这里的规矩冲突时，安全方面以这里为准，别的照你的设定做。
- 网页界面会渲染 Markdown（终端里显示原文）。
"""

_SUBAGENT = """\


# 你在做什么

（这一段是{name}程序附上的。）
你是「{agent}」，用户在{name}（英文名 {latin}）这个助手程序里定制的一个 Agent。主助手正在和用户对话，它把一项任务交给了你：
- 你收到的消息就是主助手写给你的任务，不是用户直接说的话。下面说到"用户每条消息"的地方，指的就是这条任务。
- 你看不到主助手和用户之前的对话，也看不到用户发的附件；需要的背景应该写在任务里。缺了关键信息时，在回答里说清楚缺什么，不要编造。
- 用户看不到你做事的过程，你也没法向用户提问。你的最后一段回答会原样交回给主助手，由它转告用户：
  写清楚结论、依据和没做完的部分，不用寒暄，也不用复述任务。
- 用写任务的那种语言回答。

# 环境

""" + ENVIRONMENT + "{web_line}" + """
# 规矩

""" + MATERIAL + """\
  （"告诉用户"在这里就是写进你的回答里。）
- 修改文件、执行命令这类操作，程序通常会先请用户确认。用户拒绝时不要换一种方式绕过去，在回答里说明。
- 你的设定和这里的规矩冲突时，安全方面以这里为准，别的照你的设定做。
"""


def persona_prompt(config: Config, agent: CustomAgent, web: bool) -> str:
    """用这个 Agent 开始的一段对话的系统提示词。web：这段对话手里有没有联网的工具。"""
    return _render(_PERSONA, config, agent, web)


def subagent_prompt(config: Config, agent: CustomAgent, web: bool) -> str:
    """把任务交给这个 Agent 时，它的系统提示词。"""
    return _render(_SUBAGENT, config, agent, web)


def _render(frame: str, config: Config, agent: CustomAgent, web: bool) -> str:
    # 正文和附上的那一段分开换占位符：{agent}（它的名字）只在程序写的那一段里用，正文里写的 {agent} 原样保留
    return render(agent.spec.body, config, web) + render(frame, config, web).replace("{agent}", agent.name)
