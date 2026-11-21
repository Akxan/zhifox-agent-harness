// 回复里 Markdown 渲染器（harness/webui/markdown.js）的测试。用 Node 自带的测试工具跑，不需要装任何东西：
//
//     node --test tests/markdown.test.mjs
//
// `uv run pytest` 也会跑它（tests/test_markdown.py 调用 node）。
//
// 页面上没有浏览器可用，这里自己做了一个很小的"假文档"：只有 createElement、createTextNode 和几个
// 增删子节点的方法。innerHTML、outerHTML、insertAdjacentHTML 一碰就报错，只允许建出白名单里的标签和属性。
// 渲染器要是哪天偷懒拼了 HTML、或者建出了 <img>、<script>、onclick，这里的测试会失败。

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const Markdown = createRequire(import.meta.url)('../harness/webui/markdown.js');

// ---- 假文档 ----------------------------------------------------------------

const TAGS = new Set(['p', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'em', 'del', 'code', 'pre', 'a', 'span', 'div',
  'ul', 'ol', 'li', 'input', 'blockquote', 'hr', 'table', 'thead', 'tbody', 'tr', 'th', 'td']);
const ATTRIBUTES = new Set(['href', 'target', 'rel', 'title', 'start', 'type', 'disabled', 'checked', 'scope']);

class FakeText {
  constructor(doc, data) { this.ownerDocument = doc; this.nodeType = 3; this.data = String(data); this.parentNode = null; }
  appendData(more) { this.data += more; }
  get textContent() { return this.data; }
}

function forbidden(name) {
  return { get() { throw new Error(name + ' 不许用'); }, set() { throw new Error(name + ' 不许用'); } };
}

class FakeElement {
  constructor(doc, tag) {
    this.ownerDocument = doc;
    this.nodeType = 1;
    this.tagName = tag.toUpperCase();
    this.childNodes = [];
    this.parentNode = null;
    this.attributes = new Map();
    this.className = '';
    const self = this;
    this.classList = {
      add(name) { const set = new Set(self.className.split(' ').filter(Boolean)); set.add(name); self.className = [...set].join(' '); },
      remove(name) { self.className = self.className.split(' ').filter((item) => item && item !== name).join(' '); },
      contains(name) { return self.className.split(' ').includes(name); },
    };
  }
  _adopt(child) {
    assert.ok(child instanceof FakeElement || child instanceof FakeText, '只能放进假文档建出来的节点');
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    return child;
  }
  appendChild(child) { this.childNodes.push(this._adopt(child)); return child; }
  insertBefore(child, before) {
    if (!before) return this.appendChild(child);
    this._adopt(child);
    this.childNodes.splice(this.childNodes.indexOf(before), 0, child);
    return child;
  }
  removeChild(child) {
    const at = this.childNodes.indexOf(child);
    assert.ok(at >= 0, 'removeChild：不是它的子节点');
    this.childNodes.splice(at, 1);
    child.parentNode = null;
    return child;
  }
  replaceChild(fresh, old) {
    const at = this.childNodes.indexOf(old);
    assert.ok(at >= 0, 'replaceChild：不是它的子节点');
    this._adopt(fresh);
    this.childNodes[this.childNodes.indexOf(old)] = fresh;
    old.parentNode = null;
    return old;
  }
  setAttribute(name, value) {
    if (!this.ownerDocument.trusted) assert.ok(ATTRIBUTES.has(name), '不该设置的属性：' + name);
    this.attributes.set(name, String(value));
  }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  get children() { return this.childNodes.filter((node) => node.nodeType === 1); }
  get lastElementChild() { const kids = this.children; return kids[kids.length - 1] || null; }
  get textContent() { return this.childNodes.map((node) => node.textContent).join(''); }
}
for (const name of ['innerHTML', 'outerHTML', 'insertAdjacentHTML', 'innerText', 'outerText']) {
  Object.defineProperty(FakeElement.prototype, name, forbidden(name));
}

class FakeDocument {
  constructor() { this.created = []; this.trusted = false; }
  createElement(tag) {
    if (!this.trusted) assert.ok(TAGS.has(tag), '不该建的标签：' + tag);
    this.created.push(tag);
    return new FakeElement(this, tag);
  }
  createTextNode(text) { return new FakeText(this, text); }
}
for (const name of ['write', 'writeln', 'createRange', 'createDocumentFragment', 'createElementNS']) {
  Object.defineProperty(FakeDocument.prototype, name, { get() { throw new Error('document.' + name + ' 不许用'); } });
}

// 页面给渲染器的两样东西：图标、代码块上的按钮。它们是页面自己建的，不受上面白名单的限制
function hooksFor(doc) {
  const own = (tag, build) => { doc.trusted = true; try { return build(new FakeElement(doc, tag)); } finally { doc.trusted = false; } };
  return {
    icon: (name) => own('svg', (node) => { node.setAttribute('data-icon', name); return node; }),
    codeTools: () => own('button', (node) => { node.className = 'md-copy'; return node; }),
  };
}

function setup() {
  const doc = new FakeDocument();
  const root = doc.createElement('div');
  return { doc, root, view: Markdown.createView(root, hooksFor(doc)) };
}

// 把假文档写成一小段 HTML，方便比较。文字里的 < > & 会转义，所以 "<script>" 文字和 <script> 元素一眼分得出。
// target 和 rel 每个链接都一样，单独测，这里不写出来；图标和代码块按钮写成 <svg> / <button>
const escapeText = (text) => text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
function html(node) {
  if (node.nodeType === 3) return escapeText(node.data);
  const tag = node.tagName.toLowerCase();
  let attrs = node.className ? ` class="${node.className}"` : '';
  for (const [name, value] of [...node.attributes].sort()) {
    if (name === 'target' || name === 'rel' || name === 'data-icon') continue;
    attrs += value === '' ? ` ${name}` : ` ${name}="${escapeText(value).replace(/"/g, '&quot;')}"`;
  }
  if (tag === 'br' || tag === 'hr' || tag === 'input') return `<${tag}${attrs}>`;
  return `<${tag}${attrs}>${node.childNodes.map(html).join('')}</${tag}>`;
}

function render(text) {
  const { root, view } = setup();
  view.update(text, true);
  return root.childNodes.map(html).join('');
}

function walk(node, visit) {
  visit(node);
  if (node.childNodes) node.childNodes.forEach((child) => walk(child, visit));
}

// ---- 段落、标题、强调 -------------------------------------------------------

test('paragraphs keep single line breaks and split on blank lines', () => {
  assert.equal(render('第一行\n第二行\n\n第二段'), '<p>第一行<br>第二行</p><p>第二段</p>');
  assert.equal(render('  前后的空白  \n\n\n'), '<p>前后的空白</p>');
  assert.equal(render('行末两个空格  \n和反斜杠\\\n都是换行'), '<p>行末两个空格<br>和反斜杠<br>都是换行</p>');
  assert.equal(render(''), '');
  assert.equal(render('\n \n\t\n'), '');
});

test('headings: # style, closing hashes, underline style', () => {
  assert.equal(render('# 一\n## 二\n###### 六'), '<h1>一</h1><h2>二</h2><h6>六</h6>');
  assert.equal(render('## 标题 ##'), '<h2>标题</h2>');
  assert.equal(render('#没有空格不是标题\n####### 七个也不是'), '<p>#没有空格不是标题<br>####### 七个也不是</p>');
  assert.equal(render('大标题\n===\n\n小标题\n---'), '<h1>大标题</h1><h2>小标题</h2>');
  assert.equal(render('\\# 转义了'), '<p># 转义了</p>');
});

test('bold, italic, strikethrough and their combinations', () => {
  assert.equal(render('**粗** *斜* __粗__ _斜_ ~~删~~'), '<p><strong>粗</strong> <em>斜</em> <strong>粗</strong> <em>斜</em> <del>删</del></p>');
  assert.equal(render('***又粗又斜***'), '<p><em><strong>又粗又斜</strong></em></p>');
  assert.equal(render('**粗体里有 *斜体* 和 `代码`**'), '<p><strong>粗体里有 <em>斜体</em> 和 <code>代码</code></strong></p>');
  assert.equal(render('a * b * c'), '<p>a * b * c</p>');
  assert.equal(render('snake_case_name 和 __init__'), '<p>snake_case_name 和 <strong>init</strong></p>');
  assert.equal(render('等 5~10 分钟，或 3~5 天'), '<p>等 5~10 分钟，或 3~5 天</p>');
  assert.equal(render('**没有结束'), '<p>**没有结束</p>');
  assert.equal(render('\\*不是强调\\*'), '<p>*不是强调*</p>');
});

test('bold works the way Chinese text is usually written', () => {
  // CommonMark 原本的规则下，这几种写法都加粗不了（记号挨着全角标点、另一边是汉字）
  assert.equal(render('**注意：**这一点很重要'), '<p><strong>注意：</strong>这一点很重要</p>');
  assert.equal(render('这是**重点**内容'), '<p>这是<strong>重点</strong>内容</p>');
  assert.equal(render('他说**“好的”**然后走了'), '<p>他说<strong>“好的”</strong>然后走了</p>');
  assert.equal(render('~~删掉（旧的）~~新的'), '<p><del>删掉（旧的）</del>新的</p>');
});

test('inline code shows its content literally', () => {
  assert.equal(render('用 `ls -la` 看'), '<p>用 <code>ls -la</code> 看</p>');
  assert.equal(render('`` a ` b ``'), '<p><code>a ` b</code></p>');
  assert.equal(render('`<b>**不加粗**</b> \\n`'), '<p><code>&lt;b&gt;**不加粗**&lt;/b&gt; \\n</code></p>');
  assert.equal(render('一个 ` 落单的反引号'), '<p>一个 ` 落单的反引号</p>');
  assert.equal(render('`跨\n行`'), '<p><code>跨 行</code></p>');
});

test('entities are decoded into text, never into markup', () => {
  assert.equal(render('&lt;b&gt;不是标签&lt;/b&gt; &amp; &copy; &#x41;&#66; &nosuch;'), '<p>&lt;b&gt;不是标签&lt;/b&gt; &amp; © AB &amp;nosuch;</p>');
  assert.equal(render('&#0; &#xD800;'), '<p>\ufffd \ufffd</p>');
});

// ---- 代码块 ----------------------------------------------------------------

test('fenced code blocks get a language label, the copy button and plain text content', () => {
  const out = render('```python\nprint("<b>")\n```');
  assert.match(out, /^<div class="md-code"><div class="md-code-bar"><span class="md-code-lang">python<\/span><button class="md-copy"><\/button><\/div><pre><code>/);
  const { root, view } = setup();
  view.update('```python\nif x:\n    print("<b>")  # 注释\n```', true);
  assert.equal(root.textContent, 'pythonif x:\n    print("<b>")  # 注释');
  const spans = [];
  walk(root, (node) => { if (node.tagName === 'SPAN' && node.className.startsWith('tk-')) spans.push([node.className, node.textContent]); });
  assert.deepEqual(spans, [['tk-k', 'if'], ['tk-s', '"<b>"'], ['tk-c', '# 注释']]);
});

test('code fences: tildes, longer closing fences, info strings, unclosed fences', () => {
  assert.equal(render('~~~\na\n~~~'), '<div class="md-code"><div class="md-code-bar"><span class="md-code-lang"></span><button class="md-copy"></button></div><pre><code>a</code></pre></div>');
  assert.match(render('````md\n```\n内层\n```\n````'), /<code>```\n内层\n```<\/code>/);
  assert.match(render('```js title="x.js"\nlet a\n```'), /md-code-lang">js</);
  assert.equal(render('```a`b\n不是代码块'), '<p>```a`b<br>不是代码块</p>');
  // 没写完的代码块：一直到结尾都是代码，不会把后面的内容当成 Markdown
  const { root, view } = setup();
  view.update('看这段：\n```html\n<script>alert(1)</script>\n# 不是标题', true);
  assert.equal(root.children.length, 2);
  assert.equal(root.children[1].children[1].textContent, '<script>alert(1)</script>\n# 不是标题');
  assert.match(html(root), /<span class="tk-k">&lt;script<\/span>/);
  assert.match(render('  ```\n  缩进的\n    多一点\n  ```'), /<code>缩进的\n  多一点<\/code>/);
});

test('code highlighting stays small: unknown languages and huge blocks are left plain', () => {
  assert.equal(Markdown.highlight('let x = 1', ''), null);
  assert.equal(Markdown.highlight('let x = 1', 'brainfuck'), null);
  assert.equal(Markdown.highlight('let x = 1;\n'.repeat(5000), 'js'), null);
  assert.deepEqual(Markdown.highlight('const n = 42 // 答案', 'ts'), [['tk-k', 'const'], ['', ' n = '], ['tk-n', '42'], ['', ' '], ['tk-c', '// 答案']]);
  assert.deepEqual(Markdown.highlight('echo $# # 注释', 'bash'), [['', 'echo $# '], ['tk-c', '# 注释']]);
  assert.deepEqual(Markdown.highlight('SELECT 1 from t', 'sql').filter(([kind]) => kind === 'tk-k').map(([, text]) => text), ['SELECT', 'from']);
  assert.deepEqual(Markdown.highlight("fn f<'a>(x: &'a str)", 'rust').map(([kind]) => kind).filter(Boolean), ['tk-k']);
  assert.deepEqual(Markdown.highlight('+加\n-减\n 同', 'diff'), [['tk-add', '+加\n'], ['tk-del', '-减\n'], ['', ' 同']]);
  assert.deepEqual(Markdown.highlight('a { color: red; } /* 注释 */', 'css').map(([kind]) => kind).filter(Boolean), ['tk-c']);
  assert.deepEqual(Markdown.highlight('<p class="x">1</p>', 'html').filter(([kind]) => kind).map(([, text]) => text), ['<p', '"x"', '</p']);
  assert.deepEqual(Markdown.highlight('<!-- 注释 --><br>', 'xml'), [['tk-c', '<!-- 注释 -->'], ['tk-k', '<br'], ['', '>']]);
  // 着色前后文字一个不差
  const code = 'function f(a) {\n  return `${a}` + "\\"" /* x */ + 0x1F;\n}';
  assert.equal(Markdown.highlight(code, 'js').map(([, text]) => text).join(''), code);
});

// ---- 列表、引用、分隔线 -----------------------------------------------------

test('bullet and numbered lists, tight and loose, with a start number', () => {
  assert.equal(render('- 一\n- 二\n* 换了记号是另一个列表'), '<ul class="md-tight"><li><p>一</p></li><li><p>二</p></li></ul><ul class="md-tight"><li><p>换了记号是另一个列表</p></li></ul>');
  assert.equal(render('3. 三\n4. 四'), '<ol class="md-tight" start="3"><li><p>三</p></li><li><p>四</p></li></ol>');
  assert.equal(render('1) 一\n\n2) 二'), '<ol><li><p>一</p></li><li><p>二</p></li></ol>');
  assert.equal(render('- 一\n接着上一行'), '<ul class="md-tight"><li><p>一<br>接着上一行</p></li></ul>');
  assert.equal(render('段落\n- 列表可以紧跟段落\n\n2. 但从 2 开始的不行'), '<p>段落</p><ul class="md-tight"><li><p>列表可以紧跟段落</p></li></ul><ol start="2" class="md-tight"><li><p>但从 2 开始的不行</p></li></ol>'.replace('<ol start="2" class="md-tight">', '<ol class="md-tight" start="2">'));
  assert.equal(render('段落\n2. 不打断段落'), '<p>段落<br>2. 不打断段落</p>');
  assert.equal(render('1.5 公斤不是列表'), '<p>1.5 公斤不是列表</p>');
});

test('nested lists and blocks inside list items', () => {
  assert.equal(render('- 外\n  - 里\n    1. 更里\n- 外二'),
    '<ul class="md-tight"><li><p>外</p><ul class="md-tight"><li><p>里</p><ol class="md-tight"><li><p>更里</p></li></ol></li></ul></li><li><p>外二</p></li></ul>');
  assert.equal(render('1. 步骤\n\n   ```sh\n   ls\n   ```\n\n   说明\n2. 下一步'),
    '<ol><li><p>步骤</p><div class="md-code"><div class="md-code-bar"><span class="md-code-lang">sh</span><button class="md-copy"></button></div><pre><code>ls</code></pre></div><p>说明</p></li><li><p>下一步</p></li></ol>');
  // 里面那个列表的空行不让外面的列表变"宽松"
  assert.equal(render('- a\n  - b\n\n  - c\n- d'), '<ul class="md-tight"><li><p>a</p><ul><li><p>b</p></li><li><p>c</p></li></ul></li><li><p>d</p></li></ul>');
});

test('task lists show disabled checkboxes', () => {
  assert.equal(render('- [ ] 没做\n- [x] 做完了\n- [X] 大写也算\n- [] 不是任务'),
    '<ul class="md-tight"><li class="md-task"><p><input disabled type="checkbox">没做</p></li><li class="md-task"><p><input checked disabled type="checkbox">做完了</p></li><li class="md-task"><p><input checked disabled type="checkbox">大写也算</p></li><li><p>[] 不是任务</p></li></ul>');
});

test('blockquotes, nesting and lazy continuation', () => {
  assert.equal(render('> 引用\n接着引用\n\n不是引用'), '<blockquote><p>引用<br>接着引用</p></blockquote><p>不是引用</p>');
  assert.equal(render('> 外\n>> 里\n>> - 里面的列表\n> - 外面的列表'),
    '<blockquote><p>外</p><blockquote><p>里</p><ul class="md-tight"><li><p>里面的列表</p></li></ul></blockquote><ul class="md-tight"><li><p>外面的列表</p></li></ul></blockquote>');
  assert.equal(render('>没有空格也行'), '<blockquote><p>没有空格也行</p></blockquote>');
});

test('horizontal rules', () => {
  assert.equal(render('---\n***\n___\n- - -'), '<hr><hr><hr><hr>');
  assert.equal(render('上\n\n---\n\n下'), '<p>上</p><hr><p>下</p>');
  assert.equal(render('--'), '<p>--</p>');
});

// ---- 表格 ------------------------------------------------------------------

test('tables with alignment, escaped pipes and pipes inside code', () => {
  assert.equal(render('| 名称 | 数量 | 说明 |\n|:--|--:|:-:|\n| 苹果 | 3 | `a|b` 和 \\| |\n| 梨 |'),
    '<div class="md-table"><table><thead><tr><th class="md-left" scope="col">名称</th><th class="md-right" scope="col">数量</th><th class="md-center" scope="col">说明</th></tr></thead>' +
    '<tbody><tr><td class="md-left">苹果</td><td class="md-right">3</td><td class="md-center"><code>a|b</code> 和 |</td></tr><tr><td class="md-left">梨</td><td class="md-right"></td><td class="md-center"></td></tr></tbody></table></div>');
  assert.equal(render('a | b\n--|--\n1 | 2 | 多出来的格子不要'),
    '<div class="md-table"><table><thead><tr><th scope="col">a</th><th scope="col">b</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table></div>');
  // 表格可以紧跟在一段话后面；列数对不上的不是表格
  assert.match(render('对比如下：\n| a | b |\n|---|---|\n| 1 | 2 |'), /^<p>对比如下：<\/p><div class="md-table">/);
  assert.equal(render('| a | b |\n|---|\n| 1 | 2 |'), '<p>| a | b |<br>|---|<br>| 1 | 2 |</p>');
  assert.match(render('| a |\n|---|\n| 1 |\n\n后面的段落'), /<\/table><\/div><p>后面的段落<\/p>$/);
});

// ---- 链接和图片 --------------------------------------------------------------

test('links open in a new tab without leaking the page', () => {
  const { root, view } = setup();
  view.update('[说明](https://example.com/a "标题") 和 <https://example.com/b>', true);
  const links = [];
  walk(root, (node) => { if (node.tagName === 'A') links.push(Object.fromEntries(node.attributes)); });
  assert.deepEqual(links, [
    { href: 'https://example.com/a', target: '_blank', rel: 'noopener noreferrer', title: '标题' },
    { href: 'https://example.com/b', target: '_blank', rel: 'noopener noreferrer' },
  ]);
});

test('bare web addresses become links, without the punctuation around them', () => {
  assert.equal(render('看 https://example.com/a。还有 https://example.com/b, 以及（https://example.com/c）'),
    '<p>看 <a href="https://example.com/a">https://example.com/a</a>。还有 <a href="https://example.com/b">https://example.com/b</a>, 以及（<a href="https://example.com/c">https://example.com/c</a>）</p>');
  assert.equal(render('https://en.wikipedia.org/wiki/Foo_(bar) 和 (https://x.com/y)'),
    '<p><a href="https://en.wikipedia.org/wiki/Foo_(bar)">https://en.wikipedia.org/wiki/Foo_(bar)</a> 和 (<a href="https://x.com/y">https://x.com/y</a>)</p>');
  assert.equal(render('**https://example.com/x_y_z**'), '<p><strong><a href="https://example.com/x_y_z">https://example.com/x_y_z</a></strong></p>');
  assert.equal(render('`https://example.com` 和 xhttps://example.com'), '<p><code>https://example.com</code> 和 xhttps://example.com</p>');
  assert.equal(render('[https://example.com](https://example.com/real)'), '<p><a href="https://example.com/real">https://example.com</a></p>');
  assert.equal(render('写信到 <me@example.com> 或 [这里](mailto:me@example.com)'),
    '<p>写信到 <a href="mailto:me@example.com">me@example.com</a> 或 <a href="mailto:me@example.com">这里</a></p>');
});

test('links with any other scheme are shown as text', () => {
  for (const target of ['javascript:alert(1)', 'JaVaScRiPt:alert(1)', 'java\tscript:alert(1)', 'data:text/html,<b>x</b>',
    'file:///etc/passwd', 'vbscript:msgbox', './relative.md', '//evil.example', '<javascript:alert(1)>']) {
    const out = render(`[点我](${target})`);
    assert.ok(!out.includes('<a'), target + ' → ' + out);
    assert.ok(out.includes('点我'), target);
  }
  assert.equal(render('[点我](javascript:alert(1))'), '<p>[点我](javascript:alert(1))</p>');
  assert.equal(render('<javascript:alert(1)>'), '<p>&lt;javascript:alert(1)&gt;</p>');
  assert.equal(render('[没有网址] 和 [空的]() 和 [a]'), '<p>[没有网址] 和 [空的]() 和 [a]</p>');
});

test('images are never loaded: alt text and address only', () => {
  assert.equal(render('![一只狐狸](https://example.com/fox.png)'),
    '<p><a class="md-image" href="https://example.com/fox.png" title="https://example.com/fox.png"><svg></svg><span class="md-image-alt">一只狐狸</span><span class="md-image-url">example.com/fox.png</span></a></p>');
  assert.equal(render('![](data:image/png;base64,AAAA)'),
    '<p><span class="md-image" title="data:image/png;base64,AAAA"><svg></svg><span class="md-image-url">data:image/png;base64,AAAA</span></span></p>');
  // 徽章的写法：链接里套着图片。外面是链接，里面的图片不再是链接（链接不能套链接）
  assert.equal(render('[![构建](https://example.com/badge.svg)](https://example.com/ci)'),
    '<p><a href="https://example.com/ci"><span class="md-image" title="https://example.com/badge.svg"><svg></svg><span class="md-image-alt">构建</span><span class="md-image-url">example.com/badge.svg</span></span></a></p>');
  const { doc, view } = setup();
  view.update('![x](https://example.com/a.png) ' + '![y](javascript:alert(1))', true);
  assert.ok(!doc.created.includes('img'));
});

test('links inside links keep only the text', () => {
  // 里面的链接先成形，外面那对括号就只是文字了（后面的网址照样认成链接，和 GitHub 一样）
  assert.equal(render('[外 [里](https://b.example) 外](https://a.example)'),
    '<p>[外 <a href="https://b.example/">里</a> 外](<a href="https://a.example/">https://a.example</a>)</p>');
  assert.equal(render('[见 https://b.example](https://a.example)'), '<p><a href="https://a.example/">见 https://b.example</a></p>');
});

test('safeUrl accepts only http, https and mailto', () => {
  assert.equal(Markdown.safeUrl(' https://example.com '), 'https://example.com/');
  assert.equal(Markdown.safeUrl('mailto:a@example.com'), 'mailto:a@example.com');
  for (const bad of ['javascript:alert(1)', ' javascript:alert(1)', 'jav&#x09;ascript:x', 'data:,x', 'ftp://x', '/local', '', 'https://' + 'a'.repeat(3000)]) {
    assert.equal(Markdown.safeUrl(bad), null, bad);
  }
});

// ---- 安全 ------------------------------------------------------------------

const NASTY = [
  '<script>alert(1)</script>',
  '<img src=x onerror=alert(1)>',
  '<a href="javascript:alert(1)">点</a>',
  '[x](javascript:alert(1)) [y]( javascript:alert(1) ) [z](JAVASCRIPT:alert(1))',
  '![x](javascript:alert(1)) ![y](https://x.example" onerror="alert(1))',
  '[x](https://example.com "title\\" onmouseover=\\"alert(1)")',
  '<style>body{display:none}</style><iframe src="https://evil.example"></iframe>',
  '```html\n<script>alert(1)</script>\n```',
  '| <b>x</b> | [a](javascript:x) |\n|---|---|\n| <svg onload=alert(1)> | ![i](data:image/svg+xml,<svg onload=alert(1)>) |',
  '> <form action="https://evil.example"><input name=password></form>',
  '- [x] <input type=checkbox onclick=alert(1)>',
  '&lt;script&gt;alert(1)&lt;/script&gt; &#60;img src=x onerror=alert(1)&#62;',
  '<https://example.com/" onclick="alert(1)>',
  '[a](<https://example.com/"onclick="alert(1)>)',
];

test('malicious input is rendered as text, with only safe tags and attributes', () => {
  for (const text of NASTY) {
    const { doc, root, view } = setup();
    view.update(text, true);  // 假文档的白名单不认的标签、属性会直接报错
    walk(root, (node) => {
      if (node.nodeType !== 1) return;
      const href = node.getAttribute('href');
      if (href !== null) assert.match(href, /^(https?:\/\/|mailto:)/, text);
      for (const name of node.attributes.keys()) assert.ok(!name.startsWith('on'), text);
    });
    assert.ok(!doc.created.some((tag) => ['script', 'img', 'iframe', 'style', 'form', 'svg'].includes(tag)), text);
  }
  assert.equal(render('<script>alert(1)</script>'), '<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>');
  assert.equal(render('<img src=x onerror=alert(1)>'), '<p>&lt;img src=x onerror=alert(1)&gt;</p>');
  // 网址在引号处就断了，后面的 onclick 只是文字
  assert.equal(render('<https://example.com/" onclick="alert(1)>'), '<p>&lt;<a href="https://example.com/">https://example.com/</a>" onclick="alert(1)&gt;</p>');
  assert.equal(render('[a](<https://example.com/"onclick="alert(1)>)'), '<p><a href="https://example.com/%22onclick=%22alert(1)">a</a></p>');
});

test('the renderer never touches HTML-parsing APIs', () => {
  const { root, view } = setup();
  // 假文档的 innerHTML 等一碰就抛错。整个渲染（包括逐字输出时的增量更新）走一遍都不该抛
  const text = NASTY.join('\n\n') + '\n\n' + RICH;
  for (let end = 1; end <= text.length; end += 7) view.update(text.slice(0, end), false);
  view.update(text, true);
  assert.ok(root.textContent.includes('<script>alert(1)</script>'));
});

// ---- 逐字输出 --------------------------------------------------------------

const RICH = `# 智狐的 Markdown

先说结论：**可以用**，而且 *很快*。参考 https://example.com/docs 和 [说明](https://example.com/guide)。

## 步骤

1. 安装依赖：\`uv sync\`
2. 运行：

   \`\`\`bash
   uv run harness --web
   \`\`\`

3. 打开浏览器
   - 浅色
   - 深色
     - [x] 已检查对比度
     - [ ] 窄屏

> 引用里也可以有 **粗体**
> 和第二行
>
> - 还有列表

| 功能 | 状态 | 备注 |
|:-----|:----:|-----:|
| 表格 | ✅ | 可以横向滚动 |
| 代码 | ✅ | \`复制\` 按钮 |
| 图片 | ❌ | ![示意图](https://example.com/a.png) |

---

\`\`\`python
def hello(name: str) -> str:
    """打招呼"""
    return f"你好，{name}"  # 注释
\`\`\`

最后一段~~删掉~~，结束。
`;

// 一个简单的伪随机数，让"每次来几个字"的切法固定下来，测试失败时可以重现
function chunks(text, seed, most) {
  let state = seed;
  const out = [];
  for (let at = 0; at < text.length;) {
    state = (state * 1103515245 + 12345) % 2147483648;
    const size = 1 + (state % most);
    out.push(text.slice(at, at + size));
    at += size;
  }
  return out;
}

test('streaming in small pieces ends up exactly like rendering all at once', () => {
  const expected = render(RICH);
  for (const [seed, most] of [[1, 1], [2, 3], [3, 8], [4, 40], [5, 400]]) {
    const { root, view } = setup();
    let text = '';
    for (const piece of chunks(RICH, seed, most)) {
      text += piece;
      view.update(text, false);
    }
    view.update(text, true);
    assert.equal(root.childNodes.map(html).join(''), expected, `seed ${seed}`);
  }
});

test('long lists and tables streamed piece by piece match a one-shot render', () => {
  const docs = [
    Array.from({ length: 60 }, (_, n) => `${n + 1}. 第 ${n + 1} 项 **粗**`).join('\n') + '\n\n后面的段落',
    // 写到一半才出现空行：整个列表从紧凑变成宽松
    Array.from({ length: 30 }, (_, n) => `- 项 ${n}`).join('\n') + '\n\n- 隔着空行的一项\n- 再一项',
    Array.from({ length: 30 }, (_, n) => `- 项 ${n}\n  - 子项 ${n}\n\n    子项的第二段`).join('\n'),
    Array.from({ length: 20 }, (_, n) => `- [${n % 2 ? 'x' : ' '}] 任务 ${n}\n  接着写`).join('\n') + '\n* 换了记号\n',
    '| a | b |\n|:-|-:|\n' + Array.from({ length: 50 }, (_, n) => `| ${n} | \`${n}|${n}\` |`).join('\n') + '\n后面紧跟着一段话',
    '| a | b |\n|---|---|\n' + Array.from({ length: 50 }, (_, n) => `| ${n} | x |`).join('\n') + '\n\n## 标题\n\n| c |\n|---|\n| 1 |',
    '> - 引用里的列表\n'.repeat(40) + '\n```\n代码\n```',
  ];
  for (const doc of docs) {
    const expected = render(doc);
    for (const [seed, most] of [[11, 2], [12, 9], [13, 60]]) {
      const { root, view } = setup();
      let text = '';
      for (const piece of chunks(doc, seed, most)) {
        text += piece;
        view.update(text, false);
      }
      view.update(text, true);
      assert.equal(root.childNodes.map(html).join(''), expected, `seed ${seed}: ${doc.slice(0, 30)}`);
    }
  }
});

test('finished blocks are drawn once and then left alone', () => {
  const { root, view } = setup();
  let text = '';
  const seen = new Map();  // 每个已经定稿的元素：第一次看到它时，它前面有几个元素
  for (const piece of chunks(RICH, 7, 5)) {
    text += piece;
    view.update(text, false);
    const kids = root.children;
    // 除了最后两块，前面的元素一旦出现就不该再被换掉
    kids.slice(0, -2).forEach((node, index) => {
      if (!seen.has(node)) seen.set(node, index);
      else assert.equal(seen.get(node), index);
    });
  }
  const finals = root.children;
  view.update(text, true);
  for (const node of seen.keys()) assert.ok(root.children.includes(node), '定稿的元素被重建了');
  assert.ok(finals.length > 8);
});

test('a long list or table being written only redraws its last item', () => {
  const { root, view } = setup();
  let text = '';
  for (let n = 1; n <= 30; n += 1) {
    text += `- 第 ${n} 项\n`;
    view.update(text, false);
  }
  const items = root.children[0].children;
  assert.equal(items.length, 30);
  text += '- 第 31 项';
  view.update(text, false);
  assert.equal(root.children[0].children[0], items[0], '前面的项不该重建');
  assert.equal(root.children[0].children.length, 31);

  const table = setup();
  let rows = '| a | b |\n|---|---|\n';
  for (let n = 1; n <= 20; n += 1) {
    rows += `| ${n} | x |\n`;
    table.view.update(rows, false);
  }
  const first = table.root.children[0];
  const firstRow = first.children[0].children[1].children[0];
  rows += '| 21 | y |\n';
  table.view.update(rows, false);
  assert.equal(table.root.children[0], first);
  assert.equal(first.children[0].children[1].children[0], firstRow);
});

test('an unclosed code fence looks like a code block while it streams', () => {
  const { root, view } = setup();
  view.update('看代码：\n```py\nprint(1)\nprint(2', false);
  const box = root.children[1];
  assert.equal(box.className, 'md-code');
  const code = box.children[1].children[0];
  assert.equal(code.textContent, 'print(1)\nprint(2');
  // 原文停在一行的末尾时，代码块下面不多出一个空行
  const blank = setup();
  blank.view.update('```py\nprint(1)\n', false);
  assert.equal(blank.root.textContent, 'pyprint(1)');
  blank.view.update('```py\nprint(1)\n\nprint(2)', false);
  assert.equal(blank.root.textContent, 'pyprint(1)\n\nprint(2)');
  assert.equal(render('```\n结尾没有收起来\n'), render('```\n结尾没有收起来'));
  assert.ok(code.classList.contains('md-caret'), '光点跟在代码后面');
  // 新来的字直接接在后面，代码元素本身不换
  view.update('看代码：\n```py\nprint(1)\nprint(2)\n# 还没完', false);
  assert.equal(root.children[1].children[1].children[0], code);
  assert.equal(code.textContent, 'print(1)\nprint(2)\n# 还没完');
  // 写完之后着色
  view.update('看代码：\n```py\nprint(1)\nprint(2)\n# 还没完\n```\n', false);
  assert.ok(root.textContent.endsWith('print(1)\nprint(2)\n# 还没完'));
  assert.ok(root.children[1].children[1].children[0].children.some((node) => node.className === 'tk-c'));
});

test('half-written markers are held back instead of flashing', () => {
  const shown = (text) => { const { root, view } = setup(); view.update(text, false); return root.childNodes.map(html).join(''); };
  assert.equal(shown('段落\n-'), '<p class="md-caret">段落</p>');
  assert.equal(shown('段落\n2'), '<p class="md-caret">段落</p>');
  assert.equal(shown('段落\n##'), '<p class="md-caret">段落</p>');
  assert.equal(shown('段落\n``'), '<p class="md-caret">段落</p>');
  assert.equal(shown('段落\n- 有字了'), '<p>段落</p><ul class="md-tight"><li><p class="md-caret">有字了</p></li></ul>');
  assert.equal(shown('段落\n```pyth'), '<p class="md-caret">段落</p>');
  assert.equal(shown('看 [说明](https://exa'), '<p class="md-caret">看</p>');
  assert.equal(shown('看 ![图](https://exa'), '<p class="md-caret">看</p>');
  assert.equal(shown('看 [说明'), '<p class="md-caret">看</p>');
  assert.equal(shown('看 [说明]'), '<p class="md-caret">看</p>');
  assert.equal(shown('- 一\n- [x'), '<ul class="md-tight"><li><p class="md-caret">一</p></li></ul>');
  assert.equal(shown('- 一\n  - [ ]'), '<ul class="md-tight"><li><p class="md-caret">一</p></li></ul>');
  assert.match(shown('看 [说明](https://example.com) 和'), /<a href="https:\/\/example.com\/">说明<\/a> 和<\/p>$/);
  // 可能是表头的那一行，等分隔行到了才画，不先露出一排竖线
  assert.equal(shown('对比：\n| 名称 | 说'), '<p class="md-caret">对比：</p>');
  assert.equal(shown('对比：\n| 名称 | 说明 |\n'), '<p class="md-caret">对比：</p>');
  assert.equal(shown('对比：\n| 名称 | 说明 |\n| --- | -'), '<p class="md-caret">对比：</p>');
  assert.match(shown('对比：\n| 名称 | 说明 |\n| --- | --- |\n'), /<div class="md-table">.*<th scope="col">名称<\/th>/);
  assert.match(shown('| a | b |\n|---|---|\n| 1 | 2'), /<td class="md-caret">2<\/td>/);
  // 写完了就全部画出来
  assert.equal(render('段落\n-'), '<h2>段落</h2>');
  assert.equal(render('| 孤零零的一行 |'), '<p>| 孤零零的一行 |</p>');
});

test('the streaming dot sits after the last character, and goes away at the end', () => {
  const { root, view } = setup();
  const carets = () => { const found = []; walk(root, (node) => { if (node.classList && node.classList.contains('md-caret')) found.push(node.tagName); }); return found; };
  view.update('', false);
  assert.deepEqual(carets(), ['DIV']);  // 还没有字：光点在开头
  view.update('- 一\n  > 引用', false);
  assert.deepEqual(carets(), ['P']);
  view.update('- 一\n  > 引用\n\n---\n', false);
  assert.deepEqual(carets(), []);  // 最后是分隔线：没有地方放光点
  view.update('- 一\n  > 引用\n\n---\n', true);
  assert.deepEqual(carets(), []);
});

// ---- 来源的记号 --------------------------------------------------------------
// 联网搜索的回答里，页面在被引用的那段文字后面插一个记号： 编号 （见 page/scripts/citations.js）。
// 渲染器认出它，调用页面的 hooks.cite(编号) 建元素，自己不建链接；代码里、链接文字里不放；不成对的半个记号丢掉。

const OPEN = '', CLOSE = '';
const mark = (n) => OPEN + n + CLOSE;

// 页面那一边的 hooks.cite：建一个 <sup>（页面自己建的元素，不受白名单限制），记下被要了哪几个编号
function citing() {
  const doc = new FakeDocument();
  const root = doc.createElement('div');
  const asked = [];
  const hooks = hooksFor(doc);
  hooks.cite = (n) => {
    asked.push(n);
    doc.trusted = true;
    try { const sup = new FakeElement(doc, 'sup'); sup.appendChild(doc.createTextNode('[' + n + ']')); return sup; } finally { doc.trusted = false; }
  };
  return { root, asked, view: Markdown.createView(root, hooks) };
}

function renderCited(text) {
  const { root, asked, view } = citing();
  view.update(text, true);
  return { html: root.childNodes.map(html).join(''), asked };
}

test('a citation mark becomes whatever the page builds for it, right where it sits', () => {
  assert.deepEqual(renderCited('Shannon was born in 1916' + mark(0) + ' in Petoskey' + mark(1) + mark(2) + '.'),
    { html: '<p>Shannon was born in 1916<sup>[0]</sup> in Petoskey<sup>[1]</sup><sup>[2]</sup>.</p>', asked: [0, 1, 2] });
  // 粗体、列表、表格、标题里都放得下
  assert.equal(renderCited('**粗体的结论' + mark(3) + '**').html, '<p><strong>粗体的结论<sup>[3]</sup></strong></p>');
  assert.equal(renderCited('- 第一项' + mark(0) + '\n- 第二项').html, '<ul class="md-tight"><li><p>第一项<sup>[0]</sup></p></li><li><p>第二项</p></li></ul>');
  assert.equal(renderCited('| 公司 | 城市 |\n|---|---|\n| Perfect Items' + mark(0) + ' | Madrid |').html,
    '<div class="md-table"><table><thead><tr><th scope="col">公司</th><th scope="col">城市</th></tr></thead><tbody><tr><td>Perfect Items<sup>[0]</sup></td><td>Madrid</td></tr></tbody></table></div>');
  assert.equal(renderCited('## 标题' + mark(5)).html, '<h2>标题<sup>[5]</sup></h2>');
});

test('citation marks never land inside code or link text, and never break a link', () => {
  // 代码里的记号去掉（行内代码和代码块都是），代码原样
  assert.deepEqual(renderCited('`npm install' + mark(0) + '`'), { html: '<p><code>npm install</code></p>', asked: [] });
  assert.deepEqual(renderCited('```sh\nls' + mark(0) + '\n```'),
    { html: '<div class="md-code"><div class="md-code-bar"><span class="md-code-lang">sh</span><button class="md-copy"></button></div><pre><code>ls</code></pre></div>', asked: [] });
  // 链接的文字里不放（链接里不能再套链接）
  assert.deepEqual(renderCited('[维基百科' + mark(0) + '](https://en.wikipedia.org)'),
    { html: '<p><a href="https://en.wikipedia.org/">维基百科</a></p>', asked: [] });
  // 裸网址到记号为止，记号照样画在后面
  assert.deepEqual(renderCited('见 https://example.com/a' + mark(1) + ' 这里'),
    { html: '<p>见 <a href="https://example.com/a">https://example.com/a</a><sup>[1]</sup> 这里</p>', asked: [1] });
});

test('without a cite hook, or with half a mark, nothing extra shows up', () => {
  assert.equal(render('结论' + mark(0) + '。'), '<p>结论。</p>');  // 子 Agent 的卡片这类地方没有 hooks.cite：什么都不画
  assert.deepEqual(renderCited('半个' + OPEN + '记号' + CLOSE + '和' + OPEN + 'x' + CLOSE + '。'), { html: '<p>半个记号和x。</p>', asked: [] });
  assert.deepEqual(renderCited(OPEN + '1234567' + CLOSE), { html: '<p>1234567</p>', asked: [] });  // 编号太长：不是记号
});

test('a mark added while streaming only redraws the block it is in', () => {
  const { root, asked, view } = citing();
  view.update('第一段。\n\n第二段的结论', false);
  const first = root.childNodes[0];
  view.update('第一段。\n\n第二段的结论' + mark(0), false);
  assert.equal(root.childNodes[0], first);  // 定稿的第一段没有重画
  assert.equal(root.childNodes.map(html).join(''), '<p>第一段。</p><p class="md-caret">第二段的结论<sup>[0]</sup></p>');
  view.update('第一段。\n\n第二段的结论' + mark(0) + '，接着写。', true);
  assert.equal(root.childNodes.map(html).join(''), '<p>第一段。</p><p>第二段的结论<sup>[0]</sup>，接着写。</p>');
  assert.deepEqual(asked, [0, 0]);
});

// ---- 很长的输入 --------------------------------------------------------------

function timed(label, budget, run) {
  const start = performance.now();
  run();
  const spent = performance.now() - start;
  assert.ok(spent < budget, `${label} 用了 ${Math.round(spent)} ms（上限 ${budget} ms）`);
}

const LINES = 10000;
const BIG = {
  mixed: Array.from({ length: LINES / 10 }, (_, n) => `## 第 ${n} 节\n\n一段 **粗体** 和 \`代码\`，链接 https://example.com/${n}。\n\n- 项 ${n}\n- [x] 完成\n\n| a | b |\n|---|---|\n| ${n} | x |\n`).join(''),
  code: '```js\n' + 'const value = compute(1, "two", 3.0); // 注释\n'.repeat(LINES) + '```\n',
  list: Array.from({ length: LINES }, (_, n) => `${n % 3 ? '  ' : ''}- 第 ${n} 项 *斜体*`).join('\n'),
  table: '| 编号 | 名字 | 说明 |\n|---|:-:|--:|\n' + Array.from({ length: LINES }, (_, n) => `| ${n} | 名字${n} | \`x|y\` 说明 |`).join('\n'),
  quote: Array.from({ length: LINES }, (_, n) => '> '.repeat(1 + (n % 5)) + `第 ${n} 行`).join('\n'),
  paragraph: Array.from({ length: LINES }, (_, n) => `第 ${n} 行有 *星号 和 **粗体 和 _下划线 和 [括号 和 \`反引号 和 <尖括号`).join('\n'),
  stars: '*a **b ***c ~~d '.repeat(LINES),
  brackets: '[['.repeat(LINES) + '](https://x.example)' + ']'.repeat(LINES) + '![a]('.repeat(LINES),
  ticks: Array.from({ length: LINES }, (_, n) => '`'.repeat(1 + (n % 7)) + ' x').join(' '),
};

test('10,000-line inputs render quickly, whatever their shape', () => {
  for (const [name, text] of Object.entries(BIG)) {
    timed(name, 3000, () => {
      const { root, view } = setup();
      view.update(text, true);
      assert.ok(root.childNodes.length > 0, name);
    });
  }
});

test('streaming a 10,000-line reply stays linear', () => {
  for (const name of ['mixed', 'list', 'table', 'code']) {
    const text = BIG[name];
    timed('逐字输出 ' + name, 8000, () => {
      const { view } = setup();
      // 页面一帧最多画一次：这里每 400 个字画一次，大约相当于每秒 60 帧、每帧来 400 个字
      for (let end = 400; end < text.length; end += 400) view.update(text.slice(0, end), false);
      view.update(text, true);
    });
  }
});
