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
from dataclasses import dataclass


class ToolError(Exception):
    """工具执行失败。错误信息会原样返回给模型（标记为出错），让它自己换个办法。"""


@dataclass(frozen=True)
class Tool:
    spec: dict
    # 接收模型给的参数（一个 dict），返回给模型看的文字。
    # None 表示这是"服务器工具"：由 Anthropic 的服务器执行，本地不需要做任何事。
    run: Callable[[dict], str] | None = None

    @property
    def name(self) -> str:
        return self.spec["name"]


_JSON_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool}


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
    function: Callable[..., str],
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

    def run(tool_input: dict) -> str:
        return function(**check_input(schema, tool_input))

    return Tool(spec=spec, run=run)
