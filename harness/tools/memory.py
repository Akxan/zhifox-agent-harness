"""长期记忆 —— 同样是第二类工具：Anthropic 定义格式，我们负责执行。

模型本身没有记忆，每次对话都从零开始。记忆工具给它一个目录（它看到的是 /memories），
它可以在里面创建、查看、修改文件；下次启动时文件还在，它读一遍就"想起来"了。

本地怎么存由我们决定。这里直接用 SDK 自带的实现：存成 data/memories/ 下的普通文件，
你可以随时打开查看或手动修改。
"""

from __future__ import annotations

from pathlib import Path

from anthropic.tools import BetaLocalFilesystemMemoryTool
from anthropic.tools import ToolError as SdkToolError

from .base import Tool, ToolError


def make_memory_tool(data_dir: Path) -> Tool:
    backend = BetaLocalFilesystemMemoryTool(base_path=str(data_dir))  # 实际位置：data_dir/memories/

    def run(tool_input: dict) -> str:
        try:
            return str(backend.call(tool_input))
        except SdkToolError as exc:
            raise ToolError(str(exc)) from exc

    # 说明书由 SDK 生成：{"type": "memory_20250818", "name": "memory"}
    return Tool(spec=dict(backend.to_dict()), run=run)
