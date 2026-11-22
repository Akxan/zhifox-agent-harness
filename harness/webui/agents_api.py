"""网页上"扩展 → Agent"一页背后的接口：/api/agent/……。

单独放一个文件，是因为它们自成一组（和 skills_api.py 一样）。server.py 只负责把 /api/agent/ 开头的请求交到这里（route），
webapp.py 把 Agent 列表放进状态里（summaries）。用一个 Agent 开始新对话走的是 /api/new（带上 agent），在 conversations_api.py 里。

  POST /api/agent/read      打开一个 Agent：全文、表单里的各项、问题
  POST /api/agent/options   编辑时要用的选项：能勾的工具（内置的、此刻连着的 MCP 服务器的）、示例
  POST /api/agent/parse     原文 → 表单（从"原文"切回"表单"时用；检查的规矩只在程序里写一份）
  POST /api/agent/compose   表单 → 原文（从"表单"切到"原文"、或者按表单保存时用；只改你改过的那几项）
  POST /api/agent/create    新建（送来的是原文）
  POST /api/agent/save      保存改过的（name 改了，文件跟着改名）
  POST /api/agent/delete    删除
  POST /api/agent/reveal    在文件管理器里显示一个 Agent 的文件（不给名字就是整个 Agent 文件夹）
  POST /api/agent/refresh   页面回到前台：Agent 文件夹变了的话，发一次新的状态
  POST /api/agent/example   把一个内置的示例加进来
  POST /api/agent/scan      找出别的工具（Claude Code、Copilot、Gemini CLI……）里的 Agent，给你勾选
  POST /api/agent/import    导入勾选的那几个（按编号重新读文件，页面送来的不是文件内容）

规矩和别的接口一样：参数不对回答 400；做不成（不存在、不合格、同名）回答 409，说明写在 error 里，
不合格的另外带着 problems（每一条问题的原文和值，页面换语言时照着重译）。

这些操作都不拿对话的锁（和 MCP 一样）：对话进行中也可以新建、修改、删除 Agent。正在进行的对话和子 Agent 用的是
开始时读好的那一份（角色在开始一段对话时读，子 Agent 在被叫的那一刻读），不受影响；下一次才用上新的。
文件本身的并发由 AgentStore 的锁管（见 agents/store.py）。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ..agents import (
    AgentConflict,
    AgentError,
    AgentInvalid,
    BadRequest,
    examples,
    from_tools,
)
from ..agents import tools as agent_tools
from ..agents.spec import COLORS, Fields, compose, normal
from ..i18n import t
from ..imports.overview import overview
from ..imports.places import Env
from ..imports.registry import AGENTS
from ..mcp.bridge import server_prefix
from ..skills.store import described

if TYPE_CHECKING:
    from .server import WebApp


class AgentsAPI:
    def __init__(self, web: WebApp, reveal: Callable[[Path], None]):
        self.web = web
        self.store = web.app.agents
        self._reveal = reveal  # 在文件管理器里显示（reveal.py 的 reveal_in_file_manager）
        self._shown: list[dict] = []  # 上一次放进状态里的列表（见 refresh）

    def summaries(self) -> list[dict]:
        """状态里的 Agent 列表：名字、说明、模型、颜色、几样工具、有没有问题。正文要看的时候另外读。"""
        self._shown = [agent.summary() for agent in self.store.list()]
        return self._shown

    def route(self, action: str, body: dict) -> tuple[int, dict]:
        """处理一个 /api/agent/<action> 请求，返回（状态码，回答）。"""
        name = body.get("name")
        if action == "read":
            return self._do(lambda: self.store.read(name))
        if action == "options":
            return self._do(self._options)
        if action == "parse":
            text = body.get("text")
            if not isinstance(text, str):
                return 400, {"error": t("参数不正确")}
            return self._do(lambda: {"fields": self.store.fields(text)})
        if action == "compose":
            return self._do(lambda: {"text": compose(_fields(body.get("fields")), _base(body.get("base")))})
        if action == "reveal":
            return self.reveal(name if isinstance(name, str) else None)
        if action == "refresh":
            if [agent.summary() for agent in self.store.list()] != self._shown:
                self.web.publish_state()
            return 200, {"ok": True}
        if action == "scan":
            return self._do(self._scan)
        if action == "create":
            return self._change(lambda: {"name": self.store.create(_text(body))})
        if action == "save":
            modified, force = body.get("modified"), body.get("force", False)
            number = modified is None or (isinstance(modified, (int, float)) and not isinstance(modified, bool))
            if not number or not isinstance(force, bool):
                return 400, {"error": t("参数不正确")}
            return self._change(lambda: {"name": self.store.save(name, _text(body), None if modified is None else float(modified), force)})
        if action == "delete":
            return self._change(lambda: self.store.delete(name))
        if action == "example":
            example = examples.find(body.get("id"))
            if example is None:
                return 400, {"error": t("参数不正确")}
            return self._change(lambda: {"name": self.store.install(example.text)})
        if action == "import":
            env = Env.here(self.web.app.config.workspace)
            return self._change(lambda: {"results": from_tools.import_picked(env.workspace, self.store, body.get("picks"), env)})
        return 404, {"error": "not found"}

    def reveal(self, name: str | None) -> tuple[int, dict]:
        """在文件管理器里显示一个 Agent 的文件。路径由服务器算：页面只能给一个名字，名字只能是一段（不带 /），
        而且必须是 Agent 文件夹里真实存在的一个文件（见 AgentStore._file）。"""
        try:
            target = self.store.file_of(name)
            self._reveal(target)
        except AgentError as exc:
            return 409, {"error": str(exc)}
        except OSError as exc:
            return 409, {"error": t("没能打开文件管理器（{error}）。文件在：{path}", error=exc, path=self.store.root)}
        return 200, {"ok": True, "path": str(target)}

    def _options(self) -> dict:
        """编辑 Agent 时能选什么。MCP 的工具按此刻连着的服务器列（没连上的服务器不在里面，文件里写着的照样保留）。"""
        app = self.web.app
        shot = app.mcp.snapshot()
        servers: dict[str, dict] = {}
        for shown, (server, remote) in shot.names.items():
            entry = servers.setdefault(server, {"server": server, "key": server_prefix(server), "tools": []})
            entry["tools"].append({"name": shown, "remote": remote})
        return {
            "builtin": agent_tools.builtin_names(app.config.web),
            "mcp": list(servers.values()),
            "colors": list(COLORS),
            "examples": [
                {"id": example.id, "name": example.name, "color": example.color, "blurb": example.blurb, "exists": self.store.exists(example.name)}
                for example in examples.EXAMPLES
            ],
        }

    def _scan(self) -> dict:
        """别的工具里的 Agent。顶上的总览（tools）里，Agent 的数目就是这里找到的，不再另外数一遍。"""
        env = Env.here(self.web.app.config.workspace)
        candidates = from_tools.discover(env.workspace, self.store, env)
        counts: dict[str, tuple[int, int]] = {}
        for candidate in candidates:
            before = counts.get(candidate["tool_id"], (0, 0))
            counts[candidate["tool_id"]] = (before[0] + 1, before[1])
        return {"candidates": candidates, "places": from_tools.places_looked(env.workspace, env), "tools": overview(env, {AGENTS: counts})}

    # ---- 内部 --------------------------------------------------------------

    def _do(self, action: Callable[[], dict]) -> tuple[int, dict]:
        """只读的操作。"""
        try:
            return 200, {"ok": True, **action()}
        except BadRequest as exc:
            return 400, {"error": str(exc)}
        except AgentError as exc:
            return 409, {"error": str(exc)}

    def _change(self, action: Callable[[], dict | None]) -> tuple[int, dict]:
        """会改动 Agent 文件的操作：做完发一次新的状态（列表跟着状态走）。"""
        try:
            result = action() or {}
        except BadRequest as exc:
            return 400, {"error": str(exc)}
        except AgentInvalid as exc:
            return 409, {"error": str(exc), "problems": [described(problem) for problem in exc.problems]}
        except AgentConflict as exc:
            return 409, {"error": str(exc), "conflict": True}
        except AgentError as exc:
            return 409, {"error": str(exc)}
        self.web.publish_state()
        return 200, {"ok": True, **result}


def _text(body: dict) -> str:
    """保存、新建送来的原文。按表单保存的，页面先用 compose 写成原文（这样保存的就是你在原文里看到的那一份）。"""
    text = body.get("text")
    if not isinstance(text, str):
        raise BadRequest(t("参数不正确"))
    return text


def _base(base: object) -> str | None:
    if base is None:
        return None
    if not isinstance(base, str):
        raise BadRequest(t("参数不正确"))
    return base


def _fields(raw: object) -> Fields:
    """页面送来的表单。只检查类型；内容合不合格，写成原文之后照常检查（见 agents/spec.py 的 check）。"""
    if not isinstance(raw, dict):
        raise BadRequest(t("参数不正确"))
    texts: dict[str, str] = {}
    for key in ("name", "description", "model", "color", "body"):
        value = raw.get(key)
        if not isinstance(value, str):
            raise BadRequest(t("参数不正确"))
        texts[key] = value
    tools = raw.get("tools")
    if tools is not None and (not isinstance(tools, list) or not all(isinstance(item, str) for item in tools)):
        raise BadRequest(t("参数不正确"))
    return Fields(
        name=normal(texts["name"].strip()),
        description=texts["description"].strip(),
        tools=None if tools is None else [item.strip() for item in tools if item.strip()],
        model=texts["model"].strip(),
        color=texts["color"].strip(),
        body=texts["body"],
    )
