"""网页界面：在浏览器里使用助手。

  ui.py       agent 这一侧：把循环里发生的事变成"事件"
  server.py   本地 HTTP 服务器：把事件推送给浏览器，接收浏览器发来的消息
  page/       浏览器里的页面：收到事件后画出界面。骨架 index.html、样式 styles/、脚本 scripts/，
              由 page/__init__.py 按清单拼成一个自给自足的 HTML
  markdown.js 回复里的 Markdown 怎么画（发页面时填进页面里）
"""
