"""几样在很多地方都要做的检查，写成函数：给一个标签页（和要查的范围），返回发现的问题（空列表就是没问题）。

每一条都对应 CLAUDE.md 里"界面的细节规矩"中的一条。新功能加了新的列表、新的窗口，把它的选择器加到下面的表里，
这些检查就会照样查到它：

  untranslated(page)   英文、西班牙语界面上还留着没翻译的中文（不算用户自己的内容和"智狐"这个名字）
  overflow(page, ...)  文字冲出了容器、冲出了窗口，或者被截断了却没有省略号
  row_trio(page, sel)  一行本地存着的东西右边是不是"在访达中显示 / 编辑 / 删除"三个按钮
  native_selects(page) 用了浏览器自带箭头的下拉框

这几个检查是"启发式"的：它们按规则去找可疑的地方，找到了不一定都是错，但每一条都值得看一眼。
真的是用户的内容（会话的标题、记忆的名字……），就把它的选择器加进 USER_CONTENT 或 NAMES。
"""

from __future__ import annotations

import json

from .pilot import Page

# 用户自己的内容（或者模型、工具写的内容）：可以是任何语言，"没翻译的中文"检查不看它们。
# 加新功能时，新的列表里显示用户内容的地方加在这里
USER_CONTENT = [
    "#log .msg", "#log .thinking", "#log .call .tool-args", "#log .call .tool-body pre", "#log .call .tool-outcome",
    "#log .confirm pre > span:last-child", "#log .md", "#log .msg-atts",
    "#title", "#workspace", ".path", "#input",
    "#memory-text", "#memory-edit-text", "#prompt-text", "#prompt-preview", "#task-prompt", "#task-title",
    ".mcp-command", ".mcp-meta", ".task-summary", ".task-run-summary",
    "#announce",  # 给读屏软件的播报：读的是回复的原文
    "#reminders .reminder p",  # 提醒的内容是你写的
    ".media-tile-open",  # 图片方格只有说明文字（文件名和会话标题），没有单独放名字的元素
    "#task-detail-body pre", "#task-detail-body .run p",  # 任务详情里：你写的指令、每次运行交回的总结
]
# 用户起的名字：既不检查它们自己，它们的文字也算"认得的"——按钮的说明里带着名字（"编辑任务：每日安排"）不算没翻译
NAMES = [
    ".conv-title", ".conv-snippet", ".todo-text", ".task-title", ".memory-name", ".memory-meta", ".skill-name",
    ".mcp-name", ".media-name", ".media-conv", ".agents-face", "#persona-name", ".chan-name", "#memory-name",
    "#task-detail-title", "#task-detail-sub", ".skill-desc", "#reminders .reminder b", "#viewer-name",
]
# 在任何语言下都照原样写的几个词：标志的名字，以及语言选单里每种语言用它自己的文字写的名字
ALLOWED_WORDS = ["智狐", "中文", "语言"]

_UNTRANSLATED = r"""
(({ roots, skip, names, allowed }) => {
  const CJK = /[㐀-䶿一-鿿豈-﫿]+/g;
  const shown = (node) => node.getClientRects().length > 0 && getComputedStyle(node).visibility !== 'hidden';
  const skipped = (node) => node.closest(skip.concat(names).join(','));
  // 认得的文字：用户起的名字，以及界面上的句子里填进去的值（data-i18n-values，比如 "「{agent}」想要" 里的名字）
  let known = [...document.querySelectorAll(names.join(','))].map((node) => node.textContent).join('\n');
  for (const node of document.querySelectorAll('[data-i18n-values]')) {
    try { known += '\n' + Object.values(JSON.parse(node.dataset.i18nValues)).join('\n'); } catch (_) { /* 坏了就不算 */ }
  }
  const fine = (run) => allowed.includes(run) || known.includes(run);
  const describe = (node) => {
    const path = [];
    for (let at = node; at && at !== document.body && path.length < 4; at = at.parentElement) {
      path.unshift(at.tagName.toLowerCase() + (at.id ? '#' + at.id : '') + (typeof at.className === 'string' && at.className.trim() ? '.' + at.className.trim().split(/\s+/).join('.') : ''));
    }
    return path.join(' > ');
  };
  const problems = [];
  const report = (node, where, text) => {
    const bad = (text.match(CJK) || []).filter((run) => !fine(run));
    if (bad.length) problems.push(describe(node) + where + ': ' + JSON.stringify(text.trim().slice(0, 120)));
  };
  const scopes = roots.flatMap((selector) => [...document.querySelectorAll(selector)]).filter(shown);
  for (const scope of scopes) {
    const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
    for (let text = walker.nextNode(); text; text = walker.nextNode()) {
      const parent = text.parentElement;
      if (!parent || !text.data.trim() || skipped(parent) || !shown(parent) || parent.closest('script, style, template, [hidden]')) continue;
      report(parent, '', text.data);
    }
    for (const node of [scope, ...scope.querySelectorAll('[title], [placeholder], [aria-label]')]) {
      if (skipped(node) || !shown(node)) continue;
      for (const name of ['title', 'placeholder', 'aria-label']) {
        if (node.hasAttribute(name)) report(node, ' [' + name + ']', node.getAttribute(name));
      }
    }
  }
  return [...new Set(problems)];
})
"""


def untranslated(page: Page, roots: list[str] | None = None) -> list[str]:
    """在 roots（默认整个页面）里找看得见的中文：界面是英文或西班牙语时，它们多半是忘了翻译的。"""
    options = {"roots": roots or ["body"], "skip": USER_CONTENT, "names": NAMES, "allowed": ALLOWED_WORDS}
    return page.js(f"({_UNTRANSLATED})({json.dumps(options, ensure_ascii=False)})")


_OVERFLOW = r"""
((roots) => {
  const shown = (node) => node.getClientRects().length > 0 && getComputedStyle(node).visibility !== 'hidden' && Number(getComputedStyle(node).opacity) > 0;
  const describe = (node) => {
    const path = [];
    for (let at = node; at && at !== document.body && path.length < 4; at = at.parentElement) {
      path.unshift(at.tagName.toLowerCase() + (at.id ? '#' + at.id : '') + (typeof at.className === 'string' && at.className.trim() ? '.' + at.className.trim().split(/\s+/).join('.') : ''));
    }
    return path.join(' > ');
  };
  const opaque = (color) => color && color !== 'transparent' && !/rgba\(.*,\s*0\)$/.test(color);
  // 这段文字的"框"：最近的一个会裁剪（overflow 不是 visible）、或者看得出边界（有边框、有底色）的上层元素
  const boxOf = (node) => {
    for (let at = node; at && at !== document.documentElement; at = at.parentElement) {
      const style = getComputedStyle(at);
      if (style.overflowX !== 'visible' || parseFloat(style.borderRightWidth) > 0 || opaque(style.backgroundColor)) return at;
    }
    return document.documentElement;
  };
  const ellipsized = (node, box) => {
    for (let at = node; at; at = at.parentElement) {
      if (getComputedStyle(at).textOverflow === 'ellipsis') return true;
      if (at === box) return false;
    }
    return false;
  };
  const problems = [];
  const width = document.documentElement.clientWidth;
  for (const scope of roots.flatMap((selector) => [...document.querySelectorAll(selector)]).filter(shown)) {
    const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
    for (let text = walker.nextNode(); text; text = walker.nextNode()) {
      const parent = text.parentElement;
      if (!parent || !text.data.trim() || !shown(parent) || parent.closest('[hidden], .sr-only')) continue;
      const range = document.createRange();
      range.selectNodeContents(text);
      const rects = [...range.getClientRects()].filter((rect) => rect.width > 0);
      if (!rects.length) continue;
      const left = Math.min(...rects.map((rect) => rect.left)), right = Math.max(...rects.map((rect) => rect.right));
      const box = boxOf(parent), frame = box.getBoundingClientRect(), style = getComputedStyle(box);
      const scrolls = /auto|scroll/.test(style.overflowX);
      const words = JSON.stringify(text.data.trim().slice(0, 60));
      // 在一个能横着滚动的框里：太宽了可以滚过去看，不算
      if (scrolls) continue;
      if (style.overflowX !== 'visible') {
        // 会裁剪的框：超出去的部分看不见了。单行的要有省略号，不能硬生生切掉半个字
        if ((right > frame.right + 1 || left < frame.left - 1) && !ellipsized(parent, box)) problems.push('截断了但没有省略号：' + describe(parent) + ' ' + words);
        continue;
      }
      if (right > frame.right + 1 || left < frame.left - 1) problems.push('冲出了边框：' + describe(parent) + ' ' + words + ' 框是 ' + describe(box));
      else if (right > width + 1 || left < -1) problems.push('冲出了窗口：' + describe(parent) + ' ' + words);
    }
  }
  return [...new Set(problems)];
})
"""


def overflow(page: Page, roots: list[str]) -> list[str]:
    """roots 里看得见的文字有没有冲出它的框（边框、底色或者会裁剪的那一层）、冲出窗口，或者被截断却没有省略号。"""
    return page.js(f"({_OVERFLOW})({json.dumps(roots)})")


_TRIO = r"""
((selector) => [...document.querySelectorAll(selector)].map((row) => {
  const buttons = [...row.querySelectorAll('button')].filter((button) => button.querySelector('svg use'));
  const icons = buttons.map((button) => button.querySelector('use').getAttribute('href'));
  return { icons, html: row.outerHTML.slice(0, 200) };
}))
"""

TRIO = ["#i-folder", "#i-edit", "#i-trash"]


def row_trio(page: Page, selector: str) -> list[str]:
    """selector 选中的每一行，最后三个带图标的按钮是不是依次为"在访达中显示、编辑、删除"。返回不是的那几行。"""
    rows = page.js(f"({_TRIO})({json.dumps(selector)})")
    assert rows, f"一行都没找到：{selector}"
    return [row["html"] for row in rows if row["icons"][-3:] != TRIO]


def opacity_of(selector: str, index: int = 0, upto: str = "") -> str:
    """一个 JavaScript 表达式：元素自己和它上面几层的透明度乘起来（到 upto 选中的那一层为止），也就是它实际看起来有多不透明。"""
    return (
        f"(node => {{ let value = 1; for (let at = node; at; at = at.parentElement) {{ value *= Number(getComputedStyle(at).opacity);"
        f" if ({json.dumps(upto)} && at.matches({json.dumps(upto or '*')})) break; }} return value; }})"
        f"(document.querySelectorAll({json.dumps(selector)})[{index}])"
    )


_SELECTS = r"""
(() => [...document.querySelectorAll('select')].map((select) => {
  const style = getComputedStyle(select);
  const drawn = select.classList.contains('field') && style.appearance === 'none' && (style.backgroundImage.match(/linear-gradient/g) || []).length === 2;
  return drawn ? null : (select.id || select.outerHTML.slice(0, 120)) + ' appearance=' + style.appearance;
}).filter(Boolean))()
"""


def native_selects(page: Page) -> list[str]:
    """页面上（包括还没打开的窗口里）用了浏览器自带箭头的下拉框：应该是 select.field，箭头由页面自己画。"""
    return page.js(_SELECTS)
