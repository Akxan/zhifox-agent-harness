"""自定义接口存在哪儿：data/endpoints.json。

为什么是单独的一个文件，而不是 settings.json 加 .env：
  - 一个接口不是一个"键 = 值"：地址、名字、可选的密钥，再加上一串模型、每个模型几样能力和价格。.env 只能放一行一个值；
  - 密钥不进环境变量。内置几家的密钥在 .env 里，启动时读进环境变量，所以执行命令、启动 MCP 服务器之前要一个个去掉
    （tools/shell.py 的 SECRET_ENV_VARS）。自定义接口的密钥只在这个文件里、只在要发请求时交给那一个客户端（见 client.py），
    哪个子进程都看不到，也就不用再往那张单子上加名字；
  - 和 mcp.json、channels.json 是同一种做法：存着密钥的配置文件权限 600、文件工具碰不到（app.py 的 protected）、
    导出时密钥换成占位符（export.MASKED_FILES）、发给页面的只有最后 4 位（Endpoint.hint）。

文件的样子：

    {"version": 1, "endpoints": [
      {"id": "ollama", "name": "Ollama", "base_url": "http://localhost:11434/v1",
       "models": [{"id": "qwen3:8b", "vision": false, "tools": true, "pdf": false, "context": 40960}]},
      {"id": "gateway", "name": "公司网关", "base_url": "https://llm.example.com/v1", "api_key": "sk-…",
       "stream_usage": false, "models": [{"id": "gpt-x", "vision": true, "tools": true, "pdf": true,
       "price": {"input": 2, "output": 8}}]}
    ]}

读的时候：整个文件读不懂（不是 JSON），一个接口都不用，problem 里说一声，而且在你修好或者删掉它之前不再写它——
免得一保存就把你手写的内容冲掉。里面有个别接口读不懂（地址变成了外面的 http://……），跳过那几个，problem 里说一声；
它们原样留在文件里（写回时照抄），不认识的东西不删。

网页上的保存、删除，对话、定时任务里发现"这个接口不认 stream_options""这个模型用不了工具"时记下来（learned），
可能同时发生，所以每次"读出、改、写回"都拿着 _lock。
"""

from __future__ import annotations

import builtins
import json
import threading
from copy import deepcopy
from pathlib import Path

from ..config import MAX_ENDPOINTS
from ..i18n import Message, t
from ..safe import private_dir, to_json, write_file
from .spec import Endpoint, EndpointError, EndpointModel, from_form, from_saved, merged

VERSION = 1


class EndpointStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._items: list[Endpoint] = []
        self.problem: Message | None = None  # 文件读不懂、有接口没用上：给人看的一句话
        self._broken = False  # 整个文件读不懂：修好之前不写它
        self._kept: list = []  # 读不懂的那几项，原样留着，写回时照抄
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
            self._broken = True
            self.problem = t("{file} 读不懂（{error}），自定义模型接口一个都没有用上。修好它，或者把它删掉重新添加。", file=self.path, error=exc)
            return
        entries = data.get("endpoints") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            self._broken = True
            self.problem = t("{file} 读不懂（{error}），自定义模型接口一个都没有用上。修好它，或者把它删掉重新添加。", file=self.path, error="endpoints")
            return
        skipped: list[str] = []
        for entry in entries:
            try:
                endpoint = from_saved(entry)
            except EndpointError as exc:
                skipped.append(f"{entry.get('name') if isinstance(entry, dict) else '?'}（{exc.message}）")
                self._kept.append(entry)
                continue
            if any(known.id == endpoint.id for known in self._items):
                skipped.append(endpoint.name)
                self._kept.append(entry)
                continue
            self._items.append(endpoint)
        if skipped:
            self.problem = t("{file} 里有几个接口读不懂，没有用上：{names}", file=self.path, names="；".join(skipped))

    # ---- 读 -----------------------------------------------------------------

    def list(self) -> list[Endpoint]:
        """全部接口。每次发状态、找一家提供方都要用，所以不复制：这里的 Endpoint 一旦交出去就不再改
        （改的时候是复制一份、改好、整个换掉，见 _change），调用的人也只读不改。要改的用 get（复制出来的）。"""
        with self._lock:
            return list(self._items)

    def get(self, provider_id: str) -> Endpoint | None:
        with self._lock:
            found = next((item for item in self._items if item.provider_id == provider_id), None)
            return deepcopy(found) if found is not None else None

    def prices(self) -> dict[str, dict]:
        """每个模型的价格，按"接口/模型"记（pricing.usage_key 给自定义接口的用量起的就是这个名字），没有价格的不在里面。

        和设置里你给内置模型填的价格（settings.prices，按模型名记）合在一起交给 pricing.estimate：
        名字带着 custom- 的前缀，不会和内置的模型撞上——本机的 vLLM 也叫 deepseek-v4-pro 时，DeepSeek 的价格不会被它改成 0。
        """
        out: dict[str, dict] = {}
        for endpoint in self.list():
            for model in endpoint.models:
                price = endpoint.price_of(model.id)
                if price is not None:
                    out[f"{endpoint.provider_id}/{model.id}"] = price
        return out

    # ---- 改 -----------------------------------------------------------------

    def save(self, form: dict, provider_id: str | None = None) -> Endpoint:
        """新建（provider_id 为 None）或者修改一个接口。表单的格式见 spec.from_form。失败抛出 EndpointError。"""
        with self._lock:
            self._writable()
            existing = None
            if provider_id is not None:
                existing = next((item for item in self._items if item.provider_id == provider_id), None)
                if existing is None:
                    raise EndpointError(t("这个接口已经不在了，可能刚被删掉。"))
            elif len(self._items) + len(self._kept) >= MAX_ENDPOINTS:
                raise EndpointError(t("最多接 {n} 个自定义接口。", n=MAX_ENDPOINTS))
            others = [item for item in self._items if item is not existing]
            endpoint = from_form(form, existing, {item.id for item in others})
            if any(item.name.casefold() == endpoint.name.casefold() for item in others):
                raise EndpointError(t("已经有一个叫“{name}”的接口了，换个名字。", name=endpoint.name))
            items = [endpoint if item is existing else item for item in self._items] if existing else [*self._items, endpoint]
            self._write(items)
            return deepcopy(endpoint)

    def delete(self, provider_id: str) -> None:
        with self._lock:
            self._writable()
            items = [item for item in self._items if item.provider_id != provider_id]
            if len(items) == len(self._items):
                raise EndpointError(t("这个接口已经不在了，可能刚被删掉。"))
            self._write(items)

    # 这个类里有一个叫 list 的方法：在它后面写 list[...]，类型检查会以为说的是这个方法，所以写成 builtins.list
    def set_models(self, provider_id: str, found: builtins.list[EndpointModel]) -> Endpoint:
        """同步来的模型列表（规矩见 spec.merged）。"""
        return self._change(provider_id, lambda item: setattr(item, "models", merged(item.models, found)))

    def set_price(self, provider_id: str, model: str, price: dict | None) -> None:
        """设置里"价格"一节改的是这个接口上的这个模型（None：回到默认的）。列表里没有这个模型的话，加进去。"""

        def change(item: Endpoint) -> None:
            found = item.model(model)
            if found is None:
                found = EndpointModel(id=model)
                item.models.append(found)
            found.price = price

        self._change(provider_id, change)

    def learned(self, provider_id: str, model: str, *, tools: bool | None = None, stream_usage: bool | None = None) -> None:
        """请求时发现的：这个接口不认 stream_options、这个模型用不了工具。记下来，以后不再先碰一次壁。

        记不下来（文件写不进去、接口刚被删掉）不要紧：这一次的对话已经照着新的办法发出去了，只是下次还要再碰一次。
        """

        def change(item: Endpoint) -> None:
            if stream_usage is not None:
                item.stream_usage = stream_usage
            if tools is not None:
                found = item.model(model)
                if found is None:
                    found = EndpointModel(id=model)
                    item.models.append(found)
                found.tools = tools

        try:
            self._change(provider_id, change)
        except (EndpointError, OSError):
            pass

    # ---- 内部 ---------------------------------------------------------------

    def _change(self, provider_id: str, change) -> Endpoint:
        with self._lock:
            self._writable()
            items = deepcopy(self._items)
            item = next((one for one in items if one.provider_id == provider_id), None)
            if item is None:
                raise EndpointError(t("这个接口已经不在了，可能刚被删掉。"))
            change(item)
            self._write(items)
            return deepcopy(item)

    def _writable(self) -> None:
        if self._broken:
            raise EndpointError(t("{file} 读不懂，修好它（或者删掉它）之前不能保存，免得把里面的内容冲掉。", file=self.path))

    def _write(self, items: builtins.list[Endpoint]) -> None:
        """写回文件（权限 600），成了才换掉内存里的。调用的人拿着 _lock。"""
        private_dir(self.path.parent)
        data = {"version": VERSION, "endpoints": [*(item.saved() for item in items), *self._kept]}
        write_file(self.path, to_json(data, readable=True, indent=2) + "\n")
        self._items = items
