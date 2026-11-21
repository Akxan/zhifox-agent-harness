// ---- 键盘 ----------------------------------------------------------------
// 整个页面的快捷键：⌘F / Ctrl+F（或者 ⌘K / Ctrl+K）搜索会话，⌘, / Ctrl+, 打开设置，⌘. / Ctrl+. 打开换模型的面板，
// Esc 依次收起顶栏上的小面板（外观、换模型、语言）和窄窗口里的抽屉。

document.addEventListener('keydown', (key) => {
  const shown = openFlyout();
  if (key.key === 'Escape' && shown) {  // Esc 先收起顶栏上开着的小面板，焦点回到它的按钮上（焦点在面板里时，面板自己先接住，见 flyouts.js）
    key.preventDefault();
    shown.close(true);
    return;
  }
  const command = commandHeld(key);  // Mac 上是 ⌘，别的电脑上是 Ctrl（见 platform.js）
  const letter = key.key.toLowerCase();
  if (command && !key.altKey && key.key === '.') {
    // ⌘. / Ctrl+.：打开（再按一次收起）换模型的面板。不用 ⌘⇧M / Ctrl+Shift+M：Chrome 用它切换用户，Firefox 用它开响应式设计模式。
    // 不看 Shift：有的键盘（法语）"."要按着 Shift 才打得出来。已经开着别的窗口时不抢
    key.preventDefault();
    if (modelsFlyout.isOpen()) modelsFlyout.close(true);
    else if (!document.querySelector('dialog[open]')) openModels();
    return;
  }
  if (command && !key.altKey && !key.shiftKey && key.key === ',') {
    // ⌘, / Ctrl+,：打开设置（Mac 上几乎所有程序都是这个键）。已经开着别的窗口时不抢
    const busyDialog = settings.open || approvalDialog.open || viewer.open || memoryDialog.open || extensionsDialog.open || taskEditor.open || taskDetail.open || promptDialog.open || spendDialog.open || commandsDialog.open;
    key.preventDefault();
    if (!busyDialog) openSettings();
    return;
  }
  if (command && !key.altKey && !key.shiftKey && (letter === 'f' || letter === 'k')) {
    // ⌘F / Ctrl+F：搜索会话（"查找"大家最熟的就是这个键）；⌘K / Ctrl+K 也照样能用。
    // 搜的是所有会话，当前这一段也在里面，比浏览器自带的"在页面里查找"管用。但浏览器那个不该就此没了：
    // 光标已经在搜索框里时再按一次 ⌘F，或者开着对话框的时候，就不拦，交给浏览器
    const dialogOpen = settings.open || approvalDialog.open || viewer.open || memoryDialog.open || extensionsDialog.open || taskEditor.open || taskDetail.open || promptDialog.open || spendDialog.open || commandsDialog.open;
    if (letter === 'f' && (dialogOpen || document.activeElement === search || document.activeElement === $('media-search'))) return;
    key.preventDefault();
    if (dialogOpen) return;
    setNav(true, false);
    // 在"多媒体"里就搜多媒体（文件名和会话标题）；在"任务"里换回"会话"再搜，搜索框在那儿
    const box = navTab === 'media' ? $('media-search') : search;
    if (navTab === 'tasks') setNavTab('chats', false, true);
    box.focus();
    box.select();
  } else if (key.key === 'Escape' && !key.defaultPrevented && !settings.open && !approvalDialog.open && !viewer.open && !memoryDialog.open && !extensionsDialog.open && !taskEditor.open && !taskDetail.open && !promptDialog.open && !spendDialog.open && !commandsDialog.open) {
    if (overlayNav.matches && !body.classList.contains('nav-closed')) { setNav(false, false); refocus($('nav-open'), nav); }
    if (overlaySide.matches && body.classList.contains('side-open')) { setSide(false, false); refocus($('side-toggle'), side); }
  }
});
