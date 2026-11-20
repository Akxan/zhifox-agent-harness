"""MCP（Model Context Protocol）：让助手用上别人写好的工具。

我们自己的工具（tools/ 目录）是写在这个程序里的。MCP 是一个公开的协议：任何人都可以写一个"MCP 服务器"，
把一组工具（读 GitHub、查数据库、操作浏览器……）按同一种格式提供出来；任何支持 MCP 的助手（"客户端"）
都能连上它、把这些工具交给模型用。智狐是一个客户端，这个目录是手写的客户端实现，没有用官方的 SDK。

一次工具调用走的路：

    模型："我要调用 mcp__github__search_issues，参数是 {...}"
      └─► agent.py      和别的工具一样：先过权限这一关（默认每次都问你），再执行
            └─► manager.py   找到 github 这个服务器的连接
                  └─► client.py    包成一个 tools/call 请求（JSON-RPC，见 jsonrpc.py）
                        └─► stdio.py / http.py   写进服务器进程的 stdin，或者 POST 到它的网址
                        ◄── 服务器的回答
            ◄── bridge.py    把回答里的文字、图片整理成交给模型的结果

几个文件各管一件事：

  jsonrpc.py    消息的信封：请求、回答、通知，怎么等一个回答
  stdio.py      本地服务器：启动子进程，消息走它的标准输入输出
  http.py       远程服务器：每条消息一次 HTTP POST，回答可能是一串服务器推送事件
  client.py     协议本身：握手（两代协议怎么分辨）、列出工具、调用工具
  bridge.py     MCP 的工具 ↔ 智狐的工具：名字、参数格式、结果
  manager.py    所有服务器的启动、停止、状态，以及"一段对话里工具不变"的快照
  config.py     配置文件 data/mcp.json，以及给页面看时怎么遮住密钥
  importers.py  从 Claude Desktop / Claude Code 导入，或者粘贴一段 JSON
  log.py        每个服务器最近的输出，出问题时看
"""

from .config import ConfigError, McpConfig
from .manager import McpManager, Snapshot

__all__ = ["ConfigError", "McpConfig", "McpManager", "Snapshot"]
