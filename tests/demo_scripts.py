"""演示服务器（tests/demo_web.py）里按"暗号"回答的几段固定剧本。

演示里平常的回复是按顺序一条条取的（打招呼、加待办、建文件……），发什么都一样。页面的自动化测试
（tests/ui/）不能靠顺序：前面哪个测试多发了一条，后面的全都对不上。所以一段对话的第一条消息里写着
[ui:名字] 的，整段对话都按这里同名的剧本回答，剧本只看这段对话自己的历史（走到了第几步、上一步的工具
有没有出错），和别的对话、别的测试谁先谁后都没有关系。

手动试的时候也能用：在演示页面上发一句 "[ui:approve] 随便说点什么"，就会弹出一张确认卡片。

  [ui:hello]    每条消息都回一段带思考过程、列表和链接的话（看逐字输出、刷新重放）
  [ui:long]     回一段很长的话，对话区能滚动（看换语言时滚动位置留不留得住）
  [ui:approve]  先执行一条要你批准的命令；批准了说"执行完了"，拒绝了说"不执行了"
  [ui:todo]     加一条标题很长的待办，然后交代一句
  [ui:flaky]    每一条消息第一次请求都失败（400，不会自动重试），再请求一次就回答（看"重新生成"怎么再试一次）
  [ui:readme]   README 截图里的那段对话：查待办 → 一段带表格的 Markdown → 一条要你批准的命令（归档上个月的笔记）。
                用户说中文就答中文，说别的就答英文；表格按 todo_list 真正交回的待办来画。截图时消息里不能有暗号，
                所以用 `demo_web.py --script readme`：没写暗号的对话也按它回答

要加新的剧本：写一个 body → 回复 的函数，放进下面的 SCRIPTS。body 是这次请求的内容（里面有整段历史）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import date, timedelta

from fake_api import failure, reply, text, thinking, tool_use

MARKER = re.compile(r"\[ui:([a-z][a-z0-9-]*)\]")

# 待办的标题故意很长：窄屏上看它会不会冲出信息栏
LONG_TODO = "核对第四季度所有供应商的发票并把有疑问的那几张单独整理成一份清单发给财务部门的同事确认"


def _results(body: dict) -> list[dict]:
    """最后一条消息里的工具结果（刚执行完工具时才有）。"""
    messages = body.get("messages") or []
    last = messages[-1] if messages else {}
    content = last.get("content") or []
    return [block for block in content if isinstance(block, dict) and block.get("type") == "tool_result"]


def _asked(body: dict) -> int:
    """用户在这段对话里说了几次话（工具结果不算）。"""
    count = 0
    for message in body.get("messages") or []:
        content = message.get("content") or []
        if message.get("role") == "user" and not any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content):
            count += 1
    return count


def hello(body: dict) -> dict:
    n = _asked(body)
    answer = reply(
        thinking(f"这是第 {n} 条消息。简单回答，列两点，再给一个链接。"),
        # 写得长一点（一百多个字，分成几十小段发）：慢的机器上也能看出它是一点点出现的
        text(f"收到第 {n} 条消息。下面这几句话会一小段一小段地出现，就像真的模型在一边想一边写。\n\n"
             "1. 第一点：回复是一小段一小段输出的，每一小段只有三个字。\n2. 第二点：刷新以后整段直接出现，不再一条条播放入场动画。\n\n"
             "详细的说明在 https://example.com/zhifox-ui 这里。"),
    )
    # 每一小段隔 30 毫秒（页面测试给演示的是 4 毫秒）：太快的话，慢的机器上浏览器会把好几段攒在一起画，
    # 测试就看不出它是一点点出现的（GitHub 上的 macOS 机器就这样失败过）
    answer["pace"] = 0.03
    return answer


def long(body: dict) -> dict:
    lines = [f"{i}. 第 {i} 段：这是一段用来把对话区撑高的文字，让页面可以上下滚动。" for i in range(1, 41)]
    return reply(text("下面是一段很长的回复。\n\n" + "\n".join(lines)))


def approve(body: dict) -> dict:
    results = _results(body)
    if any(block.get("is_error") for block in results):
        return reply(text("好的，不执行了。"))
    if results:
        return reply(text("命令执行完了。"))
    return reply(
        text("我来执行一条命令。"),
        tool_use(f"toolu_ui_approve_{_asked(body)}", "run_command", {"command": "echo zhifox-ui-test"}),
        stop_reason="tool_use",
    )


def todo(body: dict) -> dict:
    if _results(body):
        return reply(text("记下了。"))
    return reply(tool_use(f"toolu_ui_todo_{_asked(body)}", "todo_add", {"title": LONG_TODO, "due": "2026-11-27"}), stop_reason="tool_use")


# flaky 记着哪些消息已经失败过一次（按这段对话里用户说过的话算）。演示服务器一直开着，换一句话就又会失败一次
_FAILED: set[str] = set()


def _last_said(body: dict) -> str:
    """最后一条用户消息里用户自己写的那句话（最后一块文字；前面的时间说明每分钟都不一样，不算）。"""
    for message in reversed(body.get("messages") or []):
        content = message.get("content") or []
        if message.get("role") != "user" or any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content):
            continue
        texts = [block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"]
        return texts[-1] if texts else ""
    return ""


def flaky(body: dict) -> dict:
    key = json.dumps([_asked(body), _last_said(body)], ensure_ascii=False)  # 第几条消息、说的是什么
    if key not in _FAILED:
        _FAILED.add(key)
        # 400：SDK 不会自动重试，这一轮在模型答出第一步之前就失败了（消息从历史里撤回，页面上留着它和出错的提示）
        return failure(400, "invalid_request_error", "这是演示里故意失败的一次")
    return reply(text(f"这次成功了（第 {_asked(body)} 条消息）。"))


CJK = re.compile(r"[㐀-䶿一-鿿]")
WEEKDAYS = "一二三四五六日"


def _steps(body: dict) -> int:
    """这一轮里助手已经回答了几次（从用户最后一次自己说话往后数，工具结果不算用户说话）。"""
    count = 0
    for message in reversed(body.get("messages") or []):
        content = message.get("content") or []
        if message.get("role") == "user" and not any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content):
            break
        count += message.get("role") == "assistant"
    return count


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") for part in content or [] if isinstance(part, dict))


def _todos(body: dict) -> list[dict]:
    """todo_list 交回的待办（工具结果是 JSON 列表）。读不懂就当没有。"""
    for block in _results(body):
        if block.get("tool_use_id") == "toolu_readme_todos":
            try:
                items = json.loads(_result_text(block))
            except ValueError:
                return []
            return [item for item in items if isinstance(item, dict) and item.get("title")] if isinstance(items, list) else []
    return []


def _day(due: str | None, zh: bool) -> str:
    try:
        day = date.fromisoformat((due or "")[:10])
    except ValueError:
        return "—"
    return f"{day.month} 月 {day.day} 日（周{WEEKDAYS[day.weekday()]}）" if zh else f"{day:%a, %b} {day.day}"


def readme(body: dict) -> dict:
    zh = bool(CJK.search(_last_said(body)))
    month = (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")  # 上个月
    step = _steps(body)
    if step == 0:
        return reply(
            thinking("先看看有哪些待办，排好这周的计划，再归档上个月的笔记。" if zh else "Look at the to-dos first, plan the week, then archive last month's notes."),
            tool_use("toolu_readme_todos", "todo_list", {}),
            stop_reason="tool_use",
        )
    if step == 1:
        todos = _todos(body)
        if todos:
            head = "| 截止 | 待办 |\n|:--|:--|" if zh else "| Due | To-do |\n|:--|:--|"
            rows = "\n".join(f"| {_day(item.get('due'), zh)} | {item['title']} |" for item in todos)
            first = todos[0]["title"]
            plan = (f"这周的待办，按截止时间排：\n\n{head}\n{rows}\n\n先做 **{first}**：它最早到期。" if zh
                    else f"Here's your week, by due date:\n\n{head}\n{rows}\n\nStart with **{first}**: it's due first.")
        else:
            plan = "这周没有待办。" if zh else "You have no open to-dos this week."
        return reply(
            text(plan + ("上个月的笔记我挪进 `archive/`。" if zh else " I'll move last month's notes into `archive/`.")),
            tool_use("toolu_readme_archive", "run_command", {"command": f"mkdir -p archive && mv notes/{month}-*.md archive/"}),
            stop_reason="tool_use",
        )
    if any(block.get("is_error") for block in _results(body)):
        return reply(text("好的，笔记先不动。" if zh else "OK, I'll leave the notes where they are."))
    return reply(text("好了：上个月的笔记都在 `archive/` 里了。" if zh else "Done: last month's notes are now in `archive/`."))


SCRIPTS: dict[str, Callable[[dict], dict]] = {
    "hello": hello, "long": long, "approve": approve, "todo": todo, "flaky": flaky, "readme": readme,
}


def scripted(first_message: str, body: dict) -> dict | None:
    """first_message 是这段对话第一条消息的 JSON。写着暗号、而且有这个剧本的，返回剧本的回复；不然返回 None。"""
    found = MARKER.search(first_message)
    script = SCRIPTS.get(found.group(1)) if found else None
    return script(body) if script else None
