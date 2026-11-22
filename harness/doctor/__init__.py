"""harness doctor（终端里是 /doctor）：一屏看清智狐在这台电脑上的情况。遇到问题时先运行它。

    uv run harness doctor          给人看的表格，跟着界面的语言
    uv run harness doctor --json   给程序看，或者贴给别人帮你排查；里面没有密钥、口令和带口令的网址

为什么要有它：智狐碰到的问题多半不在代码里，而在这台电脑上——密钥只设在终端里、后台服务读不到；数据目录的权限被改宽了；
MCP 的配置写着 npx，这台电脑却没装 Node.js；Linux 上没装 bubblewrap，沙箱一开命令就全被拒绝……这些散在网页的好几个角落里，
有的根本没地方显示。Windows 尤其需要它：开发用的是 Mac，Windows 只在 GitHub 的测试机上跑过，真的 Windows 电脑上有什么不一样，
得靠你运行一下告诉我们。

每一项是三种状态之一：好、有问题（后面跟着怎么修，按这台电脑的系统写：PowerShell 还是 zsh）、没用上（没配置的功能不算问题）。
有一项有问题，退出码就是 1。

规矩（这个文件夹里的每一个检查都守着）：
  - 只看不改：不改任何文件、权限、设置，不启动 MCP 服务器、Chrome，不碰屏幕。数据目录的占用只是"试着锁一下马上放开"。
    目录的检查排在最前面，别的检查也都只读不写，所以看到的权限就是原来的样子，不会被谁顺手改对了。
  - 不发任何网络请求：网络那一节只看设置（代理、改过的接口地址）。
  - 不写出任何密钥：只说有没有、从哪儿来，连最后几位也不写。
  - 判断照着原来的那一处（capabilities.py、model_catalog.py、providers.has_key、各个后端的 detect），不另写一份规矩：
    这里说"能用"的，真用的时候也能用。

文件：
  report.py      结果的格式（Check、Section、Report）和 --json 的样子
  fixes.py       怎么修：按系统写的命令（zsh / bash / PowerShell 的引号、chmod 还是 icacls……）
  environment.py 环境、目录、占用
  keys.py        密钥、网络
  features.py    浏览器、命令的沙箱、操作整台电脑、后台服务
  extensions.py  MCP 服务器、聊天通道、钩子、定时任务
  text.py        给人看的表格
  cli.py         harness doctor 这条命令
"""

from __future__ import annotations

from .. import i18n, instance, machine
from ..channels.store import ChannelStore
from ..config import Config
from ..endpoints import EndpointStore
from ..service import cli as service_cli
from ..settings import Settings
from . import environment, extensions, features, keys
from .report import OK, PROBLEM, UNUSED, Check, Report, Section

__all__ = ["OK", "PROBLEM", "UNUSED", "Check", "Report", "Section", "examine"]


def examine(config: Config, system: service_cli.System | None = None) -> Report:
    """把每一项看一遍。system：后台服务用哪种服务管家（测试给一个假的；不给就看这台电脑）。"""
    # 目录先看：后面读设置、读各个文件的那些类只读不写，但万一哪天有一个顺手建了文件夹、改了权限，这里也已经看过原来的样子了
    folders = environment.folders(config)
    settings = Settings(config.settings_file, config.default_provider, config.default_model)
    endpoints = EndpointStore(config.endpoints_file)
    channels = ChannelStore(config.channels_file)
    tasks, task_problem = extensions.task_list(config)
    running = instance.in_use(config.data_dir)
    elsewhere = features.sandbox_users(settings, tasks, extensions.channel_entries(channels))
    sections = [
        environment.environment(),
        folders,
        environment.occupancy(config),
        keys.keys(config, settings, endpoints),
        features.features(config, settings, elsewhere, system),
        extensions.extensions(config, channels, tasks, task_problem, running),
        keys.network(config),
    ]
    app = {"version": environment.version(), "commit": environment.commit()}
    return Report(sections, app=app, system=machine.system(), language=i18n.current)
