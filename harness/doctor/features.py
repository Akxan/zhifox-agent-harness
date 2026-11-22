"""自检里看"这台电脑上能不能用"的几样：浏览器、命令的沙箱、操作整台电脑、后台服务。

每一样都照着它自己的模块判断，这里不另写一份规矩：
  - 浏览器：browser/chrome.find_chrome（和真的去打开 Chrome 时找的是同一处，设置里填的路径优先）
  - 沙箱：sandbox/backend.detect().problem()（真的建一个什么都不做的沙箱试一下，和执行命令前的检查一样）
  - 操作整台电脑：desktop/backend.detect() 和后端的 checks()。macOS 上 checks 只调用不弹窗的预检函数
    （CGPreflightScreenCaptureAccess、AXIsProcessTrusted），不请求权限、不改系统设置。用不了的原因和信息栏里的同一句
    （capabilities._NO_DESKTOP）。
  - 后台服务：service/cli.detect() 和服务管家的 state()，说法用 harness service status 的那一句（service/cli._describe）。
    查状态要运行 launchctl list / systemctl --user show / schtasks /Query，都只是看；测试里这些命令一调用就失败，
    那时这一项写"查不了"，不让整个自检崩掉。

开关在设置里关着的，算"没用上"，同时说一句这台电脑上能不能用（以后想打开时心里有数）；开着却用不了的才算"有问题"。
"""

from __future__ import annotations

import os

from .. import machine
from ..browser import chrome
from ..capabilities import _NO_DESKTOP
from ..config import Config
from ..desktop import backend as desktop_backend
from ..i18n import t
from ..sandbox import LEVELS as SANDBOX_LEVELS
from ..sandbox import backend as sandbox_backend
from ..sandbox import windows as sandbox_windows
from ..sandbox.policy import resolve
from ..service import cli as service_cli
from ..service.launchd import Launchd
from ..service.plan import protected_folders, shell_only
from ..service.windows import TaskScheduler
from ..settings import Settings
from . import fixes
from .report import OK, PROBLEM, UNUSED, Check, Section

# ---- 浏览器 ----------------------------------------------------------------------


def browser(settings: Settings) -> Check:
    label = t("浏览器")
    chosen = str(settings.browser.get("path") or "").strip()
    named = os.environ.get(chrome.PATH_ENV, "").strip()
    enabled = bool(settings.browser.get("enabled"))
    found = chrome.find_chrome(chosen)
    facts = {"enabled": enabled, "path": str(found) if found else "", "chosen": chosen, "from_env": bool(named and not chosen)}
    if found is None:
        if chosen:
            detail = t("设置里填的 Chrome 路径不对：{path}", path=chosen)
        elif named:
            detail = t("{name} 指的路径不对：{path}", name=chrome.PATH_ENV, path=named)
        else:
            detail = t("找不到 Chrome。")
        if enabled or chosen or named:  # 打开了，或者特意指了路径：都是想用它的
            return Check("browser", label, PROBLEM, detail, fixes.install_chrome(), facts)
        return Check("browser", label, UNUSED, t("设置里关着。{detail}", detail=detail), facts=facts)
    where = str(found)
    if chrome.needs_headless():
        where = t("{path}（这台电脑没有图形界面，只能用无界面模式）", path=found)
    if not enabled:
        return Check("browser", label, UNUSED, t("设置里关着。找到了 Chrome：{path}", path=where), facts=facts)
    return Check("browser", label, OK, where, facts=facts)


# ---- 命令的沙箱 --------------------------------------------------------------------


def sandbox(settings: Settings, elsewhere: list[str]) -> Check:
    """elsewhere：开着的定时任务、聊天通道里，自己选了沙箱（或者跟着设置走、设置里开着）的那些的名字。"""
    label = t("命令的沙箱")
    found = sandbox_backend.detect()
    problem = found.problem()
    level = settings.sandbox["level"]
    facts = {"level": level, "backend": found.id, "available": problem is None, "elsewhere": len(elsewhere)}
    here = t("这台电脑上能用（{backend}）", backend=t(found.label)) if problem is None else t("这台电脑上用不了：{problem}", problem=problem)
    if level == "off" and not elsewhere:
        return Check("sandbox", label, UNUSED, t("设置里关着。{here}", here=here), facts=facts)
    if problem is not None:
        users: list[str] = [t("对话（“{level}”）", level=t(SANDBOX_LEVELS[level]))] if level != "off" else []
        users += elsewhere
        detail = t("{users} 要用沙箱，可是这台电脑上用不了，命令会被拒绝执行：{problem}", users=t("、").join(users), problem=problem)
        fix: list[str] = [t("修好它，或者先把沙箱关掉：{run}", run=fixes.harness("sandbox", "level", "off"))]
        if machine.system() == machine.WINDOWS:
            fix.insert(0, t(sandbox_windows.NOTE))
        return Check("sandbox", label, PROBLEM, detail, fix, facts)
    detail = t("“{level}” · {backend}", level=t(SANDBOX_LEVELS[level]), backend=t(found.label)) if level != "off" else here
    if elsewhere:
        detail = t("{detail}；在用它的定时任务、聊天通道：{names}", detail=detail, names=t("、").join(elsewhere))
    return Check("sandbox", label, OK, detail, facts=facts)


def sandbox_users(settings: Settings, tasks: list[dict], channels: list[tuple[str, dict]]) -> list[str]:
    """开着的定时任务、聊天通道里，实际会用沙箱的那些（自己选了一档，或者"跟随设置"而设置里开着）。"""
    level = settings.sandbox["level"]
    names = [task["title"] for task in tasks if task["enabled"] and task["kind"] == "agent" and resolve(task["sandbox"], level) != "off"]
    names += [label for label, entry in channels if entry.get("enabled") and resolve(str(entry.get("sandbox") or "default"), level) != "off"]
    return names


# ---- 操作整台电脑 -------------------------------------------------------------------


def computer(settings: Settings) -> Check:
    label = t("操作整台电脑")
    enabled = bool(settings.computer.get("enabled"))
    facts: dict = {"enabled": enabled, "unattended": bool(settings.computer.get("unattended"))}
    found, why = desktop_backend.detect()
    if found is None:
        facts.update(available=False, reason=why)
        reason = t(_NO_DESKTOP.get(why, _NO_DESKTOP[desktop_backend.PLATFORM]))
        if enabled:
            return Check("computer", label, PROBLEM, reason, [t("这台电脑上用不了的话，在“设置 → 操作整台电脑”里把它关掉。")], facts)
        return Check("computer", label, UNUSED, t("设置里关着。{reason}", reason=reason), facts=facts)
    try:
        checks = found.checks()
    except Exception:  # noqa: BLE001 —— 看不出来就当作都没准备好，下面照样说清楚
        checks = {}
    facts.update(available=True, platform=found.platform)
    if found.platform == "macos":
        missing: list[str] = [name for name, key in ((t("录屏与系统录音"), "screen"), (t("辅助功能"), "input")) if not checks.get(key)]
        host = str(checks.get("host") or t("启动智狐的那个程序（终端、iTerm、VS Code……）"))
        fix: list[str] = [t("在“系统设置 → 隐私与安全性”的 {places} 里打开“{host}”，然后重新启动智狐。", places=t("、").join(missing), host=host)] if missing else []
        facts["missing"] = [key for key in ("screen", "input") if not checks.get(key)]
    else:
        keys = [key for key in ("xdotool", "capture", "convert") if not checks.get(key)]
        missing = [{"xdotool": "xdotool", "capture": t("截图的程序（ImageMagick 的 import 或者 scrot）"), "convert": "ImageMagick"}[key] for key in keys]
        fix = [fixes.install_x11_tools(keys)] if keys else []
        facts["missing"] = keys
    if not enabled:
        state = t("这台电脑上能用") if not missing else t("这台电脑上还缺：{missing}", missing=t("、").join(missing))
        return Check("computer", label, UNUSED, t("设置里关着。{state}", state=state), facts=facts)
    if missing:
        return Check("computer", label, PROBLEM, t("打开了，可是还缺：{missing}", missing=t("、").join(missing)), fix, facts)
    detail = t("打开了，定时任务和聊天通道也能用") if facts["unattended"] else t("打开了（只在你看着的对话里）")
    return Check("computer", label, OK, detail, facts=facts)


# ---- 后台服务 ----------------------------------------------------------------------


def service(config: Config, system: service_cli.System | None = None) -> Check:
    """装没装、在不在跑。system 是测试给的假系统；不给就看这台电脑用哪种服务管家。"""
    label = t("后台服务")
    try:
        system = system or service_cli.detect()
        manager = system.manager
        if manager is None:
            return Check("service", label, UNUSED, system.unsupported, facts={"installed": False, "supported": False})
        state = manager.state()
        plan = manager.read_plan()
    except Exception as exc:  # noqa: BLE001 —— 查不了就说查不了，别的检查照常做完
        error = str(exc).strip()[:200] or type(exc).__name__
        detail = t("查不了：{error}", error=error)
        return Check("service", label, PROBLEM, detail, [fixes.harness("service", "status")], {"checked": False})
    title = t(service_cli.TASK_SCHEDULER_TITLE) if isinstance(manager, TaskScheduler) else manager.name
    described = t("{manager}：{state}", manager=title, state=service_cli._describe(state, manager))
    facts = {
        "checked": True, "manager": manager.name, "installed": state.installed, "running": state.running,
        "exit_code": state.exit_code, "signal": state.signal,
    }
    if not state.installed and not state.loaded:
        return Check("service", label, UNUSED, described, facts=facts)
    problems: list[str] = []
    fix: list[str] = []
    if isinstance(manager, TaskScheduler) and not state.loaded:
        problems.append(t("任务计划程序里没有这个任务了"))
        fix.append(fixes.harness("service", "install"))
    elif plan is None:
        problems.append(t("服务的设置看不懂（被手改过？）：{path}", path=manager.file))
        fix.append(fixes.harness("service", "install"))
    else:
        facts["web"] = plan.web
        if plan.data_dir != config.data_dir:
            described = t("{state}；它用的数据目录是 {path}", state=described, path=plan.data_dir)
        missing = shell_only(plan, system.environ)
        if missing:
            # 只在这个终端里设置了的：服务从登录开始运行，读不到它们（和 harness service install 时的提醒是同一条规矩）
            problems.append(t("这些只在终端的环境变量里设置了，后台服务读不到：{names}", names=", ".join(missing)))
            fix.append(t("写进 {env}，再重启服务：{run}", env=plan.env_file, run=fixes.harness("service", "restart")))
    crashed = state.restarting or (not state.running and (state.exit_code not in (None, 0) or state.signal is not None))
    if crashed:
        problems.append(t("它出错退出了"))
        fix.append(t("看看日志里的错误：{run}", run=fixes.harness("service", "logs")))
        if isinstance(manager, Launchd) and plan is not None:
            folders = protected_folders(plan, system.home)
            if folders:
                fix.append(t(service_cli.MAC_FOLDERS, folders=t("、").join(str(folder) for folder in folders), python=os.path.realpath(plan.python)))
    if problems:
        return Check("service", label, PROBLEM, t("{state}。{problems}", state=described, problems=t("；").join(problems)), fix, facts)
    status = OK if state.running else UNUSED  # 装了、你自己停下的：不算问题
    return Check("service", label, status, described, facts=facts)


def features(config: Config, settings: Settings, elsewhere: list[str], system: service_cli.System | None = None) -> Section:
    return Section("features", t("这台电脑上能不能用"), [browser(settings), sandbox(settings, elsewhere), computer(settings), service(config, system)])
