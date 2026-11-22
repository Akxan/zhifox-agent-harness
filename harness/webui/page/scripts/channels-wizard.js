// ---- 扩展 · 通道 · 设置向导 -------------------------------------------------
// 向导里你正在填的东西（token、选的确认方式）记在 chanView 里：状态更新、换语言时重画，填的内容不丢。
// 这里是向导的框架：从哪一步开始、换步、离开之前问一声（填了没保存的先提醒一次）、画上面的步骤条和下面的按钮。
// 每一步里画什么在 channels-steps.js。

function openChanWizard(platform, step, repair) {
  const item = chanFind(platform);
  if (!item) return;
  const start = step || (item.configured ? (item.owner ? 4 : 3) : 1);
  // values：第 2 步里正在填的那几项（字段名 → 文字）；show：哪几个密钥框现在显示着明文
  chanView = {
    kind: 'wizard', platform, step: start, values: {}, show: {}, checking: false, error: '', connected: '',
    mode: item.approval || 'ask', sandbox: item.sandbox || 'default', pairedAt: item.owner ? item.owner.paired_at : null, pairing: false, armed: '',
  };
  chanDrawn = '';
  drawChanWizard('step');
  if (start === 3 && (repair || !item.owner) && !item.pairing) chanPair();
}

const chanTyped = (view) => Object.values(view.values || {}).some((value) => String(value).trim());

// 第 4 步上换了的：确认方式、命令的沙箱（和现在用的比）
const chanModeChanged = (item) => Boolean(item) && chanView.mode !== item.approval;
const chanSandboxChanged = (item) => Boolean(item) && chanView.sandbox !== (item.sandbox || 'default');

// 有没有填了、还没保存的东西：填了 token（或者 ID、密钥）没点"验证"，或者换了确认方式、沙箱没点"完成"
function chanDirty() {
  if (chanView.kind !== 'wizard') return false;
  const item = chanFind(chanView.platform);
  if (chanView.step === 2 && chanTyped(chanView)) return true;
  return chanView.step === 4 && (chanModeChanged(item) || chanSandboxChanged(item));
}

// 想离开向导（返回列表、关掉对话框）。有没保存的东西：先提醒一次，再来一次才放弃。
// 提醒过了记在 chanView.armed 上，那句话由 drawChanWizard 每次照着写（chanUnsaved）：通道的状态一变向导就重画，
// 只写进页面元素的话一重画就没了，再点一次就悄悄放弃了修改
function leaveChanWizard(why) {
  if (chanDirty() && chanView.armed !== why) {
    chanView.armed = why;
    chanView.error = '';
    drawChanWizard();
    return false;
  }
  return true;
}

// 提醒过以后那句话：是哪一样还没保存
function chanUnsaved(item) {
  return chanView.step === 2 ? k('填的东西还没保存。再点一次就放弃它。')
    : chanModeChanged(item) ? k('确认方式还没保存。再点一次就放弃这次的修改。') : k('沙箱的选择还没保存。再点一次就放弃这次的修改。');
}

function chanBack() {
  if (!leaveChanWizard('back')) return;
  const platform = chanView.platform;
  chanView = { kind: 'list' };
  clearInterval(chanTicker);
  chanTicker = 0;
  drawChanList('open:' + platform);
  if (!chanPanel.querySelector('[data-focus="open:' + CSS.escape(platform) + '"]')) {
    const card = chanPanel.querySelector('[data-focus="card:' + CSS.escape(platform) + '"]');
    if (card) card.focus();
  }
}

function chanGo(step) {
  chanView.step = step;
  chanView.armed = '';
  chanView.error = '';
  drawChanWizard('step');
  const item = chanFind(chanView.platform);
  if (step === 3 && item && !item.owner && !item.pairing) chanPair();
}

function drawChanWizard(focus) {
  const view = chanView, item = chanFind(view.platform);
  if (!item) { chanView = { kind: 'list' }; drawChanList(); return; }
  // 输入框正拿着焦点时，记下是哪一个、光标在哪儿，重画以后放回去
  const field = document.activeElement && chanPanel.contains(document.activeElement) && document.activeElement.classList.contains('chan-input') ? document.activeElement : null;
  const caret = field ? [field.id, field.selectionStart, field.selectionEnd] : null;
  const lengths = Object.fromEntries(Object.entries(view.values).map(([name, value]) => [name, value.length]));  // 填的内容本身不进"变了没有"
  // 第 4 步的沙箱下拉框里写着"跟随设置（现在是：……）"：设置里那一档变了也要重画
  const print = JSON.stringify([{ ...view, values: lengths }, item, language, sandboxInfo && [sandboxInfo.level, sandboxInfo.levels, Boolean(sandboxInfo.problem)]]);
  if (!focus && print === chanDrawn && chanPanel.dataset.view === 'wizard') { syncChanTicker(); return; }
  chanDrawn = print;

  const head = el('div', 'chan-head');
  const back = head.appendChild(el('button', 'chan-back'));
  back.type = 'button';
  back.dataset.focus = 'back';
  back.append(icon('chevron'), tel('span', null, k('通道')));
  back.addEventListener('click', chanBack);
  const title = head.appendChild(el('h3', 'chan-title'));
  title.appendChild(chanMark(item, item.configured ? (item.enabled ? item.status.state : 'off') : ''));
  title.appendChild(el('span', null, item.configured ? t('{platform} 的设置', { platform: item.label }) : t('连接 {platform}', { platform: item.label })));

  const stepper = el('div', 'chan-stepper');
  const steps = stepper.appendChild(el('ol', 'chan-steps'));
  steps.setAttribute('aria-label', t('设置的步骤'));
  CHAN_STEPS.forEach((name, index) => {
    const number = index + 1;
    const done = number < view.step || (number === 2 && item.configured) || (number === 3 && item.owner);
    const entry = steps.appendChild(el('li'));
    const button = entry.appendChild(el('button', 'chan-step' + (done && number !== view.step ? ' done' : '')));
    button.type = 'button';
    button.dataset.focus = 'step:' + number;
    const badge = button.appendChild(el('b'));
    if (done && number !== view.step) badge.appendChild(icon('check')); else badge.textContent = String(number);
    const words = number === 2 && item.step ? item.step : t(name);  // 第 2 步叫"填 token"还是"填 ID 和密钥"，看平台
    button.appendChild(el('span', null, words));
    button.setAttribute('aria-label', t('第 {n} 步：{name}', { n: number, name: words }));
    if (number === view.step) button.setAttribute('aria-current', 'step');
    // 前两步随时可以回去；配对和确认方式要等 token 填好（连上了机器人）以后才能去
    button.disabled = number === view.step || view.checking || (number > 2 && !item.configured);
    button.addEventListener('click', () => { if (leaveChanWizard('step:' + number)) chanGo(number); });
  });
  const bar = stepper.appendChild(el('i', 'chan-progress'));
  bar.style.transform = 'scaleX(' + view.step / CHAN_STEPS.length + ')';

  const pane = el('div', 'chan-pane');
  const foot = el('div', 'chan-foot');
  const help = foot.appendChild(el('p', 'chan-help'));
  help.setAttribute('role', 'status');
  [chanStepOne, chanStepTwo, chanStepThree, chanStepFour][view.step - 1](item, pane, foot, help);
  if (view.armed && chanDirty()) { retext(help, chanUnsaved(item)); help.className = 'chan-help bad'; }
  if (view.error) { help.textContent = view.error; help.className = 'chan-help bad'; }
  chanShow([head, stepper, pane, foot], 'wizard', focus === 'step' ? null : focus);
  const again = caret ? document.getElementById(caret[0]) : null;
  if (again && chanPanel.contains(again)) { again.focus(); again.setSelectionRange(caret[1], caret[2]); }
  if (focus === 'step') {  // 刚换到这一步：焦点放在这一步最该先做的那件事上
    const first = chanPanel.querySelector('.chan-input:not(:disabled), .chan-mode[aria-checked="true"], .chan-foot .button.primary:not(:disabled), .chan-foot .button:not(:disabled)');
    if (first) first.focus({ preventScroll: true });
  }
  syncChanTicker();
}
