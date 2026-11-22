"""设置里"自定义模型接口"一节背后的接口：/api/endpoint/……。

  POST /api/endpoint/save     新建或修改一个接口（id 是要改的那一个的编号 custom-…，新建的不给；form 是表单，格式见 endpoints/spec.from_form）
  POST /api/endpoint/delete   删掉一个（id）。正在用它的话不删
  POST /api/endpoint/probe    测试连接、同步模型：拿着还没保存的表单（form，编辑时还有 id）去问那个地址，回答有哪些模型、每个能做什么
  POST /api/endpoint/reveal   在文件管理器里显示 data/endpoints.json（路径由程序算，页面什么都不给）

接口的清单在状态里（state.endpoints，见 state()），页面照着它画。密钥只有"有没有"和最后 4 位，表单里不改密钥时交回
{"keep": true}（和 MCP 服务器的密钥一样），服务器用存着的那个——只在地址的协议、主机、端口都没变时才行（见 spec.keep_or_new_key）。

为什么"测试连接"也从服务器发：页面的安全策略不许它连别的地址；而且密钥在服务器上，页面根本拿不到。
地址在发请求之前查过（endpoints/urls.py：外面的一律 https://，明文的 http:// 只许本机和内网），别的几家的密钥不会跟过去（endpoints/client.py）。

保存、删除会动到你正在用的模型（地址、密钥换了，要换上新的连接），所以和换模型一样要拿对话的锁；测试连接只是去问一声，不拿。
这几个接口只有带口令的页面能用（和别的接口一样，见 server.py）：模型没有任何能添加、修改接口的工具，endpoints.json 在文件工具的 protected 名单里。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import CUSTOM_CONTEXT_WINDOW, MAX_ENDPOINT_MODELS, MAX_ENDPOINTS
from ..endpoints import Endpoint, EndpointError, EndpointModel
from ..endpoints import probe as endpoint_probe
from ..endpoints.spec import check_models, draft, merged
from ..i18n import t
from ..safe import private_dir
from .ui import said

if TYPE_CHECKING:
    from .server import WebApp


class EndpointsAPI:
    def __init__(self, web: WebApp, reveal: Callable[[Path], None], probe: Callable[[Endpoint], endpoint_probe.Found] = endpoint_probe.probe):
        self.web = web
        self._reveal = reveal
        self._probe = probe  # 测试换成假的也行；默认就是真的去问（对着本地的假服务器）

    @property
    def store(self):
        return self.web.app.endpoints

    def state(self) -> dict:
        """状态里的清单。"""
        used = self.web.app.providers_in_use()  # 活着的每一段对话用的（网页上在后台跑着的也算），加上设置里选的
        return {
            "items": [summary(item, item.provider_id in used) for item in self.store.list()],
            "file": str(self.store.path),
            "problem": said(self.store.problem) if self.store.problem else None,
            "limit": MAX_ENDPOINTS,
            "models_limit": MAX_ENDPOINT_MODELS,
            "default_context": CUSTOM_CONTEXT_WINDOW,
            # 自己填一个模型名时它默认能做什么（和 endpoints/spec.EndpointModel 一样，页面不另写一份）
            "blank": {key: value for key, value in EndpointModel(id="").saved().items() if key != "id"},
        }

    def route(self, action: str, body: dict) -> tuple[int, dict]:
        if action == "reveal":
            return self._reveal_file()
        if action == "probe":
            return self._probe_form(body)
        if action not in ("save", "delete"):
            return 404, {"error": t("没有这个操作")}
        ident = body.get("id")
        if ident is not None and not isinstance(ident, str):
            return 400, {"error": t("参数不正确")}
        if action == "save" and not isinstance(body.get("form"), dict):
            return 400, {"error": t("参数不正确")}
        if action == "delete" and not ident:
            return 400, {"error": t("参数不正确")}
        # 保存会换掉你正看着的那一段的连接（它在用这个接口的话），所以拿它的锁，和换模型一样（见 WebApp._viewed_idle）
        with self.web._viewed_idle() as free:
            if not free:
                return 409, {"error": self.web._busy()}
            try:
                if action == "save":
                    endpoint = self.web.app.save_endpoint(body["form"], ident or None)
                else:
                    self.web.app.delete_endpoint(ident)
            except EndpointError as exc:
                return 409, {"error": exc.message}
            except OSError as exc:
                return 409, {"error": t("没能写进 {file}（{error}）。", file=self.store.path, error=exc)}
        self.web.publish_state()
        if action == "delete":
            return 200, {"ok": True}
        return 200, {"ok": True, "endpoint": summary(endpoint, False)}

    def _probe_form(self, body: dict) -> tuple[int, dict]:
        """拿还没保存的表单去问一声。不拿对话的锁：不改任何东西。

        表单里带着模型列表（form.models）的话，回答的 models 是"同步以后"的列表：你在窗口里改过的设置保留，
        服务器上新有的加进来，没有了的去掉（规矩在 endpoints/spec.merged，和换模型面板里的"同步"是同一条）。不带就是服务器上的原样。
        """
        form, ident = body.get("form"), body.get("id")
        if not isinstance(form, dict) or (ident is not None and not isinstance(ident, str)):
            return 400, {"error": t("参数不正确")}
        existing = self.store.get(ident) if ident else None
        if ident and existing is None:
            return 409, {"error": t("这个接口已经不在了，可能刚被删掉。")}
        try:
            mine = check_models(form["models"]) if "models" in form else None
            endpoint = draft(form, existing)
            found = self._probe(endpoint)
        except EndpointError as exc:
            return 409, {"error": exc.message}
        models = found.models if mine is None or not found.listed else merged(mine, found.models)
        return 200, {
            "ok": True,
            "listed": found.listed,
            "server": found.server,
            "where": endpoint.where,
            "models": [model.saved() for model in models],
        }

    def _reveal_file(self) -> tuple[int, dict]:
        """在文件管理器里显示 endpoints.json。还没有这个文件（一个接口都没加过）就打开它将来所在的文件夹。"""
        path = self.store.path
        shown = path if path.exists() else path.parent
        try:
            private_dir(path.parent)
            self._reveal(shown)
        except OSError as exc:
            return 409, {"error": t("没能打开文件管理器（{error}）。文件在：{path}", error=exc, path=path)}
        return 200, {"ok": True, "path": str(shown)}


def summary(endpoint: Endpoint, in_use: bool) -> dict:
    """页面上的一个接口。密钥只有"有没有"和最后 4 位。"""
    return {
        "id": endpoint.provider_id,
        "slug": endpoint.id,
        "name": endpoint.name,
        "base_url": endpoint.base_url,
        "where": endpoint.where,  # local 这台电脑 / lan 内网 / remote 外面
        "has_key": bool(endpoint.api_key),
        "key_hint": endpoint.hint(),
        "stream_usage": endpoint.stream_usage,
        "in_use": in_use,  # 正在用它上面的模型：删不了
        "models": [model.saved() for model in endpoint.models],
    }
