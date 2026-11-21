"""有哪些聊天平台，各自到了哪一步。网页上"通道"那一页的卡片照着这张表画（页面不自己再抄一份）。

stage 的意思：
  ready        现在就能用
  soon         第二阶段做：官方的接口不需要公网地址（机器人主动连平台，和 Telegram 一样），只是还没写
  public_url   第二阶段做：平台只会把消息推到一个公网能访问的网址（webhook），要先有隧道或者服务器
  unsupported  不做：没有官方接口

为什么个人微信不做：微信没有给个人号的机器人接口。网上能找到的办法都是模拟登录网页版或者电脑版微信，
违反它的使用条款，账号随时可能被限制登录甚至封掉。智狐不拿你的微信号冒这个险；想在微信里用，
可以等第二阶段的企业微信（官方的接口，在微信里也能收到消息）。
"""

from __future__ import annotations

from ..i18n import k, t

PLATFORMS = [
    {"id": "telegram", "label": "Telegram", "stage": "ready", "needs": k("在 @BotFather 那里建一个机器人，拿到它的 token。不需要公网地址。")},
    {"id": "feishu", "label": k("飞书"), "stage": "soon", "needs": k("飞书开放平台的自建应用（App ID 和 App Secret），用长连接收消息，不需要公网地址。")},
    {"id": "qq", "label": "QQ", "stage": "soon", "needs": k("QQ 开放平台的机器人（AppID 和 AppSecret），用 WebSocket 收消息，不需要公网地址。")},
    {"id": "dingtalk", "label": k("钉钉"), "stage": "soon", "needs": k("钉钉开放平台的应用机器人（Client ID 和 Client Secret），用 Stream 模式收消息，不需要公网地址。")},
    {"id": "discord", "label": "Discord", "stage": "soon", "needs": k("Discord 开发者后台的机器人 token，用 Gateway 收消息，不需要公网地址。")},
    {"id": "slack", "label": "Slack", "stage": "soon", "needs": k("Slack 应用的 Bot token 和 App token，用 Socket Mode 收消息，不需要公网地址。")},
    {"id": "whatsapp", "label": "WhatsApp", "stage": "public_url", "needs": k("WhatsApp Cloud API 只会把消息推到一个公网能访问的网址，要先有服务器或者隧道。")},
    {"id": "wecom", "label": k("企业微信"), "stage": "public_url", "needs": k("企业微信的应用只会把消息推到一个公网能访问的网址，要先有服务器或者隧道。")},
    {"id": "wechat", "label": k("个人微信"), "stage": "unsupported", "needs": k("微信没有给个人号的机器人接口。别的办法都是模拟登录，违反使用条款，账号可能被封。")},
]

READY = {item["id"] for item in PLATFORMS if item["stage"] == "ready"}
LABELS = {item["id"]: item["label"] for item in PLATFORMS}


def label(platform: str) -> str:
    """给人看的平台名字（"Telegram"、"飞书"……）。"""
    return t(LABELS.get(platform, platform))


def catalog() -> list[dict]:
    """发给页面的那一份：按现在的界面语言译好。"""
    return [{**item, "label": t(item["label"]), "needs": t(item["needs"])} for item in PLATFORMS]
