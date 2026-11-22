"""运行记录：一轮对话里，agent 循环到底做了什么、每一步花了多久、花了多少钱。

你在页面上看到的是一问一答，但在它背后，循环（agent.py）可能已经来回跑了好几圈：

    你说了一句话
      ├─ 请求 1：把整段对话发给模型 ──── 模型想了想，说"我要调用 todo_list"
      ├─ 工具：todo_list ──────────────── 在这台电脑上执行，结果追加进对话
      ├─ 请求 2：再把整段对话发过去 ──── 模型说"我要运行 ls"
      ├─ 工具：run_command ─────────────── 要先问你：等你点"允许"的这段时间也算在这一轮里
      └─ 请求 3：再发一次 ──────────────── 模型不再要工具，写出回答：这一轮结束

运行记录把这条时间线画出来（网页上是一张"瀑布图"，终端里是 /trace），每一段写明：

  模型请求  哪一家的哪个模型、花了多久、多久才吐出第一个字（首字时间）、输入和输出各多少 token、
            多少命中了缓存、花了多少钱（就是花费账本记的那一笔，见 ledger.py，不另算一遍）、为什么停下
            （stop_reason）、是不是一次压缩历史的请求、中途重试过没有、服务器上用了几次联网搜索
  工具调用  给人看的名字（和信息栏"智狐能做的事"同一张表，见 capabilities.label_of）和真名、花了多久、
            结果有多大、出没出错、问没问你（你允许的、你拒绝的、没问就放行的和为什么）、等你答复等了多久；
            交给子 Agent 的那一次，子 Agent 自己的请求和工具调用挂在它下面
  合计      这一轮一共多久，其中模型、工具、等你各占多少，一共多少钱，缓存命中了多少

看它能学到的几件事：一轮的时间大多花在模型上还是等你；每多一圈，整段对话就要重发一次（输入的 token 一圈比一圈多，
但大部分命中缓存，所以便宜）；子 Agent 的那几圈不进主助手的历史，只有它交回的结论进去。

记录在哪儿：每一轮结束时，循环把它交给界面（Agent.on_trace）。网页界面存成会话 events 里的一个 trace 事件
（定时任务、聊天通道的每一轮也一样，见 webui/ui.py 的 trace），刷新页面、重新打开会话都还在；
倒回最后一轮（编辑、重新生成）时它跟着那一轮的事件一起去掉。它从不进 messages：发给模型的内容一个字都不变。
终端里只留在内存里，/trace 看最后一轮。

格式（全部是普通的数、字符串、列表和字典，可以直接写成 JSON）：

  {"version": 1, "at": 1763622180.123, "wall": 12400, "ended": "done", "again": true,
   "spans": [段, …], "totals": 合计, "omitted": 0}

  at       这一轮开始的时刻（1970 年起的秒数），页面换算成本地时间
  wall     这一轮从开始到结束一共多少毫秒。下面所有的时刻都是"从这一轮开始过了多少毫秒"
  ended    怎么结束的：done 答完了 / truncated 回复被截断 / refused 被拒绝 / steps 用完了步数 /
           stopped 你点了停止 / error 出错了
  again    这一轮是"重新生成"出来的（没有就是 false，不写）
  omitted  段太多（超过 config.TRACE_MAX_SPANS）没有一段段存下来的有几段；合计里照样算着它们

  一段模型请求：
    {"kind": "model", "start": 0, "end": 1834, "provider": "anthropic", "model": "claude-opus-5-5",
     "status": "tools", "stop": "tool_use", "first": 612, "input": 100, "cache_read": 40, "cache_write": 10,
     "output": 20, "cost": 0.00123, "calls": 1, "compaction": true, "retries": 1, "truncated": true,
     "server_tools": {"web_search": 1}}
    status   done / tools（要调用工具）/ paused（服务器上的搜索还没做完，接着请求）/ refused /
             compacted（这次是写摘要、压缩历史，见 providers/compaction.py）/ stopped / error（没有答完；
             模型暂时用不了、改用了备用模型的那一次也是 error，下一段就是用备用模型重发的同一个请求）
    first    从发出请求到第一段输出（思考或文字）过了多少毫秒；一个字都没写就调用工具的没有这一项
    cost     这次请求的钱（美元），就是账本里的那一笔；没有价格的模型是 null；没答完的没有这一项
    后面几项只在有的时候才写：calls 要调用几个工具、compaction、retries、truncated、server_tools（服务器端的工具各用了几次）、
             fallback（这次用的是备用模型：写的是它替的那个模型，就是你选的那个，见 fallback.py）、
             note（status 是 error、而且循环接着改用了备用模型时：出错的原因，第一行、截短）

  一段工具调用：
    {"kind": "tool", "start": 1834, "end": 4750, "name": "run_command", "group": "shell", "icon": "terminal",
     "label": "执行命令", "detail": "$ ls -la", "result": "ok", "size": 120, "images": 1, "redacted": 2,
     "approval": "allowed", "why": "readonly", "asked": 1840, "waited": 2900, "note": "…",
     "server": "files", "agent": "资料研究员", "sandbox": "workspace", "children": [段, …]}
    name     真名（工具包的成员写成 browser.left_click）；label 是给人看的名字（原文，页面和终端各自翻译），
             认不出来的工具是空的；group、icon 和信息栏里的一样
    detail   参数的一行说明（不含工具名），最多 TRACE_SUMMARY_CHARS 个字
    result   ok / error / skipped（没有执行：前面一步失败了，或者参数被截断）/ stopped（被打断，没有结果）
    size     结果有多少个字（只记大小，不存内容）；images 结果里有几张图
    redacted 结果里遮住了几处像密钥的东西（见 redact.py；size 是遮好以后的大小）；一处都没遮的没有这一项
    approval 问没问你：allowed 你允许了 / refused 你拒绝了 / auto 没问就放行（why 是为什么：readonly、listed、
             trusted、mode，见 permissions.Permissions.allowance）/ stopped 还在等你答复就停了；不用问的没有这一项
    asked、waited  什么时候开始等你答复、等了多少毫秒（只有问了你的才有）
    note     出错时结果的第一行（截短）
    server   MCP 服务器的名字；agent：交给的是哪个子 Agent，children 是它的请求和工具调用
    sandbox  执行命令时沙箱是哪一档（见 sandbox/）：workspace / network；off 是这次运行没用沙箱（设置里开着沙箱时才写）；
             outside 是请你放行、在沙箱之外运行的那一条。从来没打开过沙箱的人，记录里没有这一项

  合计：
    {"model": 6200, "tools": 3100, "waiting": 2900, "other": 200,
     "requests": 4, "tool_calls": 5, "errors": 0, "cost": 0.0312, "unpriced": 0,
     "input": 12000, "cache_read": 9000, "cache_write": 500, "output": 1200, "cache_hit": 0.7347,
     "server_tools": {"web_search": 1}, "asked": 1, "allowed": 1, "refused": 0, "auto": 2}
    model、tools、waiting、other  这一轮的时间怎么分的（毫秒，加起来是 wall）：模型在答、工具在执行、等你确认、
             别的（存盘、组装请求这些零碎）。子 Agent 那一次调用的时间，按它里面的模型、工具、等你分别算进去
    cost     一共多少钱（只算有价格的）；unpriced 有几次请求没有价格；全都没有价格时 cost 是 null
    cache_hit  输入里命中缓存的比例（0 到 1）；没有输入时是 null
    requests、tool_calls、errors、asked、allowed、refused、auto  各有几次（子 Agent 里的也算）

记录器本身在 recorder.py（agent.py 在几个"记录点"调用它），终端里 /trace 的样子在 text.py。
"""

from .recorder import VERSION, Tracer

__all__ = ["VERSION", "Tracer"]
