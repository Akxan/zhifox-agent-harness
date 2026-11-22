"""网页上"设置 → 沙箱"背后的接口：/api/sandbox/……（沙箱本身见 ../sandbox/）。

  POST /api/sandbox/level    对话用哪一档（{"level": "off" / "workspace" / "network"}），也是定时任务、聊天通道"跟随设置"时的那一档
  POST /api/sandbox/add      加一条额外允许的路径（{"path", "write"}：write 为真是能读写，否则只读）
  POST /api/sandbox/edit     改一条（{"old", "path", "write"}），位置不变
  POST /api/sandbox/delete   删一条（{"path"}）
  POST /api/sandbox/reveal   在文件管理器里显示一条（{"path"}）：只认列表里已有的那几条，路径不是页面随便给的

定时任务的那一档跟着任务一起存（/api/task/save 的 sandbox），聊天通道的那一档是 /api/channel/sandbox（见 channels_api.py）。

状态里的这一节（state.sandbox，见 state()）：
  level      设置里的那一档
  levels     [{"id", "label", "text"}]：几档的名字和说明（话由程序写，带着原文，页面换语言时就地重译）
  paths      [{"path", "write", "exists"}]
  limit、chars  最多几条、每条最长多少个字
  backend    {"id", "label", "note"}：这台电脑用哪一种沙箱（sandbox-exec / bwrap / none）
  problem    这台电脑上用不了的原因（能用是 null）。选了一档、它却用不了时，命令一律拒绝执行

只是设置，不拿对话的锁：对话进行中也能改，下一条命令就按新的来。模型没有任何能改这些设置的工具。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import SANDBOX_MAX_PATHS, SANDBOX_PATH_CHARS
from ..i18n import t
from ..sandbox import LEVEL_TEXT, LEVELS
from ..sandbox import backend as sandbox_backend
from ..sandbox.policy import inside, real
from ..settings import path_is_absolute
from .ui import said

if TYPE_CHECKING:
    from .server import WebApp


class SandboxAPI:
    def __init__(self, web: WebApp, reveal: Callable[[Path], None]):
        self.web = web
        self._reveal = reveal

    def state(self) -> dict:
        settings = self.web.app.settings.sandbox
        found = sandbox_backend.detect()
        problem = found.problem()
        return {
            "level": settings["level"],
            "levels": [{"id": level, "label": said(t(LEVELS[level])), "text": said(t(LEVEL_TEXT[level]))} for level in LEVELS],
            "paths": [{**item, "exists": os.path.exists(os.path.expanduser(item["path"]))} for item in settings["paths"]],
            "limit": SANDBOX_MAX_PATHS,
            "chars": SANDBOX_PATH_CHARS,
            "backend": {"id": found.id, "label": said(t(found.label)), "note": said(t(found.note)) if found.note else None},
            "problem": said(problem) if problem is not None else None,
        }

    def route(self, action: str, body: dict) -> tuple[int, dict]:
        if action == "level":
            return self._level(body.get("level"))
        if action == "reveal":
            return self._reveal_path(body.get("path"))
        if action in ("add", "edit", "delete"):
            return self._paths(action, body)
        return 404, {"error": t("没有这个操作")}

    def _level(self, level: object) -> tuple[int, dict]:
        if not isinstance(level, str) or level not in LEVELS:
            return 400, {"error": t("参数不正确")}
        return self._save({"level": level})

    def _paths(self, action: str, body: dict) -> tuple[int, dict]:
        paths = [dict(item) for item in self.web.app.settings.sandbox["paths"]]
        known = [item["path"] for item in paths]
        if action == "delete":
            path = body.get("path")
            if not isinstance(path, str) or path not in known:
                return 409, {"error": t("这条路径已经不在列表里了。")}
            paths.pop(known.index(path))
            return self._save({"paths": paths}, path)
        write = body.get("write", False)
        if not isinstance(write, bool):
            return 400, {"error": t("参数不正确")}
        path, problem = self._check(body.get("path"))
        if problem:
            return 400, {"error": problem}
        if action == "edit":
            old = body.get("old")
            if not isinstance(old, str) or old not in known:
                return 409, {"error": t("这条路径已经不在列表里了。")}
            if path != old and path in known:
                return 409, {"error": t("列表里已经有 {path} 了。", path=path)}
            paths[known.index(old)] = {"path": path, "write": write}
        else:
            if path in known:
                return 409, {"error": t("列表里已经有 {path} 了。", path=path)}
            if len(paths) >= SANDBOX_MAX_PATHS:
                return 409, {"error": t("最多 {n} 条。先删掉一些用不到的再加。", n=SANDBOX_MAX_PATHS)}
            paths.append({"path": path, "write": write})
        return self._save({"paths": paths}, path)

    def _check(self, value: object) -> tuple[str, str]:
        """一条要加进来的路径：整理好的样子（~ 展开成家目录），和不能加的原因（能加是空的）。"""
        if not isinstance(value, str) or not value.strip():
            return "", t("写一个路径，比如 ~/.nvm 或者 /srv/app。")
        path = value.strip()
        if len(path) > SANDBOX_PATH_CHARS or not path_is_absolute(path):
            return "", t("要写完整的路径（从 / 或者 ~ 开始），最长 {n} 个字。", n=SANDBOX_PATH_CHARS)
        path = os.path.normpath(os.path.expanduser(path))
        layout = self.web.app.sandbox_layout
        forbidden = [real(place) for place in (*layout.private, *layout.protected)]
        if inside(real(path), forbidden):
            return "", t("这是智狐自己的数据（或者保存密钥的文件）：在沙箱里永远碰不到它，加进来也不会生效。")
        return path, ""

    def _save(self, changes: dict, path: str = "") -> tuple[int, dict]:
        try:
            self.web.app.settings.set_sandbox(changes)
        except OSError as exc:
            return 409, {"error": t("没能保存设置：{error}", error=str(exc))}
        self.web.publish_state()
        return 200, {"ok": True, "path": path} if path else {"ok": True}

    def _reveal_path(self, path: object) -> tuple[int, dict]:
        """在文件管理器里显示列表里的一条。不存在的话打开离它最近的、存在的上一层文件夹。"""
        known = [item["path"] for item in self.web.app.settings.sandbox["paths"]]
        if not isinstance(path, str) or path not in known:
            return 409, {"error": t("这条路径已经不在列表里了。")}
        shown = Path(os.path.expanduser(path))
        while not shown.exists() and shown.parent != shown:
            shown = shown.parent
        try:
            self._reveal(shown)
        except OSError as exc:
            return 409, {"error": t("没能打开文件管理器（{error}）。文件在：{path}", error=exc, path=path)}
        return 200, {"ok": True, "path": str(shown)}
