"""同一步里的几个只读调用一起跑。

模型一步里常常要好几样东西："读这五个文件，再列一下那两个文件夹"。一个一个读，要等的是每一次加起来那么久；
可它们谁也不依赖谁（都只是读），同时读的话，要等的只是最慢的那一次。

哪些调用能一起跑
----------------
判断在 agent.py 的 _batch_at，三样都要满足：

  1. 工具自己说这一次只是读（Tool.parallel，见 tools/base.py）。默认不能：新加的工具一律当作会改东西（opt-in）。
     现在说能的：读文件和列目录（文件工具的 view）、看记忆（memory 的 view）、列出待办、列出定时任务、读 Skill。
  2. 根本不用问你：permissions.review 说这一次不用问（和一个一个跑时问不问是同一段代码，这里不另写规矩）。
     本来要问、只是这一档设成了不问的（"只读的命令不用问"里读工作目录之外的文件、"全部自动允许"里的读）也单独跑：
     没问就做的，要先在它的卡片上留下"自动允许"的记录再做（见 permissions.py 的"问不问用户只能放宽，不能绕开"）。
     一起跑的话它会在记录之前就做完，中途停下时还会只做了、没记下。
  3. 没有你的 PreToolUse 钩子要看它（hooks.HookRun.looks_before）：钩子可能要你再确认一次，确认只能一个一个来；
     它要看的也是"马上就要执行这一次"的那一刻，提前替后面几个跑掉就不是那一刻了。
     PostToolUse 钩子不碍事：它在结果出来以后跑，回到这一轮的线程里，照样按调用的顺序一个一个跑。

连着的、三样都满足的调用（至少两个）算一批。别的——要问你的、会改东西的、执行命令、浏览器、操作电脑、MCP、
交给子 Agent、发文件——照旧单独、按顺序、在这一轮的线程里跑，和以前一模一样。所以一步里的调用可能分成几段：
[读, 读] [改文件：先问你] [读, 读, 读]。问你的时候，前一批已经全做完了，后一批还没开始：确认卡片永远不会和别的调用一起出现。

为什么"只是看"还不够
--------------------
rewind.py 也有一张"只是看、没改东西"的名单（倒回一轮之前要不要问你），但"没改东西"不等于"可以同时做"：

  浏览器、操作电脑   一个浏览器、一块屏幕。"滚动"和"截图"都只是看，可谁先谁后，截到的东西完全不同
  交给子 Agent       它本身不改东西（它的每一步另外算），可它自己跑一整个循环：问你、画卡片、挂运行记录，
                     都要在叫它的那个线程里一步一步来
  执行命令           只读的命令（ls、cat）也有它自己的 Shell：工作目录、正在运行的那个进程（"停止"结束的就是它）
  MCP                别人写的程序，它说"只读"我们也验证不了（和 permissions.py 一样不信）

所以这里另起一个开关（Tool.parallel），只有确实能同时跑的才打开。反过来一定成立：能一起跑的都在 rewind 的名单里
（tests/test_parallel_tools.py 对着整个工具箱查一遍）。

怎么跑
------
一个线程池，最多 config.MAX_PARALLEL_TOOLS 个线程（Config.max_parallel_tools，设成 1 就是一个一个跑）。
线程里只做一件事：执行工具本身（tool.run，和一个一个跑时是同一段 Agent._execute）。界面、运行记录、遮密钥、钩子、
结果放进历史，都回到跑这一轮的线程里做：它们本来就只在一个线程里用，不用为了这件事都加上锁。

每个调用在 contextvars.copy_context() 复制的一份里跑：新开的线程看不到这一轮的 ContextVar（撤回文件的记账，
见 undo/journal.py），复制一份它才和在这一轮的线程里跑一样。每个调用各复制一份：同一份 context 不能同时在两个线程里进。

工具自己的锁照旧管用：记忆的 _LOCK 让几次看记忆排队；读文件不用锁（tools/files.py 的 _EDITING 只管改文件）；
待办、定时任务的文件都是先写临时文件再一步换上去，读到的不会是写了一半的。同一批里本来就没有会写的调用。

给人看的仍然一个一个来
----------------------
页面上的卡片、终端里的那几行、"自动允许"的标记，都按调用的顺序一个接一个出现：第一张卡片出现时整批已经开始了，
后面几张往往一出现就已经有了结果。这是有意的：页面靠"结果紧跟在它的调用后面"把结果画进卡片（事件里没有编号），
倒回一轮时也靠这个顺序认哪一步做过什么（rewind._executed），重放存下的会话时看到的和实时的一模一样。
要是谁先做完谁先说，这几处都得改成按编号配对，旧会话也得另外认。同时跑这件事只在运行记录里看得出来：
同一批的几段横条叠在一起，标着"同时执行"（trace/ 的 batch）。

交给模型的结果也按调用的顺序，和一个一个跑时一字不差（两家的适配层都是照着列表的顺序放进历史的）。

停下
----
你点了"停止"（终端里是 Ctrl-C），下一次碰界面时这一轮停下，和别的工具一样。还没开始的取消掉（不再开始），
已经在跑的不等（只是读，跑完把结果扔掉）；没交出结果的调用照样补上"用户中断了操作"（agent.py 的 _run_tools 里的 finally），
历史仍然合法。等某一个调用的时候每隔一会儿醒一下（PARALLEL_TOOLS_POLL_SECONDS）：Windows 上一直等着的线程收不到 Ctrl-C。
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_for
from dataclasses import dataclass
from types import TracebackType
from typing import Self

from .config import PARALLEL_TOOLS_POLL_SECONDS
from .providers.base import ToolCall
from .tools.base import Tool, ToolOutput

THREAD_NAME = "harness-tool"  # 线程池里的线程叫这个名字开头（测试靠名字认出"是不是在线程里跑的"）


@dataclass
class Job:
    """一批里的一个调用。started、finished、output、took 由线程填（填完才算做完）；别的只在这一轮的线程里读。"""

    index: int  # 这一步里的第几个调用
    call: ToolCall
    tool: Tool
    started: float | None = None  # 在线程里真正开始执行的时刻（运行记录的表：Tracer.timer）
    finished: float | None = None  # 执行完的时刻（同一块表）
    output: ToolOutput | None = None
    took: float | None = None  # 执行了多久（秒，交给 PostToolUse 钩子）


class Batch:
    """一批一起跑的调用。用 with：出来的时候（做完了、停下了、出错了），还没开始的取消掉，正在跑的不等。

        with Batch(jobs, execute, workers) as running:
            for job in jobs:
                ...画卡片...
                running.start()    # 第一次调用时把整批交给线程，后面几次什么都不做
                running.wait(job)  # 等这一个做完（做完了马上返回）
                ...交出结果...
    """

    def __init__(self, jobs: list[Job], execute: Callable[[Job], None], workers: int):
        self._jobs = jobs
        self._execute = execute
        # 线程用到时才建（submit 的时候），所以建一个池子不花什么
        self._pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs))), thread_name_prefix=THREAD_NAME)
        self._futures: dict[int, Future[None]] = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None) -> None:
        self.close()

    def start(self) -> None:
        """把整批交给线程池。只在第一张卡片画出来以后才调用：那时你已经点了"停止"的话，画卡片那一步就停下了，一个都不跑。"""
        if self._futures:
            return
        for job in self._jobs:
            # 每个调用各复制一份这一轮的 context（见文件开头），线程里在它里面执行
            context = contextvars.copy_context()
            self._futures[job.index] = self._pool.submit(context.run, self._execute, job)

    def wait(self, job: Job) -> None:
        """等 job 做完。工具里抛出的 Exception 已经在 execute 里变成了出错的结果；KeyboardInterrupt 这类在这里原样抛出来，
        和一个一个跑时一样。每隔 PARALLEL_TOOLS_POLL_SECONDS 醒一下：Windows 上一直等着的线程收不到 Ctrl-C（见文件开头）。"""
        future = self._futures[job.index]
        while not wait_for([future], timeout=PARALLEL_TOOLS_POLL_SECONDS).done:
            pass
        future.result()

    def close(self) -> None:
        """还没开始的取消掉，正在跑的不等（只是读，跑完结果没人要）。做完了的再关一次也没事。"""
        self._pool.shutdown(wait=False, cancel_futures=True)
