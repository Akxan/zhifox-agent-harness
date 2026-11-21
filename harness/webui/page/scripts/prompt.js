  // ---- 提示词 --------------------------------------------------------------
  // 设置里的"编辑提示词"：改系统提示词（告诉模型它是谁、怎么做事的那段话）。改的是模板，里面可以写占位符，
  // 发给模型之前由助手程序换成实际的值（见 prompt.py）；预览也请它来算，页面不重复写一遍替换的规则。
  // 改了以后从下一段新对话开始生效：一段对话里提示词不能变（缓存、思考内容的校验）。当前这段还没聊的话，马上用上。

  const promptDialog = $('prompt-dialog');
  let promptBase = null;   // 打开时读到的：{text, default, custom, preview, placeholders}
  let promptArmed = '';    // 'close'：提醒过"有没保存的修改"；'reset'：提醒过"恢复默认会丢掉改过的内容"
  let promptPreviewSeq = 0;
  const PLACEHOLDER_LABELS = { name: k('名字'), latin: k('英文名'), workspace: k('工作目录'), system: k('操作系统'), web_line: k('联网的那一行说明（关掉联网搜索时是空的）') };

  function promptSay(text, bad) {
    $('prompt-message').textContent = text;
    $('prompt-message').classList.toggle('bad', Boolean(bad));
  }

  async function openPrompt() {
    const reply = await call('/api/prompt');
    if (!reply.ok) { say(reply.error, 'bad'); return; }
    if (settings.open) settings.close();
    promptBase = reply.data;
    promptArmed = '';
    $('prompt-text').value = promptBase.text;
    drawPlaceholders();
    promptSay('');
    showPromptPane('edit');
    promptDialog.showModal();
    $('prompt-text').focus();
    $('prompt-text').setSelectionRange(0, 0);
    $('prompt-text').scrollTop = 0;
  }

  function drawPlaceholders() {
    const box = $('prompt-placeholders');
    box.replaceChildren(...(promptBase ? promptBase.placeholders : []).map(({ name, value }) => {
      const chip = el('button', 'prompt-chip');
      chip.type = 'button';
      chip.appendChild(el('code', null, '{' + name + '}'));
      chip.appendChild(el('span', null, t(PLACEHOLDER_LABELS[name] || name) + (value.trim() ? ' · ' + value.trim() : '')));
      chip.title = value;
      chip.addEventListener('click', () => {
        const area = $('prompt-text'), at = area.selectionStart, end = area.selectionEnd;
        area.setRangeText('{' + name + '}', at, end, 'end');
        area.focus();
        promptEdited();
      });
      return chip;
    }));
  }

  async function showPromptPane(pane) {
    const preview = pane === 'preview';
    for (const [id, on] of [['prompt-tab-edit', !preview], ['prompt-tab-preview', preview]]) {
      $(id).setAttribute('aria-selected', String(on));
      $(id).tabIndex = on ? 0 : -1;
    }
    // 藏起来的那一边要是正拿着焦点，焦点会掉到窗口外面（之后的 Esc 就绕过了下面的"有没保存的修改"），先挪到标签上
    if (promptDialog.contains(document.activeElement) && document.activeElement.closest('.prompt-pane:not([hidden])')) $(preview ? 'prompt-tab-preview' : 'prompt-tab-edit').focus();
    $('prompt-edit-pane').hidden = preview;
    $('prompt-preview-pane').hidden = !preview;
    if (!preview) return;
    const mine = ++promptPreviewSeq;
    const reply = await call('/api/prompt/preview', { text: $('prompt-text').value });
    if (mine !== promptPreviewSeq) return;
    $('prompt-preview').textContent = reply.ok ? reply.data.preview : reply.error;
  }

  function promptEdited() {
    if (promptArmed) { promptArmed = ''; promptSay(''); }
  }

  const promptChanged = () => Boolean(promptBase) && $('prompt-text').value !== promptBase.text;

  function requestClosePrompt() {
    if (promptChanged() && promptArmed !== 'close') {
      promptArmed = 'close';
      promptSay(t('还有没保存的修改，再按一次就放弃。'), true);
      return;
    }
    promptDialog.close();
  }

  async function savePrompt(reset) {
    if (reset && promptArmed !== 'reset' && (promptBase.custom || promptChanged())) {
      promptArmed = 'reset';
      promptSay(t('恢复成内置的提示词？你改过的内容会丢掉。再点一次“恢复默认”确认。'), true);
      return;
    }
    const buttons = [$('prompt-save'), $('prompt-reset')];
    for (const button of buttons) button.disabled = true;
    const reply = reset ? await call('/api/prompt/reset') : await call('/api/prompt/save', { text: $('prompt-text').value });
    for (const button of buttons) button.disabled = false;
    if (!reply.ok) { promptArmed = ''; promptSay(reply.error, true); return; }
    promptBase = null;
    promptDialog.close();
    toast(reset ? (reply.data.applied ? t('已恢复内置的提示词，这段对话就用它。') : t('已恢复内置的提示词，从下一段新对话开始生效。'))
      : reply.data.applied ? t('提示词已保存，这段对话就用新的。') : t('提示词已保存，从下一段新对话开始生效。'));
  }

  $('prompt-open').addEventListener('click', openPrompt);
  tattr($('prompt-reveal'), 'title', ON_MAC ? k('在访达中显示 prompt.md') : k('在文件夹中显示 prompt.md'));
  tattr($('prompt-reveal'), 'aria-label', ON_MAC ? k('在访达中显示 prompt.md') : k('在文件夹中显示 prompt.md'));
  $('prompt-reveal').addEventListener('click', () => revealData('prompt', promptSay));
  $('prompt-save').addEventListener('click', () => savePrompt(false));
  $('prompt-reset').addEventListener('click', () => savePrompt(true));
  $('prompt-cancel').addEventListener('click', requestClosePrompt);
  $('close-prompt').addEventListener('click', requestClosePrompt);
  $('prompt-text').addEventListener('input', promptEdited);
  $('prompt-tab-edit').addEventListener('click', () => showPromptPane('edit'));
  $('prompt-tab-preview').addEventListener('click', () => showPromptPane('preview'));
  promptDialog.querySelector('.tabs').addEventListener('keydown', (key) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(key.key)) return;
    key.preventDefault();
    const preview = key.key === 'End' || (key.key !== 'Home' && $('prompt-tab-edit').getAttribute('aria-selected') === 'true');
    showPromptPane(preview ? 'preview' : 'edit');
    $(preview ? 'prompt-tab-preview' : 'prompt-tab-edit').focus();
  });
  promptDialog.addEventListener('keydown', (key) => {
    if (key.isComposing || key.keyCode === 229) return;  // 输入法正在组字
    if (key.key === 'Escape') { key.preventDefault(); key.stopPropagation(); requestClosePrompt(); }
    else if (key.key === 'Enter' && (ON_MAC ? key.metaKey : key.ctrlKey)) { key.preventDefault(); savePrompt(false); }
  });
  // 保险：焦点不在窗口里的时候按 Esc，浏览器直接发 cancel（不经过上面的 keydown）。照样先看有没有没保存的修改
  promptDialog.addEventListener('cancel', (event) => { if (promptChanged() && promptArmed !== 'close') { event.preventDefault(); requestClosePrompt(); } });
  promptDialog.addEventListener('close', () => {
    promptBase = null;
    promptArmed = '';
    $('prompt-text').value = '';
    $('prompt-preview').textContent = '';
  });
