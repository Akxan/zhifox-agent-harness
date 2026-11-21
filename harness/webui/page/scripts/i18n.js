  // ---- 多语言 --------------------------------------------------------------
  // 界面上的每一句话都用中文写在它出现的地方，外面包一层 t('…')。界面语言不是中文时，t 去翻译表里查
  // 这句话的译文，查不到就照原样显示中文。句子里会变的部分写成 {名字}，比如 t('找到 {n} 段会话', { n: 3 })。
  //
  // 换语言不刷新页面：已经画在页面上的每一句话都记着自己的"原文"（放在 data-i18n 里），
  // 换语言时照着原文重新查一遍、就地换掉，滚动位置和正在进行的对话都不受影响。
  // 所以凡是要留在页面上的文字，都用下面的 tel() / tattr() 来创建，它们会把原文记下来。

  const I18N = (() => { try { return JSON.parse($('i18n').textContent); } catch (_) { return {}; } })();
  const LOCALES = { zh: 'zh-CN', en: 'en', es: 'es' };  // 日期和数字的写法跟着界面语言走
  let language = I18N.language in LOCALES ? I18N.language : 'zh';
  let strings = (I18N.tables || {})[language] || {};

  const k = (text) => text;  // 只是做个记号：这是一句以后会交给 t() 的原文（检查"有没有漏翻译"的测试靠它找到这些句子）

  // 平台的名字（{platform}）旁边的一个空格，两边都是汉字时去掉（和助手程序的 i18n.fill 是同一条规矩）：中文的句子给
  // {platform} 两边留了空格，是为 Telegram 这样的英文名字留的；填进去的是"飞书"时，"连接 飞书"就多了一个空格。
  // 只管 {platform}：文件名、机器人的名字这些值两边的空格正好把它们和句子隔开，留着
  const CJK_FILL = /(?:(?<=[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]) (?=\{platform\}))?\{(\w+)\}(?:(?<=\{platform\}) (?=[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]))?/g;
  const CJK_EDGE = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/;
  function t(text, values) {
    if (typeof text !== 'string') return text === undefined || text === null ? '' : String(text);
    const out = strings[text] || text;
    if (!values) return out;
    return out.replace(CJK_FILL, (whole, name) => {
      if (!Object.hasOwn(values, name)) return whole;
      // 填进去的值本身也是一句话（程序给的 {key, values}，见 harness/webui/ui.py 的 page_values）：它也照当前的语言译
      const nested = values[name] && typeof values[name] === 'object' && typeof values[name].key === 'string';
      const value = nested ? t(values[name].key, values[name].values) : String(values[name]);
      const before = whole.startsWith(' ') && !CJK_EDGE.test(value.charAt(0)) ? ' ' : '';
      const after = whole.endsWith(' ') && !CJK_EDGE.test(value.charAt(value.length - 1)) ? ' ' : '';
      return before + value + after;
    });
  }

  // 创建一个带文字的元素，并记下原文（和句子里会变的部分）
  function tel(tag, className, text, values) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    return retext(node, text, values);
  }

  // 给已有的元素换一句话（同样记下原文）
  function retext(node, text, values) {
    node.dataset.i18n = text;
    if (values) node.dataset.i18nValues = JSON.stringify(values); else delete node.dataset.i18nValues;
    node.textContent = t(text, values);
    return node;
  }

  // 设置 title、placeholder、aria-label 这类属性上的文字（同样记下原文）
  const ATTRIBUTES = { title: 'i18nTitle', placeholder: 'i18nPlaceholder', 'aria-label': 'i18nAriaLabel' };
  function tattr(node, name, text) {
    node.dataset[ATTRIBUTES[name]] = text;
    node.setAttribute(name, t(text));
    return node;
  }

  // 把页面上所有记着原文的地方按当前语言重写一遍。刚打开页面时做一次，每次换语言再做一次。
  function translatePage() {
    root.lang = LOCALES[language];
    for (const node of document.querySelectorAll('[data-i18n]')) {
      if (!node.dataset.i18n) node.dataset.i18n = node.textContent.trim();  // 第一次：写在页面里的中文就是原文
      node.textContent = t(node.dataset.i18n, node.dataset.i18nValues ? JSON.parse(node.dataset.i18nValues) : undefined);
    }
    for (const [name, key] of Object.entries(ATTRIBUTES)) {
      for (const node of document.querySelectorAll('[data-i18n-' + name + ']')) {
        if (!node.dataset[key]) node.dataset[key] = node.getAttribute(name) || '';
        node.setAttribute(name, t(node.dataset[key]));
      }
    }
  }
  translatePage();
