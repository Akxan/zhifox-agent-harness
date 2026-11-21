// ---- 动效 ----------------------------------------------------------------
// 页面里所有会动的东西都只改 transform 和 opacity。浏览器可以把这两样交给专门的合成线程去做，
// 不用重新排版，所以即使脚本正忙着处理模型不断吐出来的文字，动画也不会卡。

// 现在要不要减少动画：看 <html data-motion>。它综合了系统的"减少动态效果"和用户在外观面板里的选择
// （见"外观"一节的 applyMotion）。脚本里要判断的地方都问它，不要直接查 prefers-reduced-motion。
const calm = () => root.dataset.motion === 'reduce';
const EASE = 'cubic-bezier(.2, .8, .2, 1)';

// 播一次性的小动画。frames 里只写 transform 和 opacity。
function play(node, frames, duration) {
  if (calm() || !node.animate) return;
  node.animate(frames, { duration, easing: EASE });
}

// 布局突然变化时（收起会话栏、展开信息栏），让画面滑到新位置，而不是跳过去。
// 做法是先量好各部分的旧位置，改完布局再量新位置，然后用 transform 把它们"摆回"旧位置，
// 再放手让它们回到原位（这个手法通常叫 FLIP）。布局只重算了一次，滑动的过程交给合成线程。
function relayout(change) {
  if (!body.classList.contains('ready') || calm() || !sheet.animate) { change(); return; }
  const parts = [[sheet, 'box'], [viewport, 'center'], [composer, 'center'], [starters, 'center'],
                 [$('title'), 'left'], [$('bar-actions'), 'right']];
  const before = parts.map(([node]) => node.getBoundingClientRect());
  change();
  parts.forEach(([node, mode], index) => {
    const from = before[index], to = node.getBoundingClientRect();
    if (!from.width || !to.width) return;  // 这一部分现在没有显示
    let transform;
    if (mode === 'box') {  // 那张"纸"的底：位置和宽度都变了，横向拉伸到位
      if (Math.abs(from.left - to.left) < 1 && Math.abs(from.width - to.width) < 1) return;
      transform = 'translateX(' + (from.left - to.left) + 'px) scaleX(' + (from.width / to.width) + ')';
    } else {               // 里面的内容：只平移，不拉伸，文字才不会变形
      const at = (rect) => (mode === 'left' ? rect.left : mode === 'right' ? rect.right : rect.left + rect.width / 2);
      const shift = at(from) - at(to);
      if (Math.abs(shift) < 1) return;
      transform = 'translateX(' + shift + 'px)';
    }
    play(node, [{ transform }, { transform: 'none' }], 380);
  });
}

// 换外观时让新旧两个画面交叉淡化。用的是浏览器自带的"视图过渡"：它给新旧画面各拍一张快照，
// 在合成线程上淡入淡出，页面本身只换了一次颜色。浏览器不支持的话就直接换。
function crossfade(update) {
  if (!document.startViewTransition || calm()) { update(); return; }
  // 前一个过渡还没开始（不到一两帧）又换了一次：浏览器跳过前一个，它的 ready 会被拒绝（AbortError）。
  // 这是预料之中的（新的那次照样会换好颜色），接住它，不然控制台里是一条"未处理的错误"
  document.startViewTransition(update).ready.catch(() => {});
}
