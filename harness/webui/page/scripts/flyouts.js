// ---- 浮出的小面板 ------------------------------------------------------------
// 顶栏上几个按钮弹出来的小面板：外观（appearance.js）、换模型（models.js）、语言（language.js）。它们是同一种东西，
// 开、关、放在哪儿、点外面收起、Esc 和 Tab 的规矩都写在这里一处，三个面板各自只管自己里面画什么。
//
//   - 用浏览器的 popover（manual 模式）显示：它在所有内容的最上层，不会被两边的栏挡住或裁掉。很旧的浏览器没有
//     popover，就当一个普通的浮层来显示和隐藏。点外面、按 Esc 收起来都由这里自己处理（原因见下面的 pointerdown）。
//   - 放在按钮的正下方，右边和按钮对齐；窗口太窄时往左让一让，不超出屏幕。
//   - sheet：窄屏（手机）上改成从底下升起来、占满宽度的面板，后面垫一层暗底，大拇指够得着。
//     点暗底只是收起面板，不会顺带点到下面被盖住的东西。
//   - trap：开着的时候 Tab 只在面板里转（换模型、语言两个面板是这样）。外观面板不拦：Tab 离开它就收起来。
//   - Esc 收起，焦点回到按钮上。面板自己要先处理的（换模型的面板里正在问"要不要开始新对话"）由 escape 说了算。
//   - 同一时间只开一个：打开一个，别的先收起来。
//
// 入场动画写在 styles/flyouts.css 里（每次从隐藏变成显示都从头播一次），所以自动遵守"减少动态效果"。
// 面板只在你点了按钮以后才出现，不会在重放旧内容时出现，不用分实时和重放。

const POPOVER = typeof HTMLElement.prototype.showPopover === 'function';
const SHEET = matchMedia('(max-width: 560px)');  // 和 styles/flyouts.css 里的 .as-sheet 一起改
const flyouts = [];
let swallowClick = false;  // 窄屏上点了暗底：接下来那一下 click 不交给下面被盖住的东西

// 点在哪儿，按位置判断，不看"点中的是谁"：换颜色时的交叉淡化那一小会儿（约 0.3 秒），浏览器把所有点击
// 都算在整个页面（<html>）头上，按钮自己收不到。只看"点中的是谁"的话，点在面板里的那一下会被当成点在外面。
const inside = (node, point) => {
  const box = node.getBoundingClientRect();
  return point.clientX >= box.left && point.clientX <= box.right && point.clientY >= box.top && point.clientY <= box.bottom;
};

// 面板里能用 Tab 走到的东西（看得见、没有禁用）
function tabStops(panel) {
  return [...panel.querySelectorAll('button, input, select, textarea, a[href], [tabindex]')]
    .filter((node) => node.tabIndex >= 0 && !node.disabled && node.getClientRects().length > 0);
}

// 建一个面板。focus：打开以后焦点放在谁身上（返回一个元素）；escape：按 Esc 时先问它，返回 true 表示它处理了，不收起来
function flyout(panel, button, { sheet = false, trap = false, focus = null, escape = null, closed = null } = {}) {
  if (POPOVER) panel.hidden = false;  // 有 popover 时由它管显示和隐藏
  const self = {
    panel,
    button,
    isOpen: () => (POPOVER ? panel.matches(':popover-open') : !panel.hidden),
    open() {
      for (const other of flyouts) if (other !== self) other.close(false);
      if (POPOVER) panel.showPopover(); else panel.hidden = false;
      self.place();  // 显示出来才量得到宽度。这两步在同一帧里，画出来时已经在对的位置上
      button.setAttribute('aria-expanded', 'true');
      const target = focus && focus();
      if (target) target.focus({ preventScroll: true });
    },
    close(refocus) {
      if (!self.isOpen()) return;
      if (POPOVER) panel.hidePopover(); else panel.hidden = true;
      button.setAttribute('aria-expanded', 'false');
      if (refocus) button.focus();
      if (closed) closed();
    },
    toggle() { if (self.isOpen()) self.close(false); else self.open(); },
    place() {
      const asSheet = sheet && SHEET.matches;
      panel.classList.toggle('as-sheet', asSheet);
      if (asSheet) { panel.style.top = panel.style.left = ''; return; }  // 贴着屏幕底边，位置写在样式里
      const box = button.getBoundingClientRect();
      const width = panel.offsetWidth;
      panel.style.top = Math.round(box.bottom + 8) + 'px';
      panel.style.left = Math.round(Math.max(8, Math.min(box.right - width, root.clientWidth - 8 - width))) + 'px';
    },
  };
  panel.addEventListener('keydown', (key) => {
    if (key.key === 'Escape' && !key.isComposing) {
      key.preventDefault();
      key.stopPropagation();  // 不再交给整个页面的 Esc（收起抽屉……）
      if (!(escape && escape())) self.close(true);
    } else if (key.key === 'Tab' && trap) {
      const stops = tabStops(panel);
      if (!stops.length) return;
      const at = stops.indexOf(document.activeElement);
      const next = key.shiftKey ? (at <= 0 ? stops.length - 1 : at - 1) : (at === stops.length - 1 ? 0 : at + 1);
      key.preventDefault();
      stops[next].focus();
    }
  });
  flyouts.push(self);
  return self;
}

const openFlyout = () => flyouts.find((one) => one.isOpen());

// 点面板外面的任何地方，面板就收起来（点它自己的按钮除外：那一下由按钮自己来开关）
document.addEventListener('pointerdown', (press) => {
  for (const one of flyouts) {
    if (!one.isOpen() || inside(one.panel, press) || inside(one.button, press)) continue;
    if (one.panel.classList.contains('as-sheet')) {  // 点的是窄屏上的暗底
      swallowClick = true;
      setTimeout(() => { swallowClick = false; }, 700);  // 按下以后没有松开成一次点击（按住、拖走了）：过一会儿就不再拦
    }
    one.close(false);
  }
});
document.addEventListener('click', (click) => {
  if (!swallowClick) return;
  swallowClick = false;
  click.preventDefault();
  click.stopPropagation();
}, true);
document.addEventListener('pointercancel', () => { swallowClick = false; });
// 焦点去了面板和它的按钮之外（打开了别的对话框、用 Tab 离开了外观面板），也收起来
document.addEventListener('focusin', (focus) => {
  for (const one of flyouts) {
    if (one.isOpen() && !one.panel.contains(focus.target) && focus.target !== one.button) one.close(false);
  }
});
window.addEventListener('resize', () => { const shown = openFlyout(); if (shown) shown.place(); });
