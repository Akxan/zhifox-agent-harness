"""遮住工具结果里像密钥的东西（harness/redact.py）。

一大张表：该遮的（各家密钥的格式、已知的值、password= 后面随机的一串）都遮了，不该遮的（git 的提交号、UUID、
base64 的图片、代码、占位符）原样留着。后面是接进 agent 循环以后的样子：模型、历史、页面、运行记录看到的都是遮好的；
设置里能关；改文件的工具不肯把记号写回去；导出和 MCP 的设置页认密钥用的是同一张表。

表里的密钥都是编出来的：格式对，但不是谁的真密钥。
"""

from __future__ import annotations

import json
import time

import pytest
import test_webui
from fake_api import reply, text, tool_use
from test_agent import tool_results
from test_webui import latest_state

from harness import export, log, redact
from harness.agent import Agent
from harness.mcp import config as mcp_config
from harness.redact import Redactor, mask_shapes, note, secretish
from harness.settings import Settings
from harness.tools.base import ToolOutput
from harness.tools.files import FileEditor
from harness.trace.text import lines

serve = test_webui.serve  # pytest 的 fixture：放在这个模块里就能用，和 test_webui.py 用的是同一个

ANTHROPIC = "sk-ant-api03-" + "Zx9Qw8Er7Ty6Ui5Op4As3Df2Gh1Jk0Lz-Xc_Vb" + "Nm1Qa2Ws3Ed4Rf5Tg6Yh7Uj8Ik9Ol0P"
OPENAI = "sk-proj-" + "Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4zAb7dEf0hIj3kLm6n"
DEEPSEEK = "sk-" + "0123456789abcdef" * 2
GEMINI = "AIza" + "SyDx7Kq2Lm9Np4Rs8Tv1Wy3Za6Bc0Df5Gh3j8"[:35]  # AIza 后面正好 35 个字符
GITHUB = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
GITHUB_PAT = "github_pat_11AB" + "CDEFGH0123456789_abcdefghijKLMNOPQRstuvwxyz"
SLACK = "xoxb-" + "123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx"
TELEGRAM = "7012345678:" + "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw0"
DISCORD = "MTE5ODc2NTQzMjEwOTg3NjU0Mw" + ".GaBcDe.Fg1Hi2Jk3Lm4No5Pq6Rs7Tu8Vw9Xy0Za1Bc2"
AWS = "AKIA" + "Q3VXK7ZP2MN4TR8W"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
PEM = "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEowIBAAKCAQEA7bq9\nQ0FBQUFBQUFB\n-----END RSA " + "PRIVATE KEY-----"

# （原文，原文里要被换掉的那一段，记号上写的名字）
POSITIVES = [
    (f"ANTHROPIC_API_KEY={ANTHROPIC}", ANTHROPIC, "疑似 Anthropic 密钥"),
    (f"key: {ANTHROPIC}\n", ANTHROPIC, "疑似 Anthropic 密钥"),
    (f"export OPENAI_API_KEY='{OPENAI}'", OPENAI, "疑似 OpenAI 密钥"),
    ("legacy sk-" + "Ab12Cd34Ef56Gh78Ij90" + "T3BlbkFJ" + "Kl12Mn34Op56Qr78St90", None, "疑似 OpenAI 密钥"),
    (f"DEEPSEEK_API_KEY={DEEPSEEK}", DEEPSEEK, "疑似 DeepSeek 密钥"),
    ("其他家的 sk-live2024abcdefghijklmnopq", "sk-live2024abcdefghijklmnopq", "疑似 API 密钥"),
    (f'{{"key": "{GEMINI}"}}', GEMINI, "疑似 Google（Gemini）密钥"),
    (f"https://generativelanguage.googleapis.com/v1/models?key={GEMINI}", GEMINI, "疑似 Google（Gemini）密钥"),
    (f"GITHUB_TOKEN={GITHUB}", GITHUB, "疑似 GitHub 令牌"),
    (f"origin https://x-access-token:{GITHUB}@github.com/o/r.git (fetch)", GITHUB, "疑似 GitHub 令牌"),
    (f"token {GITHUB_PAT}", GITHUB_PAT, "疑似 GitHub 令牌"),
    (f"SLACK_BOT_TOKEN={SLACK}", SLACK, "疑似 Slack 令牌"),
    ("xapp-1-" + "A0123456789-1234567890123-abcdef0123456789", None, "疑似 Slack 令牌"),
    (f"https://api.telegram.org/bot{TELEGRAM}/getMe", TELEGRAM, "疑似 Telegram 机器人令牌"),
    (f"DISCORD_BOT_TOKEN={DISCORD}", DISCORD, "疑似 Discord 机器人令牌"),
    (f"aws_access_key_id = {AWS}", AWS, "疑似 AWS 访问密钥"),
    ("ASIA" + "Z7Y6X5W4V3U2T1S0", None, "疑似 AWS 访问密钥"),
    ("sk_live_" + "51HxYzAbCdEfGhIjKlMnOpQr", None, "疑似 Stripe 密钥"),
    (f"Cookie: session={JWT}", JWT, "疑似 JWT 令牌"),
    (f"before\n{PEM}\nafter", PEM, "私钥"),
    ("-----BEGIN OPENSSH " + "PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH " + "PRIVATE KEY-----", None, "私钥"),
    ("-----BEGIN " + "PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\nAAAA", None, "私钥"),  # 被截断了，没有 END
    # 跟在提示词后面、随机的一串
    ("DB_PASSWORD=Summer2024!x", "Summer2024!x", "疑似密码"),
    ('{"password": "c0rrect-H0rse-battery"}', "c0rrect-H0rse-battery", "疑似密码"),
    ("mysql -u root --password=Tr0ub4dor3xyz", "Tr0ub4dor3xyz", "疑似密码"),
    ("client_secret: 9f8e7d6c5b4a39281706f5e4d3c2b1a0", "9f8e7d6c5b4a39281706f5e4d3c2b1a0", "疑似密钥"),
    ("aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYzq8Ztm3KLp", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYzq8Ztm3KLp", "疑似密钥"),
    ("refresh_token: 1//0gXk9a8B7c6D5e4F3g2H1i0J", "1//0gXk9a8B7c6D5e4F3g2H1i0J", "疑似令牌"),
    ('"access_token": "ya29.a0AfH6SMBx3K9q2Lm8Np4Rs7Tv1Wy"', "ya29.a0AfH6SMBx3K9q2Lm8Np4Rs7Tv1Wy", "疑似令牌"),
    ("myapp --api-key a1b2c3d4e5f6g7h8i9j0", "a1b2c3d4e5f6g7h8i9j0", "疑似密钥"),
    ("deploy --token Q2xpZW50S2V5OTg3NjU0MzIx", "Q2xpZW50S2V5OTg3NjU0MzIx", "疑似令牌"),
    ('curl -H "Authorization: Bearer abcDEF1234567890ghijKLmn"', "abcDEF1234567890ghijKLmn", "疑似令牌"),
    ("authorization: Basic dXNlcjpwYXNzd29yZDEyMw==", "dXNlcjpwYXNzd29yZDEyMw==", "疑似令牌"),
    ("postgres://admin:s3cretPass@db.local:5432/app", "s3cretPass", "网址里的密码"),
    ("redis://default:hunter2@cache:6379", "hunter2", "网址里的密码"),
]

NEGATIVES = [
    "commit 3f2a9c1e4b5d6f708192a3b4c5d6e7f8091a2b3c",  # git 的提交号
    "3f2a9c1 修好了登录",  # 短的提交号
    "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "request id 550e8400-e29b-41d4-a716-446655440000",  # UUID
    "token: 550e8400-e29b-41d4-a716-446655440000",  # 提示词后面的 UUID 也不遮
    "data:image/png;base64," + "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk" * 20,
    "QUtJQVEzVlhLN1pQMk1ONFRSOFc=" * 20,  # 一大段 base64（里面编着一个 AWS 的密钥），当作数据不碰
    "password=hunter2",  # 太短
    "password: required",  # 是个词
    "password: ********",
    '"password": "••••"',
    "token = request.headers.get('x-token')",  # 代码
    "api_key = process.env.OPENAI_API_KEY_V2",
    "token = os.environ['GITHUB_TOKEN']",
    "api_key: ${OPENAI_API_KEY}",  # 占位符
    "API_KEY=<your-api-key>",
    "api_key: your-api-key-here-123",
    "secret = GITHUB_TOKEN2",
    "max_tokens: 4096",
    "input_tokens: 128000, output_tokens: 2048",
    "next_page_token: CiAKGjBpNDd2Nmp2Zml2cXRuMThyMW5yMDBvMBAA",  # 翻页用的，模型还要靠它往下翻
    "csrf_token=Zm9vYmFyMTIzNDU2Nzg5MGFi",
    "token_type: bearer",
    "author: Akxan <ak@example.com>",
    "pip install scikit-learn; sk-learn-preprocessing-pipeline",
    "ssh -i ~/.ssh/id_ed25519 git@github.com",
    "git@github.com:Akxan/zhifox-agent-harness.git",
    "http://localhost:8765/path@section",
    "https://user:@example.com",
    "postgres://user:${DB_PASSWORD}@db/app",
    "Bearer realm=example",
    "AKIAIOSFODNN7",  # 不够长
    "the token abcdefghij1234567890 was mentioned in prose",
    "--- a/file.py\n+++ b/file.py\n" + "-" * 3000,
    "价格：$0.0045 / 分钟，Telegram 最多 20 MB",
]


@pytest.mark.parametrize(("source", "secret", "label"), POSITIVES)
def test_secrets_are_masked(source, secret, label):
    done = redact.redact(source)
    assert done.count >= 1, source
    assert redact.mark(label) in done.text, (source, done.text)
    if secret:
        assert secret not in done.text
    assert done.kinds[label] >= 1


@pytest.mark.parametrize("source", NEGATIVES)
def test_ordinary_output_is_left_alone(source):
    done = redact.redact(source)
    assert (done.count, done.text) == (0, source)


def test_the_name_before_a_value_is_kept():
    # 换掉的只是值：哪一项被遮了一眼看得出来，引号原样留着（不然 JSON 就坏了）
    assert redact.redact("DB_PASSWORD=Summer2024!x").text == "DB_PASSWORD=‹已遮住：疑似密码›"
    assert redact.redact('{"password": "c0rrect-H0rse-battery"}').text == '{"password": "‹已遮住：疑似密码›"}'
    assert redact.redact("postgres://admin:s3cretPass@db.local").text == "postgres://admin:‹已遮住：网址里的密码›@db.local"


def test_everything_in_one_output_is_counted_once():
    source = f"{OPENAI}\n{GITHUB}\n{OPENAI}\nDB_PASSWORD=Summer2024!x"
    done = redact.redact(source)
    assert done.count == 4
    assert done.kinds == {"疑似 OpenAI 密钥": 2, "疑似 GitHub 令牌": 1, "疑似密码": 1}
    # 已经换上去的记号，后面的规则认不出来，不会再遮一遍
    assert redact.redact(done.text).count == 0


def test_a_secret_next_to_data_is_still_found():
    image = "data:image/png;base64," + "iVBORw0KGgo" * 40
    done = redact.redact(f"{image} 旁边是 {GITHUB}")
    assert done.count == 1 and image in done.text and GITHUB not in done.text


def test_the_note_tells_the_model_what_happened():
    done = redact.redact(f"{OPENAI} {OPENAI} {GITHUB}")
    said = note(done)
    assert "3 处" in said and "疑似 OpenAI 密钥 2 处" in said and "疑似 GitHub 令牌" in said
    assert "不要把这些记号写回去" in said
    assert note(redact.redact("没什么")) == ""


def test_the_heuristic_thresholds():
    assert secretish("a8f5f167f44f4964e6c998dee827110c")
    assert not secretish("aaaaaaaaaaaa1111")  # 信息量太低
    assert not secretish("abcdefghijklmnop")  # 没有数字
    assert secretish("Summer2024!x", password=True)
    assert not secretish("hunter2", password=True)
    assert redact.entropy("") == 0 and redact.entropy("aaaa") == 0
    assert redact.entropy("abcd") == pytest.approx(2.0)


def test_big_and_nasty_inputs_finish_quickly():
    # 正则写得不好会在这种输入上卡住（回溯爆炸，几分钟都出不来）。平时每一条不到半秒；机器忙（并行跑测试）时慢几倍也不算错
    nasty = [
        "-" * 200_000,
        "a=" * 100_000,
        "password" * 50_000,
        "x" * 300_000 + " token: " + "y" * 300_000,
        ("Bearer " + "A" * 15 + " ") * 30_000,
        "https://" + "a" * 100_000 + ":" + "b" * 100_000,
        "-----BEGIN PRIVATE KEY-----" + "\n" * 100_000,
    ]
    for text_ in nasty:
        started = time.perf_counter()
        redact.redact(text_)
        assert time.perf_counter() - started < 15, text_[:30]


# ---- 已经知道的密钥：按值认 -------------------------------------------------------


def test_known_values_from_the_environment_and_env_file(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "plain-value-without-a-shape-123")
    monkeypatch.setenv("MY_SERVICE_TOKEN", "another-known-value-456")
    monkeypatch.setenv("HOME_FOLDER_SECRET", "/Users/someone/secret")  # 路径不算
    monkeypatch.setenv("TOKEN_SHORT", "abc")  # 太短
    env = tmp_path / ".env"
    env.write_text("export DB_PASSWORD='only-in-dot-env-789'\nHARNESS_WEB_PORT=8765\n", encoding="utf-8")
    redactor = Redactor(env, known=lambda: [("自定义接口的密钥", "endpoint-key-000111")])

    done = redactor("a plain-value-without-a-shape-123 b another-known-value-456 c only-in-dot-env-789 d endpoint-key-000111 e 8765 /Users/someone/secret abc")
    assert done.text == (
        "a ‹已遮住：OPENAI_API_KEY 的值› b ‹已遮住：MY_SERVICE_TOKEN 的值› c ‹已遮住：DB_PASSWORD 的值› "
        "d ‹已遮住：自定义接口的密钥› e 8765 /Users/someone/secret abc"
    )
    assert done.count == 4


def test_values_hidden_from_the_log_are_hidden_from_the_model_too(monkeypatch):
    monkeypatch.setattr(log, "_SECRETS", set())
    log.hide("bot-token-from-a-channel-42")
    assert Redactor()("got bot-token-from-a-channel-42").text == "got ‹已遮住：密钥›"


def test_a_longer_known_value_wins(monkeypatch):
    monkeypatch.setenv("SHORT_TOKEN", "overlap-1234")
    monkeypatch.setenv("LONG_TOKEN", "overlap-1234-and-more")
    assert Redactor()("x overlap-1234-and-more y").text == "x ‹已遮住：LONG_TOKEN 的值› y"


def test_turned_off_means_untouched():
    on = [True]
    redactor = Redactor(enabled=lambda: on[0])
    assert redactor(OPENAI).count == 1
    on[0] = False
    assert (redactor(OPENAI).text, redactor(OPENAI).count) == (OPENAI, 0)


def test_nested_data_is_masked_everywhere():
    state = {"tabs": [{"title": "设置", "url": "https://example.com/cb?access_token=Zx9Qw8Er7Ty6Ui5Op4As"}, {"title": GITHUB, "id": 3}]}
    masked, count = redact.redact_data(state, Redactor())
    assert count == 2
    assert masked == {"tabs": [{"title": "设置", "url": "https://example.com/cb?access_token=‹已遮住：疑似令牌›"}, {"title": "‹已遮住：疑似 GitHub 令牌›", "id": 3}]}


# ---- 接进 agent 循环 ------------------------------------------------------------------


def test_the_model_history_page_and_trace_only_see_the_masked_result(start):
    h = start(
        reply(tool_use("toolu_1", "str_replace_based_edit_tool", {"command": "view", "path": "creds.txt"}), stop_reason="tool_use"),
        reply(text("看完了。")),
    )
    (h.config.workspace / "creds.txt").write_text(f"OPENAI_API_KEY={OPENAI}\nname=demo\n", encoding="utf-8")
    h.agent.run_turn("看看 creds.txt")

    sent = json.dumps(h.api.requests[1], ensure_ascii=False)
    assert OPENAI not in sent  # 发给模型的请求里没有
    (result,) = tool_results(h.api.requests[1])
    assert "OPENAI_API_KEY=‹已遮住：疑似 OpenAI 密钥›" in result["content"]
    assert "name=demo" in result["content"]
    assert "智狐遮住了这段结果里 1 处像密钥的内容" in result["content"]  # 也告诉了模型
    assert OPENAI not in json.dumps(h.agent.messages, ensure_ascii=False)  # 历史里没有（会话文件存的就是它）
    assert all(OPENAI not in shown for shown, _ in h.ui.results)  # 页面上显示的也没有
    span = next(item for item in h.agent.trace.last["spans"] if item["kind"] == "tool")
    assert span["redacted"] == 1
    assert any("遮住了 1 处密钥" in str(line) for line, _ in lines(h.agent.trace.last))  # 终端的 /trace
    assert (h.config.workspace / "creds.txt").read_text(encoding="utf-8").startswith(f"OPENAI_API_KEY={OPENAI}")  # 文件本身不动


def test_values_in_the_dot_env_file_are_known(start):
    h = start(
        reply(tool_use("toolu_1", "str_replace_based_edit_tool", {"command": "view", "path": "notes.txt"}), stop_reason="tool_use"),
        reply(text("好。")),
    )
    h.config.env_file.write_text("CUSTOM_SERVICE_TOKEN=nothing-shaped-but-known-9\n", encoding="utf-8")
    (h.config.workspace / "notes.txt").write_text("the value is nothing-shaped-but-known-9\n", encoding="utf-8")
    h.agent.run_turn("看看")
    (result,) = tool_results(h.api.requests[1])
    assert "‹已遮住：CUSTOM_SERVICE_TOKEN 的值›" in result["content"]
    assert "nothing-shaped-but-known-9" not in result["content"]


def test_turning_it_off_in_settings_passes_results_through(start):
    h = start(
        reply(tool_use("toolu_1", "str_replace_based_edit_tool", {"command": "view", "path": "creds.txt"}), stop_reason="tool_use"),
        reply(text("看完了。")),
    )
    h.app.settings.set_redact(False)
    (h.config.workspace / "creds.txt").write_text(f"{GITHUB}\n", encoding="utf-8")
    h.agent.run_turn("看看")
    (result,) = tool_results(h.api.requests[1])
    assert GITHUB in result["content"] and "已遮住" not in result["content"]
    span = next(item for item in h.agent.trace.last["spans"] if item["kind"] == "tool")
    assert "redacted" not in span


def test_other_runs_share_the_same_redactor(start):
    h = start()
    run = h.app.unattended_agent(h.ui, "ask")
    assert run.agent.redact is h.agent.redact


def test_a_plain_agent_masks_by_default(start):
    h = start()
    agent = Agent(h.agent.backend, h.config, h.agent.tools, h.agent.permissions, h.ui)
    assert agent.redact is not None and agent.redact(GITHUB).count == 1
    text_, state, count = agent._redact(GITHUB, {"url": f"https://x/?token={GITHUB}"})
    assert count == 2 and GITHUB not in text_ and GITHUB not in json.dumps(state)


def test_browser_state_is_masked_before_it_reaches_the_model(start):
    h = start()
    output = ToolOutput("ok", state={"tabs": [{"url": f"https://example.com/?api_key={GEMINI}"}]})
    text_, state, count = h.agent._redact(output.text, output.state)
    assert count == 1 and GEMINI not in json.dumps(state) and text_.startswith("ok")


# ---- 改文件的工具不把记号写回去 -----------------------------------------------------------


def test_the_file_tool_refuses_to_write_masks_back(tmp_path):
    from harness.tools import ToolError

    workspace = tmp_path / "w"
    workspace.mkdir()
    editor = FileEditor(workspace, tmp_path / "backups")
    target = workspace / "config.env"
    target.write_text(f"TOKEN={GITHUB}\nDEBUG=0\n", encoding="utf-8")
    masked = redact.mark("疑似 GitHub 令牌")

    with pytest.raises(ToolError, match="已遮住"):
        editor.run({"command": "create", "path": "config.env", "file_text": f"TOKEN={masked}\nDEBUG=1\n"})
    with pytest.raises(ToolError, match="已遮住"):
        editor.run({"command": "str_replace", "path": "config.env", "old_str": "DEBUG=0", "new_str": f"TOKEN={masked}"})
    with pytest.raises(ToolError, match="已遮住"):
        editor.run({"command": "insert", "path": "config.env", "insert_line": 0, "insert_text": masked})
    assert target.read_text(encoding="utf-8") == f"TOKEN={GITHUB}\nDEBUG=0\n"  # 真正的密钥还在
    # 只改不含记号的部分照样可以
    editor.run({"command": "str_replace", "path": "config.env", "old_str": "DEBUG=0", "new_str": "DEBUG=1"})
    assert target.read_text(encoding="utf-8") == f"TOKEN={GITHUB}\nDEBUG=1\n"


def test_a_file_that_already_has_the_mark_can_still_be_edited(tmp_path):
    workspace = tmp_path / "w"
    workspace.mkdir()
    editor = FileEditor(workspace, tmp_path / "backups")
    source = workspace / "redact_notes.md"
    source.write_text("记号长这样：‹已遮住：私钥›\n", encoding="utf-8")
    editor.run({"command": "str_replace", "path": "redact_notes.md", "old_str": "记号长这样：‹已遮住：私钥›", "new_str": "例子：‹已遮住：私钥›"})
    assert source.read_text(encoding="utf-8") == "例子：‹已遮住：私钥›\n"


# ---- 设置、网页 ---------------------------------------------------------------------------


def test_the_setting_defaults_to_on_and_is_remembered(tmp_path):
    path = tmp_path / "settings.json"
    settings = Settings(path, "anthropic", "claude-opus-5-5")
    assert settings.redact_secrets is True
    settings.save()
    assert "redact_secrets" not in json.loads(path.read_text(encoding="utf-8"))  # 没动过的人，文件和以前一样
    settings.set_redact(False)
    assert json.loads(path.read_text(encoding="utf-8"))["redact_secrets"] is False
    assert Settings(path, "anthropic", "claude-opus-5-5").redact_secrets is False
    path.write_text(json.dumps({"redact_secrets": "no"}), encoding="utf-8")  # 写坏了：按默认的（开着）
    assert Settings(path, "anthropic", "claude-opus-5-5").redact_secrets is True


def test_the_page_can_turn_it_off_and_on(serve):
    s = serve()
    assert latest_state(s.browser)["redact_secrets"] is True
    assert s.browser.post("/api/redact", {"enabled": False}) == (200, {"ok": True})
    assert s.app.settings.redact_secrets is False
    deadline = time.monotonic() + 5
    while latest_state(s.browser)["redact_secrets"] is not False:  # 新的状态随后就到
        assert time.monotonic() < deadline, "状态里没有跟着变"
        time.sleep(0.01)
    assert s.browser.post("/api/redact", {"enabled": True})[0] == 200
    assert s.app.settings.redact_secrets is True
    status, answer = s.browser.post("/api/redact", {"enabled": "off"})
    assert status == 400 and answer["error"]


# ---- 别处认密钥用的是同一张表 ----------------------------------------------------------------


@pytest.mark.parametrize("secret", [OPENAI, ANTHROPIC, GITHUB, GITHUB_PAT, SLACK, GEMINI, AWS, JWT, TELEGRAM, "sk-abcdefghijklmnop"])
def test_exports_and_the_mcp_page_use_the_same_shapes(secret):
    assert secret not in export.mask({"note": f"x {secret} y"})["note"]
    assert secret not in mcp_config.mask_text(f"https://example.com/?x={secret}")
    assert secret not in mask_shapes(secret, "<gone>")
