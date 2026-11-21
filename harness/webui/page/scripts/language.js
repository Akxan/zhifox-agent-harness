// ---- 语言 ----------------------------------------------------------------
// 换语言不刷新页面：先请助手程序记下新的选择（下次打开、终端版都跟着用），它会发来新的状态，
// 页面收到后在原地把文字换掉（见 applyLanguage）。正在进行的对话、滚动的位置、输入框里写了一半的话都不受影响。
//
// 选语言的地方是地球按钮弹出来的小面板，和外观、换模型是同一种（开、关、位置、窄屏上从底下升起来，见 flyouts.js）。
// 每一项上面大字是这门语言用它自己的文字写的名字（谁都认得出自己的那一个），下面一行小字是用现在的界面语言写的名字
// （界面是中文时，English 下面写"英语"）。现在用的那一项小字写"当前语言"（不然 English 下面又是一个 English），并打一个勾。
// 是一个菜单（role=menu，每一项是 menuitemradio）：↑↓ 移动，回车选，Esc 收起来、焦点回到地球按钮上。

const languageButton = $('language'), languagePanel = $('languages'), languageOptions = $('language-options');
const LANGUAGE_NAMES = { zh: '中文', en: 'English', es: 'Español' };  // 每种语言都用它自己的文字写
const LANGUAGE_IN_UI = { zh: k('简体中文'), en: k('英语'), es: k('西班牙语') };  // 用现在的界面语言写的名字

const languageFlyout = flyout(languagePanel, languageButton, {
  sheet: true,
  trap: true,
  focus: () => languageOptions.querySelector('[aria-checked="true"]'),
});

function renderLanguageOptions() {
  const focused = languagePanel.contains(document.activeElement) ? document.activeElement.lang : '';
  languageOptions.replaceChildren(...Object.keys(LANGUAGE_NAMES).map((code) => {
    const current = code === language;
    const option = el('button', 'lang-option');
    option.type = 'button';
    option.lang = LOCALES[code];
    option.setAttribute('role', 'menuitemradio');
    option.setAttribute('aria-checked', String(current));
    option.tabIndex = current ? 0 : -1;
    const words = option.appendChild(el('span', 'lang-words'));
    words.appendChild(el('b', null, LANGUAGE_NAMES[code]));
    // 小字用界面的语言写，所以不带这一项的 lang（读屏软件照界面的语言读它）
    const small = words.appendChild(tel('small', null, current ? k('当前语言') : LANGUAGE_IN_UI[code]));
    small.lang = LOCALES[language];
    option.appendChild(icon('check', 'lang-check'));
    option.addEventListener('click', () => chooseLanguage(code));
    return option;
  }));
  const again = focused && languageOptions.querySelector('[lang="' + focused + '"]');
  if (again) again.focus({ preventScroll: true });
}

async function chooseLanguage(code) {
  languageFlyout.close(true);
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
  if (languageFlyout.isOpen()) renderLanguageOptions();
  drawMediaFilters(); // 多媒体：筛选按钮上的字、日期、每一项的说明（"指纹"里带着语言，一定会重画）
  drawMedia();
  if (viewer.open) { labelViewer(); drawViewerChrome(); }  // 查看器：按钮的说明（带着快捷键）、上面的信息
  if (taskEditor.open) { drawEditor(); previewSoon(); }  // 编辑任务的窗口：星期几的按钮、接下来几次的时间
  renderBrowser(browserStatus, true);  // 顶上的提示、设置里的"浏览器"一节
  renderComputer(computerStatus, true);  // 顶上的"智狐正在控制这台电脑"、设置里的"操作整台电脑"一节
  retitleSettingsButton();  // 顶栏齿轮的提示：译好的"设置"后面接着快捷键
  retitleModelButton();     // 模型按钮的提示也一样（见 models.js）
  if (modelsFlyout.isOpen()) drawModels();  // 换模型的面板：价格的写法、按钮的提示跟着界面语言
  if (endpointDialog.open) drawEndpointEditor();  // 编辑自定义模型接口的窗口：按钮的说明里带着模型名、数字的写法
}

$('language').hidden = !I18N.tables;  // 助手程序是旧版本、不支持多语言时，不显示这个按钮
languageButton.addEventListener('click', () => {
  if (languageFlyout.isOpen()) { languageFlyout.close(false); return; }
  renderLanguageOptions();
  languageFlyout.open();
});
languageOptions.addEventListener('keydown', (key) => {
  const items = [...languageOptions.querySelectorAll('.lang-option')];
  const at = items.indexOf(document.activeElement);
  const next = { ArrowDown: at + 1, ArrowUp: at - 1, Home: 0, End: items.length - 1 }[key.key];
  if (next === undefined) return;
  key.preventDefault();
  items[(next + items.length) % items.length].focus();
});
languageOptions.addEventListener('focusin', (focus) => {
  for (const item of languageOptions.querySelectorAll('.lang-option')) item.tabIndex = item === focus.target ? 0 : -1;
});
