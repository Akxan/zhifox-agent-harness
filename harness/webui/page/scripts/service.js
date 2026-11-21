// ---- 后台运行 ------------------------------------------------------------
// 设置里"后台运行"一节：智狐现在是作为后台服务开着的（launchd / systemd 管着它：登录时自动启动、出错退出后自己重启），
// 还是跟着启动它的终端（终端关了，网页和聊天通道也跟着停）。跟着终端的时候，给出装成后台服务的那条命令，可以直接复制。
// 是哪一种由助手程序在状态里说（state.service，见 server.py 的 WebApp.service），页面不自己猜；
// 说不清的时候（在终端或者系统服务里开的 harness serve）是 null，这一节不显示。
// 智狐开着的这段时间里它不会变，所以只在打开设置时画一次；换语言时 tel() 画的字会就地重译。

function renderService() {
  const kind = state ? state.service : null;
  const section = $('service-section');
  section.hidden = !kind;
  if (!kind) return;
  const managed = kind === 'launchd' || kind === 'systemd';
  const card = $('service-card');
  card.classList.toggle('on', managed);
  const glyph = el('span', 'service-glyph');
  glyph.appendChild(icon(managed ? 'loop' : 'terminal'));
  const words = el('div', 'service-words');
  if (managed) {
    words.appendChild(tel('b', null, k('作为后台服务运行（{manager}）'), { manager: kind }));
    words.appendChild(tel('p', null, k('登录时自动启动，出错退出后自己重启。关掉这个页面、关掉终端，定时任务和聊天通道都照常运行。')));
    words.appendChild(tel('p', 'service-note', k('要停下、看日志、卸载，在项目文件夹里运行 uv run harness service stop、logs -f、uninstall。')));
  } else {
    words.appendChild(tel('b', null, k('跟着启动它的终端')));
    words.appendChild(tel('p', null, k('终端关了（或者开它的 Claude Code 会话结束了），网页和聊天通道也跟着停。装成后台服务，就会登录时自动启动、出错退出后自己重启：')));
    words.appendChild(serviceCommand('uv run harness service install'));
    words.appendChild(tel('p', 'service-note', k('在项目文件夹里运行。装好以后，用 uv run harness service open 打开网页。')));
  }
  card.replaceChildren(glyph, words);
}

// 一条可以复制的命令。复制的结果写在设置面板底下（窗口里的提示写在窗口里）
function serviceCommand(command) {
  const row = el('div', 'service-command');
  row.appendChild(el('code', null, command));
  const copy = row.appendChild(el('button', 'button with-icon'));
  copy.type = 'button';
  copy.appendChild(icon('copy'));
  copy.appendChild(tel('span', null, k('复制命令')));
  copy.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(command); say(t('已复制。'), 'good'); }
    catch (_) { say(t('没能复制，请手动选中复制。'), 'bad'); }
  });
  return row;
}
