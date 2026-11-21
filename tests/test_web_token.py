"""后台服务的网页口令重启前后不变（harness/webui/token.py）。

用户的智狐装成后台服务以后，每次 `harness service restart`（更新代码之后）口令都换一个，浏览器里开着的那一页
就打不开了（"我的智狐咋打不开了呢"）。现在后台服务的口令存在 data/.web-token（600），重启时接着用；
卸载服务时删掉，下次装上换新的。在终端里开的 harness --web 照旧每次换。
"""

from __future__ import annotations

from modes import has_mode

from harness.config import WEB_TOKEN
from harness.export import plan
from harness.webui.token import forget_token, stable_token


def test_the_service_token_survives_a_restart(tmp_path):
    first = stable_token(tmp_path)
    assert stable_token(tmp_path) == first  # "重启"：再要一次，还是同一个
    path = tmp_path / WEB_TOKEN
    assert has_mode(path, 0o600)
    path.chmod(0o644)  # 被别的程序改宽了：读的时候收回来
    assert stable_token(tmp_path) == first and has_mode(path, 0o600)
    forget_token(tmp_path)  # 卸载服务
    assert not path.exists() and stable_token(tmp_path) != first
    forget_token(tmp_path)
    forget_token(tmp_path)  # 已经没有了，再删一次也没事


def test_a_damaged_or_linked_token_file_is_replaced_not_followed(symlinks, tmp_path):
    path = tmp_path / WEB_TOKEN
    path.write_text("太短\n", encoding="utf-8")
    token = stable_token(tmp_path)
    assert len(token) >= 16 and path.read_text(encoding="utf-8").strip() == token
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("A" * 40, encoding="utf-8")
    path.unlink()
    path.symlink_to(elsewhere)  # 指到别处的链接：不读它，也不顺着它往别处写
    fresh = stable_token(tmp_path)
    assert fresh != "A" * 40 and not path.is_symlink() and elsewhere.read_text(encoding="utf-8") == "A" * 40


def test_the_token_is_never_exported(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    stable_token(data)
    (data / "todos.json").write_text("[]", encoding="utf-8")
    exported = plan(data)
    names = [name for _, name in exported.files] + list(exported.masked)
    assert "todos.json" in names and WEB_TOKEN not in names, names
