"""待办事项 —— 第一类工具：完全自定义。

说明书（名字、描述、参数格式）是我们写的，执行的函数也是我们写的。
想给助手加新能力，照着这个文件的样子写就行。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .base import Tool, ToolError, function_tool


class TodoStore:
    """待办事项保存在一个 JSON 文件里。"""

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict:
        if not self.path.exists():
            return {"next_id": 1, "items": []}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)  # 先写临时文件再替换，中途崩溃也不会留下半个文件

    def items(self, status: str = "pending") -> list[dict]:
        items = self._load()["items"]
        if status == "pending":
            items = [item for item in items if not item["done"]]
        elif status == "done":
            items = [item for item in items if item["done"]]
        # 有截止时间的排前面，按时间先后；没有的按创建顺序
        return sorted(items, key=lambda item: (item["due"] is None, item["due"] or "", item["id"]))

    def add(self, title: str, due: str | None = None, note: str | None = None) -> dict:
        data = self._load()
        item = {
            "id": data["next_id"],
            "title": title,
            "due": _check_due(due),
            "note": note,
            "done": False,
            "created_at": _now(),
            "completed_at": None,
        }
        data["next_id"] += 1
        data["items"].append(item)
        self._save(data)
        return item

    def update(self, id: int, **changes: object) -> dict:
        data = self._load()
        item = _find(data, id)
        if "due" in changes:
            changes["due"] = _check_due(changes["due"])
        if "done" in changes:
            item["completed_at"] = _now() if changes["done"] else None
        item.update(changes)
        self._save(data)
        return item

    def delete(self, id: int) -> dict:
        data = self._load()
        item = _find(data, id)
        data["items"].remove(item)
        self._save(data)
        return item


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _find(data: dict, id: int) -> dict:
    for item in data["items"]:
        if item["id"] == id:
            return item
    raise ToolError(f"没有编号为 {id} 的待办。先用 todo_list 查看现有的待办。")


def _check_due(due: object) -> str | None:
    if due is None or due == "":
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(due), fmt).strftime(fmt)
        except ValueError:
            continue
    raise ToolError(f"截止时间 {due!r} 的格式不对，应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM。")


def format_todos(items: list[dict]) -> str:
    """给人看的待办列表（/todos 命令用）。"""
    if not items:
        return "没有待办事项。"
    lines = []
    for item in items:
        mark = "✓" if item["done"] else "□"
        due = f"  （截止 {item['due']}）" if item["due"] else ""
        lines.append(f"{mark} #{item['id']} {item['title']}{due}")
    return "\n".join(lines)


def _as_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def make_todo_tools(store: TodoStore) -> list[Tool]:
    due_property = {
        "type": "string",
        "description": "截止时间，格式 YYYY-MM-DD 或 YYYY-MM-DD HH:MM。"
        "用户说的是相对时间（明天、周五）时，先换算成具体日期。",
    }

    def todo_add(title: str, due: str | None = None, note: str | None = None) -> str:
        return _as_json(store.add(title, due, note))

    def todo_list(status: str = "pending") -> str:
        return _as_json(store.items(status))

    def todo_update(id: int, **changes: object) -> str:
        if not changes:
            raise ToolError("没有提供要修改的内容。")
        return _as_json(store.update(id, **changes))

    def todo_delete(id: int) -> str:
        return _as_json({"deleted": store.delete(id)})

    return [
        function_tool(
            name="todo_add",
            description="添加一条待办事项。用户提到要做的事、要记得的事或某个截止日期时使用。",
            properties={
                "title": {"type": "string", "description": "待办的内容，简短的一句话"},
                "due": due_property,
                "note": {"type": "string", "description": "补充说明，可以不填"},
            },
            required=["title"],
            function=todo_add,
        ),
        function_tool(
            name="todo_list",
            description="查看待办事项，返回每条的编号、内容、截止时间和完成状态。"
            "修改或删除待办之前先用它查到编号。",
            properties={
                "status": {
                    "type": "string",
                    "enum": ["pending", "done", "all"],
                    "description": "pending=未完成（默认），done=已完成，all=全部",
                },
            },
            required=[],
            function=todo_list,
        ),
        function_tool(
            name="todo_update",
            description="修改一条待办：标记完成或未完成，或者改内容、截止时间、备注。只填要改的字段。"
            "把 due 设为空字符串可以清除截止时间。",
            properties={
                "id": {"type": "integer", "description": "待办的编号"},
                "done": {"type": "boolean", "description": "true=标记完成，false=改回未完成"},
                "title": {"type": "string", "description": "新的内容"},
                "due": due_property,
                "note": {"type": "string", "description": "新的备注"},
            },
            required=["id"],
            function=todo_update,
        ),
        function_tool(
            name="todo_delete",
            description="彻底删除一条待办。事情做完了应该用 todo_update 标记完成，而不是删除。",
            properties={"id": {"type": "integer", "description": "待办的编号"}},
            required=["id"],
            function=todo_delete,
        ),
    ]
