"""网页上的"从备份恢复…"（/api/restore/……，见 webui/restore_api.py）。

上传是原样的字节（不是 JSON）；恢复以后程序照着新的数据在原地重新组装一遍（换上一个新的 WebApp），
口令和事件流都不变，页面收到 reset 和新的状态。
"""

from __future__ import annotations

import http.client
import io
import json
import socket
import tempfile
import zipfile
from datetime import datetime

import pytest
import test_webui
from fake_api import reply, text
from test_restore import handmade, snapshot, write
from test_webui import latest_state, request

from harness import export, restore
from harness.webui import lifecycle, restore_api

serve = test_webui.serve


def upload(browser, data: bytes, name: str = "backup.zip", headers: dict | None = None) -> tuple[int, dict]:
    """和页面一样：请求体就是压缩包原样的字节。"""
    connection = http.client.HTTPConnection(browser.host, timeout=30)
    sent = {"X-Harness-Token": browser.token, "Origin": f"http://{browser.host}", "Content-Type": "application/zip", "X-Restore-Name": name, **(headers or {})}
    connection.request("POST", "/api/restore/upload", body=data, headers=sent)
    response = connection.getresponse()
    body = json.loads(response.read().decode())
    connection.close()
    return response.status, body


def exported(data_dir) -> bytes:
    out = io.BytesIO()
    export.write_archive(data_dir, out, datetime(2026, 11, 20, 9, 30))
    return out.getvalue()


def test_upload_preview_and_restore_in_place(serve):
    s = serve()
    data = s.config.data_dir
    (data / "memories").mkdir(parents=True, exist_ok=True)
    (data / "memories" / "旧的.md").write_text("备份里的记忆", encoding="utf-8")
    backup = exported(data)
    (data / "memories" / "旧的.md").unlink()
    (data / "memories" / "新的.md").write_text("现在的记忆", encoding="utf-8")

    status, body = upload(s.browser, backup)
    assert status == 200, body
    assert body["preview"]["exported_at"].startswith("2026-11-20T09:30")
    steps = {step["id"]: step for step in body["preview"]["modes"]["overwrite"]["steps"]}
    assert steps["memories"]["action"] == "replace"

    status, body = s.browser.post("/api/restore/apply", {"stage": body["stage"], "mode": "overwrite", "confirm": True})
    assert status == 200, body
    assert (data / "memories" / "旧的.md").read_text(encoding="utf-8") == "备份里的记忆"
    assert not (data / "memories" / "新的.md").exists()
    assert body["result"]["backup"] and zipfile.ZipFile(body["result"]["backup"]).namelist()
    s.browser.wait_for("reset")
    state = latest_state(s.browser)
    assert [memory["path"] for memory in state["memories"]] == ["旧的.md"]


@pytest.fixture
def temp_dir(tmp_path, monkeypatch):
    """上传的备份先写进系统的临时文件夹：换成这个测试自己的，才数得清留下了几个。"""
    folder = tmp_path / "system-temp"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    return folder


def uploads_left(folder) -> list[str]:
    return sorted(path.name for path in folder.iterdir() if path.name.startswith("zhifox-restore-"))


def a_backup(s) -> bytes:
    """当前数据文件夹的一份备份（里面有一条记忆）。"""
    write(s.config.data_dir, "memories/备份里的.md", "备份里的")
    raw = exported(s.config.data_dir)
    (s.config.data_dir / "memories" / "备份里的.md").unlink()
    return raw


def staged(s) -> str:
    status, body = upload(s.browser, a_backup(s))
    assert status == 200, body
    return body["stage"]


def raw_post(host: str, head: str, body: bytes = b"", close_early: bool = False) -> tuple[int, dict]:
    """自己写请求行和请求头（http.client 会替你算好 Content-Length，这里要故意写错）。"""
    address, port = host.split(":")
    with socket.create_connection((address, int(port)), timeout=10) as connection:
        connection.sendall(head.encode() + b"\r\n" + body)
        if close_early:
            connection.shutdown(socket.SHUT_WR)  # 说好的还没发完，就不发了
        chunks = []
        while chunk := connection.recv(65536):
            chunks.append(chunk)
    raw = b"".join(chunks).decode()
    status = int(raw.split(" ", 2)[1])
    return status, json.loads(raw.split("\r\n\r\n", 1)[1])


# ---- 口令和上限 ----------------------------------------------------------------


def test_every_restore_request_needs_the_token(serve, temp_dir):
    s = serve()
    host = s.browser.host
    before = snapshot(s.config.data_dir)
    raw = a_backup(s)
    for headers in ({}, {"X-Harness-Token": "wrong"}, {"X-Harness-Token": s.browser.token, "Origin": "http://evil.example"}):
        connection = http.client.HTTPConnection(host, timeout=10)
        connection.request("POST", "/api/restore/upload", body=raw, headers={"Content-Type": "application/zip", **headers})
        response = connection.getresponse()
        assert response.status == 403 and response.read()  # 拒绝的上传也读掉了请求体，回答收得到
        connection.close()
        for action in ("info", "apply", "discard", "reveal"):
            assert request(host, "POST", f"/api/restore/{action}", {"stage": "x", "mode": "overwrite", "confirm": True}, headers)[0] == 403
    assert uploads_left(temp_dir) == [] and s.server.web.restore._stages == {}
    assert snapshot(s.config.data_dir) == before


def test_a_backup_too_big_for_the_page_gets_413_and_the_body_is_read(serve, temp_dir, monkeypatch):
    monkeypatch.setattr(restore_api, "RESTORE_MAX_UPLOAD_BYTES", 1000)
    s = serve()
    status, body = upload(s.browser, b"PK" + b"0" * 5000)
    assert status == 413 and "harness restore" in body["error"]
    assert uploads_left(temp_dir) == []
    status, body = s.browser.post("/api/restore/info")
    assert status == 200 and body["max_bytes"] == 1000  # 页面上传之前就照着它拦下
    assert body["folder"] == str(s.config.data_dir / "restore-backups") and body["blocked"] == ""


def test_wrong_lengths_and_cut_off_uploads(serve, temp_dir):
    s = serve()
    host, token = s.browser.host, s.browser.token
    head = f"POST /api/restore/upload HTTP/1.1\r\nHost: {host}\r\nX-Harness-Token: {token}\r\nContent-Type: application/zip\r\n"
    assert raw_post(host, head + "Content-Length: abc\r\n")[0] == 411
    assert raw_post(host, head)[0] == 411  # 没有 Content-Length：不知道要读多少，不读
    status, body = upload(s.browser, b"")
    assert status == 400 and body["error"]
    status, body = raw_post(host, head + "Content-Length: 100000\r\n", b"PK\x03\x04 only a little", close_early=True)
    assert status == 400 and "没有完成" in body["error"]
    assert uploads_left(temp_dir) == []


def test_a_bad_archive_is_refused_and_its_upload_deleted(serve, temp_dir, tmp_path):
    s = serve()
    before = snapshot(s.config.data_dir)
    bad = handmade(tmp_path / "bad.zip", [("settings.json", "{}"), ("../../evil.md", "x")]).read_bytes()
    status, body = upload(s.browser, bad)
    assert status == 409 and "不安全的路径" in body["error"]
    status, body = upload(s.browser, b"not a zip at all")
    assert status == 409 and "zip" in body["error"]
    assert uploads_left(temp_dir) == [] and s.server.web.restore._stages == {}
    assert snapshot(s.config.data_dir) == before


def test_apply_checks_its_arguments(serve, temp_dir):
    s = serve()
    stage = staged(s)
    for body in ({"stage": stage, "mode": "overwrite"}, {"stage": stage, "mode": "overwrite", "confirm": "yes"},
                 {"stage": stage, "mode": "nuke", "confirm": True}, {"stage": 3, "mode": "merge", "confirm": True}):
        assert s.browser.post("/api/restore/apply", body)[0] == 400, body
    status, body = s.browser.post("/api/restore/apply", {"stage": "no-such-stage", "mode": "merge", "confirm": True})
    assert status == 409 and body["gone"] is True
    assert uploads_left(temp_dir) and s.server.web.restore._stages  # 参数不对的请求不碰上传好的那一份


def test_only_one_upload_is_kept_and_discard_deletes_it(serve, temp_dir):
    s = serve()
    first = staged(s)
    second = staged(s)
    assert first != second and len(uploads_left(temp_dir)) == 1  # 再传一个就换掉前一个
    assert s.browser.post("/api/restore/apply", {"stage": first, "mode": "merge", "confirm": True})[0] == 409
    assert s.browser.post("/api/restore/discard", {"stage": second}) == (200, {"ok": True})
    assert uploads_left(temp_dir) == []
    assert s.browser.post("/api/restore/apply", {"stage": second, "mode": "merge", "confirm": True})[1]["gone"] is True


def test_quitting_deletes_an_upload_nobody_confirmed(serve, temp_dir):
    s = serve()
    staged(s)
    assert uploads_left(temp_dir)
    lifecycle.stop(s.server.web, 1)
    assert uploads_left(temp_dir) == []


def test_an_old_upload_expires(serve, temp_dir, monkeypatch):
    s = serve()
    stage = staged(s)
    monkeypatch.setattr(restore_api, "RESTORE_STAGE_SECONDS", -1)
    status, body = s.browser.post("/api/restore/apply", {"stage": stage, "mode": "overwrite", "confirm": True})
    assert status == 409 and body["gone"] is True and uploads_left(temp_dir) == []


# ---- 什么时候不恢复 --------------------------------------------------------------


def test_it_refuses_while_the_conversation_is_busy(serve, temp_dir):
    s = serve()
    stage = staged(s)
    before = snapshot(s.config.data_dir)
    web = s.server.web
    assert web._turn.acquire(blocking=False)  # 像是你的这一轮还没结束（或者正在改设置）
    try:
        assert s.browser.post("/api/restore/info")[1]["blocked"] == web._busy()
        status, body = s.browser.post("/api/restore/apply", {"stage": stage, "mode": "overwrite", "confirm": True})
        assert status == 409 and body["error"] == web._busy()
    finally:
        web._turn.release()
    assert snapshot(s.config.data_dir) == before and s.server.web is web
    assert s.browser.post("/api/restore/apply", {"stage": stage, "mode": "overwrite", "confirm": True})[0] == 200  # 上传的那一份还在


def test_it_refuses_while_a_task_runs(serve, temp_dir, monkeypatch):
    s = serve()
    stage = staged(s)
    web = s.server.web
    monkeypatch.setattr(web.tasks, "snapshot", lambda: ([{"id": "t1", "title": "每日安排", "running": {"run_id": "r1"}}], []))
    assert "每日安排" in s.browser.post("/api/restore/info")[1]["blocked"]
    status, body = s.browser.post("/api/restore/apply", {"stage": stage, "mode": "overwrite", "confirm": True})
    assert status == 409 and "每日安排" in body["error"]
    assert s.server.web is web and not web._turn.locked()


def test_it_refuses_while_a_chat_holds_a_conversation(serve, temp_dir):
    s = serve()
    stage = staged(s)
    web = s.server.web
    assert web.claims.take("20261120-0900-aaaaaa", "telegram:12345") is None
    status, body = s.browser.post("/api/restore/apply", {"stage": stage, "mode": "merge", "confirm": True})
    assert status == 409 and "Telegram" in body["error"]
    web.claims.take("20261120-0901-bbbbbb", "web")  # 网页自己认领着的不算
    web.claims.release("20261120-0900-aaaaaa", "telegram:12345")
    assert s.browser.post("/api/restore/apply", {"stage": stage, "mode": "merge", "confirm": True})[0] == 200


# ---- 恢复以后 --------------------------------------------------------------------


def test_after_a_restore_a_new_web_app_takes_over_with_the_same_token(serve, temp_dir):
    s = serve()
    old = s.server.web
    stage = staged(s)
    assert s.browser.post("/api/restore/apply", {"stage": stage, "mode": "overwrite", "confirm": True})[0] == 200
    fresh = s.server.web
    assert fresh is not old and fresh.token == old.token and fresh.ui is old.ui
    assert old._turn.locked()  # 旧的不再用：锁一直拿着，万一还有请求拿着它，只会得到"正在忙"
    assert old.new_conversation() == old._busy()
    assert not fresh._turn.locked()
    assert uploads_left(temp_dir) == []
    # 页面照常能用：同一个口令、同一个事件流
    assert s.browser.post("/api/restore/info")[0] == 200
    assert latest_state(s.browser)["memories"][0]["path"] == "备份里的.md"


def test_merge_keeps_the_open_conversation_open(serve, temp_dir):
    s = serve(reply(text("你好呀")))
    assert s.browser.post("/api/message", {"text": "在吗"})[0] == 200
    s.browser.wait_for("turn_end")
    opened = latest_state(s.browser)["conversation"]
    stage = staged(s)
    assert s.browser.post("/api/restore/apply", {"stage": stage, "mode": "merge", "confirm": True})[0] == 200
    s.browser.wait_for("reset")
    assert s.server.web.app.conversation.id == opened
    assert any(event["type"] == "user" and event.get("text") == "在吗" for event in s.server.web.ui.events_after(0, timeout=0))


def test_overwrite_starts_a_new_conversation_when_the_open_one_is_gone(serve, temp_dir):
    s = serve(reply(text("你好呀")))
    stage = staged(s)  # 备份里没有接下来这段会话
    assert s.browser.post("/api/message", {"text": "在吗"})[0] == 200
    s.browser.wait_for("turn_end")
    opened = latest_state(s.browser)["conversation"]
    assert s.browser.post("/api/restore/apply", {"stage": stage, "mode": "overwrite", "confirm": True})[0] == 200
    s.browser.wait_for("reset")
    assert s.server.web.app.conversation.id != opened
    assert latest_state(s.browser)["conversations"] == []


def test_a_failed_restore_still_brings_the_program_back(serve, temp_dir, monkeypatch):
    """恢复失败（磁盘满了……）：数据原样不动；定时任务、通道已经停下了，所以照着原来的数据重新组装一个，页面接着用。"""
    s = serve()
    stage = staged(s)
    before = snapshot(s.config.data_dir)
    old = s.server.web

    def full(*args, **kwargs):
        raise restore.RestoreError("磁盘满了")

    monkeypatch.setattr(restore, "apply", full)
    status, body = s.browser.post("/api/restore/apply", {"stage": stage, "mode": "overwrite", "confirm": True})
    assert status == 409 and body == {"error": "磁盘满了", "restarted": True}
    assert s.server.web is not old and snapshot(s.config.data_dir) == before
    assert uploads_left(temp_dir) == []


def test_if_the_new_data_cannot_start_the_old_data_comes_back(serve, temp_dir, monkeypatch):
    s = serve()
    stage = staged(s)
    before = snapshot(s.config.data_dir)
    real = restore_api.build_app
    calls = []

    def first_fails(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("新的设置读不了")
        return real(*args, **kwargs)

    monkeypatch.setattr(restore_api, "build_app", first_fails)
    status, body = s.browser.post("/api/restore/apply", {"stage": stage, "mode": "overwrite", "confirm": True})
    assert status == 409 and "新的设置读不了" in body["error"] and body["restarted"] is True
    assert snapshot(s.config.data_dir) == before  # 换回了原来的数据
    assert len(calls) == 2 and s.server.web.app.config is s.config


def test_the_automatic_backup_can_be_shown_but_nothing_else(serve, temp_dir):
    s = serve()
    write(s.config.data_dir, "settings.json", "{}")  # 数据文件夹不是空的：恢复之前会先备份
    stage = staged(s)
    status, body = s.browser.post("/api/restore/apply", {"stage": stage, "mode": "overwrite", "confirm": True})
    assert status == 200
    name = body["result"]["backup_name"]
    assert restore.BEFORE.fullmatch(name)
    folder = s.config.data_dir / "restore-backups"
    assert s.browser.post("/api/restore/reveal", {"name": name}) == (200, {"ok": True, "path": str(folder / name)})
    assert s.revealed[-1] == folder / name
    assert s.browser.post("/api/restore/reveal", {}) == (200, {"ok": True, "path": str(folder)})
    for bad in ("../x", "a.zip", "../../.env", "zhifox-before-restore-20261122-100000.zip/..", 3):
        assert s.browser.post("/api/restore/reveal", {"name": bad})[0] == 400, bad
    assert s.browser.post("/api/restore/reveal", {"name": "zhifox-before-restore-20000101-000000.zip"})[0] == 409  # 样子对，可是没有这个文件
    assert len(s.revealed) == 2
