  // ---- 语言 ----------------------------------------------------------------
  // 换语言不刷新页面：先请助手程序记下新的选择（下次打开、终端版都跟着用），它会发来新的状态，
  // 页面收到后在原地把文字换掉（见 applyLanguage）。正在进行的对话、滚动的位置、输入框里写了一半的话都不受影响。

  const languageDialog = $('language-dialog');
  const LANGUAGE_NAMES = { zh: '中文', en: 'English', es: 'Español' };  // 每种语言都用它自己的文字写，谁都认得出自己的那一个

  function renderLanguageOptions() {
    $('language-options').replaceChildren(...Object.keys(LANGUAGE_NAMES).map((code) => {
      const option = el('button', 'option' + (code === language ? ' current' : ''));
      option.type = 'button';
      option.lang = LOCALES[code];
      option.setAttribute('aria-pressed', String(code === language));
      option.appendChild(el('b', null, LANGUAGE_NAMES[code]));
      option.appendChild(icon('check'));
      option.addEventListener('click', () => chooseLanguage(code));
      return option;
    }));
  }

  async function chooseLanguage(code) {
    languageDialog.close();
    if (code === language) return;
    const reply = await call('/api/language', { language: code });
    if (!reply.ok) toast(reply.error);  // 成功的话，接下来收到的状态里就是新的语言，在那里换（见 handlers.state）
  }

  // 在原地换成另一种语言。已经画在页面上、记着原文的文字就地重写；按状态画出来的部分（会话列表、用量、
  // 待办、设置面板……）由紧接着的 handlers.state 重画，它们的"指纹"里都带着语言，所以一定会重画。
  function applyLanguage(code) {
    language = code;
    strings = (I18N.tables || {})[code] || {};
    translatePage();
    refreshStatus();    // 发送按钮、"正在工作"
    renderTray();       // 附件卡片上"移除 某某"的提示
    if (turnEdit) renderTurnEditAtts();  // 编辑最后一条消息时，附件卡片上的也一样
    if (languageDialog.open) renderLanguageOptions();
    drawMediaFilters(); // 多媒体：筛选按钮上的字、日期、每一项的说明（"指纹"里带着语言，一定会重画）
    drawMedia();
    if (viewer.open) { labelViewer(); drawViewerChrome(); }  // 查看器：按钮的说明（带着快捷键）、上面的信息
    if (taskEditor.open) { drawEditor(); previewSoon(); }  // 编辑任务的窗口：星期几的按钮、接下来几次的时间
    renderBrowser(browserStatus, true);  // 顶上的提示、设置里的"浏览器"一节
    renderComputer(computerStatus, true);  // 顶上的"智狐正在控制这台电脑"、设置里的"操作整台电脑"一节
  }

  $('language').hidden = !I18N.tables;  // 助手程序是旧版本、不支持多语言时，不显示这个按钮
  $('language').addEventListener('click', () => { renderLanguageOptions(); languageDialog.showModal(); });
  $('close-language').addEventListener('click', () => languageDialog.close());
