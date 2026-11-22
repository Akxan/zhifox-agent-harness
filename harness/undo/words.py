"""撤回、重做时给人看的话：为什么要先确认、为什么做不了。网页和终端说的是同一句（只写在这里）。

页面拿到的是带原文的话（server 用 said() 包一层，见 webui/changes_api.py），换语言时照着原文在原地重译。
"""

from __future__ import annotations

from ..config import UNDO_FILE_BYTES, UNDO_TURN_BYTES
from ..display import file_size
from ..i18n import Message, k, t

# 要你确认的
ASKING = {
    "later": k("后面还有 {n} 轮也改过它：撤回会把它恢复成这一轮之前的样子，后面那几轮对它的改动也会一起没了。"),
    "changed": k("这一轮之后它又被改过（可能是你自己、执行的命令或者别的程序）：撤回会把那些改动一起冲掉。"),
    "missing": k("这一轮之后它被删掉了：撤回会按这一轮之前的样子把它重新建出来。"),
    "outside": k("它在工作目录之外。"),
    "private": k("它在智狐自己的数据目录里。"),
}
# 重做时同一个原因的说法（只有"又被改过"不一样）
REDO_ASKING = {"changed": k("撤回以后它又被改过：重做会把那些改动冲掉。"), "missing": k("撤回以后它被删掉了：重做会把它重新建出来。")}
# 做不了的
BLOCKING = {
    "protected": k("这个文件保存着密钥或者权限的设置，不能撤回。"),
    "moved": k("这个路径现在指向别处（换成了链接），为了安全不往那里写。"),
    "directory": k("这个位置现在是一个文件夹。"),
    "snapshot": k("留底不见了或者对不上（被改过、坏了）。"),
    "big": k("文件太大（超过 {size}），当时没有留底。"),
    "budget": k("这一轮留的底超了上限（{size}），这个文件没有留底。"),
    "unreadable": k("当时读不出这个文件，没有留底。"),
    "done": k("已经撤回过了。"),
    "applied": k("没有撤回过，不用重做。"),
    "failed": k("没能写进去：{error}"),
}


def explain(action: str, issue: dict) -> list[Message]:
    """一个文件（restore.Issue 整理成的 dict）的每个原因，各一句话。"""
    said = []
    for reason in issue["reasons"]:
        if action == "redo" and reason in REDO_ASKING:
            said.append(t(REDO_ASKING[reason]))
        elif reason in ASKING:
            said.append(t(ASKING[reason], n=issue.get("later", 0)))
        elif reason in BLOCKING:
            size = file_size(UNDO_FILE_BYTES if reason == "big" else UNDO_TURN_BYTES)
            said.append(t(BLOCKING[reason], size=size, error=issue.get("error", "")))
    return said
