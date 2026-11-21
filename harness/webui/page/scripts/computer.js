// ---- 操作整台电脑 ----------------------------------------------------------
// 进阶选项（见 harness/desktop/）：助手看你的整个屏幕、动你的鼠标键盘。这一节管页面上和它有关的几样东西：
//   顶栏上的提示   有运行正在操作电脑时出现："智狐正在控制这台电脑"，旁边一颗"停止"。停下以后那个运行这一轮里不能再动电脑
//   对话里的一步   和浏览器一样：工具卡片下面一张小截图，点开看大图（大图问 /api/computer/shot 要）
//   确认卡片       截图的卡片上多一个"这段对话里截图不再问"（见上面 handlers.confirm）
//   设置里的一节   开关（打开之前先在原地讲清楚风险、勾了"我明白"才能打开）、权限和要装的程序检查好了没有、
//                  定时任务和聊天通道能不能用、不许碰的程序
// 开没开、能不能用、权限、截图多大、留几天……都由助手程序在状态里给（state.computer），页面不另算一份。

let computerStatus = null;     // 状态里最新的那一份
let computerDrawn = '';        // 设置里那一节上一次按什么画的（没变就不重画，保留焦点和写了一半的名字）
const isComputerCall = (name) => typeof name === 'string' && /^computer[._]/.test(name);
const computerShotDays = () => (computerStatus && computerStatus.shot_days) || 7;
// 谁占着屏幕（程序给的是原文，见 harness/desktop/manager.py）。你自己的对话不用多说
const COMPUTER_WHO = { [k('一个定时任务')]: k('（一个定时任务）'), [k('聊天通道里的一个聊天')]: k('（聊天通道里的一个聊天）') };
const COMPUTER_REASONS = {
  wayland: k('这台电脑用的是 Wayland 桌面：Wayland 不允许一个程序截别的程序的图、替别人点鼠标，所以用不了。登录时换成 X11（Xorg）桌面就可以。'),
  no_display: k('这台电脑没有图形界面（比如在服务器上），没有屏幕可以操作。'),
  platform: k('只能在 macOS 和 Linux（X11 桌面）上用，这台电脑的系统不支持。'),
};

function renderComputer(status, force) {
  if (!status) return;
  computerStatus = status;
  const live = $('computer-live');
  const wasHidden = live.hidden;
  live.hidden = !status.active;
  live.classList.toggle('busy', Boolean(status.busy));
  const who = COMPUTER_WHO[status.who] || '';
  if (who) retext($('computer-live-text'), k('智狐正在控制这台电脑{who}'), { who: t(who) });
  else retext($('computer-live-text'), k('智狐正在控制这台电脑'));
  const stop = $('computer-live-stop');
  stop.replaceChildren(icon('stop'), tel('span', null, k('停止')));
  tattr(stop, 'title', k('马上停下，这一轮里不再动这台电脑'));
  tattr(stop, 'aria-label', k('马上停下，这一轮里不再动这台电脑'));
  if (wasHidden && !live.hidden && !isLive()) live.style.animation = 'none';  // 重放旧内容时不播入场动画
  else if (!live.hidden) live.style.animation = '';
  if (settings.open) drawComputerSettings(force);
}

$('computer-live-stop').addEventListener('click', async () => {
  const button = $('computer-live-stop');
  button.disabled = true;
  const reply = await call('/api/computer/stop', {});
  button.disabled = false;
  toast(reply.ok ? t('已经停下：这一轮里智狐不会再动这台电脑。') : reply.error);
});

async function setComputer(changes, done) {
  const reply = await call('/api/computer/settings', changes);
  if (!reply.ok) { say(reply.error, 'bad'); return false; }  // 设置窗口开着：提示写在窗口里
  say(reply.warning || done, reply.warning ? 'bad' : 'good');
  return true;
}

// 打开之前的那一步：在原地讲清楚风险，勾了"我明白"才能点"打开"。Esc、"取消"都收起它，焦点回到开关上
function showComputerConfirm(open) {
  const box = $('computer-confirm');
  const hadFocus = box.contains(document.activeElement);
  box.hidden = !open;
  $('computer-ack').checked = false;
  $('computer-confirm-yes').disabled = true;
  $('computer-enabled').setAttribute('aria-expanded', String(open));
  if (open) $('computer-ack').focus();
  else if (hadFocus) $('computer-enabled').focus();  // 要藏起来的那一块正拿着焦点：先把焦点挪到看得见的开关上
}
$('computer-ack').addEventListener('change', () => { $('computer-confirm-yes').disabled = !$('computer-ack').checked; });
$('computer-confirm-no').addEventListener('click', () => showComputerConfirm(false));
$('computer-confirm').addEventListener('keydown', (key) => {
  if (key.key === 'Escape') { key.preventDefault(); key.stopPropagation(); showComputerConfirm(false); }
});
$('computer-confirm-yes').addEventListener('click', async () => {
  if (!$('computer-ack').checked) return;
  $('computer-confirm-yes').disabled = true;
  const done = await setComputer({ enabled: true, acknowledge: true }, t('已经打开：每一步都会先问你。'));
  if (done) showComputerConfirm(false); else $('computer-confirm-yes').disabled = false;
});
$('computer-enabled').addEventListener('click', async () => {
  const on = $('computer-enabled').getAttribute('aria-checked') === 'true';
  if (!on) { showComputerConfirm($('computer-confirm').hidden); return; }  // 关着：先讲风险（再点一下开关就收起）
  await setComputer({ enabled: false }, t('已经关掉了“操作整台电脑”。'));
});
$('computer-unattended').addEventListener('click', async () => {
  const on = $('computer-unattended').getAttribute('aria-checked') !== 'true';
  await setComputer({ unattended: on }, on ? t('定时任务和聊天通道也能操作电脑了：每一步照样要你批准。') : t('定时任务和聊天通道不能再操作电脑。'));
});

function drawComputerSettings(force) {
  const status = computerStatus;
  if (!status) return;
  const print = JSON.stringify([status, language]);
  if (!force && print === computerDrawn) return;
  computerDrawn = print;

  const enabled = $('computer-enabled');
  enabled.setAttribute('aria-checked', String(Boolean(status.enabled)));
  enabled.disabled = !status.supported && !status.enabled;
  $('computer-card').classList.toggle('on', Boolean(status.enabled));
  if (status.enabled && !$('computer-confirm').hidden) showComputerConfirm(false);
  retext($('computer-state'), !status.supported ? k('（这台电脑上用不了）') : !status.enabled ? k('（关着）') : status.busy ? k('（正在操作）') : k('（开着，每一步都会问你）'));
  const unattended = $('computer-unattended');
  unattended.setAttribute('aria-checked', String(Boolean(status.unattended)));
  unattended.disabled = !status.enabled;

  drawComputerChecks(status);
  drawComputerBlocked(status);

  // 每一家怎么用（程序按 vision 算好的，见 harness/webui/computer_api.py）。由几句拼成：换语言时整段重画
  const providers = status.providers || {};
  const names = (kind) => (state && state.providers ? state.providers : [])
    .filter((one) => providers[one.id] === kind).map((one) => one.label.replace(/\s*\(.*\)$/, ''));
  const parts = [];
  if (names('toolset').length) parts.push(t('{names} 用 Anthropic 专门训练过的电脑工具包。', { names: names('toolset').join(t('、')) }));
  if (names('standin').length) parts.push(t('{names} 用智狐自己写的替身工具（没有专门训练过，点得没那么准）。', { names: names('standin').join(t('、')) }));
  if (names('none').length) parts.push(t('{names} 看不了图片，用不了。', { names: names('none').join(t('、')) }));
  const models = $('computer-models');
  delete models.dataset.i18n;
  models.textContent = parts.join(language === 'zh' ? '' : ' ');
  const image = status.image ? t('交给模型的截图是 {width}×{height}，', { width: status.image[0], height: status.image[1] }) : '';
  retext($('computer-cost'), k('用 Claude 时，工具包的说明书每次请求约 {toolset} 个 token（命中缓存时很便宜）；{image}每张截图大约 {tokens} 个 token。截图的大图在这台电脑上保留 {days} 天。'),
    { toolset: Number(status.toolset_tokens || 0).toLocaleString(LOCALES[language]), image, tokens: Number(status.shot_tokens || 0).toLocaleString(LOCALES[language]), days: status.shot_days || 7 });
}

// 这台电脑上能不能用：macOS 上两样权限（屏幕录制、辅助功能），Linux 上两样程序（xdotool、ImageMagick）。
// 没有的写清楚去哪儿打开、怎么装；智狐自己从不请求权限、不装任何东西（"打开系统设置"只是打开那一页给你看）
function drawComputerChecks(status) {
  const box = $('computer-checks');
  const hadFocus = box.contains(document.activeElement) ? document.activeElement.dataset.pane : null;
  const rows = [];
  const row = (good, title, note, values, button) => {
    const item = el('div', 'computer-check ' + (good ? 'good' : 'bad'));
    item.appendChild(icon(good ? 'check' : 'alert'));
    const words = item.appendChild(el('div', 'computer-check-words'));
    words.appendChild(tel('b', null, title));
    if (note) words.appendChild(tel('span', null, note, values));
    if (button) item.appendChild(button);
    rows.push(item);
  };
  const checks = status.checks || {};
  if (!status.supported) {
    row(false, k('这台电脑上用不了'), COMPUTER_REASONS[status.reason] || COMPUTER_REASONS.platform);
  } else if (status.platform === 'macos') {
    const host = checks.host || t('启动智狐的那个程序（终端、iTerm、VS Code……）');
    const open = (pane) => {
      if (!status.can_open_settings) return null;
      const button = tel('button', 'mini', k('打开系统设置'));
      button.type = 'button';
      button.dataset.pane = pane;
      button.addEventListener('click', async () => {
        const reply = await call('/api/computer/permission', { pane });
        if (!reply.ok) say(reply.error, 'bad');
      });
      return button;
    };
    row(Boolean(checks.screen), checks.screen ? k('截图：有“屏幕录制”的权限') : k('截图：还没有“屏幕录制”的权限'),
      checks.screen ? '' : k('在“系统设置 → 隐私与安全性 → 录屏与系统录音”（旧一些的系统里叫“屏幕录制”）里打开“{host}”，然后重新启动智狐。'), { host }, checks.screen ? null : open('screen'));
    row(Boolean(checks.input), checks.input ? k('鼠标键盘：有“辅助功能”的权限') : k('鼠标键盘：还没有“辅助功能”的权限'),
      checks.input ? '' : k('在“系统设置 → 隐私与安全性 → 辅助功能”里打开“{host}”，然后重新启动智狐。'), { host }, checks.input ? null : open('input'));
  } else {
    const imaging = Boolean(checks.capture && checks.convert);
    const missing = [checks.xdotool ? '' : 'xdotool', imaging ? '' : 'imagemagick'].filter(Boolean);
    row(Boolean(checks.xdotool), checks.xdotool ? k('鼠标键盘：装了 xdotool') : k('鼠标键盘：还没有装 xdotool'), '');
    row(imaging, imaging ? k('截图：装了 ImageMagick') : k('截图：还没有装 ImageMagick'), '');
    if (missing.length) {
      const item = el('div', 'computer-check');
      item.appendChild(icon('terminal'));
      const words = item.appendChild(el('div', 'computer-check-words'));
      words.appendChild(tel('b', null, k('在终端里装上它们（Debian、Ubuntu）：')));
      words.appendChild(el('code', null, 'sudo apt install ' + missing.join(' ')));
      rows.push(item);
    }
  }
  box.replaceChildren(...rows);
  if (hadFocus) { const again = box.querySelector('[data-pane="' + hadFocus + '"]'); if (again) again.focus(); }
}

// 不许碰的程序：一个个小标签，× 去掉；下面填名字、"添加"。最前面是它们的时候，智狐不截图也不操作
function drawComputerBlocked(status) {
  const list = $('computer-chips');
  const hadFocus = list.contains(document.activeElement) ? document.activeElement.dataset.name : null;
  const blocked = status.blocked || [];
  retext($('computer-blocked-note'), status.platform === 'x11'
    ? k('最前面的窗口是这些程序的时候，智狐不截图、也不操作。按窗口的类名或标题里的字认，不分大小写。')
    : k('最前面是这些程序、或者点的地方是它们的窗口时，智狐不截图、也不操作。按程序的名字认，不分大小写。'));
  const chips = blocked.map((name) => {
    const chip = el('li', 'computer-chip');
    const label = chip.appendChild(el('span', null, name));
    label.title = name;
    const remove = chip.appendChild(el('button'));
    remove.type = 'button';
    remove.dataset.name = name;
    tattr(remove, 'aria-label', k('不再拦着这个程序'));
    tattr(remove, 'title', k('不再拦着这个程序'));
    remove.appendChild(icon('x'));
    remove.addEventListener('click', async () => {
      const index = blocked.indexOf(name);
      const next = blocked[index + 1] || blocked[index - 1];
      if (await setComputer({ blocked: blocked.filter((one) => one !== name) }, t('已经去掉了“{name}”。', { name }))) {
        const again = next && list.querySelector('[data-name="' + CSS.escape(next) + '"]');
        (again || $('computer-blocked-name')).focus();  // 去掉的那一个没了：焦点交给旁边那个，或者输入框
      }
    });
    return chip;
  });
  if (!chips.length) list.replaceChildren(tel('li', 'computer-chips-empty', k('还没有。比如可以加上密码管理器、网银、聊天软件。')));
  else list.replaceChildren(...chips);
  if (hadFocus) { const again = list.querySelector('[data-name="' + CSS.escape(hadFocus) + '"]'); if (again) again.focus(); }
}

async function addBlockedApp() {
  const field = $('computer-blocked-name');
  const name = field.value.trim().replace(/\s+/g, ' ');
  if (!name) { field.focus(); return; }
  const blocked = (computerStatus && computerStatus.blocked) || [];
  if (blocked.some((one) => one.toLowerCase() === name.toLowerCase())) { say(t('“{name}”已经在里面了。', { name }), 'bad'); return; }
  if (await setComputer({ blocked: [...blocked, name] }, t('已经加上了“{name}”。', { name }))) field.value = '';
}
$('computer-blocked-add').addEventListener('click', addBlockedApp);
$('computer-blocked-name').addEventListener('keydown', (key) => {
  if (key.key === 'Enter' && !key.isComposing) { key.preventDefault(); addBlockedApp(); }
});
