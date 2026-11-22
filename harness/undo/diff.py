"""改动的预览：一个文件改之前和改之后，按行比一比（页面上展开"改了 N 个文件"时看的就是它）。

交给页面的是一行一行的数据，不是 HTML：页面用 textContent 一行行画出来（见 page/scripts/file-changes.js），
文件里写着什么都只会是文字。

  rows   [[种类, 原来的行号, 现在的行号, 这一行的字], …]
         种类：" " 没变（上下文）、"-" 删掉的、"+" 加上的、"@" 一段改动的开头（行号是这一段从哪儿起，字是空的）
         没有的行号是 0（加上的行没有原来的行号）
  more   太多了没列出来的还有几行（最多 config.UNDO_DIFF_LINES 行）
  binary 不是文本（有 NUL、不是 UTF-8）：不比，只说改了

用的是标准库的 difflib：和 git diff 不完全一样（它们找"最像的那一段"的办法不同），但预览只是让你看一眼改了哪儿，够用。
"""

from __future__ import annotations

from difflib import SequenceMatcher

from ..config import UNDO_DIFF_LINE_CHARS, UNDO_DIFF_LINES

CONTEXT = 3  # 每段改动上下各带几行没变的


def lines_of(data: bytes) -> list[str] | None:
    """按行拆开。不是文本返回 None。\\r\\n 和 \\n 一样算换行：Windows 上写的文件和别处的比，不会整篇都算改了。"""
    if b"\x00" in data:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text.splitlines()


def counts(old: bytes, new: bytes) -> tuple[int, int] | None:
    """加了几行、删了几行。不是文本返回 None。"""
    before, after = lines_of(old), lines_of(new)
    if before is None or after is None:
        return None
    added = removed = 0
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, before, after).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def preview(old: bytes, new: bytes, limit: int = UNDO_DIFF_LINES) -> dict:
    """页面上的预览（格式见文件开头）。"""
    before, after = lines_of(old), lines_of(new)
    if before is None or after is None:
        return {"binary": True, "rows": [], "more": 0}
    rows: list[list] = []
    more = 0

    def add(row: list) -> None:
        nonlocal more
        if len(rows) < limit:
            rows.append(row)
        elif row[0] != "@":
            more += 1

    for group in SequenceMatcher(None, before, after).get_grouped_opcodes(CONTEXT):
        first = group[0]
        add(["@", first[1] + 1, first[3] + 1, ""])
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for offset in range(i2 - i1):
                    add([" ", i1 + offset + 1, j1 + offset + 1, _cut(before[i1 + offset])])
                continue
            for offset in range(i2 - i1):
                add(["-", i1 + offset + 1, 0, _cut(before[i1 + offset])])
            for offset in range(j2 - j1):
                add(["+", 0, j1 + offset + 1, _cut(after[j1 + offset])])
    return {"binary": False, "rows": rows, "more": more}


def _cut(line: str) -> str:
    return line if len(line) <= UNDO_DIFF_LINE_CHARS else line[: UNDO_DIFF_LINE_CHARS - 1] + "…"
