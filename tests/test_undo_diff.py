"""撤回功能里和系统无关的两件小事：改动的预览（harness/undo/diff.py），和比较路径时的规整写法（restore.same_path）。

预览交给页面的是一行一行的数据（[种类, 原来的行号, 现在的行号, 字]），不是 HTML，所以这里直接比数据。
\\r\\n 和 \\n 算同一种换行：Windows 上写的文件和别处的比，不能整篇都算改了。

一轮里调用文件工具、撤回和重做在 test_undo.py 里测。
"""

from __future__ import annotations

import sys

import pytest

from harness.config import UNDO_DIFF_LINE_CHARS
from harness.undo import diff
from harness.undo.restore import same_path

# ---- 按行拆开 ----------------------------------------------------------------


def test_lines_treat_crlf_and_lf_the_same():
    """\\r\\n 和 \\n 都算换行，拆出来的行一样；最后一行有没有换行不影响行数。"""
    assert diff.lines_of(b"a\r\nb\r\n") == diff.lines_of(b"a\nb\n") == diff.lines_of(b"a\nb") == ["a", "b"]
    assert diff.lines_of(b"") == []
    assert diff.lines_of("中文\r\n".encode()) == ["中文"]


@pytest.mark.parametrize("data", [b"a\x00b\n", b"\xff\xfe\xfd", b"ok\n\x80\n"])
def test_binary_is_not_split(data):
    """有 NUL、不是 UTF-8 的不当文字比（按行比出来也没有意义），只说改了。"""
    assert diff.lines_of(data) is None
    assert diff.counts(data, b"text\n") is None and diff.counts(b"text\n", data) is None
    assert diff.preview(data, b"text\n") == {"binary": True, "rows": [], "more": 0}
    assert diff.preview(b"text\n", data) == {"binary": True, "rows": [], "more": 0}


# ---- 加了几行、删了几行 ------------------------------------------------------


def test_counts():
    """页面上的 +N −M：改一行算加一行、删一行；只换了换行的写法不算改。"""
    assert diff.counts(b"a\nb\nc\n", b"a\nB\nc\nd\n") == (2, 1)
    assert diff.counts(b"", b"x\ny\n") == (2, 0)
    assert diff.counts(b"x\ny\n", b"") == (0, 2)
    assert diff.counts(b"one\r\ntwo\r\n", b"one\ntwo\n") == (0, 0)
    assert diff.counts(b"one\r\ntwo\r\nthree\r\n", b"one\nTWO\nthree\n") == (1, 1)


# ---- 预览 --------------------------------------------------------------------


def test_preview_rows_format():
    """一段改动：先是一行 "@"（这一段从原来的第几行、现在的第几行起，字是空的），上下各带 3 行没变的；
    删掉的行没有现在的行号（0），加上的行没有原来的行号（0）。"""
    old = "".join(f"{n}\n" for n in range(1, 11)).encode()
    new = old.replace(b"5\n", b"five\n")
    assert diff.preview(old, new) == {
        "binary": False,
        "more": 0,
        "rows": [
            ["@", 2, 2, ""],
            [" ", 2, 2, "2"], [" ", 3, 3, "3"], [" ", 4, 4, "4"],
            ["-", 5, 0, "5"],
            ["+", 0, 5, "five"],
            [" ", 6, 6, "6"], [" ", 7, 7, "7"], [" ", 8, 8, "8"],
        ],
    }  # fmt: skip


def test_preview_of_a_new_file_and_no_change():
    """新建的文件（改之前是空的）全是加上的行；内容一样、或者只换了换行的写法，什么都不列。"""
    assert diff.preview(b"", b"a\nb\n")["rows"] == [["@", 1, 1, ""], ["+", 0, 1, "a"], ["+", 0, 2, "b"]]
    assert diff.preview(b"same\n", b"same\n") == {"binary": False, "rows": [], "more": 0}
    assert diff.preview(b"", b"") == {"binary": False, "rows": [], "more": 0}
    assert diff.preview(b"a\r\nb\r\n", b"a\nb\n") == {"binary": False, "rows": [], "more": 0}
    # \r\n 的文件改了一行：只列这一行，不是整篇
    rows = diff.preview(b"a\r\nb\r\nc\r\n", b"a\nB\nc\n")["rows"]
    assert [row[0] for row in rows] == ["@", " ", "-", "+", " "]


def test_far_apart_changes_are_separate_hunks():
    """离得远的两处改动分成两段，各有自己的 "@"，中间没变的不全列出来。"""
    old = "".join(f"{n}\n" for n in range(1, 31)).encode()
    new = old.replace(b"\n2\n", b"\nTWO\n").replace(b"\n28\n", b"\nTWENTY-EIGHT\n")
    rows = diff.preview(old, new)["rows"]
    heads = [row for row in rows if row[0] == "@"]
    assert heads == [["@", 1, 1, ""], ["@", 25, 25, ""]]
    assert [" ", 15, 15, "15"] not in rows


@pytest.mark.parametrize("limit", [1, 3, 7])
def test_preview_stops_at_the_limit_and_counts_the_rest(limit):
    """超过 limit 行的不列，more 说还有几行（"@" 那种标记行不算在内：它不是文件里的一行）。"""
    old = "".join(f"{n}\n" for n in range(1, 41)).encode()
    new = old.replace(b"\n3\n", b"\nthree\n").replace(b"\n30\n", b"\nthirty\n")
    full = diff.preview(old, new, limit=10_000)
    assert full["more"] == 0
    cut = diff.preview(old, new, limit=limit)
    assert cut["rows"] == full["rows"][:limit]
    assert cut["more"] == sum(row[0] != "@" for row in full["rows"][limit:]) > 0


def test_long_lines_are_cut():
    """压缩过的 JS、很长的一行 JSON：一行最多 UNDO_DIFF_LINE_CHARS 个字，超了的截断，末尾一个省略号；正好那么长的不动。"""
    long = "x" * (UNDO_DIFF_LINE_CHARS + 100)
    exact = "y" * UNDO_DIFF_LINE_CHARS
    rows = diff.preview(b"", f"{long}\n{exact}\n".encode())["rows"]
    assert rows[1][3] == "x" * (UNDO_DIFF_LINE_CHARS - 1) + "…" and len(rows[1][3]) == UNDO_DIFF_LINE_CHARS
    assert rows[2][3] == exact


# ---- 比较路径 ----------------------------------------------------------------


def test_same_path_normalizes_dots_and_slashes():
    """多余的斜杠、"."、".."、末尾的斜杠规整掉以后再比：清单里记的写法和现在算出来的写法不一样时，也认得出是同一个文件。"""
    assert same_path("/w/a/../b.txt") == same_path("/w/b.txt")
    assert same_path("/w//sub/./b.txt") == same_path("/w/sub/b.txt")
    assert same_path("/w/sub/") == same_path("/w/sub")
    assert same_path("/w/a.txt") != same_path("/w/b.txt")


@pytest.mark.skipif(sys.platform != "win32", reason="只有 Windows 上的 normcase 不分大小写、把 / 换成 \\；别的系统上它什么都不做")
def test_same_path_on_windows_ignores_case_and_slash_style():
    """Windows 上不分大小写、正反斜杠一样：C:\\Users\\X\\a.txt 和 c:/users/x/A.TXT 是同一个文件。"""
    assert same_path("C:\\Users\\X\\a.txt") == same_path("c:/users/x/A.TXT")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 上 normcase 不分大小写，这条只在 macOS、Linux 上成立")
def test_same_path_keeps_case_elsewhere():
    """macOS、Linux 上 same_path 不改大小写（Linux 的文件系统本来就分大小写；normcase 在这两个系统上什么都不做）。"""
    assert same_path("/w/A.txt") != same_path("/w/a.txt")
