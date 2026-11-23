from __future__ import annotations

import math
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from .i18n import fill, k, t
from .tasks_check import TaskError, check_schedule

MAX_COUNTED = 1000  # 数"错过了几次"时最多数到这里（一个每天的任务关了三年也就一千来次）

WEEKDAY_NAMES = (k("周一"), k("周二"), k("周三"), k("周四"), k("周五"), k("周六"), k("周日"))
PAST = k("这个时间已经过去了，请选一个以后的时间。")


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
        following = next_after(schedule, moment, anchor)
        if following is None or following > now:
            break
        moment = following
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
