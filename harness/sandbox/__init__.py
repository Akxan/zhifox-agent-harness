"""命令的沙箱：给 run_command 加的第二道关卡（可选，默认关着）。

为什么要它。run_command 以你的身份运行命令，能碰你能碰的一切。第一道关卡是"要不要先问你"（permissions.py）：
每条命令先给你看、你点了"允许"才执行。可是你会放宽它（"只读的命令不用问""全部自动允许"），
也会在手机上（聊天通道）匆匆看一眼就点；模型又可能被网页、文件里藏着的一句话带偏（提示注入）。
沙箱管的是另一件事：就算一条命令被放行了，它运行的时候也只碰得到该碰的地方。两道关卡各管各的，谁也不替代谁：
放宽了"问不问"，沙箱照样挡着；开了沙箱，该问的照样问。

什么是沙箱。操作系统给一个进程（和它开的所有子进程）划一个圈：圈外的文件读不到、写不了，网络连不上。
圈是操作系统内核来守的，不是智狐在命令运行前"检查一下命令写了什么"——那种检查一定有办法绕过去
（换个写法、写个脚本再运行它），内核这一层绕不过去。

三档（设置里选，定时任务、聊天通道可以各选各的，默认跟着设置）：
  关                     和以前一样
  只在工作目录里          能读写工作目录、能读系统目录、碰不到家目录里别的东西、碰不到 data/ 和 .env、不能联网。远程使用时推荐
  工作目录 + 联网         同上，加上能联网
具体哪些路径见 policy.py；每个系统怎么做见 macos.py（sandbox-exec）、linux.py（bubblewrap）、windows.py（暂时没有，写了为什么）。
这台电脑上用不了的时候，命令一律拒绝执行（不会悄悄在沙箱之外运行），并说清楚怎么修、或者怎么把它关掉。
被沙箱挡住、又确实需要的时候，模型可以请你放行一条（unsandboxed: true），规矩见 session.py。

沙箱挡不住的（docs/zh-CN/sandbox-and-hooks.md 里也写着）：
  - 工作目录本身：在里面删文件、改代码，沙箱不管（备份和"问不问"管）。
  - 以后才运行的东西：命令在工作目录里留下的脚本、git 的钩子和设置（.git/hooks、.git/config）、package.json 里的 scripts，
    你以后在沙箱之外运行它们时，照样以你的身份运行。
  - "工作目录 + 联网"那一档：工作目录里的东西可以被发到网上去。
  - 被你放行的那一条，和关掉沙箱的任务、通道。
  - 操作系统自己的漏洞：沙箱是内核守的，内核有漏洞时也守不住。
"""

from __future__ import annotations

from .backend import Backend, detect
from .policy import CHOICES, DEFAULT, LEVEL_TEXT, LEVELS, Policy
from .session import SCOPES, Facts, Jail, Layout, Sandbox, escape_question, question

__all__ = [
    "CHOICES",
    "DEFAULT",
    "LEVELS",
    "LEVEL_TEXT",
    "SCOPES",
    "Backend",
    "Facts",
    "Jail",
    "Layout",
    "Policy",
    "Sandbox",
    "detect",
    "escape_question",
    "question",
]
