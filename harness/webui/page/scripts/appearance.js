// ---- 外观：颜色和动效 ----------------------------------------------------
// 点右上角的外观按钮，弹出一个小面板：上面一排选颜色（默认浅色），下面一排选动效（默认跟随系统）。
// 两样都记在浏览器里。页面最前面那段脚本在画出页面之前就按它们定好了，这里负责之后的切换。
//
// "动效"是给系统的"减少动态效果"开的一个口子：很多人不知道这个设置藏在系统的哪里，或者只想让这一个页面
// 安静些（选"减少"）；也有人系统里开着它，却想在这里看完整的动画（选"完整"）。

const THEMES = ['light', 'dark', 'auto'];
const MOTIONS = ['auto', 'reduce', 'full'];
const systemDark = matchMedia('(prefers-color-scheme: dark)');
const systemCalm = matchMedia('(prefers-reduced-motion: reduce)');  // 系统设置里的"减少动态效果"
const themeButton = $('theme'), appearance = $('appearance');
let theme = recall('harness.theme');
if (!THEMES.includes(theme)) theme = 'light';
let motion = recall('harness.motion');
if (!MOTIONS.includes(motion)) motion = 'auto';

function applyTheme() {
  root.dataset.theme = theme === 'dark' || (theme === 'auto' && systemDark.matches) ? 'dark' : 'light';
  themeButton.dataset.mode = theme;  // 按钮上显示太阳、月亮还是显示器
  for (const tile of appearance.querySelectorAll('[data-theme-pick]')) {
    tile.setAttribute('aria-pressed', String(tile.dataset.themePick === theme));
  }
}

function applyMotion() {
  root.dataset.motion = motion === 'reduce' || (motion === 'auto' && systemCalm.matches) ? 'reduce' : 'full';
  for (const tile of appearance.querySelectorAll('[data-motion-pick]')) {
    tile.setAttribute('aria-pressed', String(tile.dataset.motionPick === motion));
  }
  // 系统里这个开关各家叫法不同（macOS 是"减弱动态效果"，Windows 是"动画效果"），所以这里不点名
  retext($('motion-note'), motion === 'reduce' ? k('不播放滑动、淡入和闪烁，内容直接出现。')
    : motion === 'full' ? k('播放全部动画，即使系统设置要求减少动画。')
    : systemCalm.matches ? k('你在系统设置里要求减少动画，所以这里的动画也减少了。')
    : k('你的系统设置没有要求减少动画，所以动画完整播放。'));
}
applyTheme();
applyMotion();

function chooseTheme(name) {
  if (name === theme) return;
  theme = name;
  remember('harness.theme', theme);
  crossfade(applyTheme);
}

function chooseMotion(name) {
  if (name === motion) return;
  motion = name;
  remember('harness.motion', motion);
  applyMotion();  // 立刻生效：正在播的动画马上停在结束的样子，或者从现在起开始播
}

// 面板用浏览器的 popover 显示：它在所有内容的最上层，不会被两边的栏挡住或裁掉。
// 很旧的浏览器没有 popover，就当一个普通的浮层来显示和隐藏。
// 用的是 manual 模式：点外面、按 Esc 收起来都由下面自己处理（原因见 root 上的 click）。
const POPOVER = typeof appearance.showPopover === 'function';
if (POPOVER) appearance.hidden = false;
const appearanceOpen = () => (POPOVER ? appearance.matches(':popover-open') : !appearance.hidden);

// 放在外观按钮的正下方，右边和按钮对齐；窗口太窄时往左让一让，不超出屏幕
function placeAppearance() {
  const box = themeButton.getBoundingClientRect();
  const width = appearance.offsetWidth;
  appearance.style.top = Math.round(box.bottom + 8) + 'px';
  appearance.style.left = Math.round(Math.max(8, Math.min(box.right - width, root.clientWidth - 8 - width))) + 'px';
}

function openAppearance() {
  if (POPOVER) appearance.showPopover(); else appearance.hidden = false;
  placeAppearance();  // 显示出来才量得到宽度。这两步在同一帧里，画出来时已经在对的位置上
  themeButton.setAttribute('aria-expanded', 'true');
  // 焦点放到当前选中的那一格：用键盘打开的，可以直接用 Tab 和空格来选；用鼠标打开的，浏览器不会画出焦点框
  appearance.querySelector('.tile[aria-pressed="true"]').focus();
}

function closeAppearance(refocus) {
  if (!appearanceOpen()) return;
  if (POPOVER) appearance.hidePopover(); else appearance.hidden = true;
  themeButton.setAttribute('aria-expanded', 'false');
  if (refocus) themeButton.focus();
}

themeButton.addEventListener('click', () => (appearanceOpen() ? closeAppearance(false) : openAppearance()));
for (const tile of appearance.querySelectorAll('[data-theme-pick]')) {
  tile.addEventListener('click', () => chooseTheme(tile.dataset.themePick));
}
for (const tile of appearance.querySelectorAll('[data-motion-pick]')) {
  tile.addEventListener('click', () => chooseMotion(tile.dataset.motionPick));
}

// 点在哪儿，按位置判断，不看"点中的是谁"：换颜色时的交叉淡化那一小会儿（约 0.3 秒），浏览器把所有点击
// 都算在整个页面（<html>）头上，按钮自己收不到。只看"点中的是谁"的话，点在面板里的那一下会被当成点在外面。
const inside = (node, point) => {
  const box = node.getBoundingClientRect();
  return point.clientX >= box.left && point.clientX <= box.right && point.clientY >= box.top && point.clientY <= box.bottom;
};
// 点面板外面的任何地方，面板就收起来（点外观按钮本身除外：那一下由按钮自己来开关）
document.addEventListener('pointerdown', (press) => {
  if (appearanceOpen() && !inside(appearance, press) && !inside(themeButton, press)) closeAppearance(false);
});
// 焦点去了面板和外观按钮之外（用 Tab 离开、或者打开了别的对话框），也收起来
document.addEventListener('focusin', (focus) => {
  if (appearanceOpen() && !appearance.contains(focus.target) && focus.target !== themeButton) closeAppearance(false);
});
window.addEventListener('resize', () => { if (appearanceOpen()) placeAppearance(); });
// 交叉淡化期间点到页面头上的那一下：按位置找出本来点的是哪个按钮，替它点一下，连点两下才不会丢掉一下
root.addEventListener('click', (click) => {
  if (click.target !== root) return;
  const buttons = [themeButton, ...(appearanceOpen() ? appearance.querySelectorAll('.tile') : [])];
  const hit = buttons.find((button) => inside(button, click));
  if (hit) hit.click();
});

systemDark.addEventListener('change', () => { if (theme === 'auto') crossfade(applyTheme); });
systemCalm.addEventListener('change', applyMotion);  // 系统里开关"减少动态效果"，选"跟随系统"时马上跟着变
