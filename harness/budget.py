"""每月预算：这个月的花费超过你定的金额时，提醒你一次。只是提醒，不拦着任何操作。

预算填在设置里（美元，可以不填），花了多少来自花费账本（见 ledger.py）。

"提醒一次"是这样算的：
  - 同一个月、同一个预算金额，只提醒一次。提醒过就记在设置里（budget_alert），重启也不会再提醒；
  - 你把预算调高，花费又超过了新的金额，再提醒一次；
  - 调低不会再提醒：你已经知道花费超过了更高的那个数，它当然也超过了更低的这个；
  - 到了下个月，重新开始。
预算填得比这个月已经花的还少，马上提醒一次（"你已经超了"）。

页面上的提示只弹一次，但"超了没有"一直显示在信息栏的费用卡片上，错过了那一下提示也看得到。
"""

from __future__ import annotations

import math
from datetime import date

MAX_BUDGET = 1_000_000  # 填得再大就不像是预算了，多半是手滑


def month_of(day: date) -> str:
    """一个日期在哪个月，写成 "2026-11"。"""
    return f"{day:%Y-%m}"


def valid_budget(value: object) -> bool:
    """预算要么不填（None），要么是一个大于 0、不超过 MAX_BUDGET 的数。"""
    if value is None:
        return True
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 < value <= MAX_BUDGET


def should_alert(spent: float, budget: float | None, last_alert: dict | None, month: str) -> bool:
    """这个月花了 spent，预算是 budget，上一次提醒是 last_alert（{"month": "2026-11", "amount": 50}）。现在该不该提醒？"""
    if not budget or spent < budget:
        return False
    # 这个月已经为这么高（或者更高）的预算提醒过了，就不再提醒
    return not (last_alert and last_alert.get("month") == month and last_alert.get("amount", 0) >= budget)


def marker(alert: dict | None, month: str) -> str | None:
    """这个月的那次提醒，写成一个短短的记号（"2026-11:50"），页面靠它判断"这次提醒我弹过没有"。不是这个月的不算。"""
    if not alert or alert.get("month") != month:
        return None
    return f"{alert['month']}:{alert['amount']}"
