// ===== 鼠标按着的时候别把列表整个换掉 =====
// 按下和松开落在同一个元素上，浏览器才算一次点击。会自己重画的列表（取回新数据就 replaceChildren 的那种），
// 重画正好落在按下和松开之间，按下的那个元素就不在了，这一下点击就丢了：换模型的面板在慢的电脑上真的丢过，
// "扩展 · 钩子"在 GitHub 的机器上也丢过（打开时取了两次列表，第二次的回答落在点击中间）。
//
// 用法：const later = holdRedraws(列表所在的区域)；取回新数据以后写 later(() => 重画())。
//   - 鼠标没在这块区域里按着：马上画，和直接调用一样；
//   - 正按着：只记下最后一次要画的，松开以后再画。松开时先让这一下点击送到（click 紧跟在 pointerup 后面、
//     同一个输入事件里派发），所以补画放进 setTimeout，不在 pointerup 里直接画。
// 只管"数据到了要重画"这一类；点击、按键自己引起的重画发生在松开以后，用不着它。
function holdRedraws(area) {
  let pressed = false;
  let pending = null;
  area.addEventListener('pointerdown', () => { pressed = true; });
  const release = () => {
    if (!pressed) return;
    pressed = false;
    const draw = pending;
    pending = null;
    if (draw) setTimeout(draw, 0);
  };
  document.addEventListener('pointerup', release);
  document.addEventListener('pointercancel', release);
  return (draw) => {
    if (pressed) pending = draw;
    else draw();
  };
}
