// ---- 提示和请求 ----------------------------------------------------------

let toastTimer = 0;
function toast(text) {
  const box = $('toast');
  box.textContent = text;
  box.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => box.classList.remove('show'), Math.min(8000, Math.max(2600, text.length * 130)));
}

function showBanner(text) {  // text 是原文，这样横幅挂着的时候换语言，它也跟着换
  retext(banner, text);
  banner.hidden = false;
}

// 向助手程序发一个请求。返回 {ok, error, warning, data}，不会抛异常。data 是服务器回答的内容。
async function call(path, data) {
  try {
    const response = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Harness-Token': token },
      body: JSON.stringify(data || {}),
    });
    const reply = await response.json().catch(() => ({}));
    if (response.ok) banner.hidden = true;  // 请求通了，说明助手程序还在，之前"连接不上"的提示可以收起来
    // 403：页面手里的口令已经失效，几乎都是因为助手程序重启过（每次启动都换一个口令）。告诉用户该做什么，
    // 而不是只给一个状态码
    const expired = response.status === 403 && !reply.error;
    if (expired) showBanner(k('连接已断开：助手程序可能已经退出或重新启动了。请回到终端，用那里显示的新地址打开。'));
    return {
      ok: response.ok,
      error: response.ok ? '' : expired ? t('链接无效或已经过期。请使用终端里显示的完整地址重新打开。')
        : reply.error || t('请求失败（{status}）', { status: response.status }),
      warning: reply.warning || '',
      data: reply,
    };
  } catch (_) {
    showBanner(k('连接不上助手程序。它可能已经退出，请回到终端重新运行。'));
    return { ok: false, error: t('连接不上助手程序'), warning: '', data: {} };
  }
}

// 对话区里用的：失败时在页面底部弹出原因
async function post(path, data) {
  const result = await call(path, data);
  if (!result.ok) toast(result.error);
  return result.ok;
}

// 给读屏软件的播报（页面上看不见）：一段回复写完了、助手要你确认了。只播实时发生的，重放旧内容时不播。
// 先清空、下一帧再写，同样的话连说两次也会再读一遍
function announce(text) {
  if (!isLive() || !text) return;
  const box = $('announce');
  box.textContent = '';
  requestAnimationFrame(() => { box.textContent = text.length > 1200 ? text.slice(0, 1199) + '…' : text; });
}

function refreshStatus() {
  const label = busy ? k('停止') : k('发送');
  sendButton.classList.toggle('stop', busy);
  sendButton.disabled = !busy && !input.value.trim() && !pending.length;  // 只带附件、不写字也能发
  tattr(sendButton, 'title', label);
  tattr(sendButton, 'aria-label', label);
  newButton.disabled = $('new-compact').disabled = $('persona-open').disabled = busy;
  body.classList.toggle('busy', busy);
  working.classList.toggle('on', busy);
  working.classList.toggle('waiting', busy && !stopping && confirms.size > 0);
  if (busy) retext($('working-text'), stopping ? k('正在停止') : confirms.size ? k('等待你确认') : k('助手正在工作'));
  else { delete $('working-text').dataset.i18n; $('working-text').textContent = ''; }
}
