// ---- 开始 ----------------------------------------------------------------
// 最后一块：前面各节都定义好了，这里定下两边的栏一开始是开是关，然后连上事件流（/events），页面从这时起动起来。

// 两边的栏一开始是展开还是收起：窄窗口里它们是盖在对话上的抽屉，默认收起；
// 宽窗口里按上次的选择来（信息栏没选过的话，窗口够宽才展开）。
const navWanted = () => !overlayNav.matches && recall('harness.nav') !== 'closed';
const sideWanted = () => {
  const choice = recall('harness.side');
  return !overlaySide.matches && (choice ? choice === 'open' : window.innerWidth >= 1400);
};
setNav(navWanted(), false);
setSide(sideWanted(), false);
// 窗口拉宽拉窄跨过界限时重新决定一次。不然原来并排展开的栏会原样变成抽屉，把对话整个盖住
overlayNav.addEventListener('change', () => setNav(navWanted(), false));
overlaySide.addEventListener('change', () => setSide(sideWanted(), false));
refreshStatus();

if (!token) {
  showBanner(k('地址里缺少口令（token）。请使用终端里显示的完整地址打开。'));
  settle();
  return;
}

// 连接事件流。服务器先把已有的事件补发一遍，然后发一个名叫 synced 的标记，之后才是实时的事件。
const source = new EventSource('/events?token=' + encodeURIComponent(token));
let syncTimer = 0;

function markSynced() {
  clearTimeout(syncTimer);
  if (synced) return;
  synced = true;
  if (block) setStreaming(true);  // 刷新页面时正好有一段文字在输出：从现在起它是实时的了
  if (unsettled && !replaying) settle();
}

let lostTimer = 0;
source.onopen = () => {
  clearTimeout(lostTimer);
  lostTimer = 0;
  banner.hidden = true;
  synced = false;
  clearTimeout(syncTimer);
  syncTimer = setTimeout(markSynced, 1500);  // 保险：万一没等到标记，也不能让页面一直不亮相
};
source.addEventListener('synced', markSynced);
source.addEventListener('synced', refreshMemories);  // 补发的状态可能是很早以前的，记忆文件夹在那之后也许变过
source.onmessage = (message) => {
  // 一条事件读不懂、或者画的时候出了错，都要在页面上说一声：不能让一次工具调用就这样不见了，
  // 也不能因为它连累后面的事件。
  let event = null;
  try {
    event = JSON.parse(message.data);
    const handle = handlers[event.type];
    if (handle) handle(event);
  } catch (problem) {
    console.error(problem);
    try {
      handlers.notice({ level: 'error', key: k('有一项内容没能显示出来（{type}）。可以点开这段会话的文件查看原始记录。'), values: { type: event && event.type ? event.type : '?' } });
    } catch (_) { /* 连提示都画不出来，就只能算了 */ }
  }
};
source.onerror = () => {
  const gone = source.readyState === EventSource.CLOSED;
  showBanner(gone
    ? k('连接已断开：助手程序可能已经退出或重新启动了。请回到终端，用那里显示的新地址打开。')
    : k('连接中断，正在重连……'));
  body.classList.add('ready');  // 连不上也要让页面亮相，否则只看得到一条提示
  if (!gone && busy && !lostTimer) {
    lostTimer = setTimeout(() => {
      lostTimer = 0;
      if (source.readyState === EventSource.OPEN || !busy) return;
      endBlock();
      busy = stopping = false;
      refreshStatus();
    }, 3000);
  }
  if (gone && busy) {  // 助手程序不在了，"正在工作"就不该一直转下去
    endBlock();
    busy = stopping = false;
    refreshStatus();
  }
};
