"""自检的结果写成给人看的表格：每一项一行（状态、名字、现在的样子），有问题的下面跟着怎么修。

  环境
    ✓ 好      Python        3.11.9 · /…/.venv/bin/python（项目的 .venv）
    ✗ 有问题  数据目录      /…/data：别的账号也能进：权限是 755，应该是 700
                → 在 zsh 里运行：chmod 700 /…/data

返回（文字，样式）的列表，和 model_catalog.lines 一样：harness doctor 照样打印，终端界面的 /doctor 交给 TerminalUI 上色。
样式是 plain（普通）、info（暗一点的说明）、error（有问题的那一行）。

对齐按"在终端里占几格"算：汉字和全角符号占两格（unicodedata 的 East Asian Width 是 W、F 的），
不然中文的名字一长，后面那一列就歪了。名字最多对齐到 NAME_COLUMNS 格，再长的（西班牙语）不再往后推。
"""

from __future__ import annotations

import unicodedata

from ..config import APP_NAME, COMMAND
from ..i18n import t
from .report import OK, PROBLEM, STATUS_TEXT, STATUSES, UNUSED, Report

MARKS = {OK: "✓", PROBLEM: "✗", UNUSED: "–"}
STYLES = {OK: "plain", PROBLEM: "error", UNUSED: "info"}
NAME_COLUMNS = 24


def columns(text: str) -> int:
    """在终端里占几格。"""
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - columns(text))


def lines(report: Report) -> list[tuple[str, str]]:
    app = report.app
    version = t("{version} · 提交 {commit}", version=app["version"] or "?", commit=app["commit"]) if app["commit"] else (app["version"] or "?")
    out: list[tuple[str, str]] = [(t("{name}自检（{version}）", name=APP_NAME, version=version), "plain")]
    out.append((t("只看不改：不联网，不改任何文件、权限和设置；密钥只说有没有。"), "info"))
    words = {status: t(STATUS_TEXT[status]) for status in STATUSES}
    status_width = max(columns(word) for word in words.values())
    name_width = min(NAME_COLUMNS, max((columns(str(check.label)) for check in report.checks()), default=0))
    for section in report.sections:
        out.append(("", "plain"))
        out.append((str(section.title), "plain"))
        for check in section.checks:
            row = f"  {MARKS[check.status]} {pad(words[check.status], status_width)}  {pad(str(check.label), name_width)}  {check.detail}"
            out.append((row.rstrip(), STYLES[check.status]))
            indent = " " * (4 + status_width + 2)
            out += [(f"{indent}→ {fix}", "plain") for fix in check.fix]
    out.append(("", "plain"))
    summary = t(
        "{problems} 项有问题，{ok} 项好，{unused} 项没用上。",
        problems=report.count(PROBLEM), ok=report.count(OK), unused=report.count(UNUSED),
    )
    out.append((summary, "error" if report.count(PROBLEM) else "plain"))
    out.append((t("要贴给别人帮你看的话，用 {command} doctor --json：里面没有密钥和口令。", command=COMMAND), "info"))
    return out
