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
  session.py   一次运行（一段对话、一次定时任务……）自己的标签页，以及在上面做的每一个动作
  page.js      放进网页"隔离世界"里的脚本：把页面整理成一行一行、给能操作的元素编号（ref_12）
  keys.py      按键的名字 → 键盘事件
  urls.py      网址怎么理解、哪些不打开、两个地址算不算同一个网站
  shots.py     截图的大图存在哪儿、留几天

能调的数（页面大小、截图质量、闲置多久关掉……）都在 config.py 的 BROWSER_* 里。

## 以后：整个桌面（"computer use"）

现在只做了浏览器。以后要让助手操作整个桌面（任何程序，不只是网页），按同样的分层加一个"桌面"的后端就行，
不用动循环和权限的框架：

  - 动作的执行：换成一个 DesktopSession，提供和 BrowserSession 同样的接口（run(action, **args) → Outcome、
    facts()、stop()、close()），动作只有截图、鼠标、键盘这些（没有 navigate、read_page、ref）。
    macOS 上用系统的辅助功能接口发鼠标键盘事件、用屏幕录制接口截图——这两样都要你在系统设置里授权，
    这也是先做浏览器的原因：浏览器是智狐自己的窗口，什么权限都不用。
  - 给模型的工具：Claude 用 Anthropic 的 computer_toolset_20260801（和浏览器工具包是同一套"工具包"机制：
    Tool.toolset="computer"，结果上写回 toolset_name，见 providers/claude.py），别家用我们自己写的函数工具当替身。
  - 问不问你：照样在 permissions.py 里加一节。整个桌面比一个浏览器危险得多（能碰到你所有的程序和文件），
    默认应该每一步都问，"全部自动允许"也不该放开它。
  - 截图、停止、每次运行各管各的，都可以照搬这里的做法（shots.py、Connection.cancel 的"主人"、manager 的闲置关闭）。
"""
