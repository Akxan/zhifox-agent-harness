"""定时任务和重复任务：任务是什么、什么时候该运行、存在哪里。

两种任务：

  agent     到时间后，助手自己跑一轮：把"指令"当成一条用户消息，像平常一样调用模型和工具。
            每次运行都要花 API 费用（每一次花了多少都记下来，见 webui/scheduler.py）。
  reminder  到时间后，只在页面上弹出一条提醒。不调用模型，不花钱。

agent 任务还记着"要不要先问你"（approval）：和对话里的三档同名、同义（ask / edits / all，见 permissions.py 的 MODES），
默认 ask。只有你在页面上编辑任务时能放宽；模型用工具添加的一律是 ask（见 tools/tasks.py）。

时间表，全部按这台电脑的本地时间：

  {"type": "once", "at": "2026-11-21T09:00"}             只运行一次
  {"type": "daily", "time": "09:00"}                      每天
  {"type": "weekly", "days": [1, 3, 5], "time": "09:00"}  每周的这几天（1 是星期一，7 是星期日）
  {"type": "monthly", "day": 31, "time": "09:00"}         每月的这一天；没有这一天的月份（比如 2 月）在最后一天
  {"type": "interval", "minutes": 90}                     每隔多久一次，从创建（或者上一次运行）的时刻算起，
                                                          最短 5 分钟，最长 7 天

为什么要强调"本地时间"：用户说"每天 9 点"，指的是他墙上的钟。夏令时开始和结束的那天，墙上的钟会跳过一个小时、
或者把一个小时走两遍，按"距离上一次正好 24 小时"去算就会差一个小时。所以这里每次都按日历重新算
"下一个 9 点是哪一刻"（见 next_after），跳过去的时刻顺延到跳过之后，走两遍的时刻只算第一遍。

这个文件只管"是什么、什么时候"。到时间之后怎么排队、怎么执行、怎么显示进度，在 webui/scheduler.py：
任务只在网页界面开着的时候运行，终端界面不运行它们。
"""

from __future__ import annotations

import copy
import json
import math
import re
import secrets
import threading
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import APP_NAME
from .i18n import Message, fill, k, t
from .safe import private_dir, to_json, write_file

FORMAT_VERSION = 1
KINDS = ("agent", "reminder")
MAX_TASKS = 100  # 任务最多这么多个。模型一时糊涂、一口气加几百个的话，在这里拦住
MAX_TITLE_CHARS = 80
MAX_PROMPT_CHARS = 10_000
MIN_INTERVAL_MINUTES = 5  # 再短的话，一天就是几百轮对话、几百段会话
MAX_INTERVAL_MINUTES = 7 * 24 * 60
MAX_RUNS = 20  # 每个任务记住最近这么多次运行
MAX_REMINDERS = 50  # 还没关掉的提醒最多留这么多条，更早的自动丢掉
MAX_COUNTED = 1000  # 数"错过了几次"时最多数到这里（一个每天的任务关了三年也就一千来次）

# 一次运行在保存的记录里可能处于的状态。
#   waiting  还没开始：在排队（同时运行的任务到了上限，见 config.py 的 MAX_PARALLEL_TASKS），
#            或者已经开始了、正停在一个要你批准的操作上
#   running  正在运行          done     做完了          failed   出错了（原因在 error 里）
#   stopped  被停下了（你点了停止，或者程序退出了）
#   missed   保留给"错过了、没有补上"的情况。现在错过的会补跑一次（见 webui/scheduler.py），所以还用不到
STATUSES = ("running", "waiting", "done", "failed", "stopped", "missed")
UNFINISHED = ("running", "waiting")

_ID = re.compile(r"[0-9a-f]{8}")
_TIME = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
_AT = re.compile(r"(\d{4})-(\d{2})-(\d{2})T([01]\d|2[0-3]):([0-5]\d)")
# 每种时间表要哪几项（除了 type）
_FIELDS = {"once": {"at"}, "daily": {"time"}, "weekly": {"days", "time"}, "monthly": {"day", "time"}, "interval": {"minutes"}}

WEEKDAY_NAMES = (k("周一"), k("周二"), k("周三"), k("周四"), k("周五"), k("周六"), k("周日"))
PAST = k("这个时间已经过去了，请选一个以后的时间。")
INTERRUPTED = k("{name}在这次运行结束之前退出了。")


class TaskError(Exception):
    """任务的内容不对（时间表写错了、名称是空的……）。错误信息是用 t() 写的、可以直接给用户看的话。"""


class TaskRefused(TaskError):
    """内容没问题，但现在做不了：任务不存在、已经在运行了、还没有密钥……"""


# ---- 检查 --------------------------------------------------------------------


def check_schedule(value: object) -> dict:
    """检查一份时间表，返回整理好的样子（多余的空值去掉，星期几排好序）。不对的话抛出 TaskError。"""
    if not isinstance(value, dict):
        raise TaskError(t("时间表的格式不对。"))
    fields = {key: item for key, item in value.items() if item is not None}  # 模型常把用不到的项填成 null
    kind = fields.pop("type", None)
    if not isinstance(kind, str) or kind not in _FIELDS:
        raise TaskError(t("重复方式（type）只能是 once、daily、weekly、monthly、interval 之一。"))
    extra = sorted(set(fields) - _FIELDS[kind])
    if extra:
        # 多出来的项多半说明写的人想要的是另一种时间表（比如 daily 却给了 days），与其悄悄丢掉，不如说出来
        raise TaskError(t("“{type}”这种时间表用不到 {fields}。", type=kind, fields=", ".join(map(str, extra))))
    if kind == "once":
        return {"type": "once", "at": _check_at(fields.get("at"))}
    if kind == "interval":
        minutes = fields.get("minutes")
        if not _is_int(minutes) or not MIN_INTERVAL_MINUTES <= minutes <= MAX_INTERVAL_MINUTES:
            raise TaskError(
                t("间隔要在 {min} 分钟到 {max} 分钟（7 天）之间。", min=MIN_INTERVAL_MINUTES, max=MAX_INTERVAL_MINUTES)
            )
        return {"type": "interval", "minutes": minutes}
    clock = _check_time(fields.get("time"))
    if kind == "daily":
        return {"type": "daily", "time": clock}
    if kind == "weekly":
        days = fields.get("days")
        if not isinstance(days, list) or not days or not all(_is_int(day) and 1 <= day <= 7 for day in days):
            raise TaskError(t("每周重复要选至少一天：1 是星期一，7 是星期日。"))
        return {"type": "weekly", "days": sorted(set(days)), "time": clock}
    day = fields.get("day")
    if not _is_int(day) or not 1 <= day <= 31:
        raise TaskError(t("每月的日期要在 1 到 31 之间。没有这一天的月份在最后一天运行。"))
    return {"type": "monthly", "day": day, "time": clock}


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)  # Python 里 True 也算 int，这里要排除


def _check_time(value: object) -> str:
    if not isinstance(value, str) or not _TIME.fullmatch(value):
        raise TaskError(t("时间要写成 24 小时制的 时:分，比如 09:00 或 21:30。"))
    return value


def _check_at(value: object) -> str:
    match = _AT.fullmatch(value) if isinstance(value, str) else None
    try:
        if match is None:
            raise ValueError
        moment = datetime(*map(int, match.groups()))  # 2 月 30 日这种不存在的日子在这里报错
        moment.timestamp()
    except (ValueError, OverflowError, OSError):
        raise TaskError(t("日期和时间要写成 2026-11-21T09:00 这样，而且得是真实存在的一天。")) from None
    return value


def check_task(value: object) -> dict:
    """检查用户（或者模型）给的一个任务：名称、种类、指令、时间表、是否启用、要不要先问。返回整理好的这几项。"""
    if not isinstance(value, dict):
        raise TaskError(t("参数不正确"))
    title = value.get("title")
    title = " ".join(title.split()) if isinstance(title, str) else ""  # 名称是一行字：换行和连续的空格压成一个
    if not 0 < len(title) <= MAX_TITLE_CHARS:
        raise TaskError(t("任务名称不能为空，最长 {n} 个字。", n=MAX_TITLE_CHARS))
    kind = value.get("kind")
    if kind not in KINDS:
        raise TaskError(t("任务的种类只能是 agent（让助手去做）或 reminder（只是提醒）。"))
    prompt = value.get("prompt")
    prompt = "" if prompt is None else prompt
    if not isinstance(prompt, str) or len(prompt.strip()) > MAX_PROMPT_CHARS:
        raise TaskError(t("内容最长 {n} 个字。", n=MAX_PROMPT_CHARS))
    prompt = prompt.strip()
    if kind == "agent" and not prompt:
        raise TaskError(t("要让助手做什么，写在“指令”里。"))
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise TaskError(t("参数不正确"))
    approval = value.get("approval", "ask")
    if not isinstance(approval, str) or approval not in _approval_modes():
        raise TaskError(t("参数不正确"))
    if kind == "reminder":
        approval = "ask"  # 提醒不调用模型、不用任何工具，没有什么可问的。存成默认值，免得文件里留着一个不起作用的"全部自动允许"
    schedule = check_schedule(value.get("schedule"))
    return {"title": title, "kind": kind, "prompt": prompt, "schedule": schedule, "enabled": enabled, "approval": approval}


def _approval_modes() -> dict:
    """任务可以选的几档"要不要先问你"，就是对话里的那几档。

    定义在 permissions.py（那里讲每一档是什么意思），在这里才导入：permissions.py 也要用这个文件
    （审查添加任务的工具调用），两边都在文件开头导入对方的话，谁先加载谁就拿不到对方。
    """
    from .permissions import MODES

    return MODES


# ---- 什么时候 ----------------------------------------------------------------


def local_moment(day: date, clock: str) -> float:
    """本地时间的某一天、某个钟点，换成"从 1970 年起的秒数"。

    datetime 在不带时区时按这台电脑的时区换算（会跟着 TZ 环境变量走），并且把两种特殊情况处理成我们想要的样子：
      夏令时开始、钟从 2:00 直接跳到 3:00：2:30 这个时刻不存在，换算出来是 3:30，也就是顺延到跳过之后；
      夏令时结束、1:00 到 2:00 走两遍：1:30 有两个，换算出来是第一个（fold=0），所以只运行一次。
    """
    hour, minute = map(int, clock.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute).timestamp()


def _at_moment(at: str) -> float:
    day, clock = at.split("T")
    return local_moment(date.fromisoformat(day), clock)


def _last_day(day: date) -> int:
    following = date(day.year + (day.month == 12), day.month % 12 + 1, 1)
    return (following - timedelta(days=1)).day


def _runs_on(schedule: dict, day: date) -> bool:
    kind = schedule["type"]
    if kind == "weekly":
        return day.isoweekday() in schedule["days"]
    if kind == "monthly":
        return day.day == min(schedule["day"], _last_day(day))  # 31 号在小月里落到最后一天
    return True  # daily


def next_after(schedule: dict, after: float, anchor: float | None = None) -> float | None:
    """严格晚于 after 的下一次运行时刻（秒）。不会再有了（一次性的任务已经过了）返回 None。

    anchor 只对 interval 有用：间隔从哪一刻开始数（创建的时刻，或者上一次运行的时刻）。
    这样算出来的时刻都落在 anchor + n × 间隔 上，不会因为程序哪一次醒得晚了一点，后面的就全跟着往后推。
    """
    kind = schedule["type"]
    if kind == "once":
        moment = _at_moment(schedule["at"])
        return moment if moment > after else None
    if kind == "interval":
        step = schedule["minutes"] * 60
        base = after if anchor is None else anchor
        return base + max(1, math.floor((after - base) / step) + 1) * step
    # 按日历一天一天往后找。最多要找一个多月（每月 31 号，从 2 月初开始找），400 天绰绰有余
    start = datetime.fromtimestamp(after).date()
    for offset in range(400):
        day = start + timedelta(days=offset)
        if _runs_on(schedule, day):
            moment = local_moment(day, schedule["time"])
            if moment > after:
                return moment
    return None


def count_due(schedule: dict, due: float, now: float, anchor: float | None = None) -> int:
    """从 due 到 now（两头都算）之间，按时间表本该运行几次。补跑的时候用它说明"错过了几次"。"""
    if now < due:
        return 0
    if schedule["type"] == "interval":
        return min(MAX_COUNTED, int((now - due) // (schedule["minutes"] * 60)) + 1)
    count, moment = 1, due
    while count < MAX_COUNTED:
        moment = next_after(schedule, moment, anchor)
        if moment is None or moment > now:
            break
        count += 1
    return count


def upcoming(schedule: dict, now: float, count: int = 3) -> list[float]:
    """从现在起的接下来几次（新建任务时让用户先看一眼"它会在哪几个时刻运行"）。"""
    times: list[float] = []
    moment = now
    while len(times) < count:
        found = next_after(schedule, moment, anchor=now)
        if found is None:
            break
        times.append(found)
        moment = found
    return times


def preview(value: object, now: float) -> list[float]:
    """检查一份时间表，返回接下来的三次。一次性的任务时间已经过了的话，抛出 TaskError。"""
    schedule = check_schedule(value)
    times = upcoming(schedule, now)
    if not times:
        raise TaskError(t(PAST))
    return times


def local_text(moment: float) -> str:
    """一个时刻写成本地时间，比如 2026-11-21 09:00。哪种语言都看得懂，所以不翻译。"""
    return datetime.fromtimestamp(moment).strftime("%Y-%m-%d %H:%M")


def describe(schedule: dict, say: Callable[..., str] = t) -> str:
    """时间表的一句话说明，比如"每天 09:00"。确认卡片和工具结果里用。

    say 是用来说话的函数：默认是 t()，按界面语言说；写给模型看的地方传 chinese，说中文。
    句子都用 k() 标出来，测试才找得到它们、检查有没有译文（见 tests/test_i18n.py）。
    要传进来而不是事后再换，是因为星期几的名字是嵌在句子里的另一句话，事后只换外面那一层就成了半中半英。
    """
    kind = schedule["type"]
    if kind == "once":
        return say(k("{at}，一次"), at=schedule["at"].replace("T", " "))
    if kind == "interval":
        hours, minutes = divmod(schedule["minutes"], 60)
        if not hours:
            return say(k("每 {n} 分钟"), n=minutes)
        if not minutes:
            return say(k("每 {n} 小时"), n=hours)
        return say(k("每 {hours} 小时 {minutes} 分钟"), hours=hours, minutes=minutes)
    clock = schedule["time"]
    if kind == "daily":
        return say(k("每天 {time}"), time=clock)
    if kind == "monthly":
        if schedule["day"] > 28:
            return say(k("每月 {day} 日 {time}（没有这一天的月份在最后一天）"), day=schedule["day"], time=clock)
        return say(k("每月 {day} 日 {time}"), day=schedule["day"], time=clock)
    days = schedule["days"]
    if days == [1, 2, 3, 4, 5, 6, 7]:
        return say(k("每天 {time}"), time=clock)
    if days == [1, 2, 3, 4, 5]:
        return say(k("工作日 {time}"), time=clock)
    return say(k("每{days} {time}"), days=say(k("、")).join(say(WEEKDAY_NAMES[day - 1]) for day in days), time=clock)


def chinese(text: str, **values: Any) -> str:
    """和 t() 用法一样，但总是说中文。写给模型看的话（工具结果、发给模型的指令）用它：那些话不翻译（见 CLAUDE.md）。"""
    return fill(text, {name: str(value) for name, value in values.items()})


def new_id() -> str:
    return secrets.token_hex(4)


def new_run(now: float, status: str, *, catch_up: bool = False, missed: int = 0, manual: bool = False) -> dict:
    """一次运行的记录。字段的意思见 webui/scheduler.py 开头的说明。"""
    return {
        "id": new_id(),
        "status": status,
        "started_at": now,
        "finished_at": None,
        "cost": None,
        "steps": 0,
        "catch_up": catch_up,
        "missed": missed,
        "manual": manual,
        "conversation": "",
        "summary": "",
        "error": "",
    }


def said(text: str) -> dict:
    """一句给用户看的话，存进记录时连同原文一起存（error_key、error_values），换了语言还能重新译。"""
    if isinstance(text, Message):
        return {"error": str(text), "error_key": text.key, "error_values": text.values}
    return {"error": str(text), "error_key": "", "error_values": {}}


def retell(text: object) -> str:
    """把一句用 t() 写的话按现在的界面语言再译一遍（用户可能刚换了语言）。普通文字原样返回。"""
    if isinstance(text, Message):
        return t(text.key, **text.values)
    return "" if text is None else str(text)


# ---- 保存 --------------------------------------------------------------------


class TaskStore:
    """所有任务，以及还没关掉的提醒。保存在一个 JSON 文件里（data/tasks.json）。

    网页界面里好几个线程会同时用它：调度线程（看谁到时间了）、执行任务的线程（记下运行的结果）、
    处理请求的线程（你在页面上改任务），还有对话中的模型（用工具加任务）。所以每个操作都在一把锁里做完。

    终端界面也能通过工具加任务，而且可以和网页界面同时开着。两个程序各自在内存里记着一份，
    所以每次用之前看一眼文件有没有被别人改过（_refresh），改过就重新读，免得把对方刚加的任务覆盖掉。
    """

    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = path
        self.clock = clock  # 现在是什么时刻。测试里换成一个假的钟，不用真的等到 9 点
        self.problem = ""  # 文件读不出来时的说明，网页界面启动时显示一次
        self._lock = threading.RLock()
        self._listeners: list[Callable[[], None]] = []
        self._tasks: list[dict] = []
        self._reminders: list[dict] = []
        self._stamp: tuple | None = None  # 上一次读或写之后，文件的修改时间和大小
        with self._lock:
            self._read(starting=True)

    def on_change(self, callback: Callable[[], None]) -> None:
        """登记一个函数，任务或提醒有了变化就调用它（网页界面用它刷新页面、叫醒调度线程）。"""
        self._listeners.append(callback)

    def _notify(self) -> None:
        # 在锁外面调用：听的人可能要反过来读任务（刷新页面），在锁里调用容易互相等死
        for callback in self._listeners:
            callback()

    # ---- 读写文件 --------------------------------------------------------

    def _file_stamp(self) -> tuple | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _refresh(self) -> None:
        """文件在我们上一次读写之后被别的程序改过的话，重新读一遍。调用的人要先拿到锁。"""
        if self._file_stamp() != self._stamp:
            self._read(starting=False)

    def _read(self, starting: bool) -> None:
        self._stamp = self._file_stamp()
        if self._stamp is None:
            self._tasks, self._reminders = [], []
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):  # RecursionError：嵌套了上千层的 JSON
            data = None
        if not isinstance(data, dict):
            self._set_aside()
            return
        tasks = data.get("tasks") if isinstance(data.get("tasks"), list) else []
        reminders = data.get("reminders") if isinstance(data.get("reminders"), list) else []
        # 一个一个检查：文件可能被手动改过，坏掉的那一个丢掉，别的照常用，不至于一处写错就全没了
        self._tasks = [task for task in map(self._restore_task, tasks) if task is not None]
        self._reminders = [item for item in map(_restore_reminder, reminders) if item is not None]
        if starting:
            # 刚启动就看到"正在运行"的记录，说明上一次程序在它跑完之前就退出了（崩溃、强制退出、断电）
            for task in self._tasks:
                for run in task["runs"]:
                    if run["status"] in UNFINISHED:
                        run.update(status="failed", **said(t(INTERRUPTED, name=APP_NAME)))

    def _set_aside(self) -> None:
        """文件读不出来：把它改名留着（里面也许还有能救回来的内容），从空的开始。"""
        backup = self.path.with_name(self.path.stem + ".broken.json")
        try:
            self.path.replace(backup)
        except OSError:
            backup = self.path
        self.problem = t("任务文件读不出来，可能已经损坏。原来的文件留在 {path}，现在从空的任务列表开始。", path=backup)
        self._tasks, self._reminders = [], []
        self._stamp = self._file_stamp()

    def _restore_task(self, raw: object) -> dict | None:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not _ID.fullmatch(raw["id"]):
            return None
        try:
            checked = check_task(raw)
        except TaskError:
            return None
        created, next_run, anchor = raw.get("created_at"), raw.get("next_run"), raw.get("anchor")
        if not _is_time(created) or not (next_run is None or _is_time(next_run)) or not (anchor is None or _is_time(anchor)):
            return None
        runs = raw.get("runs") if isinstance(raw.get("runs"), list) else []
        return {
            "id": raw["id"],
            **checked,
            "created_at": float(created),
            "next_run": None if next_run is None else float(next_run),
            "anchor": None if anchor is None else float(anchor),
            "runs": [run for run in map(_restore_run, runs) if run is not None][:MAX_RUNS],
        }

    def _save(self) -> None:
        """写回文件。调用的人要先拿到锁。"""
        private_dir(self.path.parent)
        data = {"version": FORMAT_VERSION, "tasks": self._tasks, "reminders": self._reminders}
        write_file(self.path, to_json(data, readable=True, indent=1))
        self._stamp = self._file_stamp()

    # ---- 查看 -----------------------------------------------------------

    def snapshot(self) -> tuple[list[dict], list[dict]]:
        """全部任务和还没关掉的提醒（都是拷贝，随便改）。出错原因按现在的界面语言重新译一遍。"""
        with self._lock:
            self._refresh()
            tasks, reminders = copy.deepcopy(self._tasks), copy.deepcopy(self._reminders)
        for task in tasks:
            for run in task["runs"]:
                if run.get("error_key"):
                    run["error"] = t(run["error_key"], **run["error_values"])
        return tasks, reminders

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            self._refresh()
            task = self._find(task_id)
            return None if task is None else copy.deepcopy(task)

    def _find(self, task_id: object) -> dict | None:
        return next((task for task in self._tasks if task["id"] == task_id), None)

    def _get(self, task_id: object) -> dict:
        task = self._find(task_id)
        if task is None:
            raise TaskRefused(t("这个任务不存在，可能已经被删除了。"))
        return task

    def due(self, now: float) -> list[dict]:
        """到时间了、还没有运行的任务（拷贝）。"""
        with self._lock:
            self._refresh()
            return [
                copy.deepcopy(task)
                for task in self._tasks
                if task["enabled"] and task["next_run"] is not None and task["next_run"] <= now
            ]

    def next_due(self, skip: set[str]) -> float | None:
        """最早的下一次运行时刻；skip 里的任务不算（它们已经在排队或者正在运行）。调度线程用它决定睡多久。"""
        with self._lock:
            self._refresh()
            times = [
                task["next_run"]
                for task in self._tasks
                if task["enabled"] and task["next_run"] is not None and task["id"] not in skip
            ]
        return min(times, default=None)

    # ---- 增删改 ---------------------------------------------------------

    def add(self, fields: object) -> dict:
        """新建一个任务。返回它（拷贝）。"""
        task = check_task(fields)
        now = self.clock()
        with self._lock:
            self._refresh()
            if len(self._tasks) >= MAX_TASKS:
                raise TaskRefused(t("任务最多 {n} 个。先删掉一些用不到的再加。", n=MAX_TASKS))
            task.update(id=self._unused_id(), created_at=now, anchor=now, runs=[])
            task["next_run"] = self._first_run(task, now)
            self._tasks.append(task)
            self._save()
            saved = copy.deepcopy(task)
        self._notify()
        return saved

    def update(self, task_id: str, fields: object) -> dict:
        """改一个任务。改完重新算下一次运行的时刻。"""
        checked = check_task(fields)
        now = self.clock()
        with self._lock:
            self._refresh()
            task = self._get(task_id)
            # 先在一份拷贝上算好，算不出来（比如新选的时间已经过了）就什么都不改
            updated = {**task, **checked}
            if checked["schedule"] != task["schedule"]:
                updated["anchor"] = now  # 换了时间表：间隔从现在重新数
                updated["next_run"] = self._first_run(updated, now)
            else:
                # 只改了名称、指令：时间表没变，下一次照旧。一次性的任务已经过了时间，也不因为改了个名字就报错
                updated["next_run"] = self._upcoming(updated, now)
            task.update(updated)
            self._save()
            saved = copy.deepcopy(task)
        self._notify()
        return saved

    def set_enabled(self, task_id: str, enabled: bool) -> dict:
        """打开或关掉一个任务。关掉期间错过的不补；重新打开时从现在往后算。"""
        now = self.clock()
        with self._lock:
            self._refresh()
            task = self._get(task_id)
            task["enabled"] = enabled
            task["next_run"] = self._upcoming(task, now)
            self._save()
            saved = copy.deepcopy(task)
        self._notify()
        return saved

    def delete(self, task_id: str) -> dict:
        with self._lock:
            self._refresh()
            task = self._get(task_id)
            self._tasks.remove(task)
            self._save()
        self._notify()
        return task

    def _unused_id(self) -> str:
        while True:
            task_id = new_id()
            if self._find(task_id) is None:
                return task_id

    def _first_run(self, task: dict, now: float) -> float | None:
        """新的时间表的第一次。一次性任务的时间已经过了的话，告诉用户（多半是日期选错了）。"""
        moment = next_after(task["schedule"], now, anchor=task["anchor"])
        if moment is None:
            raise TaskError(t(PAST))
        return moment if task["enabled"] else None

    def _upcoming(self, task: dict, now: float) -> float | None:
        if not task["enabled"]:
            return None
        return next_after(task["schedule"], now, anchor=task["anchor"])

    # ---- 运行的记录（webui/scheduler.py 用）-------------------------------

    def add_run(self, task_id: str, run: dict) -> bool:
        """记下一次新的运行（排在最前面）。任务已经不在了返回 False。"""
        with self._lock:
            self._refresh()
            task = self._find(task_id)
            if task is None:
                return False
            task["runs"] = [run, *task["runs"]][:MAX_RUNS]
            self._save()
        self._notify()
        return True

    def update_run(self, task_id: str, run_id: str, **changes: Any) -> bool:
        """改一次运行的记录（开始了、停在确认上了、做完了……）。任务或这条记录已经不在了返回 False。"""
        with self._lock:
            self._refresh()
            task = self._find(task_id)
            run = None if task is None else next((run for run in task["runs"] if run["id"] == run_id), None)
            if run is None:
                return False
            run.update(changes)
            self._save()
        self._notify()
        return True

    def advance(self, task_id: str, now: float) -> None:
        """一次运行开始了：时间表上已经到了的那一次（或者几次）就算是它跑过了，下一次往后排。

        立即运行（手动）的时候，要是计划的时间还没到，计划不变；要是计划的时间已经到了（比如正好在排队），
        这一次就算数，不会紧接着再跑一遍。
        """
        with self._lock:
            self._refresh()
            task = self._find(task_id)
            if task is None or task["next_run"] is None or task["next_run"] > now:
                return
            task["anchor"] = now  # 间隔从这一次运行开始数
            task["next_run"] = self._upcoming(task, now)
            self._save()
        self._notify()

    def remind(self, task_id: str, now: float, run: dict) -> dict | None:
        """提醒到时间了：记下这次运行、放进"还没关掉的提醒"里，并把下一次往后排。一口气做完、只写一次文件。"""
        with self._lock:
            self._refresh()
            task = self._find(task_id)
            if task is None:
                return None
            if task["next_run"] is not None and task["next_run"] <= now:
                task["anchor"] = now
                task["next_run"] = self._upcoming(task, now)
            run.update(status="done", finished_at=now, summary=_brief(task["prompt"] or task["title"]))
            existing = next((item for item in task["runs"] if item["id"] == run["id"]), None)
            if existing is None:
                task["runs"] = [run, *task["runs"]][:MAX_RUNS]
            else:
                existing.update(run)  # 排过队的那一条（任务在排队期间从"让助手做"改成了"只是提醒"）
            reminder = {
                "id": new_id(),
                "task_id": task_id,
                "title": task["title"],
                "text": task["prompt"],
                "at": now,
                "catch_up": run["catch_up"],
            }
            self._reminders = [reminder, *self._reminders][:MAX_REMINDERS]
            self._save()
        self._notify()
        return reminder

    def dismiss(self, reminder_id: str | None) -> None:
        """关掉一条提醒；reminder_id 为 None 时全部关掉。已经关掉了的再关一次也不算错（比如页面上连点了两下）。"""
        with self._lock:
            self._refresh()
            kept = [] if reminder_id is None else [item for item in self._reminders if item["id"] != reminder_id]
            if len(kept) == len(self._reminders):
                return
            self._reminders = kept
            self._save()
        self._notify()


def _brief(text: str, limit: int = 120) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _is_time(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _restore_run(raw: object) -> dict | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or raw.get("status") not in STATUSES:
        return None
    if not _is_time(raw.get("started_at")):
        return None
    run = new_run(float(raw["started_at"]), raw["status"])
    run["id"] = raw["id"]
    finished, cost = raw.get("finished_at"), raw.get("cost")
    run["finished_at"] = float(finished) if _is_time(finished) else None
    run["cost"] = float(cost) if _is_time(cost) else None
    for key in ("steps", "missed"):
        run[key] = raw[key] if _is_int(raw.get(key)) and raw[key] >= 0 else 0
    for key in ("catch_up", "manual"):
        run[key] = raw.get(key) is True
    for key in ("conversation", "summary", "error", "error_key"):
        if isinstance(raw.get(key), str):
            run[key] = raw[key]
    values = raw.get("error_values")
    if run.get("error_key") and isinstance(values, dict) and all(isinstance(value, (str, int, float)) for value in values.values()):
        run["error_values"] = values
    elif run.get("error_key"):
        run["error_key"] = ""  # 原文的值对不上，就只用存下来的那句话，不重新译了
    return run


def _restore_reminder(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    texts = all(isinstance(raw.get(key), str) for key in ("id", "task_id", "title", "text"))
    if not texts or not _is_time(raw.get("at")):
        return None
    return {
        "id": raw["id"],
        "task_id": raw["task_id"],
        "title": raw["title"],
        "text": raw["text"],
        "at": float(raw["at"]),
        "catch_up": raw.get("catch_up") is True,
    }
