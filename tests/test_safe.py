"""safe.py 的测试：编码不了的字符、不是数的数、以及往磁盘上写文件。"""

from __future__ import annotations

import json
import os
import stat

import pytest

from harness.safe import (
    clean,
    clean_text,
    has_bad_number,
    loads_strict,
    private_dir,
    tame,
    to_json,
    write_file,
)

HALF = "\ud83d"  # 半个表情：另一半（\ude00）丢了。这样的字符没法编码成 UTF-8


def strict(text: str):
    """像浏览器的 JSON.parse 那样解析：NaN、Infinity 这些 Python 肯收的写法，在这里算错。"""

    def refuse(name: str):
        raise ValueError(name)

    return json.loads(text, parse_constant=refuse)


def test_half_an_emoji_is_replaced_and_everything_else_is_left_alone():
    assert clean_text(f"表情只剩一半 {HALF} 了") == "表情只剩一半 � 了"
    assert clean_text("\ude00开头和结尾\ud83d") == "�开头和结尾�"
    # 完整的表情、普通的文字一个字都不动（包括本来就是"�"的）
    for text in ("", "你好 hello", "完整的表情 😀 和 👨‍👩‍👧", "已经是 � 的", "全角 ＡＢＣ 和换行\n\t"):
        assert clean_text(text) == text
    # 被拆成两半、但两半都在的表情，合回成一个字符
    assert clean_text("😀") == "😀"
    # 洗过之后一定编码得出来
    clean_text(f"{HALF}a{HALF}{HALF}b").encode("utf-8")


def test_clean_reaches_every_string_in_parsed_data():
    data = {"text": f"a{HALF}", f"key{HALF}": [f"b{HALF}", {"deep": f"c{HALF}"}, 5, None, True, 1.5]}
    assert clean(data) == {"text": "a�", "key�": ["b�", {"deep": "c�"}, 5, None, True, 1.5]}
    assert clean(7) == 7 and clean(None) is None


def test_numbers_that_are_not_numbers_are_found_and_tamed():
    nan, inf = float("nan"), float("inf")
    assert has_bad_number(nan) and has_bad_number([1, {"a": [inf]}]) and has_bad_number({"a": {"b": -inf}})
    assert not has_bad_number({"a": [1, 2.5, "NaN", None, True, {"b": 1e308}]})
    # 换成说明它是什么的文字：这样才能写成合法的 JSON，看的人也知道原来是什么
    assert tame({"a": nan, "b": [inf, -inf, 1.5], "c": "原样"}) == {"a": "NaN", "b": ["Infinity", "-Infinity", 1.5], "c": "原样"}


def test_strict_parsing_refuses_what_cannot_be_written_back():
    assert loads_strict('{"a": 1, "b": [1.5, -2e3, "NaN"], "c": null}') == {"a": 1, "b": [1.5, -2000.0, "NaN"], "c": None}
    # Python 自带的解析器对这几样来者不拒，但它们写回去就不是合法的 JSON 了
    for raw in ('{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}', '{"a": 1e999}', '[-1e999]', '{"a": 1.0e400}'):
        assert json.loads(raw) is not None  # 自带的解析器肯收
        with pytest.raises(ValueError):
            loads_strict(raw)
    with pytest.raises(ValueError):
        loads_strict('{"a": ')
    # 解析出来的文字顺便洗干净：JSON 里可以用转义写出半个表情
    assert loads_strict('{"title": "buy milk \\ud83d"}') == {"title": "buy milk �"}


def test_to_json_never_fails_and_is_always_valid():
    awkward = {"text": f"半个 {HALF}", "n": float("nan"), "big": float("inf"), "list": [1, "中文", {"x": -float("inf")}]}
    for readable in (False, True):
        text = to_json(awkward, readable=readable)
        text.encode("utf-8")  # 编码得出来
        assert text.isascii()  # 有编码不了的字符时，退回到全部转义的写法
        back = strict(text)  # 是合法的 JSON：浏览器解析得了
        assert back["n"] == "NaN" and back["big"] == "Infinity" and back["list"][2] == {"x": "-Infinity"}
        assert back["text"] == f"半个 {HALF}"  # 转义写出去、再读回来，文字还是原来那样

    # 没有问题的内容：readable 时中文原样写出来（给人看的文件），否则全部转义（发给浏览器，不怕编码出错）
    plain = {"标题": "你好 😀", "n": 1.5}
    assert to_json(plain, readable=True) == '{"标题": "你好 😀", "n": 1.5}'
    assert to_json(plain).isascii() and strict(to_json(plain)) == plain
    # 别的选项照常传给编码器；不认识的对象转成文字，而不是报错
    assert to_json({"a": 1}, readable=True, indent=2) == '{\n  "a": 1\n}'
    assert strict(to_json({"path": os.sep, "object": object}))["object"].startswith("<class")


def test_write_file_replaces_the_file_in_one_step(tmp_path, monkeypatch):
    target = tmp_path / "settings.json"
    write_file(target, "第一版")
    assert target.read_text(encoding="utf-8") == "第一版"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600  # 只有自己的账号能读
    write_file(target, "第二版")
    assert target.read_text(encoding="utf-8") == "第二版"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["settings.json"]  # 没有留下临时文件

    # 写之前要确认内容已经交给磁盘
    synced = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda descriptor: (synced.append(descriptor), real_fsync(descriptor)))
    write_file(target, "第三版")
    assert len(synced) == 1

    # 中途出错（这里是最后一步"替换"失败）：原来的文件原封不动，也不留下半截的临时文件
    def broken_replace(source, destination):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError, match="No space left"):
        write_file(target, "写不进去的第四版")
    assert target.read_text(encoding="utf-8") == "第三版"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["settings.json"]
    # 内容本身编码不了时也一样
    monkeypatch.undo()
    with pytest.raises(UnicodeEncodeError):
        write_file(target, f"半个表情 {HALF}")
    assert target.read_text(encoding="utf-8") == "第三版"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["settings.json"]


def test_two_writers_never_share_a_temporary_file(tmp_path, monkeypatch):
    # 临时文件的名字里带一段随机字符。以前是固定的名字，两处同时写同一个文件时会互相踩到：
    # 一个刚把临时文件换成正式文件，另一个再去换就找不到自己的临时文件了
    names = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda source, destination: (names.append(str(source)), real_replace(source, destination)))
    target = tmp_path / "data.json"
    for version in range(20):
        write_file(target, str(version))
    assert len(set(names)) == 20 and all(name.endswith(".tmp") and "data.json." in name for name in names)
    assert target.read_text(encoding="utf-8") == "19"


def test_private_dir_is_only_for_the_owner(tmp_path):
    old_mask = os.umask(0o022)  # 常见的默认设置：新建的目录别人可以进
    try:
        target = tmp_path / "data" / "conversations"
        private_dir(target)
        assert stat.S_IMODE(target.stat().st_mode) == 0o700
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700  # 沿途新建的上级目录也一样
        # 早就存在、而且别人进得来的目录：把权限收回来
        loose = tmp_path / "old"
        loose.mkdir(mode=0o755)
        private_dir(loose)
        assert stat.S_IMODE(loose.stat().st_mode) == 0o700
        private_dir(loose)  # 再调一次也没事
        # 本来就在的上级目录不归我们管，不动它
        shared = tmp_path / "shared"
        shared.mkdir(mode=0o755)
        private_dir(shared / "mine")
        assert stat.S_IMODE(shared.stat().st_mode) == 0o755 and stat.S_IMODE((shared / "mine").stat().st_mode) == 0o700
    finally:
        os.umask(old_mask)
