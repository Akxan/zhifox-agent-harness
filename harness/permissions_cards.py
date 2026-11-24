"""权限 · 确认卡片上的那句话：几个小工具，各种操作的 review_xxx 都用（见 permissions.py 开头）。

  offering   问题上多挂一个选择（"这段对话里不再问"），页面照着它画按钮（见 webui/ui.py 的 confirm）
  always     问题上标一下"在任何模式下都要问"：页面不放"不想每次都问？"的入口，也不给"不再问"
  _preview   要写进去、要输入的内容缩进着显示，太长只显示开头

为什么单独一个文件：执行命令（permissions_command.py）、浏览器、操作整台电脑、文件和定时任务（permissions_review.py）都要用它们，
放进其中哪一个，别的就得为了这几行去 import 那一个。这里只管"怎么写"，"问不问"不在这里定。
"""

from __future__ import annotations

from .i18n import t


def offering(question: str, offer: str) -> str:
    """一个问题，确认卡片上多给一个选择（offer 是固定的几个词之一，页面按它画按钮，见 webui/ui.py 的 confirm）。
    问题本身是 t() 给的一句话（带着原文，页面换语言时能重译），选择记在它身上，不改它的文字。"""
    # question 其实是 t() 给的 Message（str 的子类，能挂上属性），页面用 getattr 取（见 webui/ui.py 的 confirm）；类型上写的是 str
    question.offer = offer  # type: ignore[attr-defined]  # 见上一行
    return question


def always(question: str) -> str:
    """一个在任何模式下都要问的问题：页面上就不放"不想每次都问？"的入口（换哪一档都不会少问这一次），也不放"不再问"的选择。"""
    question.always = True  # type: ignore[attr-defined]  # 和 offering 一样：挂在 t() 给的 Message 上
    return question


def _preview(text: object, max_lines: int = 12) -> str:
    """把要写入的内容缩进显示出来，太长就只显示开头。"""
    lines = str(text if text is not None else "").split("\n")
    shown = [f"    {line}" for line in lines[:max_lines]]
    if len(lines) > max_lines:
        shown.append("    " + t("……（共 {n} 行）", n=len(lines)))
    return "\n".join(shown)
