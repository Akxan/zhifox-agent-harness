"""记忆窗口：查看、修改、删除一条记忆，在文件管理器里显示（文件的读写和路径检查见 ../memories.py）。

  POST /api/memory/read      读一条（只是读，对话进行中也可以看）
  POST /api/memory/save      改内容和名字（modified 对不上、又没有 force，回答 409 和 conflict）
  POST /api/memory/delete    删一条
  POST /api/memory/show      在文件管理器里显示一条记忆的文件
  POST /api/memory/reveal    打开记忆文件夹（请求里带什么都不看）
  POST /api/memory/refresh   页面回到前台：文件夹在这期间变了的话，发一次新的状态

MemoryActions 混进 WebApp（见 webapp.py）：只有方法，用到的状态都在 WebApp.__init__ 里。
改和删拿对话的锁：模型可能正读着这条记忆、马上要改其中一段（原因见 delete_memory）。

route_* 由 server.py 的 Handler._route 调用（写法和约定见 server.py 开头）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import memories
from ..i18n import t
from ..memories import MemoryConflict, MemoryFileError
from ..safe import private_dir
from . import reveal
from .webapp_types import WebAppPart

if TYPE_CHECKING:
    from .server import Handler
    from .webapp import WebApp


class MemoryActions(WebAppPart):
    """记忆：改、删、显示，文件夹变了没有。"""

    def delete_memory(self, relative: str) -> str:
        """删掉一条记忆。成功返回空字符串，否则返回原因。"""
        # 对话进行中不让删。模型可能正在改这条记忆：先 view 读出来，再 str_replace 改其中一段。
        # 这时删掉它，模型的下一步会莫名其妙地失败；或者它紧接着用 create 把刚读到的内容又写回去，
        # 你以为删掉了，其实它又回来了。和删除会话一样，等这一轮结束再删。查看和打开文件夹只是读，不受限制。
        if not self._turn.acquire(blocking=False):
            return self._busy()
        try:
            try:
                memories.delete(self.app.config.memory_dir, relative)
            except MemoryFileError as exc:
                return str(exc)
            self.publish_state()  # 列表跟着状态走：删完马上发一次
        finally:
            self._turn.release()
        return ""

    def save_memory(self, relative: str, name: str, text: str, modified: float | None, force: bool) -> tuple[str, str, bool]:
        """改一条记忆的内容和名字。返回（失败原因，改完之后的路径，是不是因为"这期间被改过了"才没保存）。

        和删除一样，对话进行中不让改：模型可能正读着它、马上要改其中一段，这时把内容换掉，它的下一步会对不上。
        """
        if not self._turn.acquire(blocking=False):
            return self._busy(), "", False
        try:
            try:
                saved = memories.save(self.app.config.memory_dir, relative, name, text, modified, force)
            except MemoryConflict as exc:
                return str(exc), "", True
            except MemoryFileError as exc:
                return str(exc), "", False
            self.publish_state()
        finally:
            self._turn.release()
        return "", saved, False

    def show_memory(self, relative: str) -> tuple[str, str]:
        """在文件管理器里显示一条记忆的文件（macOS 上是在访达里选中它）。返回（失败原因，显示的路径）。"""
        try:
            target = memories.file_of(self.app.config.memory_dir, relative)
        except MemoryFileError as exc:
            return str(exc), ""
        try:
            reveal.reveal_in_file_manager(target)
        except OSError as exc:
            return t("没能打开文件管理器（{error}）。文件在：{path}", error=exc, path=target), ""
        return "", str(target)

    def refresh_memories(self) -> None:
        """页面刚连上、或者回到前台时问一声：记忆文件夹在这期间有没有变。

        模型改的，下一次发状态时自然就带上了；可你自己在访达里删的、改的，程序不会知道，页面上的列表就旧了，
        点开一条才发现"不存在"。所以页面在这两个时刻来问。变了就发一次新的状态，列表照常跟着状态走；
        没变就什么都不发，免得每切换一次窗口，日志里就多一张状态快照。
        """
        if memories.list_files(self.app.config.memory_dir) != self._memories_shown:
            self.publish_state()

    def reveal_memories(self) -> tuple[str, str]:
        """在文件管理器里打开记忆文件夹。返回（失败原因，打开的路径）。

        和 reveal_conversation 一样：路径是程序自己定的（配置里的记忆文件夹），浏览器什么都指定不了；
        只是打开来看看，所以不拿对话的锁。文件夹还不存在的话（被你删了，或者换了数据目录）先建一个空的，
        不然点了没反应，也不知道该去哪儿找。
        """
        folder = self.app.config.memory_dir
        try:
            private_dir(folder)  # 和数据目录里别的文件夹一样，只有你自己的账号能进
            reveal.reveal_in_file_manager(folder)
        except OSError as exc:
            return t("没能打开文件管理器（{error}）。文件在：{path}", error=exc, path=folder), ""
        return "", str(folder)


def route_memory(handler: Handler, web: WebApp, path: str, body: dict) -> None:
    action, relative = path.removeprefix("/api/memory/"), body.get("path")
    if action == "refresh":
        web.refresh_memories()
        handler._send_json(200, {"ok": True})
    elif action == "reveal":
        error, shown = web.reveal_memories()  # 请求里带什么都不看：打开哪个文件夹由服务器定
        if error:
            handler._send_json(409, {"error": error})
        else:
            handler._send_json(200, {"ok": True, "path": shown, "opened": reveal.has_file_manager()})
    elif action not in ("read", "save", "delete", "show") or not isinstance(relative, str):
        handler._send_json(400, {"error": t("参数不正确")})
    elif action == "delete":
        handler._finish(web.delete_memory(relative))
    elif action == "show":
        error, shown = web.show_memory(relative)
        if error:
            handler._send_json(409, {"error": error})
        else:
            handler._send_json(200, {"ok": True, "path": shown, "opened": reveal.has_file_manager()})
    elif action == "save":
        name, text, modified, force = body.get("name"), body.get("text"), body.get("modified"), body.get("force", False)
        number = isinstance(modified, (int, float)) and not isinstance(modified, bool)
        if not isinstance(name, str) or not isinstance(text, str) or not number or not isinstance(force, bool):
            handler._send_json(400, {"error": t("参数不正确")})
        else:
            error, saved, conflict = web.save_memory(relative, name, text, float(modified), force)
            if error:
                handler._send_json(409, {"error": error, "conflict": conflict})
            else:
                handler._send_json(200, {"ok": True, "path": saved})
    else:
        # 只是读，对话进行中也可以看。模型正在改的话，看到的是改之前或之后的样子，不会是半截：
        # 记忆工具改文件时先写好一个临时文件，再一步换上去
        try:
            handler._send_json(200, {"ok": True, **memories.read(web.app.config.memory_dir, relative)})
        except MemoryFileError as exc:
            handler._send_json(409, {"error": str(exc)})
