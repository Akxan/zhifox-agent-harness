"""终端里 /trace 的样子：把最后一轮的运行记录（格式见 __init__.py）写成几行字，外加一张用方块字符画的小瀑布图。

    这一轮的运行记录 · 一共 12.4 秒 · $0.0312 · 缓存命中 78% · 答完了
    模型 6.2 秒 · 工具 3.1 秒 · 等你确认 2.9 秒 · 其他 0.2 秒
    4 次请求、3 次工具调用；输入 12,000 token（命中缓存 9,000），输出 1,200 token
       0.0 秒 ███████······················  模型 claude-opus-5-5 · 1.8 秒 · 首字 0.6 秒 · tool_use · $0.0040
       1.8 秒 ·······▓······················  待办 · todo_list · 0.01 秒
       1.9 秒 ·······░░░░░░░▓▓▓·············  执行命令 · run_command $ ls · 3.1 秒 · 你允许了（等了 2.9 秒）
         └ 2.0 秒 ··········██·················  模型 claude-sonnet-5-5 · …   （交给子 Agent 的，往里缩一格）
         └ 4.9 秒 ·················▒············  钩子 fmt（PostToolUse）· 0.05 秒 · 放行   （工具调用前后跑的钩子，也缩一格）
       5.0 秒 ·················▓············  读写文件 · str_replace_based_edit_tool view a.txt · 0.02 秒 · 和另外 1 步同时执行
       5.0 秒 ·················▓············  读写文件 · str_replace_based_edit_tool view b.txt · 0.03 秒 · 和另外 1 步同时执行
                                                       （一步里的几次读一起跑的，横条叠在一起，见 ../parallel.py）

图里的一格是这一轮总时长的 1/BAR 那么长：█ 模型在答，▓ 工具在执行，░ 等你确认，▒ 你的钩子在跑。数字由记录器算好（合计），这里只照着写。
"""

from __future__ import annotations

from collections import Counter

from ..i18n import k, number, t

BAR = 30
ENDED = {
    "done": k("答完了"),
    "truncated": k("回复被截断了"),
    "refused": k("模型拒绝了"),
    "steps": k("用完了步数"),
    "stopped": k("你停下了它"),
    "error": k("出错了"),
    "blocked": k("被你的钩子拦下了"),
}
APPROVAL = {
    "allowed": k("你允许了"),
    "refused": k("你拒绝了"),
    "stopped": k("等你答复时停下了"),
}
WHY = {"readonly": k("只读的命令"), "listed": k("你加的命令"), "trusted": k("这段对话里放开了"), "mode": k("确认方式")}
RESULT = {"error": k("出错"), "skipped": k("没有执行"), "stopped": k("没有执行完")}
# 一个钩子怎么说的（见 hooks/protocol.py 的 Outcome）
HOOK_RESULT = {
    "ok": k("放行"),
    "blocked": k("拦下"),
    "ask": k("再问你"),
    "error": k("出错"),
    "timeout": k("超时"),
    "stopped": k("停下"),
}
# 执行命令时沙箱是哪一档（见 __init__.py 的 sandbox）。页面上的说法在 page/scripts/trace-chart.js 的 TRACE_SANDBOX，两边一致
SANDBOX = {"workspace": k("在沙箱里：只在工作目录里"), "network": k("在沙箱里：工作目录 + 联网"), "off": k("没有用沙箱"), "outside": k("在沙箱之外运行（你放行的）")}
# 一次请求没有 API 给的停止原因时（没答完、写摘要），写这一句；有的话照原样写 API 的原词（end_turn、tool_use……，那是技术细节）
STATUS = {"stopped": k("被打断"), "error": k("出错了"), "compacted": k("压缩历史"), "paused": k("搜索还没做完"), "refused": k("模型拒绝了")}


def seconds(ms: float) -> str:
    """毫秒 → "1.8 秒"。不到 0.1 秒的写两位小数，免得一律写成 0.0。"""
    value = max(0.0, float(ms)) / 1000
    return t("{n} 秒", n=f"{value:.2f}" if value < 0.1 else f"{value:.1f}")


def money(cost: float | None) -> str:
    return "—" if cost is None else f"${cost:.4f}"


def lines(record: dict | None) -> list[tuple[str, str]]:
    """[(一行字, "plain" 或 "info")]。还没有记录时只有一行说明。"""
    if not record:
        return [(t("这次运行里还没有完成的一轮。说一句话，等它答完，再输入 /trace。"), "info")]
    totals, wall = record.get("totals") or {}, int(record.get("wall") or 0)
    head = [t("这一轮的运行记录"), t("一共 {time}", time=seconds(wall)), money(totals.get("cost"))]
    if totals.get("cache_hit") is not None:
        head.append(t("缓存命中 {percent}%", percent=round(totals["cache_hit"] * 100)))
    head.append(t(ENDED.get(record.get("ended") or "", k("出错了"))))  # 没写的当作空串：查不到，和认不出的一样算"出错了"
    out = [(" · ".join(head), "plain")]
    split = [(k("模型 {time}"), "model"), (k("工具 {time}"), "tools"), (k("钩子 {time}"), "hooks"), (k("等你确认 {time}"), "waiting"), (k("其他 {time}"), "other")]
    # 钩子那一项只在这一轮真的跑过钩子时才写（以前的记录里没有这一项）
    shown = [(text, name) for text, name in split if name != "hooks" or totals.get("hook_runs")]
    out.append((" · ".join(t(text, time=seconds(totals.get(name, 0))) for text, name in shown), "info"))
    counts = t(
        "{requests} 次请求、{calls} 次工具调用；输入 {input} token（命中缓存 {cached}），输出 {output} token",
        requests=totals.get("requests", 0), calls=totals.get("tool_calls", 0),
        input=number(sum(int(totals.get(name, 0)) for name in ("input", "cache_read", "cache_write"))),
        cached=number(int(totals.get("cache_read", 0))), output=number(int(totals.get("output", 0))),
    )
    searches = (totals.get("server_tools") or {}).get("web_search", 0)
    if searches:
        counts = t("{counts}；联网搜索 {n} 次", counts=counts, n=searches)
    if totals.get("unpriced"):
        counts = t("{counts}；{n} 次请求没有价格", counts=counts, n=totals["unpriced"])
    if totals.get("hook_runs"):
        counts = t("{counts}；跑了 {n} 次你的钩子（拦下 {blocked} 次）", counts=counts, n=totals["hook_runs"], blocked=totals.get("hooks_blocked", 0))
    out.append((counts, "info"))
    for span, depth, others in _flatten(record.get("spans") or [], 0):
        out.append((_row(span, depth, wall, others), "plain" if depth == 0 else "info"))
    if record.get("omitted"):
        out.append((t("……还有 {n} 段太多了没有存下来（合计里算着它们）", n=record["omitted"]), "info"))
    return out


def _flatten(spans: list, depth: int) -> list[tuple[dict, int, int]]:
    """[(一段, 缩进几层, 和它同一批同时跑的还有几段)]。同一批只在同一层里认（子 Agent 的编号是它自己数的）。"""
    batches = Counter(span.get("batch") for span in spans if isinstance(span, dict) and isinstance(span.get("batch"), int))
    rows = []
    for span in spans:
        if isinstance(span, dict):
            batch = span.get("batch")
            rows.append((span, depth, batches[batch] - 1 if isinstance(batch, int) else 0))
            rows += _flatten(span.get("children") or [], depth + 1)
    return rows


def _bar(span: dict, wall: int) -> str:
    """一段在时间轴上的位置：前面用 · 占位，这一段用方块。等你确认的那一截（工具调用才有）用 ░。"""
    scale = BAR / wall if wall > 0 else 0
    start, end = int(span.get("start", 0)), int(span.get("end", 0))
    first = min(BAR - 1, int(start * scale))
    last = max(first + 1, min(BAR, round(end * scale)))
    cells = ["·"] * BAR
    fill = {"model": "█", "hook": "▒"}.get(span.get("kind") or "", "▓")  # 没写 kind 的和别的一样用 ▓
    for index in range(first, last):
        cells[index] = fill
    if span.get("asked") is not None and span.get("waited"):
        wait_from = min(BAR - 1, int(int(span["asked"]) * scale))
        wait_to = max(wait_from + 1, min(last, round((int(span["asked"]) + int(span["waited"])) * scale)))
        for index in range(wait_from, wait_to):
            cells[index] = "░"
    return "".join(cells)


def _row(span: dict, depth: int, wall: int, others: int = 0) -> str:
    indent = "  " * depth + ("└ " if depth else "")
    duration = seconds(int(span.get("end", 0)) - int(span.get("start", 0)))
    if span.get("kind") == "model":
        parts = [t("模型 {model}", model=span.get("model", "")), duration]
        if span.get("first") is not None:
            parts.append(t("首字 {time}", time=seconds(span["first"])))
        if span.get("fallback"):
            parts.append(t("备用模型（替 {model}）", model=span["fallback"]))
        if span.get("compaction"):
            parts.append(t("压缩历史"))
        if span.get("retries"):
            parts.append(t("重试 {n} 次", n=span["retries"]))
        if span.get("stop"):
            parts.append(span["stop"])
        elif span.get("status") in STATUS:
            parts.append(t(STATUS[span["status"]]))
        if "cost" in span:
            parts.append(money(span["cost"]))
        if span.get("note"):
            parts.append(span["note"])  # 失败了、改用备用模型的那一次：为什么失败
    elif span.get("kind") == "hook":
        parts = [t("钩子 {name}（{event}）", name=span.get("name", ""), event=span.get("event", "")), duration]
        result = t(HOOK_RESULT[span["result"]]) if span.get("result") in HOOK_RESULT else ""
        if span.get("exit") is not None:
            result = t("{result}，退出码 {code}", result=result, code=span["exit"])
        parts.append(t("{result}：{note}", result=result, note=span["note"]) if span.get("note") else result)
        if span.get("added"):
            parts.append(t("交给模型 {n} 个字", n=span["added"]))
    else:
        name = span.get("name", "")
        label = t(span["label"]) if span.get("label") else ""
        if span.get("server"):
            label = f"{label} {span['server']}".strip()
        if span.get("agent"):
            label = t("{label}「{agent}」", label=label, agent=span["agent"])
        parts = [" · ".join(part for part in (label, f"{name} {span.get('detail', '')}".strip()) if part), duration]
        approval = span.get("approval")
        if approval == "auto":
            parts.append(t("没问你：{why}", why=t(WHY.get(span.get("why", ""), WHY["mode"]))))
        elif approval in APPROVAL:
            parts.append(t("{answer}（等了 {time}）", answer=t(APPROVAL[approval]), time=seconds(span.get("waited", 0))))
        if span.get("sandbox") in SANDBOX:
            parts.append(t(SANDBOX[span["sandbox"]]))
        if span.get("redacted"):  # 结果里遮住了几处密钥（见 ../redact.py）。页面上的说法在 page/scripts/trace-chart.js 的 traceRedacted
            parts.append(t("遮住了 1 处密钥") if span["redacted"] == 1 else t("遮住了 {n} 处密钥", n=span["redacted"]))
        if others:  # 一步里的几次读一起跑的（见 ../parallel.py）。页面上的说法在 page/scripts/trace-chart.js 的 traceTogether，两边一致
            parts.append(t("和另外 1 步同时执行") if others == 1 else t("和另外 {n} 步同时执行", n=others))
        if span.get("result") in RESULT:
            result = t(RESULT[span["result"]])
            parts.append(t("{result}：{note}", result=result, note=span["note"]) if span.get("note") else result)
    return f"{indent}{seconds(span.get('start', 0)):>8} {_bar(span, wall)}  " + " · ".join(str(part) for part in parts if part)
