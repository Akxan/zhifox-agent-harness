// ---- 键盘 ----------------------------------------------------------------
// 整个页面的快捷键：⌘F / Ctrl+F（或者 ⌘K / Ctrl+K）搜索会话，Esc 依次收起外观面板和窄窗口里的抽屉。

document.addEventListener('keydown', (key) => {
  if (key.key === 'Escape' && appearanceOpen()) {  // Esc 先收起外观面板，焦点回到外观按钮上
    key.preventDefault();
    closeAppearance(true);
    return;
  }
  const command = ON_MAC ? key.metaKey && !key.ctrlKey : key.ctrlKey && !key.metaKey;  // Mac 上的 Ctrl+K 是"删到行尾"，不能占用
  const letter = key.key.toLowerCase();
  if (command && !key.altKey && !key.shiftKey && (letter === 'f' || letter === 'k')) {
    // ⌘F / Ctrl+F：搜索会话（"查找"大家最熟的就是这个键）；⌘K / Ctrl+K 也照样能用。
    // 搜的是所有会话，当前这一段也在里面，比浏览器自带的"在页面里查找"管用。但浏览器那个不该就此没了：
    // 光标已经在搜索框里时再按一次 ⌘F，或者开着对话框的时候，就不拦，交给浏览器
    const dialogOpen = settings.open || approvalDialog.open || languageDialog.open || viewer.open || memoryDialog.open || extensionsDialog.open || taskEditor.open || taskDetail.open || promptDialog.open || spendDialog.open;
    if (letter === 'f' && (dialogOpen || document.activeElement === search || document.activeElement === $('media-search'))) return;
    key.preventDefault();
    if (dialogOpen) return;
    setNav(true, false);
    // 在"多媒体"里就搜多媒体（文件名和会话标题）；在"任务"里换回"会话"再搜，搜索框在那儿
    const box = navTab === 'media' ? $('media-search') : search;
    if (navTab === 'tasks') setNavTab('chats', false, true);
    box.focus();
    box.select();
  } else if (key.key === 'Escape' && !key.defaultPrevented && !settings.open && !approvalDialog.open && !languageDialog.open && !viewer.open && !memoryDialog.open && !extensionsDialog.open && !taskEditor.open && !taskDetail.open && !promptDialog.open && !spendDialog.open) {
    if (overlayNav.matches && !body.classList.contains('nav-closed')) { setNav(false, false); refocus($('nav-open'), nav); }
    if (overlaySide.matches && body.classList.contains('side-open')) { setSide(false, false); refocus($('side-toggle'), side); }
  }
});
