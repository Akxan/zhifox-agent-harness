  // ---- 跟着最新内容滚动 ----------------------------------------------------
  // 用户正看着最新内容时，新内容出现后自动滚到底；他一往上翻就不再跟，直到他自己回到底部。
  //
  // 怎么知道他往上翻了：看滚动位置是不是变小了。程序自己滚动只会往下（滚到底），所以位置变小一定是
  // 用户翻的，滚轮、拖滚动条、键盘都算。不能只看"离底部远不远"：回复逐字输出时，用户刚往上翻了一点，
  // 下一段文字一到就又被拉回底部，怎么翻都翻不上去。
  // 这些在滚动事件里判断，一帧最多一次，不用每来一个字就去量位置；"滚到底"这个动作也是一帧最多做一次。

  let stick = true;          // 用户是不是正看着最新的内容
  let lastTop = 0;           // 上一次滚动事件时的位置，用来判断这一次是往上还是往下
  let scrollQueued = false;

  function setStick(value) {
    if (stick === value) return;
    stick = value;
    toBottom.classList.toggle('show', !value);  // 不跟了，就给一个"回到最新"的按钮
    toBottom.tabIndex = value ? -1 : 0;
  }

  function follow() {
    if (!stick || scrollQueued) return;
    scrollQueued = true;
    requestAnimationFrame(() => {
      scrollQueued = false;
      if (stick) chat.scrollTop = chat.scrollHeight;
    });
  }

  chat.addEventListener('scroll', () => {
    const top = chat.scrollTop;
    if (chat.scrollHeight - top - chat.clientHeight < 4) setStick(true);  // 到底了：重新开始跟
    else if (top < lastTop - 1) setStick(false);                          // 位置变小了：是用户往上翻的
    lastTop = top;
  }, { passive: true });

  // 有一种"位置变小"不是用户翻的：内容自己变矮了（思考块折叠、卡片收起、窗口变宽），浏览器把滚动位置
  // 收了回来。这时把上面用来比较的基准一起收回来，否则紧跟着来的新内容会被当成"用户往上翻了"而不再跟。
  // ResizeObserver 在浏览器排完版之后、下一次滚动事件之前通知我们，正好赶在前面。
  if ('ResizeObserver' in window) {
    const settled = new ResizeObserver(() => {
      if (stick) lastTop = Math.min(lastTop, chat.scrollHeight - chat.clientHeight);
    });
    settled.observe(log);
    settled.observe(chat);
  }

  toBottom.addEventListener('click', () => {
    setStick(true);
    chat.scrollTo({ top: chat.scrollHeight, behavior: calm() ? 'auto' : 'smooth' });
  });
