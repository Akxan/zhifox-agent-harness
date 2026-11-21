"""Skill：教助手做某一类事的"说明书"。

一个 Skill 是一个文件夹，里面的 SKILL.md 写着做这类事的步骤，旁边可以带着脚本、参考资料和模板。
格式是开放的 Agent Skills 标准（https://agentskills.io），Claude、Claude Code 和别家的工具用的都是它，
所以在那边写好、用好的 Skill，可以原样装进智狐。

为什么需要它：模型什么都懂一点，但不知道"你希望这件事怎么做"。每次都把要求重新说一遍很累，
写进系统提示词又会让每一次请求都背着所有的要求。Skill 的办法是：平时模型只看到每个 Skill 的名字和一句说明，
真要做这件事了，再把完整的说明读进来（见 catalog.py 开头的"三层"）。

这个包里每个文件讲一件事：

  frontmatter.py   读 SKILL.md 开头的那几行 YAML（自己写的小解析器，为什么不用 YAML 库也在那里）
  spec.py          规范：哪些字段、各有什么要求
  store.py         智狐自己的 Skill 文件夹（data/skills/）：列出、读、改、删、启用、停用
  catalog.py       告诉模型现在有哪些 Skill：放在哪里、变了怎么办
  bundle.py        从外面拿来的文件（压缩包、上传的文件夹）：装之前的安全检查
  download.py      从网址下载（GitHub 的文件夹、SKILL.md、zip）
  from_tools.py    从别的工具导入（Claude Code、Codex、Cursor……，复制一份过来）
  installer.py     先给你看、你确认了才装

模型读 Skill 用的是 tools/skill.py 里的 skill 工具，它只能读、不能改，也只能读启用着的 Skill。
Skill 里的脚本由模型用 run_command 运行，和别的命令一样要你确认：Skill 不能替你放开任何权限。
装、改、删 Skill 只能由你在网页上做，模型做不了。
"""

from .store import (
    BadRequest,
    Skill,
    SkillConflict,
    SkillError,
    SkillInvalid,
    SkillStore,
)

__all__ = ["BadRequest", "Skill", "SkillConflict", "SkillError", "SkillInvalid", "SkillStore"]
