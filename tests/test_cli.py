"""终端里的斜杠命令的测试。目前只有 /auto：别的命令只是把已有的东西打印出来。"""

from __future__ import annotations

from fake_api import reply, text, tool_use

from harness import cli
from harness.cli import AUTO_NOTES, AUTO_RISK, HELP, handle_command
from harness.permissions import MODES
from harness.ui import TerminalUI


def test_auto_shows_and_changes_how_much_is_asked(start, capsys):
    ui = TerminalUI()
    h = start(ui=ui)

    def run(command: str) -> str:
        assert handle_command(command, h.app, ui) is True  # 返回 True：不是退出
        return capsys.readouterr().out

    # 不带参数：显示现在是哪一种，顺便告诉用户怎么改
    out = run("/auto")
    assert "现在是：每次都问（ask）" in out
    assert all(f"/auto {mode}" in out for mode in MODES)

    out = run("/auto edits")
    assert h.agent.permissions.mode == "edits"
    assert AUTO_NOTES["edits"] in out and AUTO_RISK not in out
    assert "现在是：改工作目录里的文件不用问（edits）" in run("/auto")

    # 全部放开：除了说明改成了什么，还要把风险和关掉的办法说清楚
    out = run("/auto all")
    assert h.agent.permissions.mode == "all"
    assert AUTO_NOTES["all"] in out and AUTO_RISK in out
    assert "/auto ask" in AUTO_RISK and "下次启动" in AUTO_RISK and ".env" in AUTO_NOTES["all"]

    out = run("/auto   ask ")  # 多打了空格也认
    assert h.agent.permissions.mode == "ask"
    assert AUTO_NOTES["ask"] in out and AUTO_RISK not in out

    # 不认识的模式：什么都不改，告诉用户有哪些可以用
    h.agent.permissions.mode = "edits"
    out = run("/auto everything")
    assert h.agent.permissions.mode == "edits"
    assert "没有 everything 这种模式" in out and "ask、edits、all" in out

    assert "/auto" in HELP and HELP in run("/help")


def test_auto_allowed_calls_are_marked_in_the_terminal(start, capsys):
    ui = TerminalUI()
    h = start(
        reply(tool_use("toolu_1", "run_command", {"command": "touch made.txt"}), stop_reason="tool_use"),
        reply(text("建好了。")),
        ui=ui,
    )
    handle_command("/auto all", h.app, ui)
    capsys.readouterr()
    h.agent.run_turn("建个文件")

    out = capsys.readouterr().out
    # 没有出现确认的提问；工具调用下面多了一行"自动允许"，然后才是结果
    assert "助手想要" not in out
    assert out.index("🔧 run_command $ touch made.txt") < out.index("（自动允许）") < out.index("↳")
    # 测试环境不是交互式终端，没有人能确认：要不是自动放行，这条命令是执行不了的
    assert (h.config.workspace / "made.txt").exists()


def test_auto_with_an_argument_is_handled_as_a_command(make_config, monkeypatch, capsys):
    """主循环里，别的命令都不带参数，只有 /auto 可以；长得像但不是它的（/autopilot）照常当作对模型说的话。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    typed = iter(["/auto", "/auto all", "  /auto edits  ", "/autopilot", None])  # None：用户按了 Ctrl-D
    monkeypatch.setattr(TerminalUI, "read_input", lambda self: next(typed))
    sent = []  # 本该发给模型的话记在这里，不真的发出去
    monkeypatch.setattr(cli, "run_turn", lambda agent, ui, text: sent.append((text, agent.permissions.mode)))

    assert cli.run_terminal(make_config()) == 0

    out = capsys.readouterr().out
    assert "现在是：每次都问（ask）" in out and AUTO_NOTES["all"] in out and AUTO_NOTES["edits"] in out
    assert sent == [("/autopilot", "edits")]
