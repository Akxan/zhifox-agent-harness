"""操作整台电脑的"问不问你"（permissions.py 的"操作整台电脑"一节）。

用户定的规矩（为什么这样定见 permissions.py 开头）：
  - 默认每一步都问（等一等、看鼠标在哪儿除外）；"只读的命令""改工作目录里的文件"这两档不放开它；
  - 两种办法可以不问：这段对话里说过这一类不再问（截图一类、点击打字一类，分开选，只有你正看着的对话能选），
    或者这一档是"全部自动允许"（对话的、任务自己的、聊天通道的都算）；
  - 放开了也不变的：危险的组合键每次都问（卡片上不给"不再问"，也不放"不想每次都问？"）；不许碰的程序照样拒绝；
  - 没问就做的每一步都要留下记录（auto_allowed 的 why：trusted 是这段对话里放开的，mode 是那一档放开的）。

这里直接给 Facts（"这一步碰的是什么"），测的是规则本身；Facts 是怎么从（假的）屏幕上看出来的，见 test_desktop_unit.py；
在 agent 循环、网页上走一遍的，见 test_desktop_tools.py 和 test_desktop_web.py。
"""

from __future__ import annotations

import pytest
from fake_desktop import FakeDesktop, install

from harness.desktop.keys import DANGEROUS
from harness.desktop.manager import DesktopManager
from harness.desktop.session import TRUSTS, Facts, trust_of
from harness.permissions import MODES, WHY_TRUSTED, Permissions, review_computer
from harness.settings import COMPUTER_DEFAULTS
from harness.tools.computer import DesktopTool
from harness.tools.files import FileEditor

DANGER = DANGEROUS[0][3]  # ⌘Q 的警告
# 每一类有代表性的动作和它们的参数（Facts 的字段）
LOOK_STEPS = {"screenshot": {}, "zoom": {"region": (0, 0, 10, 10)}}
INPUT_STEPS = {
    "click": {"point": (10, 10)},
    "drag": {"point": (1, 1), "end": (9, 9)},
    "move": {"point": (3, 3)},
    "press": {},
    "release": {},
    "scroll": {"direction": "down", "amount": 2},
    "type": {"text": "hi"},
    "key": {"text": "ctrl+s"},
    "hold": {"text": "shift", "duration": 0.0},
}
TRUST_STATES = {"none": set(), "screenshots": {"screenshots"}, "actions": {"actions"}, "both": {"screenshots", "actions"}}


@pytest.fixture
def permissions(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    return Permissions(FileEditor(workspace, tmp_path / "backups"))


def tool(facts: Facts) -> DesktopTool:
    return DesktopTool(spec={"name": "computer_probe"}, stands_in_for="computer", run=lambda tool_input: "ok", facts=lambda tool_input: facts)


def outcome(permissions: Permissions, facts: Facts) -> tuple[str | None, str | None]:
    """（问题，为什么没问）：问题是 None 表示根本不用问；为什么没问是 None 表示要问你。和 agent.py 走的是同一条路。"""
    probe = tool(facts)
    question = permissions.review(facts.action, {}, probe)
    return question, permissions.allowance(facts.action, {}, probe) if question is not None else None


def facts_for(action: str, trusted: set[str], attended: bool = True, **extra) -> Facts:
    fields = {**LOOK_STEPS, **INPUT_STEPS}[action]
    return Facts(action, trusted=trust_of(action) in trusted, attended=attended, **{**fields, **extra})


# ---- 每一档 × 放开了什么 × 每一类动作 ----------------------------------------------------------


@pytest.mark.parametrize("mode", list(MODES))
@pytest.mark.parametrize("trust", list(TRUST_STATES))
@pytest.mark.parametrize("action", [*LOOK_STEPS, *INPUT_STEPS])
def test_every_mode_and_trust(permissions, mode, trust, action):
    """不问的只有两种：这一类在这段对话里放开了（why 是 trusted），或者这一档是"全部自动允许"（why 是 mode）。"""
    permissions.mode = mode
    trusted = trust_of(action) in TRUST_STATES[trust]
    question, why = outcome(permissions, facts_for(action, TRUST_STATES[trust]))
    assert question is not None  # 这一步本来就要问（没放开的时候卡片上写的就是它）
    if trusted:
        assert why == WHY_TRUSTED
    elif mode == "all":
        assert why == "mode"
    else:
        assert why is None, f"{mode} 这一档不该放开 {action}"


def test_the_two_kinds_of_trust_are_separate(permissions):
    """截图一类、点击打字一类各管各的：放开了截图，点击照样问；放开了点击打字，截图照样问。"""
    assert outcome(permissions, facts_for("click", {"screenshots"}))[1] is None
    assert outcome(permissions, facts_for("screenshot", {"actions"}))[1] is None
    assert outcome(permissions, facts_for("zoom", {"screenshots"}))[1] == WHY_TRUSTED
    assert outcome(permissions, facts_for("type", {"actions"}))[1] == WHY_TRUSTED


def test_waiting_and_the_pointer_never_ask(permissions):
    for action in ("wait", "cursor"):
        assert permissions.review(action, {}, tool(Facts(action))) is None


# ---- 放开了也不变的 ---------------------------------------------------------------------


@pytest.mark.parametrize("mode", list(MODES))
@pytest.mark.parametrize("trust", list(TRUST_STATES))
@pytest.mark.parametrize("action", ["key", "hold"])
def test_dangerous_keys_ask_in_every_mode_and_with_any_trust(permissions, mode, trust, action):
    permissions.mode = mode
    facts = facts_for(action, TRUST_STATES[trust], text="cmd+q", danger=DANGER)
    question, why = outcome(permissions, facts)
    assert question is not None and why is None
    assert "⌘Q" in question
    assert getattr(question, "always", False) is True  # 页面不放"不想每次都问？"：换哪一档都照样问
    assert getattr(question, "offer", "") == ""  # 也不给"这段对话里不再问"


@pytest.mark.parametrize("mode", list(MODES))
def test_a_blocked_app_is_never_asked_about(permissions, mode):
    """碰到不许碰的程序：不问（工具会直接拒绝，见 test_desktop_tools.py），在任何模式下都一样。"""
    permissions.mode = mode
    for action in ("screenshot", "click", "type"):
        assert permissions.review(action, {}, tool(facts_for(action, {"screenshots", "actions"}, blocked="1Password"))) is None


def test_trust_from_a_run_nobody_watches_does_not_count(permissions):
    """定时任务、聊天通道的运行（attended 是 False）：哪怕 trusted 被谁设上了，也不算数，照样问。"""
    for action in ("screenshot", "click", "type"):
        question, why = outcome(permissions, facts_for(action, {"screenshots", "actions"}, attended=False))
        assert question is not None and why is None


# ---- 卡片上画什么 ----------------------------------------------------------------------


@pytest.mark.parametrize("action", [*LOOK_STEPS, *INPUT_STEPS])
def test_the_card_offers_the_matching_trust_only_where_someone_watches(action):
    question = review_computer(facts_for(action, set()))
    assert getattr(question, "offer", "") == ("screenshots" if action in LOOK_STEPS else "actions")
    assert not getattr(question, "always", False)  # 换成"全部自动允许"就不问了：可以放"不想每次都问？"
    unattended = review_computer(facts_for(action, set(), attended=False))
    assert unattended is not None and getattr(unattended, "offer", "") == ""


# ---- DesktopSession：只有你看着的对话能放开 ---------------------------------------------------------


class Settings:
    def __init__(self):
        self.computer = {**COMPUTER_DEFAULTS, "enabled": True}


def manager(tmp_path) -> DesktopManager:
    found = DesktopManager(tmp_path / "desktop", Settings())
    install(found, FakeDesktop())
    return found


def test_only_the_watched_conversation_can_be_trusted(tmp_path):
    desktop = manager(tmp_path)
    chat = desktop.session("你的对话", attended=True)
    task = desktop.session("一个定时任务")
    assert set(TRUSTS.values()) == {"screenshots", "actions"}
    assert chat.trust("actions") and chat.trust_actions and not chat.trust_screenshots
    assert chat.trust("screenshots") and chat.trust_screenshots
    assert not chat.trust("everything") and not chat.trust("")
    for word in ("screenshots", "actions"):
        assert task.trust(word) is False
    assert not task.trust_screenshots and not task.trust_actions
    # 直接改字段也不行：没人看着的运行，trusts 一律说"没放开"
    task.trust_actions = task.trust_screenshots = True
    assert not task.trusts("click") and not task.trusts("screenshot")
    assert not task.facts("click", point=(5, 5)).trusted and not task.facts("click", point=(5, 5)).attended


def test_the_facts_carry_which_kind_is_trusted(tmp_path):
    desktop = manager(tmp_path)
    chat = desktop.session("你的对话", attended=True)
    assert chat.facts("click", point=(5, 5)).attended and not chat.facts("click", point=(5, 5)).trusted
    chat.trust("actions")
    assert chat.facts("click", point=(5, 5)).trusted and chat.facts("type", text="hi").trusted
    assert not chat.facts("screenshot").trusted
    chat.trust("screenshots")
    assert chat.facts("zoom", region=[0, 0, 10, 10]).trusted
    assert not chat.facts("wait", duration=0).trusted  # 什么都不碰的不归哪一种管
    chat.untrust()
    assert not chat.trust_actions and not chat.trust_screenshots and not chat.facts("click", point=(5, 5)).trusted
