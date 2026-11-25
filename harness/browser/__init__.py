"""浏览器：让助手操作一个智狐自己启动的 Chrome（"browser use"）。

为什么是"一个单独的浏览器"：它有自己的配置文件夹（data/browser/profile），和你平时用的浏览器完全分开，
看不到你的登录、书签和密码；它是智狐自己开的窗口，不需要系统的"辅助功能 / 屏幕录制"权限；每一步都在智狐手里，
看得见（对话里每一步一张截图）、停得下（"停止"）、记得住（会话里留着全部过程）。

    模型说 navigate / read_page / left_click ……（工具，见 tools/browser.py）
      └─► permissions.py 先看一眼要碰的是什么（BrowserSession.facts），决定问不问你
            └─► session.py 在这次运行自己的标签页上做这个动作
                  └─► cdp.py 把它变成 Chrome DevTools Protocol 的命令，经过管道发给 Chrome（chrome.py 启动的那个）
                        └─► 结果（文字、截图、标签页的情况）交回给模型；截图的小图给你看

这个文件夹里每个文件只讲一件事：

  cdp.py       CDP 客户端：一来一回的 JSON、事件、标签页的会话、超时、按"主人"取消
  chrome.py    找到 Chrome、用智狐的配置文件夹启动它（管道，不开端口）、连同子进程一起关掉
  manager.py   什么时候开、什么时候关（用到才开、闲置就关、退出必关），几次运行怎么共用一个 Chrome，下载一律拒绝
  session.py   一次运行（一段对话、一次定时任务……）自己的标签页，以及在上面做的每一个动作（BrowserSession）。
               动作按小节分在同一前缀的几个文件里，混进 BrowserSession（见 session.py 开头）：
               session_tabs.py 标签页、session_page.py 调用 page.js / 等待 / 截图、session_look.py 打开和读页面、
               session_input.py 鼠标和键盘、session_facts.py 动作之前先看一眼、session_types.py 写给 mypy 看的类型
  page.js      放进网页"隔离世界"里的脚本：把页面整理成一行一行、给能操作的元素编号（ref_12）
  keys.py      按键的名字 → 键盘事件
  urls.py      网址怎么理解、哪些不打开、两个地址算不算同一个网站
  redirects.py navigate 时网站要跳到哪儿、跟不跟：只跟到要去的、允许过的网站（CDP 的 Fetch 域），别的拦下来告诉模型
  shots.py     截图的大图存在哪儿、留几天

能调的数（页面大小、截图质量、闲置多久关掉……）都在 config.py 的 BROWSER_* 里。

## 整个桌面（"computer use"）在 desktop/ 里

操作整台电脑（任何程序，不只是网页）是按这里的分层另加的一个后端，循环和权限的框架没有动（详细的说明见 desktop/__init__.py）：

  - 动作的执行：DesktopSession（desktop/session.py）提供和 BrowserSession 同样的接口（run(action, **args) → Outcome、
    facts()、stop()、close()），动作只有截图、鼠标、键盘这些（没有 navigate、read_page、ref）。
    macOS 上用 CoreGraphics 的 CGEvent 发鼠标键盘事件、screencapture 截图——要你在系统设置里给"辅助功能"和"屏幕录制"的权限，
    这也是先做浏览器的原因：浏览器是智狐自己的窗口，什么权限都不用。
  - 给模型的工具：Claude 用 Anthropic 的 computer_toolset_20260801（和浏览器工具包是同一套"工具包"机制：
    Tool.toolset="computer"，结果上写回 toolset_name，见 providers/claude.py），别家用我们自己写的函数工具当替身（tools/computer.py）。
  - 问不问你：permissions.py 里另有一节。整个桌面比一个浏览器危险得多，每一步都问，"全部自动允许"也不放开它。
  - 截图（shots.py 的 ShotStore，另一个文件夹）、停止、每次运行各管各的，照搬了这里的做法。和浏览器不一样的是：
    屏幕只有一个，同一时间只让一个运行操作（desktop/manager.py 的 claim）。
"""
