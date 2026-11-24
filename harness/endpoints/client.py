"""连一个自定义接口的客户端：OpenAI 的 SDK，但别的几家的密钥一个字都不带过去。

为什么要特别小心：OpenAI 的 SDK 为了方便，会自己去环境变量里找东西——没给密钥就用 OPENAI_API_KEY，
还会把 OPENAI_ORG_ID、OPENAI_PROJECT_ID 写进请求头，把 OPENAI_CUSTOM_HEADERS 里的每一行都加进请求头。
这些是给 OpenAI 用的，要是跟着请求发到了你填的那个地址（一台局域网里的电脑、一个第三方的网关），
就等于把 OpenAI 的密钥交给了它。所以：

  - 密钥永远明着给：填了就用填的；没填就给一个占位的 no-key（本地的服务器不看它；不给的话 SDK 会去拿 OPENAI_API_KEY）；
  - 建好以后，把 SDK 从环境变量里拿到的组织、项目、管理员密钥、自定义请求头全部清掉。这几个是 SDK 内部的名字，
    升级 SDK 以后要是改了名字，tests/test_endpoints_client.py 会发现请求里又带上了它们；
  - 不跟着重定向走：一个 3xx 可以把带着密钥和整段对话的请求转到任何地方去。SDK 默认会跟，这里关掉，
    碰到重定向就报错，请你直接填最终的地址；
  - 本机和内网的地址不走代理（trust_env=False）：数据本来就不出你的网络，交给代理反而出去了，
    而且很多代理根本连不到你家里的 192.168.x.x。外面的网关照常按 HTTPS_PROXY 走。
"""

from __future__ import annotations

from typing import Any

import openai

from ..providers import inflight
from .spec import Endpoint

NO_KEY = "no-key"  # 不用密钥的服务器：Authorization 里放这个占位的（Ollama 的文档也是这么写的）


def make(endpoint: Endpoint, *, timeout: float | None = None, max_retries: int = 2) -> Any:
    """连这个接口的客户端。timeout 不给就用 SDK 默认的（本地的大模型第一次加载可能要好几分钟，聊天时不能催它）。"""
    # 钩子："停止"时连回答的头还没到的请求也能马上断开（本地的大模型第一次加载时可能好几分钟才回答，见 providers/inflight.py）
    options: dict[str, Any] = {"follow_redirects": False, "trust_env": not endpoint.local, "event_hooks": inflight.event_hooks()}
    if timeout is not None:
        options["timeout"] = timeout
    client = openai.OpenAI(
        api_key=endpoint.api_key or NO_KEY,
        admin_api_key="",
        organization=None,
        project=None,
        base_url=endpoint.base_url,
        max_retries=max_retries,
        default_headers={},
        http_client=openai.DefaultHttpxClient(**options),
    )
    _forget_environment(client)
    return client


def _forget_environment(client: Any) -> None:
    """清掉 SDK 自己从环境变量里拿的东西（见开头的说明）。"""
    client.organization = None
    client.project = None
    client.webhook_secret = None
    client.admin_api_key = None
    if hasattr(client, "_custom_headers"):
        client._custom_headers = {}
    if hasattr(client, "_ambient_authorizations"):
        client._ambient_authorizations = frozenset()
