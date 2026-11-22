"""命令行里的 `harness doctor`：把自检的结果打印出来。有一项有问题，退出码就是 1（脚本、CI 里可以直接拿来判断）。

    harness doctor          给人看的表格（终端里有问题的那几行是红的）
    harness doctor --json   给程序看；贴给别人帮你排查也用它：里面没有密钥、口令和带口令的网址

颜色只在输出接着终端、又没有设置 NO_COLOR（https://no-color.org）时才有：重定向到文件、贴进聊天里的，是干净的文字。

--json 只用 ASCII 写（中文写成 \\uXXXX）：它多半是被接到管道、存成文件的。Windows PowerShell 5.1 读别的程序的输出时
按控制台的代码页（中文系统是 GBK，英文系统是 437）解码，智狐写出的 UTF-8 中文到了那里就成了乱码，存进文件也是乱码；
全是 ASCII 的话，哪种代码页读出来都一样，读 JSON 的程序照样认得 \\uXXXX。
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import IO

from ..config import Config
from ..i18n import t
from ..safe import to_json
from ..service import cli as service_cli
from . import examine
from .text import lines

RED, DIM, RESET = "\033[31m", "\033[2m", "\033[0m"
COLORS = {"error": RED, "info": DIM, "plain": ""}


def add_parser(commands: argparse._SubParsersAction) -> None:
    """在 harness 的命令行里加上 doctor 这条命令。"""
    parser = commands.add_parser("doctor", help=t("自检：一屏看清智狐在这台电脑上的情况（不联网，不改任何东西）"))
    parser.add_argument("--json", dest="doctor_json", action="store_true", help=t("输出 JSON：给程序看，或者贴给别人帮你排查（里面没有密钥和口令）"))


def run(args: argparse.Namespace, config: Config, out: IO[str] | None = None, system: service_cli.System | None = None) -> int:
    out = out or sys.stdout
    report = examine(config, system)
    if args.doctor_json:
        print(to_json(report.to_dict(), indent=2), file=out)
        return report.exit_code
    color = out.isatty() and not os.environ.get("NO_COLOR")
    for text, style in lines(report):
        print(f"{COLORS[style]}{text}{RESET}" if color and COLORS[style] and text else text, file=out)
    return report.exit_code
