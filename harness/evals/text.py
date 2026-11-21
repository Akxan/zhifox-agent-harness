"""评测在终端里给人看的几种写法：钱、百分比、用时，以及把几列对齐（中文一个字占两格）。"""

from __future__ import annotations

import unicodedata


def money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"${value:.4f}" if abs(value) < 10 else f"${value:.2f}"


def signed_money(value: float) -> str:
    return ("+" if value >= 0 else "-") + money(abs(value))


def percent(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def seconds(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f} s"


def width(text: str) -> int:
    """在终端里占几格：中文、全角字符占两格。"""
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


def pad(text: str, size: int) -> str:
    return text + " " * max(0, size - width(text))


def table(rows: list[list[str]], indent: str = "") -> list[str]:
    """几行几列对齐成一张表（最后一列不补空格）。"""
    if not rows:
        return []
    sizes = [max(width(row[n]) for row in rows if n < len(row)) for n in range(max(len(row) for row in rows))]
    return [indent + "  ".join(pad(cell, sizes[n]) if n < len(row) - 1 else cell for n, cell in enumerate(row)).rstrip() for row in rows]
