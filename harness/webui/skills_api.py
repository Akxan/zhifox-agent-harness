"""网页上"扩展 → Skill"一页背后的接口：/api/skill/……。

单独放一个文件，是因为它们自成一组，和对话、会话、设置都不相干。server.py 只负责把 /api/skill/ 开头的请求
交到这里（route），以及把 Skill 列表放进状态里（summaries）。

  POST /api/skill/read      打开一个 Skill：SKILL.md 全文、文件列表、各项信息
  POST /api/skill/file      查看 Skill 里的一个文件
  POST /api/skill/save      保存改过的 SKILL.md（name 改了，文件夹跟着改名）
  POST /api/skill/create    按表单新建一个 Skill
  POST /api/skill/delete    删除一个 Skill
  POST /api/skill/enable    启用或停用一个 Skill
  POST /api/skill/reveal    在文件管理器里显示一个 Skill 的文件夹（不给名字就是整个 Skill 文件夹）
  POST /api/skill/refresh   页面回到前台：Skill 文件夹变了的话，发一次新的状态
  POST /api/skill/fetch     从网址下载，先认出里面的 Skill 给你确认（还没有装）
  POST /api/skill/upload    上传的压缩包、SKILL.md 或文件夹，同样先给你确认
  POST /api/skill/scan      找出别的工具（Claude Code、Codex、Cursor……）里的 Skill，给你勾选
  POST /api/skill/install   装你确认过的那几个

规矩和别的接口一样：参数不对回答 400；做不成（不存在、不合规范、对话进行中）回答 409，说明写在 error 里。
会改动 Skill 的操作和对话共用一把锁：对话进行中不能改，因为模型可能正读着这个 Skill、照着它做到一半。
只是看一眼的（打开、查看文件、在访达中显示、下载预览）不拿锁，对话进行中也可以做。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ..i18n import t
from ..imports.overview import overview
from ..imports.places import Env
from ..imports.registry import SKILLS
from ..skills import (
    BadRequest,
    SkillConflict,
    SkillError,
    SkillInvalid,
    bundle,
    download,
    from_tools,
)
from ..skills.installer import Installer
from ..skills.store import described

if TYPE_CHECKING:
    from .server import WebApp

MAX_UPLOAD_BODY_BYTES = 30_000_000  # 上传的压缩包（base64 之后大三分之一）最大约 20 MB，见 download.MAX_DOWNLOAD_BYTES


class SkillsAPI:
    def __init__(self, web: WebApp, reveal: Callable[[Path], None]):
        self.web = web
        self.store = web.app.skills
        self.installer = Installer(self.store)
        self._reveal = reveal  # 在文件管理器里显示（server.py 的 reveal_in_file_manager）
        self._shown: list[dict] = []  # 上一次放进状态里的列表（见 refresh）

    def summaries(self) -> list[dict]:
        """状态里的 Skill 列表：名字、说明、启用没有、有没有问题、几个文件。正文要看的时候另外读。"""
        self._shown = [skill.summary() for skill in self.store.list()]
        return self._shown

    def route(self, action: str, body: dict) -> tuple[int, dict]:
        """处理一个 /api/skill/<action> 请求，返回（状态码，回答）。"""
        name = body.get("name")
        if action == "read":
            return self._look(lambda: self.store.read(name))
        if action == "file":
            return self._look(lambda: self.store.read_file(name, body.get("path")))
        if action == "reveal":
            return self.reveal(name if isinstance(name, str) else None)
        if action == "refresh":
            if [skill.summary() for skill in self.store.list()] != self._shown:
                self.web.publish_state()
            return 200, {"ok": True}
        if action == "fetch":
            url = body.get("url")
            if not isinstance(url, str) or not 0 < len(url.strip()) <= 2000:
                return 400, {"error": t("请填写一个网址。")}
            return self._look(lambda: self._fetch(url.strip()))
        if action == "upload":
            return self._look(lambda: self._upload(body))
        if action == "scan":
            return self._look(self._scan)
        if action == "save":
            text, modified, force = body.get("text"), body.get("modified"), body.get("force", False)
            number = modified is None or (isinstance(modified, (int, float)) and not isinstance(modified, bool))
            if not isinstance(text, str) or not number or not isinstance(force, bool):
                return 400, {"error": t("参数不正确")}
            return self._change(lambda: {"name": self.store.save(name, text, None if modified is None else float(modified), force)})
        if action == "create":
            return self._change(lambda: {"name": self.store.create(name, body.get("description"), body.get("body", ""))})
        if action == "delete":
            return self._change(lambda: self.store.delete(name))
        if action == "enable":
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                return 400, {"error": t("参数不正确")}
            return self._change(lambda: self.store.set_enabled(name, enabled))
        if action == "install":
            return self._change(lambda: {"results": self.installer.install(body.get("stage"), body.get("picks"))})
        return 404, {"error": "not found"}

    def reveal(self, name: str | None) -> tuple[int, dict]:
        """在文件管理器里显示一个 Skill 的文件夹。路径由服务器算：页面只能给一个名字，名字只能是一段（不带 /），
        而且必须是 Skill 文件夹里真实存在的一个子文件夹（见 SkillStore._folder）。"""
        try:
            target = self.store.folder_of(name)
            self._reveal(target)
        except SkillError as exc:
            return 409, {"error": str(exc)}
        except OSError as exc:
            return 409, {"error": t("没能打开文件管理器（{error}）。文件在：{path}", error=exc, path=self.store.root)}
        return 200, {"ok": True, "path": str(target)}

    # ---- 先给你看 ----------------------------------------------------------

    def _fetch(self, url: str) -> dict:
        fetched = download.from_url(url)
        source = {"kind": "url", "url": url[:500], "label": fetched.label}
        if fetched.commit:
            source["commit"] = fetched.commit
        return self.installer.stage([("", candidate) for candidate in bundle.candidates(fetched.files)], source)

    def _upload(self, body: dict) -> dict:
        kind, label = body.get("kind"), body.get("label")
        label = label[:200] if isinstance(label, str) else ""
        if kind == "folder":
            files = bundle.from_upload(body.get("files"))
        elif kind == "file":
            data = bundle.decode(body.get("data"))
            if data.startswith(b"PK"):
                files, _ = bundle.from_zip(data)  # .zip 和 .skill 都是 zip
            elif data.removeprefix(b"\xef\xbb\xbf").lstrip(b" \t\r\n").startswith(b"---"):
                files = {"SKILL.md": (data, False)}
            else:
                raise SkillError(t("上传的文件既不是 SKILL.md，也不是 zip 压缩包（.zip 或 .skill）。"))
        else:
            raise BadRequest(t("参数不正确"))
        return self.installer.stage([("", candidate) for candidate in bundle.candidates(files)], {"kind": "upload", "label": label})

    def _scan(self) -> dict:
        """别的工具里的 Skill。顶上的总览（tools）里，Skill 的数目就是这里找到的，不再另外数一遍。"""
        env = Env.here(self.web.app.config.workspace)
        found = from_tools.discover(env.workspace, env)
        counts: dict[str, tuple[int, int]] = {}
        for _, candidate in found:
            before = counts.get(candidate.tool_id, (0, 0))
            counts[candidate.tool_id] = (before[0] + 1, before[1])
        extra = {"places": from_tools.places_looked(env.workspace, env), "tools": overview(env, {SKILLS: counts})}
        if not found:
            return {"stage": "", "candidates": [], **extra}
        return {**self.installer.stage(found, {"kind": "import", "label": ""}), **extra}

    # ---- 内部 --------------------------------------------------------------

    def _look(self, action: Callable[[], dict]) -> tuple[int, dict]:
        """只读的操作：不拿锁。"""
        try:
            return 200, {"ok": True, **action()}
        except BadRequest as exc:
            return 400, {"error": str(exc)}
        except SkillError as exc:
            return 409, {"error": str(exc)}

    def _change(self, action: Callable[[], dict | None]) -> tuple[int, dict]:
        """会改动 Skill 的操作：和对话共用一把锁，做完发一次新的状态（列表跟着状态走）。"""
        web = self.web
        if not web._turn.acquire(blocking=False):
            return 409, {"error": web._busy()}
        try:
            try:
                result = action() or {}
            except BadRequest as exc:
                return 400, {"error": str(exc)}
            except SkillInvalid as exc:
                return 409, {"error": str(exc), "problems": [described(problem) for problem in exc.problems]}
            except SkillConflict as exc:
                return 409, {"error": str(exc), "conflict": True}
            except SkillError as exc:
                return 409, {"error": str(exc)}
            web.publish_state()
            return 200, {"ok": True, **result}
        finally:
            web._turn.release()
