"""有哪些聊天平台、各自到了哪一步、接入时要填什么。网页上"通道"那一页的卡片和向导、命令行的 setup 都照着这张表
（页面不自己再抄一份）。每个平台怎么一步步建机器人，写在 guides.py。

stage 的意思：
  ready        现在就能用
  soon         以后做：官方的接口不需要公网地址（机器人主动连平台），只是还没写
  public_url   以后做：平台只会把消息推到一个公网能访问的网址（webhook），要先有隧道或者服务器
  unsupported  不做：没有官方接口

fields 是连接时要填的东西。secret 为真的是密钥：只存在 channels.json 里（权限 600），页面上只露出最后 4 位，
不写进日志，导出时换成占位符，助手运行的命令也拿不到同名的环境变量（见 tools/shell.SECRET_ENV_VARS）。
不是密钥的（App ID、Client ID）是应用的编号，谁都可以知道，页面上照常显示。env 是命令行里可以预先放它的环境变量。

sdk 是这个平台要用的官方 SDK（可选的依赖，见 pyproject.toml 的 optional-dependencies）。飞书的长连接、钉钉的
Stream 模式协议复杂，用官方的 SDK 收消息；别的平台的协议很朴素，手写。没装 SDK 时页面和命令行会说清楚怎么装。

为什么个人微信不做：微信没有给个人号的机器人接口。网上能找到的办法都是模拟登录网页版或者电脑版微信，
违反它的使用条款，账号随时可能被限制登录甚至封掉。智狐不拿你的微信号冒这个险；想在微信里用，
可以等以后的企业微信（官方的接口，在微信里也能收到消息）。
"""

from __future__ import annotations

import importlib.util

from ..i18n import k, t
from .guides import GUIDES


def _token(env: str, placeholder: str) -> dict:
    return {"id": "token", "label": k("机器人的 token"), "secret": True, "env": env, "placeholder": placeholder}


PLATFORMS = [
    {
        "id": "telegram",
        "label": "Telegram",
        "stage": "ready",
        "needs": k("在 @BotFather 那里建一个机器人，拿到它的 token。不需要公网地址。"),
        "fields": [_token("TELEGRAM_BOT_TOKEN", "123456789:AAH…")],
        "step": k("填 token"),
        "pair": k("也可以在 Telegram 里搜索 {bot}，点“开始”，再把这 6 位数字发过去。"),
    },
    {
        "id": "discord",
        "label": "Discord",
        "stage": "ready",
        "needs": k("Discord 开发者后台的机器人 token，用 Gateway 收消息，不需要公网地址。"),
        "fields": [_token("DISCORD_BOT_TOKEN", "MTIz…")],
        "step": k("填 token"),
        "pair": k("在 Discord 里打开和 {bot} 的私聊（把它加到你的账号以后，在应用列表里点它），把这 6 位数字发过去。"),
    },
    {
        "id": "slack",
        "label": "Slack",
        "stage": "ready",
        "needs": k("Slack 应用的 Bot token 和 App token，用 Socket Mode 收消息，不需要公网地址。"),
        "fields": [
            {"id": "bot_token", "label": k("Bot token（xoxb- 开头）"), "secret": True, "env": "SLACK_BOT_TOKEN", "placeholder": "xoxb-…"},
            {"id": "app_token", "label": k("App-level token（xapp- 开头）"), "secret": True, "env": "SLACK_APP_TOKEN", "placeholder": "xapp-…"},
        ],
        "step": k("填 token"),
        "pair": k("在 Slack 里打开 {bot} 的 Messages 标签，把这 6 位数字发过去。"),
    },
    {
        "id": "feishu",
        "label": k("飞书"),
        "stage": "ready",
        "needs": k("飞书开放平台的自建应用（App ID 和 App Secret），用长连接收消息，不需要公网地址。"),
        "fields": [
            {"id": "app_id", "label": "App ID", "secret": False, "env": "FEISHU_APP_ID", "placeholder": "cli_…"},
            {"id": "app_secret", "label": "App Secret", "secret": True, "env": "FEISHU_APP_SECRET", "placeholder": ""},
        ],
        "step": k("填 ID 和密钥"),
        "pair": k("在飞书里搜索 {bot}，打开和它的单聊，把这 6 位数字发过去。没有回音的话，看看开放平台里事件是不是选了长连接、版本是不是发布了。"),
        "sdk": {"module": "lark_oapi", "package": "lark-oapi", "extra": "feishu"},
    },
    {
        "id": "dingtalk",
        "label": k("钉钉"),
        "stage": "ready",
        "needs": k("钉钉开放平台的应用机器人（Client ID 和 Client Secret），用 Stream 模式收消息，不需要公网地址。"),
        "fields": [
            {"id": "client_id", "label": "Client ID", "secret": False, "env": "DINGTALK_CLIENT_ID", "placeholder": "ding…"},
            {"id": "client_secret", "label": "Client Secret", "secret": True, "env": "DINGTALK_CLIENT_SECRET", "placeholder": ""},
        ],
        "step": k("填 ID 和密钥"),
        "pair": k("在钉钉里搜索 {bot}（或者在工作台里打开这个应用），和它单聊，把这 6 位数字发过去。"),
        "sdk": {"module": "dingtalk_stream", "package": "dingtalk-stream", "extra": "dingtalk"},
    },
    {
        "id": "qq",
        "label": "QQ",
        "stage": "ready",
        "needs": k("QQ 开放平台的机器人（AppID 和 AppSecret），用 WebSocket 收消息，不需要公网地址。"),
        "fields": [
            {"id": "app_id", "label": "AppID", "secret": False, "env": "QQ_APP_ID", "placeholder": "102…"},
            {"id": "app_secret", "label": "AppSecret", "secret": True, "env": "QQ_APP_SECRET", "placeholder": ""},
        ],
        "step": k("填 ID 和密钥"),
        "pair": k("在 QQ 里打开和 {bot} 的私聊（建好机器人时它给你发过一条消息），把这 6 位数字发过去。"),
    },
    {"id": "whatsapp", "label": "WhatsApp", "stage": "public_url", "needs": k("WhatsApp Cloud API 只会把消息推到一个公网能访问的网址，要先有服务器或者隧道。")},
    {"id": "wecom", "label": k("企业微信"), "stage": "public_url", "needs": k("企业微信的应用只会把消息推到一个公网能访问的网址，要先有服务器或者隧道。")},
    {"id": "wechat", "label": k("个人微信"), "stage": "unsupported", "needs": k("微信没有给个人号的机器人接口。别的办法都是模拟登录，违反使用条款，账号可能被封。")},
]

READY = {item["id"] for item in PLATFORMS if item["stage"] == "ready"}
ORDER = [item["id"] for item in PLATFORMS if item["stage"] == "ready"]  # 现在能用的，按上面的顺序（命令行里一个个列出来时用）
LABELS = {item["id"]: item["label"] for item in PLATFORMS}
_BY_ID = {item["id"]: item for item in PLATFORMS}


def label(platform: str) -> str:
    """给人看的平台名字（"Telegram"、"飞书"……）。"""
    return t(LABELS.get(platform, platform))


def fields(platform: str) -> list[dict]:
    """连接这个平台要填的东西（没有的平台是空列表）。"""
    return list(_BY_ID.get(platform, {}).get("fields", []))


def secret_fields(platform: str) -> list[str]:
    return [field["id"] for field in fields(platform) if field["secret"]]


def secret_env_vars() -> set[str]:
    """所有平台的密钥可以放在哪些环境变量里（tools/shell.py 不让助手运行的命令拿到它们，测试会对一遍）。"""
    return {field["env"] for item in PLATFORMS for field in item.get("fields", []) if field["secret"]}


def sdk(platform: str) -> dict | None:
    return _BY_ID.get(platform, {}).get("sdk")


def install_command(platform: str) -> str:
    spec = sdk(platform)
    return f"uv sync --extra {spec['extra']}" if spec else ""


def sdk_ready(platform: str) -> bool:
    """这个平台要的官方 SDK 装了没有（没有要求的平台永远是 True）。只是找一找，不真的导入：飞书的 SDK 导入一次要好几秒。"""
    spec = sdk(platform)
    if spec is None:
        return True
    try:
        return importlib.util.find_spec(spec["module"]) is not None
    except (ImportError, ValueError):
        return False


def missing_sdk(platform: str) -> str:
    """SDK 没装时给人看的话（装了的话是空字符串）。网页、命令行、通道自己的状态都用这一句。"""
    if sdk_ready(platform):
        return ""
    return t(
        "{platform} 要用官方的 SDK（{package}），还没有装。在智狐的文件夹里运行 {command}，再重新启动智狐。以后运行 uv sync 时也带上 --extra {extra}，不然它会被卸掉。",
        platform=label(platform),
        package=sdk(platform)["package"],
        command=install_command(platform),
        extra=sdk(platform)["extra"],
    )


def catalog() -> list[dict]:
    """发给页面的那一份：按现在的界面语言译好。向导第一步的说明（guide）也在里面，{code} 留着给页面换成代码的样子。"""
    out = []
    for item in PLATFORMS:
        entry = {**item, "label": t(item["label"]), "needs": t(item["needs"])}
        if "fields" in item:
            entry["fields"] = [{**field, "label": t(field["label"])} for field in item["fields"]]
            entry["step"] = t(item["step"])
            entry["pair"] = t(item["pair"])
            entry["guide"] = [_step(step) for step in GUIDES.get(item["id"], [])]
        spec = item.get("sdk")
        if spec:
            entry["sdk"] = {**spec, "ready": sdk_ready(item["id"]), "install": install_command(item["id"]), "message": missing_sdk(item["id"])}
        out.append(entry)
    return out


def _step(step: dict) -> dict:
    return {key: t(value) if key in ("text", "link_label", "copy_label") else value for key, value in step.items()}
