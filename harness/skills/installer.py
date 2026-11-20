"""先看后装：下载、上传、找到的 Skill 先列给你确认，你点了"安装"才写进 Skill 文件夹。

Skill 里的说明会被助手照着做，里面的脚本也可能被运行，所以装之前要让你看清楚：叫什么、说自己做什么、
带了哪些文件、从哪儿来的。认出来的东西暂时放在内存里（stage），你确认的时候装的就是你看到的那一份，
不会在这期间被网上的新版本换掉。只留最近几份，过一阵子没装的自动丢掉。
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from ..i18n import t
from .bundle import Candidate
from .spec import name_problem
from .store import BadRequest, SkillError, SkillInvalid, SkillStore

MAX_STAGES = 3
STAGE_SECONDS = 30 * 60


@dataclass
class Stage:
    candidates: dict[str, tuple[str, Candidate]]  # key → (来自哪里, 候选)
    source: dict  # {"kind": "url" | "upload" | "claude-code", "label": …, 可能还有 "url"、"commit"}
    created: float


class Installer:
    def __init__(self, store: SkillStore):
        self.store = store
        self._stages: OrderedDict[str, Stage] = OrderedDict()
        self._lock = threading.Lock()

    def stage(self, found: list[tuple[str, Candidate]], source: dict) -> dict:
        """记下认出来的候选，返回给页面确认用的内容：{"stage", "source", "candidates": [...]}。"""
        if not found:
            raise SkillError(t("里面没有找到 SKILL.md：一个 Skill 至少要有这个文件。"))
        stage_id = secrets.token_hex(8)
        with self._lock:
            now = time.monotonic()
            for old in [key for key, stage in self._stages.items() if now - stage.created > STAGE_SECONDS]:
                del self._stages[old]
            self._stages[stage_id] = Stage({candidate.key: (group, candidate) for group, candidate in found}, source, now)
            while len(self._stages) > MAX_STAGES:
                self._stages.popitem(last=False)
        return {
            "stage": stage_id,
            "source": source,
            "candidates": [{**candidate.preview(self.store), "group": group} for group, candidate in found],
        }

    def install(self, stage_id: object, picks: object) -> list[dict]:
        """装你勾选的那几个。picks 是 [{"key", "name", "replace"}]：name 可以改（同名时改个名字），
        replace 为真表示替换掉已有的同名 Skill。返回每一个的结果 [{"key", "name", "ok", "error"}]。"""
        with self._lock:
            stage = self._stages.get(stage_id) if isinstance(stage_id, str) else None
            if stage is not None and time.monotonic() - stage.created > STAGE_SECONDS:
                del self._stages[stage_id]  # 放得太久了：网上的那一份可能早就变了，重新获取一次再装
                stage = None
        if stage is None:
            raise SkillError(t("这份预览已经过期了，请重新获取一次。"))
        if not isinstance(picks, list) or not picks or not all(isinstance(pick, dict) for pick in picks):
            raise BadRequest(t("请至少选一个要安装的 Skill。"))
        results = []
        for pick in picks:
            key, name, replace = pick.get("key"), pick.get("name"), pick.get("replace", False)
            entry = stage.candidates.get(key) if isinstance(key, str) else None
            if entry is None or not isinstance(name, str) or not isinstance(replace, bool):
                raise BadRequest(t("参数不正确"))
            results.append(self._one(stage, key, entry[1], name.strip(), replace))
        if all(result["ok"] for result in results):
            with self._lock:
                self._stages.pop(stage_id, None)  # 都装好了：这份预览用完了
        return results

    def _one(self, stage: Stage, key: str, candidate: Candidate, name: str, replace: bool) -> dict:
        result = {"key": key, "name": name, "ok": False, "error": ""}
        problem = name_problem(name)
        if problem:
            result["error"] = str(problem)
            return result
        source = dict(stage.source)
        if len(stage.candidates) > 1 and key and stage.source.get("kind") != "claude-code":
            source["label"] = f"{source.get('label', '')}/{key}".lstrip("/")
        if stage.source.get("kind") == "claude-code":
            source["label"] = key
        try:
            self.store.install(candidate.load(), name, replace=replace, source=source)
        except SkillInvalid as exc:
            result["error"] = " ".join(str(item) for item in exc.problems)
            return result
        except SkillError as exc:
            result["error"] = str(exc)
            return result
        result["ok"] = True
        return result
