"""配置的值里"引用一个环境变量"的各种写法 → 智狐的写法 ${名字}。

把密钥写在环境变量里、配置里只写一个引用，是各家都推荐的做法，只是写法不一样：

  ${名字}、${名字:-默认值}    Claude Code、GitHub Copilot、Kiro……（智狐也是这个写法，见 mcp/config.py 的 expand）
  ${env:名字}                Cursor、VS Code、Windsurf、Roo Code
  $名字                      Gemini CLI、Qwen Code（也认 ${名字}）
  {env:名字}                 OpenCode
  ${{ secrets.名字 }}        Continue（值一般写在 .env 文件里，Continue 自己去读）

导入时一律换成 ${名字}：智狐启动这个服务器时，从环境变量里取它的值。换过的名字记下来，页面上告诉你。

编辑器自己的几个变量（${userHome}、${workspaceFolder}……）在导入时直接换成这台电脑上的值。
要你当场输入的（VS Code 的 ${input:…}）、要从别的文件里读的（OpenCode 的 {file:…}）智狐做不到，
原样留着，提醒你导入后编辑这个服务器、填上真正的值。

换完之后，引用了智狐自己的模型密钥的（${ANTHROPIC_API_KEY} 这些）由 mcp/config.py 的 model_key_references 认出来，
这个服务器不导入。
"""

from __future__ import annotations

import os
import re

from ..i18n import Message, t
from .places import Env

_ENV_COLON = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
_DOLLAR = re.compile(r"(?<![\\$])\$([A-Za-z_][A-Za-z0-9_]*)")  # $名字（不是 ${……}，也不是 $$）
_BRACE = re.compile(r"(?<!\$)\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
_SECRETS = re.compile(r"\$\{\{\s*secrets\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_EDITOR = re.compile(r"\$\{(userHome|workspaceFolder|workspaceFolderBasename|pathSeparator|/)\}")
# 智狐做不到的：要你当场输入的、要从别的文件或命令里读的
_UNRESOLVED = re.compile(r"\$\{(?:input|command|file|config):[^}]*\}|(?<!\$)\{file:[^}]*\}|\$\{\{\s*inputs\.[^}]*\}\}")


class Refs:
    """换一个服务器的配置里的引用，顺便记下换了什么，最后写成几句给人看的提醒（notes）。"""

    def __init__(self, env: Env, tool: str, *, dollar: bool = False, brace: bool = False, editor: bool = False, secrets: bool = False):
        self.env, self.tool = env, tool
        self.dollar, self.brace, self.editor, self.secrets = dollar, brace, editor, secrets
        self.renamed: dict[str, None] = {}  # 换了写法的环境变量（按出现的顺序）
        self.from_secrets: dict[str, None] = {}
        self.filled: dict[str, str] = {}  # 编辑器的变量 → 换成的值
        self.unresolved: dict[str, None] = {}

    def text(self, value: object) -> object:
        """换一个值。不是文字的原样返回（交给 normalize 去说哪里不对）。"""
        if not isinstance(value, str):
            return value
        value = _ENV_COLON.sub(lambda match: self._rename(match.group(1)), value)
        if self.secrets:
            value = _SECRETS.sub(lambda match: self._secret(match.group(1)), value)
        if self.brace:
            value = _BRACE.sub(lambda match: self._rename(match.group(1)), value)
        if self.dollar:
            value = _DOLLAR.sub(lambda match: self._rename(match.group(1)), value)
        if self.editor:
            value = _EDITOR.sub(lambda match: self._fill(match.group(1)), value)
        for match in _UNRESOLVED.finditer(value):
            self.unresolved[match.group(0)] = None
        return value

    def pairs(self, value: object) -> object:
        if not isinstance(value, dict):
            return value
        return {key: self.text(item) for key, item in value.items()}

    def items(self, value: object) -> object:
        if not isinstance(value, list):
            return value
        return [self.text(item) for item in value]

    def _rename(self, name: str) -> str:
        self.renamed[name] = None
        return "${" + name + "}"

    def _secret(self, name: str) -> str:
        self.from_secrets[name] = None
        return "${" + name + "}"

    def _fill(self, name: str) -> str:
        value = {
            "userHome": str(self.env.home),
            "workspaceFolder": str(self.env.workspace),
            "workspaceFolderBasename": self.env.workspace.name,
            "pathSeparator": "\\" if self.env.windows else os.sep,
            "/": "\\" if self.env.windows else os.sep,
        }[name]
        self.filled["${" + name + "}"] = value
        return value

    def notes(self) -> list[Message]:
        found: list[Message] = []
        if self.renamed:
            found.append(t("引用环境变量的写法换成了智狐的 {syntax}：{names}。启动时从环境变量里取它们的值。", syntax="${NAME}", names=", ".join(self.renamed)))
        if self.from_secrets:
            found.append(t("Continue 的 secrets 换成了环境变量：{names}。启动时从环境变量里取它们的值（Continue 的 .env 文件智狐不读）。", names=", ".join(self.from_secrets)))
        for name, value in self.filled.items():
            found.append(t("{name} 换成了 {value}。", name=name, value=value))
        if self.unresolved:
            found.append(t("{what} 要在 {tool} 里当场输入或者从别处读，智狐做不到：导入后编辑这个服务器，填上真正的值。", what=", ".join(self.unresolved), tool=self.tool))
        return found
