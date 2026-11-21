  // ---- 扩展：Skill、MCP、通道、Agent -----------------------------------------
  // 一个对话框，四个标签页。这一节只管"壳"：打开、关上、切换标签页、记住上次看的是哪一页。
  // 每一页里的内容由各自那一节画（见下面的 Skill、MCP、通道、Agent 几节），它们通过 extensionTabs 挂上来：
  //   extensionTabs.skills = { open() { …这一页被打开时调用，适合在这时去读列表… } }
  // 切换标签页按的是 WAI-ARIA 的"标签页"模式：左右方向键在标签之间移动，Home / End 到头尾。

  const extensionsDialog = $('extensions');
  const TABS = ['skills', 'mcp', 'channels', 'agents'];
  const extensionTabs = {};
  let extensionTab = 'skills';
  try { if (TABS.includes(localStorage.getItem('harness.extensions-tab'))) extensionTab = localStorage.getItem('harness.extensions-tab'); } catch (_) { /* 隐私模式下读不了，用默认的 */ }

  function showExtensionTab(name, focus) {
    extensionTab = TABS.includes(name) ? name : 'skills';
    try { localStorage.setItem('harness.extensions-tab', extensionTab); } catch (_) { /* 记不住也没关系 */ }
    for (const tab of TABS) {
      const button = $('tab-' + tab), on = tab === extensionTab;
      button.setAttribute('aria-selected', on ? 'true' : 'false');
      button.tabIndex = on ? 0 : -1;  // 标签页只有选中的那个在 Tab 键的顺序里，其余的用方向键去
      $('panel-' + tab).hidden = !on;
    }
    extensionSay(null);
    if (focus) $('tab-' + extensionTab).focus();
    if (extensionsDialog.open && extensionTabs[extensionTab] && extensionTabs[extensionTab].open) extensionTabs[extensionTab].open();
  }

  function openExtensions(tab) {
    if (!extensionsDialog.open) extensionsDialog.showModal();
    showExtensionTab(tab || extensionTab, true);
  }

  for (const tab of TABS) $('tab-' + tab).addEventListener('click', () => showExtensionTab(tab, true));
  $('extensions').querySelector('.tabs').addEventListener('keydown', (key) => {
    const at = TABS.indexOf(extensionTab);
    const next = { ArrowRight: at + 1, ArrowLeft: at - 1, Home: 0, End: TABS.length - 1 }[key.key];
    if (next === undefined) return;
    key.preventDefault();
    showExtensionTab(TABS[(next + TABS.length) % TABS.length], true);
  });
  $('extensions-open').addEventListener('click', () => openExtensions());

  // 对话框底下那一行（role=status，读屏软件也会读到）。key 是原文：换语言时它跟着换；text 是服务器给的、已经译好的话
  function extensionSay(key, values, kind, text) {
    const box = $('extensions-message');
    if (key) retext(box, key, values);
    else { delete box.dataset.i18n; delete box.dataset.i18nValues; box.textContent = text || ''; }
    box.className = 'settings-message' + (kind ? ' ' + kind : '');
  }

  // 想关掉对话框（×、点外面、Esc）。每一页都可以拦一下（比如表单还没保存）：它的 beforeClose 返回 false 就不关。
  // 不只问眼前这一页：在一页里填了一半、切到另一页再关，填的东西也会丢。拦下来的那一页不是眼前这页的话，切过去让你看到提醒
  function closeBlocker() {
    for (const tab of [extensionTab, ...TABS.filter((name) => name !== extensionTab)]) {
      const handlers = extensionTabs[tab];
      if (handlers && handlers.beforeClose && handlers.beforeClose() === false) return tab;
    }
    return null;
  }
  function requestCloseExtensions() {
    const blocked = closeBlocker();
    if (blocked === null) { extensionsDialog.close(); return true; }
    if (blocked !== extensionTab) showExtensionTab(blocked, false);
    return false;
  }
  $('close-extensions').addEventListener('click', requestCloseExtensions);
  extensionsDialog.addEventListener('cancel', (event) => {
    if (!requestCloseExtensions()) event.preventDefault();
  });
