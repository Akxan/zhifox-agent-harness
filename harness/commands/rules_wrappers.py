from __future__ import annotations

from typing import TYPE_CHECKING

from .words import Unclear

if TYPE_CHECKING:
    from .rules import Context


# ---- 套在别的命令外面的 --------------------------------------------------------------


def _env(args: list[str], context: Context) -> None:
    """env 命令：只许原样运行后面那条命令（它本身得是只读的）。光写 env 会打印所有环境变量（常常有密钥）；
    env 名字=值 命令、env -i、-S 都能改变那条命令的行为。"""
    words = args[1:] if args[:1] == ["--"] else args
    if not words:
        raise Unclear("env 会打印所有环境变量")
    if words[0].startswith("-") or "=" in words[0]:
        raise Unclear("env 改了环境变量")
    context.stage(words)


TIMEOUT_OPTIONS = {"--preserve-status": 0, "--foreground": 0, "-v": 0, "--verbose": 0, "-k": 1, "--kill-after": 1, "-s": 1, "--signal": 1}


def _timeout(args: list[str], context: Context) -> None:
    """timeout 时长 命令：后面那条命令得是只读的。timeout 碰到第一个不是选项的词就不再认选项。"""
    index = 0
    while index < len(args) and args[index].startswith("-"):
        name = args[index].partition("=")[0]
        if name not in TIMEOUT_OPTIONS:
            raise Unclear(f"timeout 不认识的选项 {name}")
        index += 1 + (TIMEOUT_OPTIONS[name] if "=" not in args[index] else 0)
    if index >= len(args) or not _duration(args[index]):
        raise Unclear("timeout 后面要先写时长")
    if index + 1 >= len(args):
        raise Unclear("timeout 后面没有命令")
    context.stage(args[index + 1 :])


def _duration(text: str) -> bool:
    number = text[:-1] if text[-1:] in "smhd" else text
    return number.replace(".", "", 1).isdigit()
