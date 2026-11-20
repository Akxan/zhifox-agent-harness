"""估算费用：把 token 数换算成钱。

这里算出来的只是估算。真正的账单以各家控制台为准：价格会调整，有的厂商还分高峰和低谷时段。

内置的只有 Claude 几个模型的价格。其他模型的价格可以在网页界面的设置里自己填，
没有价格的模型只显示 token 数，不计入估算。
"""

from __future__ import annotations

from .providers.base import Usage

# 单位：美元 / 每百万 token。2026 年 11 月的公开价格。
BUILTIN_PRICES: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {"input": 4.0, "cache_read": 0.20, "cache_write": 5.0, "output": 20.0},
    "claude-sonnet-5-5": {"input": 2.0, "cache_read": 0.10, "cache_write": 2.5, "output": 10.0},
    "claude-fable-5-2": {"input": 8.0, "cache_read": 0.20, "cache_write": 10.0, "output": 40.0},
}

# Anthropic 的联网搜索：每 1,000 次 10 美元
SEARCH_PRICE = 0.01


def price_of(model: str, custom: dict[str, dict]) -> dict[str, float] | None:
    """一个模型的价格：用户自己填的优先，其次是内置的；都没有就返回 None。"""
    price = custom.get(model) or BUILTIN_PRICES.get(model)
    if not price or "input" not in price or "output" not in price:
        return None
    return {
        "input": float(price["input"]),
        "output": float(price["output"]),
        # 没有单独填缓存价格的，按普通输入的价格算（宁可估高，不要估低）
        "cache_read": float(price.get("cache_read", price["input"])),
        "cache_write": float(price.get("cache_write", price["input"])),
    }


def estimate(usage_by_model: dict[str, Usage], custom: dict[str, dict]) -> tuple[float, list[str]]:
    """估算总费用。返回（美元，因为没有价格而没算进去的模型）。"""
    total = 0.0
    unpriced = []
    for model, usage in usage_by_model.items():
        price = price_of(model, custom)
        if price is None:
            unpriced.append(model)
            continue
        total += (
            usage.input_tokens * price["input"]
            + usage.cache_read_tokens * price["cache_read"]
            + usage.cache_write_tokens * price["cache_write"]
            + usage.output_tokens * price["output"]
        ) / 1_000_000
        total += usage.searches * SEARCH_PRICE
    return total, unpriced
