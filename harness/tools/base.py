"""工具的基本结构。

一个工具由两部分组成：

  spec —— 发给模型看的"说明书"：名字、用途、参数格式。模型只能看到这一部分，
          它根据说明书决定要不要调用、怎么填参数。
  run  —— 真正干活的 Python 函数。模型看不到代码，只能看到它返回的文字。

模型自己什么都执行不了。它只会说"我想调用 X，参数是 Y"，
执行是 harness 的事（见 agent.py）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class ToolError(Exception):
    """工具执行失败。错误信息会原样返回给模型（标记为出错），让它自己换个办法。"""


@dataclass(frozen=True)
class ToolImage:
    """工具结果里的一张图片。data 是 base64（不带 data: 前缀），media_type 是检查过的真实格式。"""

    media_type: str
    data: str


@dataclass(frozen=True)
class ToolOutput:
    """结果不只是一段文字的时候用它：可以带上图片，也可以标明"这是一个出错的结果"。

    普通的工具直接返回字符串就够了。MCP 服务器的工具会返回这个（见 mcp/bridge.py）：
    它们可能返回截图，也会用 isError 告诉我们"这次没做成"，同时附上给模型看的原因。
    """

    text: str
    images: tuple[ToolImage, ...] = ()
    is_error: bool = False
    # 下面三样只有浏览器的工具会用到（见 tools/browser.py）。
    # state：标签页的情况（Anthropic 浏览器工具包的 browser_state 块）。Claude 的适配层把它原样放进结果里
    state: dict | None = None
    # state_only：结果只有 state、没有文字（工具包规定：开、关、换、列出标签页这四个动作的结果就是这样）
    state_only: bool = False
    # preview：只给界面看的东西（截图的小图和大图的编号），不发给模型
    preview: dict | None = None
    # shown：给人看的说法和给模型的不一样时用（t() 译好的）。比如被你的钩子拦下的调用：模型看到中文原文，你看到界面的语言
    shown: str = ""


@dataclass(frozen=True)
class Tool:
    # 发给 Claude 的说明书（Anthropic 的格式）
    spec: dict
    # 接收模型给的参数（一个 dict），返回给模型看的文字（或者带图片的 ToolOutput）。
    # None 表示这是"服务器工具"：由 Anthropic 的服务器执行，本地不需要做任何事。
    run: Callable[[dict], str | ToolOutput] | None = None
    # 发给其他厂商模型的说明书：{"name", "description", "parameters"}，各家通用的函数格式。
    # None 表示这个工具只有 Claude 能用（比如 Anthropic 服务器上的联网搜索）。
    portable: dict | None = None
    # 执行之前、问用户之前先看一眼参数：返回一段话，表示这次调用这个工具不做（这段话作为出错的结果交回给模型，
    # 不问用户）；返回 None 表示照常。比如只许读文件的 Agent 拿到的文件工具，遇到"改文件"就直接退回
    # （见 agents/tools.py）：明知不会做的事，不该先弹一张确认卡片让你点
    check: Callable[[dict], str | None] | None = None
    # 这个工具是 Anthropic 的哪个"工具包"（client toolset）里的一员，比如 "browser"；空的是普通的工具。
    # 工具包的成员不各发各的说明书：spec 是整个工具包的那一项（同一个包的成员共用同一份），Claude 的适配层只发一次。
    # 模型调用时用成员的名字（member），并带上 toolset_name（见 providers/claude.py）
    toolset: str = ""
    member: str = ""
    # 这个工具是哪个工具包的"替身"：模型用不了那个工具包时（别家的模型、不支持它的 Claude 模型）才用它，
    # 用得了的时候不发。比如浏览器：Claude 用工具包，GPT 用我们自己写的 browser_* 函数工具
    stands_in_for: str = ""
    # 只给看得懂图片的模型：工具全靠截图（操作整台电脑，见 tools/computer.py）。看不了图的模型（DeepSeek）拿不到它，
    # 给了它也只能闭着眼睛乱点
    vision: bool = False

    @property
    def name(self) -> str:
        return self.member or self.spec["name"]

    @property
    def key(self) -> str:
        """在工具箱里的编号。工具包的成员前面加上包名：成员的名字（navigate、find……）可能和普通工具重名"""
        return f"{self.toolset}:{self.member}" if self.toolset else self.spec["name"]


@dataclass(frozen=True)
class FactsTool(Tool):
    """动手之前要先看一眼"要碰的是什么"的工具（浏览器、操作整台电脑）：permissions.py 靠 facts 的结果决定问不问你、怎么问。

    同一次调用会被看好几次（要不要问、能不能不问就做、动手前和问你时的情况比一比），只在第一次真的去看，后面用上一次的结果：
    一来省事，二来动手前比的就是"问你的时候"看到的那一份。
    """

    facts: Callable[[dict], Any] | None = None
    _seen: dict = field(default_factory=dict, compare=False, repr=False)  # 上一次看的结果（见 look）

    def look(self, tool_input: dict) -> Any:
        if self._seen.get("input") is tool_input:
            return self._seen["facts"]
        found = self.facts(tool_input) if self.facts is not None else None
        self._seen.update(input=tool_input, facts=found)
        return found


# 参数的类型。对象和数组只检查最外面这一层，里面的内容由工具自己检查（比如定时任务的时间表，见 tasks.py）
_JSON_TYPES: dict[str, type | tuple[type, ...]] = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict, "array": list}


def check_input(schema: dict, tool_input: object) -> dict:
    """检查模型给的参数是否符合 schema，返回可以直接传给函数的参数。

    模型偶尔会填错（漏掉必填项、类型不对）。这里不去猜它的意思，
    而是抛出 ToolError 把问题告诉它，它看到后会自己改正重试。
    """
    if not isinstance(tool_input, dict):
        raise _invalid("参数必须是一个 JSON 对象", tool_input)
    properties = schema["properties"]
    args = {key: value for key, value in tool_input.items() if value is not None}
    for key in schema.get("required", []):
        if key not in args:
            raise _invalid(f"缺少必填参数 {key}", tool_input)
    for key, value in args.items():
        if key not in properties:
            raise _invalid(f"没有 {key} 这个参数", tool_input)
        expected = properties[key]
        is_bool = isinstance(value, bool)
        type_ok = isinstance(value, _JSON_TYPES[expected["type"]])
        if expected["type"] != "boolean" and is_bool:
            type_ok = False  # Python 里 True 也算 int，这里要排除
        if not type_ok:
            raise _invalid(f"参数 {key} 的类型应该是 {expected['type']}", tool_input)
        if "enum" in expected and value not in expected["enum"]:
            raise _invalid(f"参数 {key} 只能是 {expected['enum']} 之一", tool_input)
    return args


def _invalid(reason: str, tool_input: object) -> ToolError:
    received = json.dumps(tool_input, ensure_ascii=False, default=str)
    return ToolError(json.dumps({"error": reason, "INVALID_JSON": received}, ensure_ascii=False))


def function_tool(
    name: str,
    description: str,
    properties: dict,
    required: list[str],
    function: Callable[..., str | ToolOutput],
) -> Tool:
    """把一个普通的 Python 函数包装成工具。自定义工具都用它来创建。"""
    schema = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    spec = {
        "name": name,
        "description": description,
        "input_schema": schema,
        # 参数边生成边传回来，而不是等 API 攒齐再发。代价是 API 不再替我们校验参数，
        # 所以执行前必须自己校验（上面的 check_input）。
        "eager_input_streaming": True,
    }

    def run(tool_input: dict) -> str | ToolOutput:  # 和 Tool.run 一样：多数工具交回文字，有的还带图片（agent 工具交回子 Agent 的结果）
        return function(**check_input(schema, tool_input))

    # 自定义工具的说明书本来就是我们写的，换个包装就能给其他厂商用
    parameters = {key: value for key, value in schema.items() if key != "additionalProperties"}
    portable = {"name": name, "description": description, "parameters": parameters}
    return Tool(spec=spec, run=run, portable=portable)
