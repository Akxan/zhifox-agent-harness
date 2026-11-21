// ---- 扩展 · 通道 · 设置向导的四步 -------------------------------------------
// 1 建一个机器人（照着平台给的步骤）→ 2 填 token，请助手程序去问平台认不认 → 3 配对（配对码和它的倒计时）
// → 4 选确认方式。向导的框架（换步、离开之前问一声、步骤条）在 channels-wizard.js。
// 文件最后把通道这一页挂到扩展对话框上（打开时、Esc 先回到列表、关上时清掉向导里刚填的 token）。

// 第 1 步：照着平台给的步骤（channels/guides.py）建一个机器人。和命令行里 harness channels setup 印的是同一份
function chanStepOne(item, pane, foot, help) {
  pane.appendChild(el('h4', null, t('先在 {platform} 里建一个机器人', { platform: item.label })));
  if (chanSdkMissing(item)) pane.appendChild(chanSdkNote(item, help));
  const steps = pane.appendChild(el('ol', 'chan-howto'));
  (item.guide || []).forEach((step, index) => {
    const entry = steps.appendChild(el('li'));
    entry.appendChild(chanRich(step.text, step.code));
    if (step.link || step.copy) {
      const tools = entry.appendChild(el('span', 'chan-how-tools'));
      if (step.link) tools.appendChild(chanLink(step.link, step.link_label || k('打开')));
      if (step.copy) tools.appendChild(chanCopy(step.copy, step.copy_label || k('复制'), help, 'copy:' + index));
    }
  });
  pane.appendChild(chanNote('calm', t('机器人是你自己的：别人也许能搜到它，但只有配对过的账号能让它做事。智狐是主动连到 {platform} 去收消息的，不需要公网地址，家里的电脑、内网里的服务器都能用。', { platform: item.label })));
  const next = foot.appendChild(chanButton(null, item.step ? k('准备好了，下一步') : k('有 token 了，下一步'), 'primary'));
  next.dataset.focus = 'next';
  next.addEventListener('click', () => chanGo(2));
}

// 第 2 步：填 token（或者 ID 和密钥）。要填哪几项、哪几项是密钥，是平台给的（item.fields）。
// 已经连着的平台，没重填的那几项接着用存着的（见 channels/hub.merge），所以只换一个 token 不用把别的也重填一遍
function chanStepTwo(item, pane, foot, help) {
  const view = chanView;
  const fields = item.fields || [];
  const secret = fields.some((field) => field.secret);
  pane.appendChild(el('h4', null, fields.length > 1 ? t('把这几项粘贴到这里') : t('把 token 粘贴到这里')));
  if (chanSdkMissing(item)) pane.appendChild(chanSdkNote(item, help));
  const label = (submitting) => submitting || !item.configured ? k('验证并连接') : k('下一步');
  let submit = null;
  const grid = pane.appendChild(el('div', 'chan-fields'));
  fields.forEach((field, index) => {
    const id = 'chan-field-' + field.id;
    const box = grid.appendChild(el('label', 'chan-field'));
    box.htmlFor = id;
    box.appendChild(el('span', null, field.label));
    const row = box.appendChild(el('span', 'chan-token'));
    const input = row.appendChild(el('input', 'field chan-input'));
    input.id = id;
    input.type = field.secret && !view.show[field.id] ? 'password' : 'text';
    input.autocomplete = 'off';
    input.spellcheck = false;
    input.maxLength = 400;
    input.value = view.values[field.id] || '';
    input.dataset.focus = 'field:' + field.id;
    const hint = item.hints ? item.hints[field.id] : '';
    input.placeholder = hint ? (field.secret ? t('已经保存了一个（{hint}）。要换就粘贴新的', { hint }) : hint) : field.placeholder || '';
    if (secret) input.setAttribute('aria-describedby', 'chan-token-help');
    input.disabled = view.checking;
    input.addEventListener('input', () => {
      view.values[field.id] = input.value;
      view.armed = '';
      view.error = '';
      if (submit) submit.replaceChildren(tel('span', null, label(chanTyped(view))));
    });
    input.addEventListener('keydown', (key) => {
      if (key.key !== 'Enter' || key.isComposing || key.keyCode === 229) return;
      key.preventDefault();
      const following = grid.querySelectorAll('.chan-input')[index + 1];  // 回车：先跳到下一格，最后一格才提交
      if (following) following.focus(); else if (submit) submit.click();
    });
    if (field.secret) {
      const shown = Boolean(view.show[field.id]);
      // 按钮上的字带着字段的名字（读屏软件要知道是哪一个框）。换语言时整个向导会重画，所以直接写译好的
      const eye = row.appendChild(el('button', 'icon-button'));
      eye.type = 'button';
      eye.title = shown ? t('隐藏 {field}', { field: field.label }) : t('显示 {field}', { field: field.label });
      eye.setAttribute('aria-label', eye.title);
      eye.appendChild(icon('eye'));
      eye.setAttribute('aria-pressed', shown ? 'true' : 'false');
      eye.setAttribute('aria-controls', id);
      eye.dataset.focus = 'eye:' + field.id;
      eye.addEventListener('click', () => { view.show[field.id] = !shown; drawChanWizard('eye:' + field.id); });
    }
  });
  if (secret) {
    const note = pane.appendChild(el('p', 'chan-help', t('这些是机器人的钥匙。它们只存在这台电脑的 channels.json 里（只有你的账号能读），不会发给模型，之后页面上也只显示最后 4 位。')));
    note.id = 'chan-token-help';
  }
  if (item.configured && item.bot) pane.appendChild(chanNote('good', t('现在连着 {bot}（密钥 {hint}）。不换机器人的话，直接下一步。', { bot: chanBot(item), hint: item.token_hint })));
  const previous = foot.appendChild(chanButton(null, k('上一步')));
  previous.dataset.focus = 'previous';
  previous.disabled = view.checking;
  previous.addEventListener('click', () => { if (leaveChanWizard('step:1')) chanGo(1); });
  submit = foot.appendChild(chanButton(null, view.checking ? k('正在验证……') : label(chanTyped(view)), 'primary'));
  submit.dataset.focus = 'submit';
  submit.disabled = view.checking;
  submit.addEventListener('click', () => chanConnect(item));
  if (view.checking) retext(help, k('正在问 {platform} 认不认……'), { platform: item.label });
  else if (!view.armed) retext(help, k('只检查这一次：智狐拿它去问 {platform}“这是哪个机器人”，认的话就记下来。'), { platform: item.label });
}

async function chanConnect(item) {
  const view = chanView;
  const given = Object.fromEntries(Object.entries(view.values).map(([name, value]) => [name, String(value).trim()]).filter(([, value]) => value));
  if (!Object.keys(given).length) {
    if (item.configured) { chanGo(item.owner ? 4 : 3); return; }
    view.error = (item.fields || []).length > 1 ? t('先把这几项粘贴进来。') : t('先把 token 粘贴进来。');
    drawChanWizard('step');
    return;
  }
  view.checking = true;
  view.error = '';
  drawChanWizard('submit');
  const reply = await call('/api/channel/connect', { platform: item.id, credentials: given });
  if (chanView !== view) return;  // 等回答的时候你已经离开了这个向导
  view.checking = false;
  if (!reply.ok) {
    view.error = reply.error;
    drawChanWizard('step');
    return;
  }
  view.values = {};
  view.show = {};
  const before = chanBot(item);
  view.connected = reply.data.bot ? reply.data.bot.handle || reply.data.bot.name : '';
  announce(t('已连接 {bot}。', { bot: view.connected }));
  const fresh = chanFind(item.id);
  view.pairedAt = fresh && fresh.owner ? fresh.owner.paired_at : null;
  chanGo(3);
  // 换成了另一个机器人：原来的绑定作废了（见 store.connect），要重新配对。状态马上就到，等它到了再看要不要生成配对码
  if (fresh && fresh.owner && view.connected && before && before !== view.connected) chanPair();
}

async function chanPair() {
  const view = chanView;
  if (view.kind !== 'wizard' || view.pairing) return;
  view.pairing = true;
  view.error = '';
  const item = chanFind(view.platform);
  view.pairedAt = item && item.owner ? item.owner.paired_at : null;
  drawChanWizard();
  const reply = await call('/api/channel/pair', { platform: view.platform });
  if (chanView !== view) return;
  view.pairing = false;
  if (!reply.ok) view.error = reply.error;
  drawChanWizard(reply.ok ? 'copy' : null);
}

function chanStepThree(item, pane, foot, help) {
  const view = chanView;
  const bot = chanBot(item);
  const paired = item.owner && item.owner.paired_at !== view.pairedAt;  // 打开这一步以后才配对上的
  if (view.connected) pane.appendChild(chanNote('good', t('已连接 {bot}。', { bot: view.connected })));
  if (paired) {
    const box = pane.appendChild(el('div', 'chan-paired'));
    box.setAttribute('role', 'status');
    const check = box.appendChild(el('span', 'chan-check'));
    check.appendChild(icon('check'));
    const words = box.appendChild(el('div'));
    words.appendChild(el('b', null, t('配对成功：{name}', { name: item.owner.name })));
    words.appendChild(el('span', null, t('以后只有这个账号能通过 {bot} 使用智狐，别人发的消息一律不理。', { bot })));
    if (chanPanel.dataset.paired !== String(item.owner.paired_at)) {  // 刚配对上：对勾弹一下，读屏软件也说一声
      chanPanel.dataset.paired = String(item.owner.paired_at);
      requestAnimationFrame(() => play(check, [{ transform: 'scale(.4)', opacity: 0 }, { transform: 'scale(1.12)', opacity: 1, offset: 0.7 }, { transform: 'none', opacity: 1 }], 520));
      announce(t('配对成功：{name}', { name: item.owner.name }));
    }
    const next = foot.appendChild(chanButton(null, k('下一步'), 'primary'));
    next.dataset.focus = 'next';
    next.addEventListener('click', () => chanGo(4));
    requestAnimationFrame(() => { if (chanPanel.contains(document.activeElement) && document.activeElement.dataset.focus === 'copy') next.focus(); });
    return;
  }
  const code = item.pairing;
  if (code) {
    pane.appendChild(el('h4', null, t('用你自己的账号，把这 6 位数字发给 {bot}', { bot })));
    const digits = pane.appendChild(el('div', 'chan-code'));
    digits.setAttribute('role', 'img');
    digits.setAttribute('aria-label', t('配对码：{code}', { code: code.code.split('').join(' ') }));
    const fresh = digits.dataset.code !== code.code && chanPanel.dataset.code !== code.code;
    chanPanel.dataset.code = code.code;
    Array.from(code.code).forEach((digit, index) => {
      if (index === 3) digits.appendChild(el('span', 'chan-gap'));
      const box = digits.appendChild(el('span', 'chan-digit', digit));
      if (fresh) play(box, [{ opacity: 0, transform: 'translateY(-10px)' }, { opacity: 1, transform: 'none' }], 300 + index * 60);
    });
    const timer = pane.appendChild(el('div', 'chan-timer'));
    const bar = timer.appendChild(el('i'));
    const total = 600, left = Math.max(0, code.expires - Date.now() / 1000);
    bar.style.setProperty('--from', String(Math.min(1, left / total)));
    bar.style.setProperty('--left', String(Math.min(1, left / total)));
    bar.style.animationDuration = left + 's';
    const meta = pane.appendChild(el('div', 'chan-code-meta'));
    const clock = meta.appendChild(el('span', 'chan-left'));
    clock.dataset.expires = String(code.expires);
    clock.textContent = t('还有 {time} 过期', { time: durationText(left) });
    if (code.attempts_left < 5) meta.appendChild(el('span', null, t('还能猜错 {n} 次', { n: code.attempts_left })));
    const tools = pane.appendChild(el('div', 'chan-code-tools'));
    const copy = tools.appendChild(chanButton('copy', k('复制配对码')));
    copy.dataset.focus = 'copy';
    copy.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(code.code); retext(help, k('已复制。')); help.className = 'chan-help good'; }
      catch (_) { retext(help, k('没能复制，请照着上面的数字输入。')); help.className = 'chan-help bad'; }
    });
    // Discord 要先把机器人加到你的账号，才能和它私聊（链接是助手程序给的，见 channels/discord.check）
    if (item.bot && item.bot.install) {
      const install = tools.appendChild(chanLink(item.bot.install, k('把机器人加到你的账号')));
      install.dataset.focus = 'install';
    }
    // 打开和机器人的聊天。Telegram 的这个链接还会替你发一句 /start 123456，点"开始"就配对上了（链接是助手程序算的，见 store.pair_link）
    if (code.link) {
      const open = tools.appendChild(chanLink(code.link, k('在 {platform} 里打开')));
      retext(open.querySelector('span'), k('在 {platform} 里打开'), { platform: item.label });
      open.dataset.focus = 'open-chat';
    }
    const how = chanNote('calm', (item.pair ? item.pair.replace('{bot}', bot) + ' ' : '') + t('配对上以后这里会自己往下走。别人发来的配对码不对的话，猜错 5 次这个码就作废了。'));
    pane.appendChild(how);
    const skip = foot.appendChild(chanButton(null, k('以后再配')));
    skip.dataset.focus = 'skip';
    skip.addEventListener('click', async () => {
      await call('/api/channel/cancel', { platform: item.id });
      chanGo(4);
    });
    const again = foot.appendChild(chanButton('refresh', k('换一个配对码')));
    again.dataset.focus = 'again';
    again.addEventListener('click', chanPair);
    retext(help, k('等你从 {platform} 发过来……'), { platform: item.label });
    return;
  }
  // 没有正在进行的配对：要么还没生成（马上就有），要么过期了、猜错太多作废了，要么已经配对过、想换一个账号
  if (view.pairing) {
    pane.appendChild(el('h4', null, t('正在生成配对码……')));
    return;
  }
  if (item.owner) {
    pane.appendChild(el('h4', null, t('已经配对过了')));
    pane.appendChild(el('p', null, t('现在的主人是 {name}（{when}配对的）。重新配对的话，配好以后只有新的那个账号能用，原来的那个不再理会。', { name: item.owner.name, when: fromNow(item.owner.paired_at) })));
    const repair = foot.appendChild(chanButton('link', k('重新配对')));
    repair.dataset.focus = 'repair';
    repair.addEventListener('click', chanPair);
    const next = foot.appendChild(chanButton(null, k('下一步'), 'primary'));
    next.dataset.focus = 'next';
    next.addEventListener('click', () => chanGo(4));
    return;
  }
  pane.appendChild(el('h4', null, t('配对码过期了')));
  pane.appendChild(el('p', null, t('配对码十分钟内有效；有人猜错 5 次，它也会作废。生成一个新的再试。')));
  const regenerate = foot.appendChild(chanButton('refresh', k('生成新的配对码'), 'primary'));
  regenerate.dataset.focus = 'regenerate';
  regenerate.addEventListener('click', chanPair);
}

function chanStepFour(item, pane, foot, help) {
  const view = chanView;
  pane.appendChild(el('h4', null, t('在聊天里，动手之前要不要先问你')));
  pane.appendChild(el('p', null, item.buttons
    ? t('这一档只管这个通道，和网页上对话里选的、定时任务选的都无关。要问你的时候，聊天里会发来一条消息，写着完整的命令和工作目录，下面是“允许 / 拒绝”两个按钮；网页上也能答复，先到的算数。')
    : t('这一档只管这个通道，和网页上对话里选的、定时任务选的都无关。要问你的时候，聊天里会发来一条消息，写着完整的命令和工作目录，回复“允许”或“拒绝”就行；网页上也能答复，先到的算数。')));
  const group = pane.appendChild(el('div', 'chan-modes'));
  group.setAttribute('role', 'radiogroup');
  group.setAttribute('aria-label', t('确认方式'));
  const choose = (mode, focus) => { view.mode = mode; view.armed = ''; drawChanWizard(focus ? 'mode:' + mode : null); };
  for (const option of CHAN_MODES) {
    const button = group.appendChild(el('button', 'chan-mode'));
    button.type = 'button';
    button.setAttribute('role', 'radio');
    button.dataset.mode = option.mode;
    button.dataset.focus = 'mode:' + option.mode;
    const on = view.mode === option.mode;
    button.setAttribute('aria-checked', on ? 'true' : 'false');
    button.tabIndex = on ? 0 : -1;  // 单选组只有选中的那个在 Tab 键的顺序里，其余的用方向键去
    button.append(el('b', null, t(APPROVAL[option.mode])), el('span', null, t(option.text)), icon('check'));
    button.addEventListener('click', () => choose(option.mode, true));
  }
  group.addEventListener('keydown', (key) => {
    const order = CHAN_MODES.map((option) => option.mode), at = order.indexOf(view.mode);
    const next = { ArrowDown: at + 1, ArrowRight: at + 1, ArrowUp: at - 1, ArrowLeft: at - 1, Home: 0, End: order.length - 1 }[key.key];
    if (next === undefined) return;
    key.preventDefault();
    choose(order[(next + order.length) % order.length], true);
  });
  if (view.mode === 'all') {
    pane.appendChild(chanNote('bad', t('确认是你和模型之间唯一的一道关卡。全部自动允许以后，聊天里的一句话就能让助手在这台电脑上执行任何命令（允许了聊天通道操作电脑的话，还有点击、打字），不再问你；网页或文件里藏着误导模型的话，它照做的时候也没人拦着。只在你清楚风险的时候用。')));
  }
  if (!item.owner) pane.appendChild(chanNote('warn', t('还没有配对：在配对之前，谁发来的消息都不会处理。')));
  const done = foot.appendChild(chanButton('check', k('完成'), 'primary'));
  done.dataset.focus = 'done';
  done.addEventListener('click', () => chanFinish(item, done));
  if (!view.armed) retext(help, item.approval === view.mode ? k('现在用的就是这一档。') : k('点“完成”才会改。'));
}

async function chanFinish(item, button) {
  const view = chanView;
  if (view.mode !== item.approval) {
    button.disabled = true;
    const reply = await call('/api/channel/approval', { platform: item.id, mode: view.mode });
    if (chanView !== view) return;
    if (!reply.ok) { button.disabled = false; view.error = reply.error; drawChanWizard('done'); return; }
  }
  chanView = { kind: 'list' };
  clearInterval(chanTicker);
  chanTicker = 0;
  drawChanList('open:' + item.id);
  const bot = chanBot(item);
  const key = item.owner ? k('{platform} 准备好了：给 {bot} 发一条消息试试。') : k('{platform} 的设置已保存。配对以后就能用了。');
  extensionSay(key, { platform: item.label, bot }, 'good');
  announce(t(key, { platform: item.label, bot }));
}

// 配对码的倒计时：只在向导的第 3 步、配对码显示着的时候走。改的是文字（和减少动画时那根线停在哪儿），不是动画
function syncChanTicker() {
  const need = chanView.kind === 'wizard' && chanView.step === 3 && chanPanel.querySelector('.chan-left');
  if (need && !chanTicker) chanTicker = setInterval(onChanTick, 1000);
  if (!need && chanTicker) { clearInterval(chanTicker); chanTicker = 0; }
}

function onChanTick() {
  const clock = chanPanel.querySelector('.chan-left');
  if (!clock) { syncChanTicker(); return; }
  const left = Number(clock.dataset.expires) - Date.now() / 1000;
  if (left <= 0) { chanDrawn = ''; drawChannels(); return; }  // 过期了：助手程序不会为此发状态，自己画成"过期了"
  clock.textContent = t('还有 {time} 过期', { time: durationText(left) });
  const bar = chanPanel.querySelector('.chan-timer i');
  if (bar) bar.style.setProperty('--left', String(Math.min(1, left / 600)));
}

// ---- 挂到"扩展"对话框上 ----

extensionTabs.channels = {
  open() {
    chanDrawn = '';
    drawChannels();
  },
  beforeClose() {
    return chanView.kind !== 'wizard' || leaveChanWizard('close');
  },
};
extensionsDialog.addEventListener('keydown', (key) => {
  if (key.key !== 'Escape' || key.isComposing || key.keyCode === 229 || extensionTab !== 'channels' || !chanPanel.contains(document.activeElement) && document.activeElement !== extensionsDialog) return;
  if (chanDeleting) {  // 这一下 Esc 只用来取消删除
    key.preventDefault();
    const platform = chanDeleting;
    chanDeleting = null;
    drawChanList('open:' + platform);
  } else if (chanView.kind === 'wizard') {  // 在向导里：先回到列表，不直接关掉对话框
    key.preventDefault();
    key.stopPropagation();
    chanBack();
  }
});
extensionsDialog.addEventListener('close', () => {
  chanView = { kind: 'list' };
  chanDeleting = null;
  chanDrawn = '';
  clearInterval(chanTicker);
  chanTicker = 0;
  delete chanPanel.dataset.view;
  chanPanel.replaceChildren();  // 向导里可能有刚填的 token：关掉就清掉
});
