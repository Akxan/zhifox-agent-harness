// ---- 外观：颜色和动效 ----------------------------------------------------
// 点右上角的外观按钮，弹出一个小面板：上面一排选颜色（默认浅色），下面一排选动效（默认跟随系统）。
// 两样都记在浏览器里。页面最前面那段脚本（first-paint.js）在画出页面之前就按它们定好了，这里负责之后的切换。
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

// 装成应用（或者做成"在窗口中打开"的快捷方式）以后，窗口的标题栏是 <meta name="theme-color"> 的颜色。
// 它跟着这里选的外观走，不跟系统走（默认是浅色，系统是深色也一样），所以不用 media 查询，而是每次换外观时改一下：
// 取的是样式里窗口底色 --bg 的值（颜色只写在 styles/tokens.css 一处）
const themeColor = document.querySelector('meta[name="theme-color"]');

function applyTheme() {
  root.dataset.theme = theme === 'dark' || (theme === 'auto' && systemDark.matches) ? 'dark' : 'light';
  if (themeColor) themeColor.content = getComputedStyle(root).getPropertyValue('--bg').trim();
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

// 面板的开、关、位置、点外面收起都是顶栏上几个小面板共用的做法（见 flyouts.js）。外观面板不拦 Tab：Tab 离开它就收起来。
// 打开时焦点放到当前选中的那一格：用键盘打开的，可以直接用 Tab 和空格来选；用鼠标打开的，浏览器不会画出焦点框
const appearanceFlyout = flyout(appearance, themeButton, { focus: () => appearance.querySelector('.tile[aria-pressed="true"]') });
const appearanceOpen = () => appearanceFlyout.isOpen();
const closeAppearance = (refocus) => appearanceFlyout.close(refocus);

themeButton.addEventListener('click', () => appearanceFlyout.toggle());
for (const tile of appearance.querySelectorAll('[data-theme-pick]')) {
  tile.addEventListener('click', () => chooseTheme(tile.dataset.themePick));
}
for (const tile of appearance.querySelectorAll('[data-motion-pick]')) {
  tile.addEventListener('click', () => chooseMotion(tile.dataset.motionPick));
}

// 交叉淡化期间点到页面头上的那一下（见 flyouts.js 的 inside）：按位置找出本来点的是哪个按钮，替它点一下，连点两下才不会丢掉一下
root.addEventListener('click', (click) => {
  if (click.target !== root) return;
  const buttons = [themeButton, ...(appearanceOpen() ? appearance.querySelectorAll('.tile') : [])];
  const hit = buttons.find((button) => inside(button, click));
  if (hit) hit.click();
});

systemDark.addEventListener('change', () => { if (theme === 'auto') crossfade(applyTheme); });
systemCalm.addEventListener('change', applyMotion);  // 系统里开关"减少动态效果"，选"跟随系统"时马上跟着变
