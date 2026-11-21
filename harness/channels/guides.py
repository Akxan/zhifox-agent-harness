"""每个平台怎么建一个机器人：网页上设置向导的第一步，和命令行里 `harness channels setup <平台>` 打印的步骤，都是这一份。

一步是一个字典：
  text        这一步要做什么（用 k() 标出来，按界面语言翻译）。里面的 {code} 会换成 code：页面上画成代码的样子，
              命令行里原样印出来。按钮上的字、菜单的名字（New Application、Bot……）是平台界面上的英文，照原样写
  code        要照着输入或者去找的那几个字（命令、菜单名）
  link        要打开的网页（平台的开发者后台），link_label 是按钮上的字
  copy        要复制的一大段（Slack 的应用清单），copy_label 是按钮上的字

步骤是照着各家官方的文档写的（2026 年 11 月看的，网址在各个平台的模块开头）。平台的后台改版了的话，改这里就行。
"""

from __future__ import annotations

import json

from ..config import APP_NAME_LATIN
from ..i18n import k

# Slack 的应用清单：照着它建应用，权限、事件、Socket Mode、命令一次就设好了（见 slack.py 开头的说明）。
# 只登记一个 /zhifox：Slack 会把所有 / 开头的话都当成命令拦下来，/status 这类名字它自己就有。
# files:read 是下载你发来的文件，files:write 是助手往聊天里发文件（send_file，见 slack.py）
SLACK_COMMAND = "/zhifox"
SLACK_MANIFEST = json.dumps(
    {
        "display_information": {"name": APP_NAME_LATIN, "description": "Your own ZhiFox assistant, in Slack."},
        "features": {
            "app_home": {"home_tab_enabled": False, "messages_tab_enabled": True, "messages_tab_read_only_enabled": False},
            "bot_user": {"display_name": APP_NAME_LATIN, "always_online": True},
            "slash_commands": [{"command": SLACK_COMMAND, "description": "new · stop · status · help", "usage_hint": "[new | stop | status | help]", "should_escape": False}],
        },
        "oauth_config": {"scopes": {"bot": ["chat:write", "im:history", "im:write", "files:read", "files:write", "commands", "users:read"]}},
        "settings": {
            "event_subscriptions": {"bot_events": ["message.im"]},
            "interactivity": {"is_enabled": True},
            "socket_mode_enabled": True,
            "org_deploy_enabled": False,
            "token_rotation_enabled": False,
        },
    },
    indent=2,
)

GUIDES: dict[str, list[dict]] = {
    "telegram": [
        {"text": k("打开 @BotFather：Telegram 官方用来建机器人的账号。"), "link": "https://t.me/BotFather", "link_label": k("打开 @BotFather")},
        {"text": k("发送 {code}，按提示先起一个名字（给人看的），再起一个用户名（必须以 bot 结尾，比如 my_zhifox_bot）。"), "code": "/newbot"},
        {"text": k("它会回你一段话，里面有一串 token，长这样：{code}。复制它，下一步要用。"), "code": "123456789:AAH…"},
    ],
    "discord": [
        {"text": k("打开 Discord 的开发者后台，点 {code}，起一个名字（就是机器人的名字）。"), "code": "New Application", "link": "https://discord.com/developers/applications", "link_label": k("打开开发者后台")},
        {"text": k("左边点 {code}，再点 Reset Token，复制出现的那一串：这就是机器人的 token，它只显示这一次。不用打开 Message Content Intent：私聊里的消息不需要它。"), "code": "Bot"},
        {"text": k("左边点 {code}，勾上 User Install。连接以后，下一步会给你一个链接，用它把机器人加到你自己的账号（Add to My Apps），就能和它私聊了。"), "code": "Installation"},
    ],
    "slack": [
        {"text": k("打开 Slack 的应用后台，选 {code}，挑一个工作区，把下面这份清单粘贴进去，然后点 Next、Create。"), "code": "From a manifest", "link": "https://api.slack.com/apps?new_app=1", "link_label": k("打开 Slack 应用后台"), "copy": SLACK_MANIFEST, "copy_label": k("复制清单")},
        {"text": k("左边点 {code}，点 Install to Workspace 并允许，复制 Bot User OAuth Token（xoxb- 开头）。"), "code": "OAuth & Permissions"},
        {"text": k("左边点 {code}，在 App-Level Tokens 里点 Generate Token and Scopes，加上 connections:write，生成以后复制它（xapp- 开头）。"), "code": "Basic Information"},
        {"text": k("Slack 会把 / 开头的话都当成命令，所以在 Slack 里，智狐的命令都写在 {code} 后面：new、stop、status、help。"), "code": "/zhifox"},
    ],
    "feishu": [
        {"text": k("打开飞书开放平台，点 {code}，填好名字和描述。"), "code": "创建企业自建应用", "link": "https://open.feishu.cn/app", "link_label": k("打开飞书开放平台")},
        {"text": k("在 {code} 里复制 App ID 和 App Secret。"), "code": "凭证与基础信息"},
        {"text": k("在 {code} 里加上机器人；在权限管理里开通这三项：读取用户发给机器人的单聊消息、以应用的身份发消息、获取与上传图片或文件资源。"), "code": "添加应用能力"},
        {"text": k("先在下一步把智狐连上（要它开着，下面的设置才保存得了）。然后在 {code} 里，事件和回调都选“使用长连接接收事件”，添加事件 im.message.receive_v1 和回调 card.action.trigger，最后创建一个版本并发布。"), "code": "事件与回调"},
    ],
    "dingtalk": [
        {"text": k("打开钉钉开发者后台，在 {code} 里创建一个应用。没有公司也行：在钉钉里自己建一个团队，你就是管理员。"), "code": "应用开发", "link": "https://open-dev.dingtalk.com", "link_label": k("打开钉钉开发者后台")},
        {"text": k("在 {code} 里复制 Client ID 和 Client Secret。"), "code": "凭证与基础信息"},
        {"text": k("加上机器人这项能力，消息接收模式选 {code}，然后发布机器人和应用的版本。"), "code": "Stream 模式"},
        {"text": k("钉钉免费版的组织，每个月调用接口的次数有上限，聊得很多的话可能会用完。")},
    ],
    "qq": [
        {"text": k("用你的 QQ 登录 QQ 开放平台，点 {code}。个人账号也能建（最多 5 个）。建好以后它会先给你的 QQ 发一条消息。"), "code": "创建机器人", "link": "https://q.qq.com/qqbot/", "link_label": k("打开 QQ 开放平台")},
        {"text": k("在机器人的 {code} 里复制 AppID 和 AppSecret。IP 白名单可以不填（家里的网络地址会变）。"), "code": "开发设置"},
        {"text": k("在 {code} 里选 WebSocket：不需要公网服务器。你自己是管理员，不用上线、不用沙箱就能和它私聊。"), "code": "事件订阅与回调地址"},
    ],
}
