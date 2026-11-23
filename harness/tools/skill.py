"""skill 工具：模型读 Skill 用的门。第一类工具（自定义工具），说明书是我们写的。

模型平时只看得到每个 Skill 的名字和一句说明（见 skills/catalog.py）。判断"这件事有现成的方法"之后，
它调用这个工具读出 SKILL.md 的正文；正文里提到别的文件，再用它读那个文件。

这个工具只能读，而且只能读启用着的 Skill 文件夹里的普通文件：路径一层一层地检查，出不了那个文件夹，
也不跟着链接走（见 skills/store.py 的 locate）。所以它不用经过权限确认，和查看记忆一样。
Skill 里的脚本不在这里运行：模型要用 run_command，那一步照常要你点头。

结果是给模型看的，所以写的是中文，不进翻译表。
"""

from __future__ import annotations

from pathlib import Path

from ..skills.store import (
    SKILL_FILE,
    Skill,
    SkillError,
    SkillStore,
    as_text,
    body_of,
    locate,
    read_bytes,
    read_skill_file,
    walk,
)
from .base import Tool, ToolError, always, function_tool

TOOL_NAME = "skill"
MAX_CHARS = 100_000  # 一次最多交给模型这么多字；更长的文件让它用命令分段读
MAX_LISTED = 100  # 文件列表最多列这么多个
# Claude Code 的 Skill 里常用这个写法指代"Skill 自己的文件夹"，比如 ${CLAUDE_SKILL_DIR}/scripts/fill.py。照样换成真实的路径
FOLDER_VARIABLES = ("${CLAUDE_SKILL_DIR}", "${SKILL_DIR}")

DESCRIPTION = (
    "读取一个 Skill。Skill 是用户安装的做事方法：一个文件夹，里面的 SKILL.md 写着做某一类事的步骤，"
    "可能还带着脚本（scripts/）、参考资料（references/）和模板素材（assets/）。"
    "可用的 Skill 列在用户消息里的 <skills> 块中。"
    "用户的请求和某个 Skill 的说明对得上时，先只填 name 调用一次，读出它的完整说明，再照着做。"
    "说明里提到同一个文件夹里的别的文件、并且确实需要时，再调用一次，用 file 填它的相对路径。"
    "要运行其中的脚本，用 run_command 并写出完整路径（结果里会给出文件夹的位置）。这个工具只能读，不能修改 Skill。"
)


def make_skill_tool(store: SkillStore) -> Tool:
    def run(name: str, file: str = "") -> str:
        skill = _usable(store, name)
        folder = skill.folder.resolve()
        relative = _relative(file, str(folder))
        if relative in ("", SKILL_FILE):
            return _instructions(skill.name, folder)
        try:
            target = locate(folder, relative)
            data, size = read_bytes(target, MAX_CHARS * 4)
        except SkillError:
            raise ToolError(f"Skill {skill.name} 里没有 {relative} 这个文件。文件路径要相对于 Skill 的文件夹，比如 references/REFERENCE.md。") from None
        text = as_text(data, MAX_CHARS * 4)
        if text is None:
            return f"{relative} 不是文字文件（{_size(size)}），没法在这里读。需要用它的话，用 run_command 处理，它的完整路径是 {target}。"
        if len(text) > MAX_CHARS or size > MAX_CHARS * 4:
            return f"{relative} 很长，下面只是开头的 {MAX_CHARS} 个字。需要后面的部分时，用 run_command 分段读取（比如 sed -n '2000,2400p'），完整路径是 {target}。\n\n{text[:MAX_CHARS]}"
        return f"{relative}（{_size(size)}）：\n\n{text}"

    return function_tool(
        name=TOOL_NAME,
        description=DESCRIPTION,
        properties={
            "name": {"type": "string", "description": "Skill 的名字，就是 <skills> 列表里 <name> 的内容"},
            "file": {
                "type": "string",
                "description": "可选：要读的文件，相对于 Skill 的文件夹，比如 references/REFERENCE.md。不填就是读 SKILL.md",
            },
        },
        required=["name"],
        function=run,
        parallel=always,  # 只能读（见文件开头），可以和同一步里别的只读调用同时跑（见 ../parallel.py）
    )


def _usable(store: SkillStore, name: str) -> Skill:
    """找到一个可以用的 Skill，用不了的话告诉模型为什么、现在有哪些可以用。"""
    try:
        skill = store.get(name)
    except SkillError:
        raise ToolError(f"没有叫 {name} 的 Skill。{_available(store)}") from None
    if not skill.enabled:
        raise ToolError(f"Skill {name} 已经被用户停用了，不能使用。{_available(store)}")
    if skill.problems:
        raise ToolError(f"Skill {name} 的 SKILL.md 不符合规范（{skill.problems[0]}），暂时不能使用。可以请用户在“扩展”里的 Skill 一页修好它。")
    return skill


def _available(store: SkillStore) -> str:
    names = [name for name, _ in store.catalog()]
    return f"现在可用的有：{'、'.join(names)}。" if names else "现在没有可用的 Skill。"


def _relative(file: str, folder: str) -> str:
    """模型给的文件路径整理成相对路径。它可能照着结果里的完整路径写，或者带着 ./ 和 ${CLAUDE_SKILL_DIR}。"""
    path = file.strip()
    for prefix in (folder + "/", *(variable + "/" for variable in FOLDER_VARIABLES)):
        path = path.removeprefix(prefix)
    while path.startswith("./"):
        path = path[2:]
    return path


def _instructions(name: str, folder: Path) -> str:
    try:
        text, _ = read_skill_file(folder)
    except SkillError as exc:
        raise ToolError(f"Skill {name} 的 SKILL.md 读不出来：{exc}") from None
    body = body_of(text).strip("\n")
    for variable in FOLDER_VARIABLES:
        body = body.replace(variable, str(folder))
    files = walk(folder)
    listed = [f"  {path}（{_size(size)}）" for path, size in files[:MAX_LISTED]]
    if len(files) > MAX_LISTED:
        listed.append(f"  ……还有 {len(files) - MAX_LISTED} 个文件")
    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS] + f"\n……（正文太长，后面的部分没有给出。完整的文件在 {folder}/{SKILL_FILE}）"
    return (
        f"已载入 Skill「{name}」。它的文件夹：{folder}\n"
        "- 说明里写的相对路径都以这个文件夹为起点。要读其中的文件，再调用 skill 工具，name 不变，file 填相对路径。\n"
        f'- 要运行其中的脚本，用 run_command 并写出完整路径，比如 python3 "{folder}/scripts/…"。执行之前同样要用户确认。\n'
        "- 不要修改这个文件夹里的文件。\n"
        f"文件夹里的文件（{len(files)} 个）：\n" + "\n".join(listed) + "\n\n"
        "SKILL.md 的正文：\n\n" + body
    )


def _size(size: int) -> str:
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    if size >= 1000:
        return f"{size / 1000:.0f} KB"
    return f"{size} 字节"
