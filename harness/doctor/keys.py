"""自检里的"密钥"和"网络"两节：哪几家有密钥、现在用的是哪一家，以及只看配置的网络设置（代理、改过的接口地址）。

密钥只说有没有、从哪儿来（.env、环境变量、登录信息），绝不写出值，连最后几位也不写（网页上的 key_hint 是给你自己看的，
自检的结果是要贴给别人看的）。"有没有"用的是 providers.has_key，和启动时、换模型的面板里是同一条规矩。
.env 里的值会读进内存，但只拿来比较"环境变量里的是不是同一个"，从不进结果。

网络这一节不发任何请求：只看环境变量里的代理、改过的接口地址。地址只写到"协议、主机和端口"为止，
用户名、密码、路径和 ? 后面的都不写（代理的地址常常带着密码）。
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from ..app import FALLBACK
from ..config import API_BASES, Config, env_file_values
from ..endpoints import EndpointStore, urls
from ..i18n import k, t
from ..model_catalog import WHERE
from ..providers import PROVIDERS, has_key
from ..settings import Settings
from . import fixes
from .report import OK, PROBLEM, UNUSED, Check, Section

PROXIES = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")
# 能改各家接口地址的环境变量：Anthropic 和 OpenAI 的 SDK 自己认的两个（OPENAI_BASE_URL 也是语音转文字的地址），
# 加上聊天平台的那几个（见 config.API_BASES）。密钥会发到这些地址去
ADDRESSES = ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "TELEGRAM_API_BASE", *(env for _, env, _ in API_BASES))
SOURCES = {"env_file": k("在 .env 里"), "environment": k("在环境变量里"), "login": k("登录信息（ANTHROPIC_AUTH_TOKEN 或者 ant auth login）")}


def _source(provider, in_file: dict[str, str]) -> str:
    """密钥是从哪儿来的。环境变量里的优先（.env 只补环境变量里没有的，见 config.load_env_file）。"""
    value = os.environ.get(provider.key_env)
    if value:
        return "env_file" if in_file.get(provider.key_env) == value else "environment"
    return "login"


def _provider(provider, settings: Settings, in_file: dict[str, str], env_file) -> Check:
    facts: dict = {"provider": provider.id, "key_env": provider.key_env}
    current = provider.id == settings.provider
    if has_key(provider):
        source = _source(provider, in_file)
        facts.update(has_key=True, source=source)
        detail = t("有密钥（{source}）", source=t(SOURCES[source]))
        if source == "environment" and provider.key_env in in_file:
            detail = t("{detail}。.env 里也有一个不一样的：用的是环境变量里的，改 .env 不起作用", detail=detail)
        return Check(f"key.{provider.id}", provider.label, OK, detail, facts=facts)
    facts.update(has_key=False, source="")
    if provider.key_env in os.environ and provider.key_env in in_file:
        # 环境变量设成了空的：.env 只补环境变量里"没有"的，空的也算有，于是 .env 里的密钥读不进来
        detail = t("{name} 在环境变量里是空的，盖住了 .env 里的密钥", name=provider.key_env)
        return Check(f"key.{provider.id}", provider.label, PROBLEM, detail, [fixes.unset(provider.key_env)], facts)
    if current:
        detail = t("没有密钥，可是现在用的就是它")
        return Check(f"key.{provider.id}", provider.label, PROBLEM, detail, fixes.add_key(provider.key_env, env_file), facts)
    return Check(f"key.{provider.id}", provider.label, UNUSED, t("没有密钥"), facts=facts)


def _current(settings: Settings, endpoints: EndpointStore) -> Check:
    """设置里选的是哪一家的哪个模型，这一家还在不在、能不能用。"""
    facts = {"provider": settings.provider, "model": settings.model}
    builtin = PROVIDERS.get(settings.provider)
    custom = endpoints.get(settings.provider)
    label = t("现在用的模型")
    if builtin is not None:
        # 选的这一家没有密钥的话，算在下面那一家的那一行上（连同修法），这里不再算一次：一个原因只算一个问题
        return Check("model", label, OK, f"{builtin.label} · {settings.model}", facts=facts)
    if custom is not None:
        detail = t("{name}（自定义接口，{where}）· {model}", name=custom.name, where=t(WHERE[custom.where]), model=settings.model)
        return Check("model", label, OK, detail, facts=facts)
    # 启动时换回哪一个，照着 build_app 的那一处（app.FALLBACK）说
    fallback = f"{PROVIDERS[FALLBACK[0]].label} · {FALLBACK[1]}"
    detail = t("设置里选的 {provider} 已经不在了（自定义接口删掉了？），启动时会换回 {fallback}", provider=settings.provider, fallback=fallback)
    return Check("model", label, PROBLEM, detail, [t("在网页界面顶上的模型按钮里另选一个。")], facts)


def _endpoints(endpoints: EndpointStore, config: Config) -> Check:
    items = endpoints.list()
    facts = {"count": len(items), "where": [item.where for item in items]}
    label = t("自定义模型接口")
    if endpoints.problem:
        return Check("endpoints", label, PROBLEM, str(endpoints.problem), [t("修好或者删掉 {path}，再重新启动智狐。", path=config.endpoints_file)], facts)
    if not items:
        return Check("endpoints", label, UNUSED, t("没有"), facts=facts)
    names = t("、").join(t("{name}（{where}）", name=item.name, where=t(WHERE[item.where])) for item in items)
    return Check("endpoints", label, OK, t("{n} 个：{names}", n=len(items), names=names), facts=facts)


def keys(config: Config, settings: Settings, endpoints: EndpointStore) -> Section:
    in_file = dict(env_file_values(config.env_file))  # 只拿来比较，从不写进结果
    checks = [_current(settings, endpoints)]
    checks += [_provider(provider, settings, in_file, config.env_file) for provider in PROVIDERS.values()]
    checks.append(_endpoints(endpoints, config))
    return Section("keys", t("密钥"), checks)


# ---- 网络 ------------------------------------------------------------------------


def origin(url: str) -> tuple[str, str, str]:
    """一个地址的（协议，主机，给人看的样子）：只到端口为止，不带用户名、密码、路径和参数。看不懂的返回空的。"""
    text = url.strip()
    try:
        parts = urlsplit(text if "://" in text else "http://" + text)
        port = parts.port
    except ValueError:
        return "", "", ""
    host = parts.hostname or ""
    if not host:
        return "", "", ""
    shown = f"[{host}]" if ":" in host else host
    return parts.scheme.lower(), host, f"{parts.scheme.lower()}://{shown}" + (f":{port}" if port is not None else "")


def _proxies() -> Check:
    found = []
    for name in PROXIES:
        value = os.environ.get(name) or os.environ.get(name.lower())
        if value:
            _, _, shown = origin(value)
            found.append(f"{name} = {shown or t('（看不懂的写法）')}")
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
    facts = {"proxies": [line.split(" = ", 1)[0] for line in found], "no_proxy": bool(no_proxy)}
    if not found:
        return Check("proxy", t("代理"), OK, t("没有设置代理：直接连"), facts=facts)
    detail = t("、").join(found)
    if no_proxy:
        detail = t("{detail}；不走代理的：{hosts}", detail=detail, hosts=no_proxy)
    return Check("proxy", t("代理"), OK, detail, facts=facts)


def _addresses() -> Check:
    """改过的接口地址。明文的 http:// 指向外面的服务器：密钥会以明文发过去，算有问题（和自定义接口的规矩一样，见 endpoints/urls.py）。"""
    changed, bad = [], []
    for name in ADDRESSES:
        value = os.environ.get(name)
        if not value:
            continue
        scheme, host, shown = origin(value)
        changed.append(f"{name} = {shown or t('（看不懂的写法）')}")
        if scheme == "http" and urls.where_is(host) == urls.REMOTE:
            bad.append(name)
    facts = {"changed": [line.split(" = ", 1)[0] for line in changed], "plain_http": bad}
    label = t("各家的接口地址")  # 不用"接口地址"：那一句在自定义接口的表单上，是另一个意思
    if bad:
        detail = t("{names} 是明文的 http://，又不在这台电脑或者内网里：密钥和对话会以明文发过去", names=t("、").join(bad))
        fix: list[str] = [t("改成 https:// 的地址，或者去掉它：{run}", run=fixes.unset(name)) for name in bad]
        return Check("addresses", label, PROBLEM, t("{detail}（{changed}）", detail=detail, changed=t("、").join(changed)), fix, facts)
    if not changed:
        return Check("addresses", label, OK, t("都是各家官方的地址"), facts=facts)
    return Check("addresses", label, OK, t("改过的：{changed}", changed=t("、").join(changed)), facts=facts)


def network(config: Config) -> Section:
    web = Check("web_search", t("联网搜索"), OK, t("开着（只有 Claude 能联网搜索）"), facts={"enabled": True})
    if not config.web:
        web = Check("web_search", t("联网搜索"), UNUSED, t("启动时关掉了（HARNESS_WEB=0）"), facts={"enabled": False})
    return Section("network", t("网络（只看设置，不发请求）"), [_proxies(), _addresses(), web])
