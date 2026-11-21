"""网页界面的页面：把这个文件夹里的几块拼成浏览器拿到的那一页。

为什么拆开：页面原来是一个一万三千行的 index.html，样式、标记、脚本全挤在一起，改一处要在几十万字里找。
现在一个文件只讲一件事：styles/ 里一种样子一个文件，scripts/ 里页面的一节一个文件。想改"记忆"，
就打开 scripts/memory.js 和 styles/memory.css。

为什么又拼回一页：页面的安全策略只认带着一次性 nonce 的内联 <script> 和 <style>（见 server.py 的 _send_page），
不加载任何外部文件，离线也要能用。所以拆开只是为了好改：发给浏览器的仍然是同一个自给自足的页面，
不多开一个地址，也不放宽安全策略。

怎么拼：index.html 是骨架（标记，外加几行单独占一行的占位符，就是下面 SLOTS 的键）。每个占位符换成
SLOTS 里列出的那几块，按列出的顺序一块接一块，中间空一行。顺序很要紧：
  - 样式：两条规则一样具体时，后写的盖过先写的。STYLES 的顺序就是它们原来在页面里的顺序，
    "减少动态效果"那一块必须排在最后。
  - 脚本：SCRIPTS 里的各块接在一起，放进骨架里同一个 (() => { 'use strict'; … })() 里，所以它们共用
    一个作用域：前面一块定义的 t()、el()、play()、calm()……后面每一块都能直接用。用 const、let 定义的名字
    要等定义它的那一行跑过以后才能用，所以定义它们的那一块要排在用它们的前面。
新加一块：把文件放进 styles/ 或 scripts/，再把名字加进下面的列表。漏了、写了两次，tests/test_page.py 都会失败。
一节太长（几百行以上）时，按它自己的小节拆成几个同一前缀的文件，在清单里挨着排：比如 skills.js 是 Skill 一页的
总说明、共用的状态和小工具，后面接着 skills-list.js、skills-detail.js……，最后一个文件的末尾把这一页挂到扩展对话框上。
用前缀而不用子文件夹，是为了和 styles/ 一样只有一层：文件夹里按名字一排，同一节的几个文件自然挨在一起。

拼好的页面里还留着 __NONCE__、__TOKEN__（头上清单和图标的地址里的口令）、__I18N__、__MARKDOWN__ 四处，
每次发页面时由 server.py 填上。
所以每一块里都不能出现这样的占位符，也不能出现 </script、</style、<!--（浏览器会以为那一段提前结束了），
测试会查。

助手程序启动时拼一次（server.PAGE）；演示（tests/demo_web.py）每次刷新都重新拼，改完刷新就能看到。
"""

from __future__ import annotations

import re
from pathlib import Path

FOLDER = Path(__file__).parent

# <head> 里那一小段脚本：页面画出来之前就定好深浅色和动效。它是单独的一段 <script>，不和主脚本共用作用域
FIRST_PAINT = ("first-paint.js",)

# 样式，按层叠的先后排：后面的同样具体的规则盖过前面的
STYLES = (
    "styles/tokens.css",  # 颜色和字体：两套外观的变量
    "styles/base.css",  # 基础：按钮、输入框、下拉框、图标
    "styles/layout.css",  # 三栏的排法，左边的会话栏
    "styles/sheet.css",  # 中间那张"纸"：极光、顶栏、对话区
    "styles/chat.css",  # 消息、编辑和重新生成、思考、提示条
    "styles/tool-cards.css",  # 工具调用卡片、确认卡片、"正在工作"
    "styles/markdown.css",  # 回复里的 Markdown
    "styles/citations.css",  # 回答引用的网页：记号、浮出的卡片、来源列表
    "styles/welcome.css",  # 空白页：问候语和建议
    "styles/composer.css",  # 输入框和附件
    "styles/viewer.css",  # 查看器
    "styles/composer-controls.css",  # 发送键、输入框下面那一行、"要不要先问你"的开关、回到底部
    "styles/info.css",  # 右边的信息栏
    "styles/capabilities.css",  # 信息栏 · 智狐能做的事
    "styles/memory.css",  # 记忆的入口和窗口
    "styles/nav-tabs.css",  # 左边栏的"会话 / 任务 / 多媒体"
    "styles/media.css",  # 多媒体
    "styles/tasks.css",  # 定时任务
    "styles/prompt.css",  # 提示词
    "styles/extensions.css",  # 扩展对话框的壳
    "styles/skills.css",  # 扩展 · Skill
    "styles/mcp.css",  # 扩展 · MCP
    "styles/channels.css",  # 扩展 · 通道
    "styles/memory-editor.css",  # 记忆窗口里的编辑
    "styles/agents.css",  # 自定义 Agent
    "styles/imports.css",  # 扩展 · 从其他工具导入（三页共用的那几块）
    "styles/settings.css",  # 设置面板
    "styles/commands.css",  # 不用问的命令
    "styles/spending.css",  # 花费
    "styles/flyouts.css",  # 顶栏上的按钮弹出来的小面板：外观、换模型、语言共用的样子（窄屏上从底下升起来）
    "styles/appearance.css",  # 外观面板
    "styles/models.css",  # 换模型的面板
    "styles/language.css",  # 选语言的面板
    "styles/notices.css",  # 浮在最上面的提示
    "styles/narrow.css",  # 窄屏
    "styles/animations.css",  # 入场动画
    "styles/browser.css",  # 浏览器
    "styles/computer.css",  # 操作整台电脑
    "styles/trace.css",  # 运行记录：每一轮上的入口、滑出来的窗口（合计、瀑布图、逐段的详情）
    "styles/service.css",  # 设置里的"后台运行"
    "styles/reduce-motion.css",  # 换外观的交叉淡化、减少动态效果（必须在最后）
)

# 主脚本的各节，按运行的先后排：前面的定义后面的要用的名字
SCRIPTS = (
    "scripts/basics.js",  # 口令、$、<html> 和 <body>
    "scripts/i18n.js",  # 多语言：t()、tel()、retext()、tattr()
    "scripts/state.js",  # 常用的几个元素、整个页面共用的状态
    "scripts/helpers.js",  # 小工具：el()、icon()……
    "scripts/motion.js",  # 动效：calm()、play()、relayout()、crossfade()
    "scripts/flyouts.js",  # 顶栏上的按钮弹出来的小面板：开、关、位置、点外面收起、Esc 和 Tab（外观、换模型、语言共用）
    "scripts/follow.js",  # 跟着最新内容滚动
    "scripts/requests.js",  # 提示和请求：toast()、call()、post()、announce()
    "scripts/approval-modes.js",  # 要不要先问你：几档的名字和说明（对话、任务、通道三处共用）
    "scripts/settle.js",  # 空白页和收尾，定位到搜索命中的地方
    "scripts/markdown-view.js",  # 回复里的 Markdown（接上 markdown.js）
    "scripts/citations.js",  # 回答引用的网页：记号、浮出的卡片、来源列表、复制时带上来源
    "scripts/streaming.js",  # 逐字输出的文字
    "scripts/subagent-cards.js",  # 交给子 Agent 的卡片
    "scripts/trace.js",  # 运行记录：每一轮上的入口、记录的检查、窗口的开和关
    "scripts/trace-chart.js",  # 运行记录 · 窗口里画的东西：合计、瀑布图、逐段的详情
    "scripts/events.js",  # 每种事件怎么画
    "scripts/attachments.js",  # 附件
    "scripts/viewer.js",  # 查看器
    "scripts/send.js",  # 发送消息
    "scripts/rewind.js",  # 编辑最后一条消息、重新生成
    "scripts/sidebars.js",  # 两边的栏：展开和收起
    "scripts/appearance.js",  # 外观：颜色和动效
    "scripts/conversations.js",  # 会话列表
    "scripts/info.js",  # 信息栏
    "scripts/capabilities.js",  # 信息栏 · 智狐能做的事（清单由 harness/capabilities.py 算）
    "scripts/spending.js",  # 花费
    "scripts/memory.js",  # 记忆
    "scripts/tasks.js",  # 定时任务：共用的状态、时间的写法、左边栏的标签
    "scripts/tasks-list.js",  # 定时任务 · 列表
    "scripts/tasks-alerts.js",  # 定时任务 · 提醒、通知、顶上的提示
    "scripts/tasks-detail.js",  # 定时任务 · 详情
    "scripts/tasks-editor.js",  # 定时任务 · 新建和编辑
    "scripts/media.js",  # 多媒体
    "scripts/prompt.js",  # 提示词
    "scripts/extensions.js",  # 扩展对话框的壳
    "scripts/imports.js",  # 扩展 · 从其他工具导入：Skill、MCP、Agent 三页共用的总览、分组的标题、"没找到的"
    "scripts/skills.js",  # 扩展 · Skill：共用的状态和小工具
    "scripts/skills-list.js",  # 扩展 · Skill · 列表
    "scripts/skills-detail.js",  # 扩展 · Skill · 打开一个、编辑 SKILL.md
    "scripts/skills-add.js",  # 扩展 · Skill · 新建、从网址添加、上传
    "scripts/skills-review.js",  # 扩展 · Skill · 从 Claude Code 导入、装之前的确认
    "scripts/mcp.js",  # 扩展 · MCP：共用的状态和小工具
    "scripts/mcp-list.js",  # 扩展 · MCP · 列表
    "scripts/mcp-detail.js",  # 扩展 · MCP · 详情
    "scripts/mcp-form.js",  # 扩展 · MCP · 添加和编辑
    "scripts/mcp-import.js",  # 扩展 · MCP · 粘贴 JSON、从 Claude 导入
    "scripts/agents.js",  # 扩展 · Agent：共用的状态和小工具
    "scripts/agents-list.js",  # 扩展 · Agent · 列表
    "scripts/agents-detail.js",  # 扩展 · Agent · 打开一个
    "scripts/agents-editor.js",  # 扩展 · Agent · 编辑和新建
    "scripts/agents-import.js",  # 扩展 · Agent · 从 Claude Code 导入
    "scripts/agent-picker.js",  # 新对话旁边的 Agent 选择
    "scripts/channels.js",  # 扩展 · 通道：共用的状态和小工具
    "scripts/channels-list.js",  # 扩展 · 通道 · 列表、顶上等你批准的提示
    "scripts/channels-wizard.js",  # 扩展 · 通道 · 设置向导的框架
    "scripts/channels-steps.js",  # 扩展 · 通道 · 设置向导的四步
    "scripts/language.js",  # 语言
    "scripts/approval.js",  # 要不要先问你
    "scripts/commands.js",  # 不用问的命令（你自己加的）
    "scripts/settings.js",  # 设置面板
    "scripts/models.js",  # 换模型：顶栏模型按钮弹出来的面板（目录由 harness/model_catalog.py 算）
    "scripts/browser.js",  # 浏览器
    "scripts/computer.js",  # 操作整台电脑
    "scripts/service.js",  # 设置里的"后台运行"：作为后台服务开着，还是跟着终端
    "scripts/keyboard.js",  # 键盘
    "scripts/start.js",  # 开始：连上事件流
)

# 骨架里的占位符 → 换成哪几块
SLOTS = {"__FIRST_PAINT__": FIRST_PAINT, "__STYLES__": STYLES, "__SCRIPTS__": SCRIPTS}
_SLOT = re.compile("^(" + "|".join(SLOTS) + ")\n", flags=re.MULTILINE)


def parts() -> tuple[str, ...]:
    """所有的块，按拼进页面的顺序。"""
    return tuple(name for names in SLOTS.values() for name in names)


def assemble() -> str:
    """读骨架和各块，拼成一页（还留着 __NONCE__ 这几处，等 server.py 发页面时填）。"""
    skeleton = (FOLDER / "index.html").read_text(encoding="utf-8")
    found = _SLOT.findall(skeleton)
    if sorted(found) != sorted(SLOTS):
        raise ValueError(f"page/index.html 里的占位符应该是 {sorted(SLOTS)} 各一行，实际是 {found}")
    # 一次换完（re.sub 不会回头再扫一遍换进去的内容），和 server.py 填 nonce 的做法一样
    return _SLOT.sub(lambda match: "\n".join((FOLDER / name).read_text(encoding="utf-8") for name in SLOTS[match.group(1)]), skeleton)
