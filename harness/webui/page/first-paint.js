// 页面画出来之前就定好用浅色还是深色、动画要不要减少，免得先亮后暗地闪一下，或者先播了一段动画。
// 颜色默认是浅色，动效默认跟随系统的"减少动态效果"；用户在外观面板里另选过的话，按他选的来。
// 算出来的结果写在 <html> 上（data-theme、data-motion），样式和脚本都只看这两个属性。
(() => {
  let theme = null, motion = null;
  try {
    theme = localStorage.getItem('harness.theme');
    motion = localStorage.getItem('harness.motion');
  } catch (_) { /* 浏览器禁用了本地存储 */ }
  const dark = theme === 'dark' || (theme === 'auto' && matchMedia('(prefers-color-scheme: dark)').matches);
  const calm = motion === 'reduce' || (motion !== 'full' && matchMedia('(prefers-reduced-motion: reduce)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  document.documentElement.dataset.motion = calm ? 'reduce' : 'full';
})();
