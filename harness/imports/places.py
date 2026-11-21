"""这台电脑上，别的工具把文件放在哪儿。

各家工具在三种系统上的习惯不一样：

  命令行工具（Claude Code、Codex、Gemini CLI……）   一般在家目录下的一个点开头的文件夹：~/.codex、~/.gemini
  遵守 XDG 的（Goose、OpenCode、Zed 在 Linux 上）  ~/.config/<名字>（设了 XDG_CONFIG_HOME 就用它）
  桌面应用（Claude Desktop、VS Code）              macOS ~/Library/Application Support/<名字>
                                                   Windows %APPDATA%\\<名字>
                                                   Linux ~/.config/<名字>

Env 把"家目录在哪、工作目录在哪、什么系统、有哪些环境变量"收在一起，登记表（registry.py）照着它算出每个位置。
测试里换一个 Env 就能假装在 Windows 上，不用真的去改这台电脑的家目录。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

# 登记表会看的环境变量：它们改了某个工具放文件的地方。测试和演示会把它们清掉，免得读到这台电脑上真正的配置
VARIABLES = ("APPDATA", "XDG_CONFIG_HOME", "CODEX_HOME", "COPILOT_HOME", "CLINE_DIR")


@dataclass(frozen=True)
class Env:
    home: Path
    workspace: Path  # 智狐的工作目录：项目级的配置（.cursor/mcp.json 这些）在它下面找
    platform: str = sys.platform  # "darwin"、"linux"、"win32"
    environ: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def here(cls, workspace: Path) -> Env:
        """这台电脑现在的样子。"""
        return cls(home=Path.home(), workspace=Path(workspace), platform=sys.platform, environ={name: os.environ[name] for name in VARIABLES if name in os.environ})

    @property
    def windows(self) -> bool:
        return self.platform == "win32"

    @property
    def mac(self) -> bool:
        return self.platform == "darwin"

    def variable(self, name: str) -> Path | None:
        """一个指向文件夹的环境变量（CODEX_HOME 这种，必须在 VARIABLES 里）。没设、或者不是绝对路径的，当作没设。"""
        value = self.environ.get(name, "").strip()
        return Path(value) if value and Path(value).is_absolute() else None

    @property
    def appdata(self) -> Path:
        """Windows 的 %APPDATA%（漫游的那个）。"""
        return self.variable("APPDATA") or self.home / "AppData" / "Roaming"

    @property
    def xdg_config(self) -> Path:
        """Linux 的 $XDG_CONFIG_HOME，默认 ~/.config。"""
        return self.variable("XDG_CONFIG_HOME") or self.home / ".config"

    def app_data(self, name: str) -> Path:
        """桌面应用放用户数据的文件夹：Claude Desktop、VS Code 这种 Electron 应用都按这个规矩。"""
        if self.mac:
            return self.home / "Library" / "Application Support" / name
        if self.windows:
            return self.appdata / name
        return self.xdg_config / name

    def shown(self, path: Path) -> str:
        """给人看的写法：家目录写成 ~，工作目录里的写成相对路径（.cursor/mcp.json），分隔符一律用 /。"""
        for base, prefix in ((self.workspace, ""), (self.home, "~/")):
            try:
                relative = path.relative_to(base)
            except ValueError:
                continue
            if base == self.workspace and self.workspace == self.home:
                prefix = "~/"  # 在家目录里运行：项目级和用户级是同一处，按用户级写
            return prefix + relative.as_posix() if relative.parts else prefix.rstrip("/") or "."
        return path.as_posix()
