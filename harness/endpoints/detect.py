"""猜一个模型能做什么：看服务器自己说了什么，没说的照名字猜。

OpenAI 格式的 /v1/models 只规定了 id，别的什么都没说。好在常见的几种服务器各自多给了一点：

  - Ollama：/api/show 回答里有 capabilities（["completion", "tools", "vision"]）和 model_info 里的 "<架构>.context_length"
  - LM Studio：/api/v0/models 里每个模型有 type（llm / vlm / embeddings）、max_context_length，新版本还有 capabilities（["tool_use"]）
  - vLLM：/v1/models 里每个模型有 max_model_len
  - OpenRouter 这类网关：/v1/models 里有 context_length、architecture.input_modalities、supported_parameters

这些都只是起点：同步完你可以在设置里改，改过的以后再同步也不会被冲掉（见 spec.merged）。
"能用工具"猜错了也不要紧：服务器拒绝的话，第一次请求时会自动关掉（见 providers/openai_compat.py）。
"""

from __future__ import annotations

from typing import Any

from .spec import MAX_CONTEXT, MIN_CONTEXT, EndpointModel

# 名字里带这些的不是用来聊天的（向量、语音、画图……），同步时去掉
NOT_CHAT = ("embed", "rerank", "whisper", "tts", "audio", "dall-e", "moderation", "transcribe", "stable-diffusion", "flux")
# 名字里带这些的多半看得懂图片（服务器没说的时候才照名字猜）
VISION_NAMES = (
    "vision", "-vl", "vl-", "_vl", "llava", "pixtral", "gemma3", "gemma-3", "minicpm-v", "moondream", "internvl",
    "qwen2.5vl", "qwen3-vl", "qwen2-vl", "molmo", "idefics", "smolvlm", "granite3.2-vision", "mistral-small3.1", "llama4",
)
# 名字里带这些的多半用不了工具（很老或者很小的模型）
NO_TOOL_NAMES = ("llama2", "codellama", "gemma:", "gemma2", "phi3", "tinyllama", "orca-mini", "vicuna", "starcoder", "deepseek-coder:")


def is_chat(name: str) -> bool:
    lowered = name.lower()
    return not any(word in lowered for word in NOT_CHAT)


def guess(name: str) -> EndpointModel:
    """只知道名字时的猜测。"""
    lowered = name.lower()
    return EndpointModel(
        id=name,
        vision=any(word in lowered for word in VISION_NAMES),
        tools=not any(word in lowered for word in NO_TOOL_NAMES),
    )


def _context(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if MIN_CONTEXT <= value <= MAX_CONTEXT else None


def from_listing(item: Any) -> EndpointModel | None:
    """/v1/models 里的一项（openai SDK 的 Model，或者一个 dict）→ 猜出来的模型。不是聊天模型的返回 None。"""
    data = item if isinstance(item, dict) else {"id": getattr(item, "id", None), **(getattr(item, "model_extra", None) or {})}
    name = data.get("id")
    if not isinstance(name, str) or not name.strip() or not is_chat(name):
        return None
    model = guess(name.strip().removeprefix("models/"))
    model.context = _context(data.get("max_model_len")) or _context(data.get("context_length")) or _context(data.get("context_window"))
    architecture = data.get("architecture")
    if isinstance(architecture, dict) and isinstance(architecture.get("input_modalities"), list):
        modalities = architecture["input_modalities"]
        model.vision = "image" in modalities
        model.pdf = "file" in modalities
    if isinstance(data.get("supported_parameters"), list):
        model.tools = "tools" in data["supported_parameters"]
    return model


def apply_ollama(model: EndpointModel, shown: Any) -> None:
    """用 Ollama 的 /api/show 的回答改正猜测。"""
    if not isinstance(shown, dict):
        return
    capabilities = shown.get("capabilities")
    if isinstance(capabilities, list):
        model.vision = "vision" in capabilities
        model.tools = "tools" in capabilities
    info = shown.get("model_info")
    if isinstance(info, dict):
        for key, value in info.items():
            if isinstance(key, str) and key.endswith(".context_length") and _context(value):
                model.context = model.context or _context(value)
                break


def apply_lmstudio(models: list[EndpointModel], listing: Any) -> list[EndpointModel]:
    """用 LM Studio 的 /api/v0/models 改正猜测；里面标成 embeddings 的去掉。"""
    data = listing.get("data") if isinstance(listing, dict) else None
    if not isinstance(data, list):
        return models
    known = {item.get("id"): item for item in data if isinstance(item, dict)}
    kept = []
    for model in models:
        item = known.get(model.id)
        if item is None:
            kept.append(model)
            continue
        if item.get("type") == "embeddings":
            continue
        if item.get("type") in ("vlm", "llm"):
            model.vision = item["type"] == "vlm"
        if isinstance(item.get("capabilities"), list):
            model.tools = "tool_use" in item["capabilities"]
        model.context = model.context or _context(item.get("max_context_length"))
        kept.append(model)
    return kept
