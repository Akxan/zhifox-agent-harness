"""把不可信的内容变成"编码得出来、别人解析得了、写得进磁盘"的样子。

用户粘贴的文字、模型的输出、工具的结果，都可能带着两种会坏事的东西：

  落单的代理字符   一个表情符号在内存里可能被拆成两半（UTF-16 的"代理对"）。只剩半个时，
                   它没法编码成 UTF-8：往磁盘上写、发给浏览器、发给模型的 API，都会在编码那一步报错。
  不是数的"数"     NaN 和无穷大。Python 会把它们写成 NaN、Infinity，但那不是合法的 JSON，
                   浏览器和各家的 API 都解析不了。

这样的内容一旦混进对话历史或事件日志，就不只是"这一条显示不出来"：之后的每一次存盘、每一次请求
都会跟着失败。所以在入口处洗干净（clean_text、clean、loads_strict），
在出口处再兜一次底（to_json 保证编码不会失败）。

这个文件里还有 write_file：把文字安全地写进文件。放在这里是因为它解决的是同一类问题，
即数据不能因为意外的内容或意外的中断而坏掉。
"""

from __future__ import annotations

import json
import math
import os
import secrets
from pathlib import Path
from typing import Any

REPLACEMENT = "�"  # 显示成"�"：这里本来有一个没法表示的字符


def clean_text(text: str) -> str:
    """把落单的代理字符换成"�"。正常的文字原样返回（这是绝大多数情况，所以先试一下能不能直接编码）。"""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        # 按 UTF-16 写出去再读回来：成对的代理字符会合成一个正常的字符，落单的被换掉
        return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    return text


def clean(value: Any) -> Any:
    """对一份解析出来的数据（字典、列表、文字……）里的每一段文字做 clean_text，其余的原样保留。"""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, dict):
        return {(clean_text(key) if isinstance(key, str) else key): clean(item) for key, item in value.items()}
    return value


def has_bad_number(value: Any) -> bool:
    """这份数据里有没有 NaN 或无穷大。"""
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, list):
        return any(has_bad_number(item) for item in value)
    if isinstance(value, dict):
        return any(has_bad_number(item) for item in value.values())
    return False


def tame(value: Any) -> Any:
    """把 NaN 和无穷大换成说明它是什么的文字（"NaN"、"Infinity"），让这份数据能写成合法的 JSON。"""
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, list):
        return [tame(item) for item in value]
    if isinstance(value, dict):
        return {key: tame(item) for key, item in value.items()}
    return value


def loads_strict(raw: str) -> Any:
    """解析 JSON，但不接受 NaN、Infinity 和大到变成无穷大的数（比如 1e999）。不合法时抛出 ValueError。

    Python 自带的解析器对这几样来者不拒，可它们写回去就不是合法的 JSON 了。模型给的工具参数用这个来解析：
    宁可当场告诉模型"参数不合法"，也不要让一个写不出去的数混进后面的流程。
    """

    def constant(name: str) -> None:
        raise ValueError(f"{name} 不是合法的 JSON 数字")

    def number(text: str) -> float:
        value = float(text)
        if not math.isfinite(value):
            raise ValueError(f"{text} 超出了数字能表示的范围")
        return value

    return clean(json.loads(raw, parse_constant=constant, parse_float=number))


def to_json(value: Any, readable: bool = False, **options: Any) -> str:
    """把数据编码成合法的 JSON 文本。保证不会因为落单的代理字符、NaN 或无穷大而失败。

    readable 为真时尽量把中文原样写出来（给人看的文件用）；写不了的时候退回到全部转义的写法。
    全部转义的写法只含 ASCII 字符，所以之后再编码成字节也不会出错。
    """
    if readable:
        try:
            text = json.dumps(value, ensure_ascii=False, allow_nan=False, default=str, **options)
            text.encode("utf-8")  # 里面有落单的代理字符的话，在这里就会发现
            return text
        except ValueError:  # 编码不了，或者有 NaN、无穷大：走下面稳妥的那条路
            pass
    try:
        return json.dumps(value, allow_nan=False, default=str, **options)
    except ValueError:
        return json.dumps(tame(value), allow_nan=False, default=str, **options)


def private_dir(path: Path) -> None:
    """建一个只有自己的账号能进的目录（已经存在的话，把权限改成这样）。沿途新建的上级目录也一样。"""
    missing = [folder for folder in (path, *path.parents) if not folder.exists()]
    for folder in reversed(missing):
        folder.mkdir(mode=0o700, exist_ok=True)
    path.chmod(0o700)


def write_file(path: Path, text: str | bytes) -> None:
    """把文字（或者原样的字节，比如多媒体库里的图片）写进文件：要么整个写成，要么原来的文件原封不动。
    新文件只有自己的账号能读。

    做法是先写到旁边的一个临时文件，确认内容已经交给磁盘（fsync）之后，再用它替换掉原来的文件。
    替换是一步完成的，所以程序中途崩溃时不会留下半个文件。断电的情况取决于操作系统和硬盘
    有没有如实执行 fsync，这里只能做到"该做的都做了"。
    临时文件的名字里带一段随机字符：两处同时在写同一个文件时，各写各的，不会互相踩到。
    """
    temp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    binary = isinstance(text, bytes)
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb" if binary else "w", encoding=None if binary else "utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)  # 没写成：不留下半截的临时文件
        raise
