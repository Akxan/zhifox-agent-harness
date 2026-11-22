"""待办和定时任务的接口。

  POST /api/todo/update、delete   改一条待办（标题、截止日期、完成没有）、删掉一条。待办的文件自己有锁（见 TodoStore），不拿对话的锁
  POST /api/task/save、preview、dismiss、delete、toggle、run、stop、confirm
                                  定时任务（route_task）：真正做事的是调度器（见 scheduler.py），任务是什么、什么时候跑见 ../tasks.py

TodoActions 混进 WebApp（见 webapp.py）：只有方法，用到的状态都在 WebApp.__init__ 里。
_valid_answer 检查"答复一个确认"的参数，聊天通道（channels_api.py）也用它。

route_* 由 server.py 的 Handler._route 调用（写法和约定见 server.py 开头）。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .. import i18n
from ..i18n import t
from ..tasks import TaskError, TaskRefused
from ..tools.base import ToolError
from .webapp_types import WebAppPart

if TYPE_CHECKING:
    from .server import Handler
    from .webapp import WebApp


class TodoActions(WebAppPart):
    """待办：改一条、删一条。"""

    def change_todo(self, todo_id: int, changes: dict | None) -> str:
        """改（changes 为 None 时删掉）一条待办。成功返回空字符串，否则返回原因。

        不拿对话的锁：待办的文件自己有锁（见 TodoStore），模型下一次看待办时看到的就是改过的
        """
        try:
            if changes is None:
                self.app.todos.delete(todo_id)
            else:
                self.app.todos.update(todo_id, **changes)
        except ToolError:  # 写给模型看的说明（"先用 todo_list 查看"），这里换成给人看的
            return t("这条待办已经不在了，可能刚被助手改过或删掉了。")
        except OSError as exc:
            return t("没能保存待办：{error}", error=exc.strerror or exc)
        self.publish_state()
        return ""


def route_todo(handler: Handler, web: WebApp, path: str, body: dict) -> None:
    todo_id = body.get("id")
    changes: dict | None = None
    bad = not isinstance(todo_id, int) or isinstance(todo_id, bool)
    if path.endswith("update") and not bad:
        changes = {}
        if "title" in body:
            title = body["title"]
            bad = bad or not isinstance(title, str) or not 0 < len(title.strip()) <= 200
            changes["title"] = title.strip() if isinstance(title, str) else ""
        if "due" in body:
            due = body["due"]
            bad = bad or not (due is None or due == "" or (isinstance(due, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", due)))
            changes["due"] = due or None
        if "done" in body:
            bad = bad or not isinstance(body["done"], bool)
            changes["done"] = body["done"]
        bad = bad or not changes
    if bad:
        handler._send_json(400, {"error": t("参数不正确")})
    else:
        handler._finish(web.change_todo(todo_id, changes))


def route_task(handler: Handler, web: WebApp, action: str, body: dict) -> None:
    """定时任务的几个操作。参数不对回答 400，现在做不了（任务不存在、已经在运行……）回答 409。"""
    tasks, task_id = web.tasks, body.get("id")
    if action not in ("save", "preview", "dismiss", "delete", "toggle", "run", "stop", "confirm"):
        handler._send_json(404, {"error": "not found"})
        return
    # 除了新建（save 不带编号）、看时间表（preview）和关提醒（dismiss），都要指明是哪个任务
    needs_id = action not in ("save", "preview", "dismiss")
    valid = {
        "save": task_id is None or isinstance(task_id, str),
        "dismiss": body.get("all") is True or isinstance(body.get("reminder_id"), str),
        "toggle": isinstance(body.get("enabled"), bool),
        "confirm": _valid_answer(body),
    }.get(action, True)
    if not valid or (needs_id and not isinstance(task_id, str)):
        handler._send_json(400, {"error": t("参数不正确")})
        return
    reply: dict = {"ok": True}
    try:
        if action == "save":
            reply["id"] = tasks.save(task_id, body)
        elif action == "preview":
            reply["next"] = tasks.preview(body.get("schedule"))
        elif action == "dismiss":
            tasks.dismiss(None if body.get("all") is True else body["reminder_id"])
        elif action == "delete":
            tasks.delete(task_id)
        elif action == "toggle":
            tasks.toggle(task_id, body["enabled"])
        elif action == "run":
            reply["queued"] = tasks.run_now(task_id)  # 同时运行的到了上限，在排队
        elif action == "stop":
            tasks.stop(task_id)
        else:
            tasks.confirm(task_id, body["confirm_id"], body["allow"], body.get("feedback", "").strip())
    except TaskRefused as exc:
        # 做不了，常常是因为页面看到的已经过时了：任务文件可能被别的程序改过（终端版、重启前的旧进程），
        # 而页面只在有改动时才收到新状态。所以先把最新的状态发过去，页面上那一行才不会一直留着、怎么删都删不掉
        web.publish_state()
        handler._send_json(409, {"error": i18n.message_of(exc)})
    except TaskError as exc:
        handler._send_json(400, {"error": i18n.message_of(exc)})
    else:
        handler._send_json(200, reply)


def _valid_answer(body: dict) -> bool:
    """答复定时任务的确认：confirm_id 是整数，allow 是真假，feedback（可以不给）是一段文字。"""
    confirm_id, feedback = body.get("confirm_id"), body.get("feedback", "")
    return isinstance(confirm_id, int) and not isinstance(confirm_id, bool) and isinstance(body.get("allow"), bool) and isinstance(feedback, str)
