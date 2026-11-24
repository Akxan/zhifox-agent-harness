"""网络的两样基本功：一个手写的 WebSocket 客户端（websocket.py，按小节分在旁边的 websocket_*.py），一个"发 JSON、收 JSON"的 HTTPS 帮手（http.py）。

聊天通道（../channels/）里的 Discord、Slack、QQ、飞书、钉钉都靠这两样和平台说话。它们只用标准库，
不知道背后是哪一家，所以单独放在这里，以后别的功能要用也能拿去用。
"""
