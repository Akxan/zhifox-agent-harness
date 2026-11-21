"""看后台服务的日志文件（macOS 上 launchd 的那一份、Windows 上任务计划程序的那一份；Linux 上的在 journald 里，
用 journalctl 看，见 systemd.py）。

全部用 Python 自己读文件，不借用 tail：Windows 上没有 tail，logs -f 也是这里一小段一小段地接着读（见 follow）。

日志一行一条，格式是 log.py 定的："2026-11-21 14:03:00 warning 还没有设置任何聊天通道……"。
另外还可能有 Python 自己写到标准错误里的东西：启动时就出了错（找不到模块、没有权限读某个文件夹），
那时日志还没接好，它会写一段 Traceback，最后一行是 "ModuleNotFoundError: …"、"PermissionError: …"。
status 要的"最近的错误"就是这两种行。
"""

from __future__ import annotations

import codecs
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO

from ..i18n import t

TAIL_BYTES = 256_000  # 看最后几行时，只读文件最后这么多字节（足够几千行）
# 后台服务每次开起来，在日志里先写一行"从这里开始"（见 log.setup）。不翻译：程序要靠它认出"这一次运行"从哪儿开始
START_MARK = "---- zhifox started, pid "
_LEVEL = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d (warning|error|critical) ")
_CRASH = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception)\b")  # 没接住的异常的最后一行
DENIED = "Operation not permitted"  # macOS 拦住了对某个文件夹的访问时，系统给的那句话


def tail(path: Path, lines: int) -> list[str]:
    """文件的最后 lines 行。文件不在、读不了时是空的。"""
    try:
        with open(path, "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - TAIL_BYTES))
            data = handle.read()
    except OSError:
        return []
    rows = data.decode("utf-8", "replace").splitlines()
    if size > TAIL_BYTES:
        rows = rows[1:]  # 从中间开始读的，第一行多半只有后半截
    return rows[-lines:] if lines > 0 else []


def show(path: Path, lines: int, follow_on: bool, out: IO[str], sleep: Callable[[float], None] = time.sleep) -> int:
    """`harness service logs [-f]`：打印最后 lines 行；follow_on 时接着往下看，按 Ctrl-C 结束。"""
    if not path.is_file():
        print(t("还没有日志：{path}", path=path), file=out)
        return 0
    for line in tail(path, lines):
        print(line, file=out)
    if follow_on:
        try:
            follow(path, out, sleep=sleep)
        except KeyboardInterrupt:
            pass
    return 0


def recent_problems(path: Path, count: int) -> list[str]:
    """日志文件里，最后一次开起来以后的最后 count 条警告和错误（status 用）。"""
    return problems(tail(path, 2000), count)


def problems(lines: list[str], count: int) -> list[str]:
    """最后一次开起来以后（见 since_start），最后 count 条警告和错误。"""
    return [line for line in since_start(lines) if _LEVEL.match(line) or _CRASH.match(line)][-count:]


def since_start(lines: list[str]) -> list[str]:
    """最后一次"从这里开始"（START_MARK）之后的那些行；一次都没有的话（比如开不起来、一直在出错）是全部。

    为什么：status 里列"最近的警告和错误"，是想告诉你现在这一次有没有问题。装好之前那几次失败
    （比如还没授权读"文稿"时的 Operation not permitted）要是也列出来，看着像是现在还有问题。
    开不起来时，出错是在写下这一行之前，所以照样列得出来。
    """
    for index in range(len(lines) - 1, -1, -1):
        if START_MARK in lines[index]:
            return lines[index + 1 :]
    return lines


def follow(path: Path, out: IO[str], keep_going: Callable[[], bool] = lambda: True, sleep: Callable[[float], None] = time.sleep) -> None:
    """接着往下看：文件变长了，就把新写的部分打印出来。

    日志写满了会换一个新文件（旧的改名成 .1），服务重装时文件也可能换掉：发现换了（文件编号变了，或者比读到的位置还短），
    就从新文件的开头接着读。按字节读、增量地解码，一个汉字被拆在两次读之间也不会变成乱码。
    """
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    identity, position = None, 0
    try:
        found = os.stat(path)
        identity, position = (found.st_dev, found.st_ino), found.st_size
    except OSError:
        pass
    while keep_going():
        try:
            found = os.stat(path)
        except OSError:
            sleep(0.5)
            continue
        if (found.st_dev, found.st_ino) != identity or found.st_size < position:
            identity, position = (found.st_dev, found.st_ino), 0
            decoder.reset()
        if found.st_size > position:
            try:
                with open(path, "rb") as handle:
                    handle.seek(position)
                    chunk = handle.read()
            except OSError:
                chunk = b""
            position += len(chunk)
            out.write(decoder.decode(chunk))
            out.flush()
        sleep(0.5)
