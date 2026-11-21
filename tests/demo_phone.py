"""演示（tests/demo_web.py）里的"手机"：Discord、Slack、飞书、钉钉、QQ 的假服务器共用的一个很简陋的聊天页面。

Telegram 的假服务器自己带着一个（tests/fake_telegram.py 的 PHONE）。别的几家的假服务器都照着 tests/channel_rig.py 的
FakePlatform 写（user_says、sent、is_prompt、press），所以这里一个页面就能对着它们全部：上面选一个平台，
下面就像拿着那个平台的手机：发配对码、聊天、点"允许 / 拒绝"（没有按钮的平台回复"允许"）。只在这台电脑上，
消息不会发到任何真的平台去。
"""

from __future__ import annotations

import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class Phone:
    def __init__(self, fakes: dict[str, Any], labels: dict[str, str]):
        self.fakes = fakes  # 平台 → 它的假服务器
        self.labels = labels
        self.mine: dict[str, list[dict]] = {name: [] for name in fakes}  # 你在每个平台上发过的话（假服务器只记机器人发的）
        self._lock = threading.Lock()

    def start(self, port: int = 0) -> Phone:
        phone = self

        class Handler(BaseHTTPRequestHandler):
            def _json(self, status: int, payload: object) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json; charset=utf-8")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                if self.path.startswith("/log?platform="):
                    self._json(200, phone.log(self.path.split("=", 1)[1]))
                    return
                tabs = "".join(f'<option value="{html.escape(name)}">{html.escape(phone.labels.get(name, name))}</option>' for name in phone.fakes)
                page = PAGE.replace("__TABS__", tabs).encode()
                self.send_response(200)
                self.send_header("content-type", "text/html; charset=utf-8")
                self.send_header("content-length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                fake = phone.fakes.get(body.get("platform"))
                if fake is None:
                    self._json(404, {"ok": False})
                    return
                if self.path == "/say":
                    words = str(body.get("text", ""))
                    with phone._lock:
                        phone.mine[body["platform"]].append({"at": len(fake.sent) - 0.5, "text": words})
                    fake.user_says(words)
                elif self.path == "/press":
                    index = int(body.get("index", -1))
                    if 0 <= index < len(fake.sent):
                        fake.press(fake.sent[index], allow=bool(body.get("allow")))
                self._json(200, {"ok": True})

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        return self

    def log(self, platform: str) -> dict:
        fake = self.fakes.get(platform)
        if fake is None:
            return {"messages": []}
        shown = []
        for index, message in enumerate(list(fake.sent)):
            buttons = platform in ("discord", "slack", "feishu")  # 钉钉、QQ 没有按钮：回复"允许"
            shown.append({"at": index, "text": message.get("plain", ""), "index": index, "prompt": bool(fake.is_prompt(message)) and buttons})
        with self._lock:
            mine = [{"at": item["at"], "text": item["text"], "me": True} for item in self.mine.get(platform, [])]
        return {"messages": sorted(shown + mine, key=lambda item: item["at"])}

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


PAGE = """<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>假的手机 · 智狐演示</title>
<style>
body { margin: 0; font: 15px/1.45 -apple-system, system-ui, sans-serif; background: #e7ebf0; display: flex; justify-content: center; }
main { width: min(440px, 100vw); height: 100vh; display: flex; flex-direction: column; background: #f4f6f8; }
header { display: flex; gap: 10px; align-items: center; padding: 10px 14px; background: #fff; border-bottom: 1px solid #dde2e8; }
header small { flex: 1; color: #7a8591; font-size: 12px; }
select { font: inherit; padding: 4px 8px; }
#log { flex: 1; overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 8px; }
.bubble { max-width: 85%; padding: 8px 11px; border-radius: 12px; background: #fff; white-space: pre-wrap; overflow-wrap: anywhere; box-shadow: 0 1px 1px rgba(0,0,0,.06); }
.me { align-self: flex-end; background: #dcf4c4; }
.keys { display: flex; gap: 6px; margin-top: 8px; }
.keys button { flex: 1; padding: 7px; border: 0; border-radius: 8px; background: #e8f1fb; color: #2a6fb8; font: inherit; cursor: pointer; }
form { display: flex; gap: 8px; padding: 10px; background: #fff; border-top: 1px solid #dde2e8; }
input { flex: 1; padding: 9px 12px; border: 1px solid #cfd6de; border-radius: 18px; font: inherit; }
form button { padding: 0 16px; border: 0; border-radius: 18px; background: #2a8ae6; color: #fff; font: inherit; }
</style>
<main>
<header><select id="platform">__TABS__</select><small>假的手机：只连着本机的演示，消息不会发到任何地方</small></header>
<div id="log"></div>
<form id="form"><input id="text" placeholder="发一条消息（比如配对码）" autocomplete="off"><button>发送</button></form>
</main>
<script>
const log = document.getElementById('log'), text = document.getElementById('text'), platform = document.getElementById('platform');
let drawn = '';
async function refresh() {
  const data = await (await fetch('/log?platform=' + platform.value)).json();
  const print = JSON.stringify(data);
  if (print === drawn) return;
  drawn = print;
  log.replaceChildren(...data.messages.map((message) => {
    const node = Object.assign(document.createElement('div'), { className: 'bubble' + (message.me ? ' me' : ''), textContent: message.text });
    if (message.prompt) {
      const keys = node.appendChild(Object.assign(document.createElement('div'), { className: 'keys' }));
      for (const [label, allow] of [['允许', true], ['拒绝', false]]) {
        const key = keys.appendChild(Object.assign(document.createElement('button'), { textContent: label }));
        key.onclick = () => fetch('/press', { method: 'POST', body: JSON.stringify({ platform: platform.value, index: message.index, allow }) });
      }
    }
    return node;
  }));
  log.scrollTop = log.scrollHeight;
}
platform.onchange = () => { drawn = ''; refresh(); };
document.getElementById('form').onsubmit = async (event) => {
  event.preventDefault();
  if (!text.value.trim()) return;
  await fetch('/say', { method: 'POST', body: JSON.stringify({ platform: platform.value, text: text.value }) });
  text.value = '';
  refresh();
};
setInterval(refresh, 500);
refresh();
</script>
"""
