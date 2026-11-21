// ---- 两边的栏：展开和收起 ------------------------------------------------

const overlayNav = matchMedia('(max-width: 960px)');    // 窄屏上，会话栏是盖在对话上面的抽屉（和样式里的 960px 一致）
const overlaySide = matchMedia('(max-width: 1180px)');  // 信息栏也一样

// 窄屏上两边的栏是盖在对话上的抽屉。抽屉开着时，被遮住的部分设成 inert：点不到，Tab 也走不进去，
// 读屏软件也不读。不然用键盘的人按 Tab 会跑到抽屉后面看不见的地方去
function updateInert() {
  const navOver = overlayNav.matches && !body.classList.contains('nav-closed');
  const sideOver = overlaySide.matches && body.classList.contains('side-open');
  main.inert = navOver || sideOver;
  side.inert = navOver;
  nav.inert = sideOver && !navOver;
}

function setNav(open, keep) {
  const was = !body.classList.contains('nav-closed');
  const change = () => {
    body.classList.toggle('nav-closed', !open);
    $('nav-open').hidden = $('new-compact').hidden = open;
    $('nav-open').setAttribute('aria-expanded', String(open));
    $('nav-close').setAttribute('aria-expanded', String(open));
    updateInert();
  };
  if (was === open) change(); else relayout(change);
  if (open) {
    placePill(true);
    if (!was) play(nav, [{ opacity: 0, transform: 'translateX(-18px)' }, { opacity: 1, transform: 'none' }], 360);
  }
  if (keep) remember('harness.nav', open ? 'open' : 'closed');
}

function setSide(open, keep) {
  const was = body.classList.contains('side-open');
  const change = () => {
    body.classList.toggle('side-open', open);
    $('side-toggle').setAttribute('aria-pressed', String(open));
    updateInert();
  };
  if (was === open) change(); else relayout(change);
  if (open && !was) play(side, [{ opacity: 0, transform: 'translateX(18px)' }, { opacity: 1, transform: 'none' }], 360);
  if (keep) remember('harness.side', open ? 'open' : 'closed');
}

// 抽屉收起来以后，焦点回到打开它的按钮上（焦点本来在抽屉里、或者已经掉到页面上的时候）
function refocus(button, drawer) {
  const active = document.activeElement;
  if (!active || active === body || drawer.contains(active)) button.focus({ preventScroll: true });
}
$('nav-close').addEventListener('click', () => { setNav(false, true); if (overlayNav.matches) refocus($('nav-open'), nav); });
// 用键盘打开的（click.detail 为 0）：焦点直接放进抽屉；用鼠标点的不动焦点，免得手机上弹出键盘
$('nav-open').addEventListener('click', (click) => { setNav(true, true); if (click.detail === 0) $('nav-close').focus(); });
$('side-toggle').addEventListener('click', (click) => {
  const open = !body.classList.contains('side-open');
  setSide(open, true);
  if (open && overlaySide.matches && click.detail === 0) $('side-close').focus();
});
$('side-close').addEventListener('click', () => { setSide(false, false); refocus($('side-toggle'), side); });
$('foot-cost').addEventListener('click', () => setSide(!body.classList.contains('side-open'), true));
$('scrim').addEventListener('click', () => {
  if (overlayNav.matches) setNav(false, false);
  if (overlaySide.matches) setSide(false, false);
});
