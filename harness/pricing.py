"""估算费用：把 token 数换算成钱。

这里算出来的只是估算。真正的账单以各家控制台为准：价格会调整，有的厂商还分高峰和闲时。

内置了 Claude 和 DeepSeek 几个模型的价格（2026-11-20 对照两家官方的定价页核对过）。其他模型的价格
可以在网页界面的设置里自己填，没有价格的模型只显示 token 数，不计入估算。

DeepSeek 分时段计价：北京时间工作日 9:00–12:00、14:00–18:00 是高峰，其余时间（包括周末）是闲时，
闲时价是高峰的一半。所以不能等一段对话结束后按总用量算一次，而是每调用一次模型就看当时是高峰还是闲时，
闲时的用量单独记一份（见 usage_key），算钱时各按各的价。
"""

from __future__ import annotations

from datetime import UTC, datetime

from .endpoints.spec import PREFIX as CUSTOM
from .providers.base import Usage

# 单位：美元 / 每百万 token。
#   input        没命中缓存的输入
#   cache_read   命中缓存的输入
#   cache_write  写入缓存（只有 Claude 收：这里用的是 5 分钟的缓存，按输入价的 1.25 倍）
#   output       输出（思考的内容也算输出）
#   offpeak      闲时价（只有 DeepSeek 有）：闲时的用量按这几项算，没写的项和上面一样
BUILTIN_PRICES: dict[str, dict] = {
    # https://platform.claude.com/docs/en/about-claude/pricing
    "claude-opus-5-5": {"input": 4.0, "cache_read": 0.20, "cache_write": 5.0, "output": 20.0},
    "claude-sonnet-5-5": {"input": 2.0, "cache_read": 0.10, "cache_write": 2.5, "output": 10.0},
    "claude-fable-5-2": {"input": 8.0, "cache_read": 0.20, "cache_write": 10.0, "output": 40.0},
    # https://api-docs.deepseek.com/quick_start/pricing 。上面是高峰价，offpeak 是闲时价。
    # DeepSeek 不收写入缓存的钱：没命中缓存的输入就按普通输入算
    "deepseek-flash": {"input": 0.30, "cache_read": 0.006, "output": 1.20, "offpeak": {"input": 0.15, "cache_read": 0.003, "output": 0.60}},
    "deepseek-v4-pro": {"input": 1.32, "cache_read": 0.044, "output": 3.96, "offpeak": {"input": 0.66, "cache_read": 0.022, "output": 1.98}},
}

# 换了名字的模型：官方说旧名字照样能用，按新名字的价格算
ALIASES = {"deepseek-v4-flash": "deepseek-flash"}

# Anthropic 的联网搜索：每 1,000 次 10 美元（搜到的内容另按输入 token 算）
SEARCH_PRICE = 0.01

OFFPEAK = "@offpeak"  # 闲时的用量记在"模型名@offpeak"下面

# 语音转文字（聊天里发来的语音，见 voice/）：不按 token，按语音的长短算，单位是美元 / 每分钟。
# 来自 OpenAI 定价页 Transcription 一节的 "Estimated cost"（https://developers.openai.com/api/docs/pricing ，2026-11-22 核对）。
# 那一节里 gpt-4o-transcribe、gpt-4o-mini-transcribe 也列了按 token 的价格，账单实际按 token 算，这里只是按分钟估算。
# gpt-transcribe 是官方现在推荐的；另外三个官方说 2027-02-26 停用（https://developers.openai.com/api/docs/guides/speech-to-text）。
# 定价页上写的是 "Whisper"，对应的模型名是 whisper-1。表里的顺序就是设置里列出来的顺序
TRANSCRIBE_PRICES: dict[str, float] = {
    "gpt-transcribe": 0.0045,
    "gpt-4o-mini-transcribe": 0.003,
    "gpt-4o-transcribe": 0.006,
    "whisper-1": 0.006,
}


def is_offpeak(moment: datetime) -> bool:
    """这一刻是不是 DeepSeek 的闲时。

    官方按协调世界时（UTC）写：周一到周五的 01:00–04:00 和 06:00–10:00 是高峰（北京时间 9:00–12:00、
    14:00–18:00），其余时间都是闲时，周末也是。官方另说中国法定节假日也算闲时；节假日每年由国务院另行公布，
    这里没有收录，所以落在工作日的节假日会按高峰价估算（估高一点，不会估低）。
    """
    utc = moment.astimezone(UTC)  # 不带时区的时间按本地时间理解
    if utc.weekday() >= 5:
        return True
    return not (1 <= utc.hour < 4 or 6 <= utc.hour < 10)


def price_key(model: str, provider: str = "") -> str:
    """在价格表里找这个模型用的名字：内置的几家就是模型名，自定义接口的是"接口/模型名"（见 usage_key 的说明）。"""
    return f"{provider}/{model}" if provider.startswith(CUSTOM) else model


def usage_key(model: str, moment: datetime, provider: str = "") -> str:
    """记用量时用的名字：价格分时段的模型，闲时的用量记在"模型名@offpeak"下面，算钱时按闲时价。

    自定义接口（provider 以 custom- 开头，见 endpoints/）上的模型记在"接口/模型名"下面：本机的 vLLM 上也可能有个
    deepseek-v4-pro，它不花钱，不能按 DeepSeek 的价格算，也不能把 DeepSeek 的价格改成 0。它们的价格在
    endpoints.json 里，由 EndpointStore.prices 按同样的名字交出来，和你填的价格合在一起（见 App.prices）。
    """
    if provider.startswith(CUSTOM):
        return price_key(model, provider)
    price = BUILTIN_PRICES.get(ALIASES.get(model, model))
    if price and "offpeak" in price and is_offpeak(moment):
        return model + OFFPEAK
    return model


def price_of(model: str, custom: dict[str, dict], offpeak: bool = False) -> dict[str, float] | None:
    """一个模型的价格：用户自己填的优先（全天都按它算），其次是内置的；都没有就返回 None。

    offpeak：要闲时的价格。没有闲时价的模型，闲时和平时一样。
    """
    price: dict | None
    if custom.get(model):
        price = custom[model]
    else:
        price = BUILTIN_PRICES.get(ALIASES.get(model, model))
        if price and offpeak and "offpeak" in price:
            price = {**price, **price["offpeak"]}
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
    unpriced: list[str] = []
    for key, usage in usage_by_model.items():
        offpeak = key.endswith(OFFPEAK)
        model = key.removesuffix(OFFPEAK)
        price = price_of(model, custom, offpeak)
        if price is None:
            if model not in unpriced:
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


def transcribe_cost(provider: str, model: str, seconds: float) -> float:
    """转一段 seconds 秒的语音估算花了多少钱（美元）。本机的兼容服务不花钱（见 voice/），表里没有的模型也当作 0：
    设置只让你在 OpenAI 那里选表里的模型，走不到这一步。"""
    if provider != "openai":
        return 0.0
    return round(TRANSCRIBE_PRICES.get(model, 0.0) * max(0.0, seconds) / 60, 6)
