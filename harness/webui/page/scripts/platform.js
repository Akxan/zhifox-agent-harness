// ---- 是哪种电脑：键盘上的键、文件管理器的名字 ----------------------------------
// 页面上有两类话要看"是哪种电脑"，可看的不是同一台，别混：
//
//   键盘上的键（⌘ 还是 Ctrl，⌥ 还是 Alt）看打开这个页面的电脑（浏览器）：键是在这里按的。
//     你可能在 Mac 上通过 SSH 隧道打开 Linux 服务器上的智狐，这时快捷键照样是 ⌘。
//   文件管理器的名字（访达 / 文件资源管理器 / 文件管理器）看助手程序所在的电脑（状态里的 platform，
//     由 harness/machine.py 算）：点"在访达中显示"，打开的是那台电脑上的窗口。"操作整台电脑"操作的也是那一台。
//
// 两条规矩都只写在这里。别处要写快捷键用 forKeys()，判断按没按"命令键"用 commandHeld()；
// 要按助手程序所在的电脑说话用 byServer()，三种说法都写成 k('…')（检查漏翻译的测试靠它找到这些句子）。

// 打开页面的这台电脑。navigator.platform 已经不推荐用了，但每个浏览器都还有，而且是唯一一个不用另外请求就能读到的
const BROWSER_SYSTEM = (() => {
  const named = navigator.platform || (navigator.userAgentData && navigator.userAgentData.platform) || '';
  return /Mac|iPhone|iPad|iPod/i.test(named) ? 'mac' : /Win/i.test(named) ? 'windows' : 'linux';
})();
const ON_MAC = BROWSER_SYSTEM === 'mac';

// 写给人看的组合键：Mac 上的写法（⌘、⌥），别的电脑上的写法（Ctrl、Alt）
const forKeys = (mac, other) => (ON_MAC ? mac : other);

// 按没按"命令键"：Mac 上是 ⌘，别的电脑上是 Ctrl。另一个键不能同时按着：Mac 上的 Ctrl+K 是"删到行尾"、
// Ctrl+F 是"光标右移一格"，不能占用；Windows 上按着 Windows 键的组合是系统的
const commandHeld = (key) => (ON_MAC ? key.metaKey && !key.ctrlKey : key.ctrlKey && !key.metaKey);

// 助手程序所在的电脑。状态到之前先当作和浏览器是同一台（最常见的情况），状态到了以它说的为准（见 events.js 的 handlers.state）
let serverSystem = BROWSER_SYSTEM;
const serverWords = [];  // 页面一打开就写好、之后不再重画的几处话：知道了是哪台电脑以后重写一遍

// 按助手程序所在的电脑选一种说法
const byServer = (mac, windows, linux) => (serverSystem === 'windows' ? windows : serverSystem === 'linux' ? linux : mac);

// write 现在写一遍；知道了助手程序在哪种电脑上以后再写一遍。write 里用 retext() / tattr()，换语言时照样就地重译
function whenServerKnown(write) {
  serverWords.push(write);
  write();
}

function noteServerSystem(system) {
  if (!['mac', 'windows', 'linux'].includes(system) || system === serverSystem) return;  // 旧版本的助手程序不给这一项：照猜的
  serverSystem = system;
  for (const write of serverWords) write();
}

// 每一样存在本地的东西都有的那个按钮（会话、记忆、待办、Skill……的一行，查看器、多媒体）
const showLabel = () => byServer(k('在访达中显示'), k('在文件资源管理器中显示'), k('在文件管理器中显示'));
const showNamedLabel = () => byServer(k('在访达中显示：{name}'), k('在文件资源管理器中显示：{name}'), k('在文件管理器中显示：{name}'));

// 路径太长时只留最后三段，前面写成"…"。Windows 的路径（C:\…、\\服务器\…）按反斜杠分
function shortPath(path) {
  const full = String(path || '');
  const windows = /^(?:[A-Za-z]:[\\/]|\\\\)/.test(full);
  const slash = windows ? '\\' : '/';
  const parts = windows ? full.split(/[\\/]/) : full.split('/');
  return (parts.length > 3 ? '…' + slash : '') + parts.slice(-3).join(slash);
}

// 在文件管理器里显示了一个文件（folder 为真时：打开了一个文件夹）之后说的那句话，写成 {key, values}，换语言时照着原文重译。
// 助手程序所在的电脑没有文件管理器时（服务器上，状态里的 file_manager 是 false），什么都打不开，就告诉你完整的路径
function revealedSaid(path, folder) {
  const full = String(path || '');
  if (state && state.file_manager === false) return { key: k('这台电脑上没有文件管理器。文件在：{path}'), values: { path: full } };
  const key = folder
    ? byServer(k('已在访达中打开：{path}'), k('已在文件资源管理器中打开：{path}'), k('已在文件管理器中打开：{path}'))
    : byServer(k('已在访达中显示：{path}'), k('已在文件资源管理器中显示：{path}'), k('已在文件管理器中显示：{path}'));
  return { key, values: { path: shortPath(full) } };
}

function revealedText(path, folder) {
  const said = revealedSaid(path, folder);
  return t(said.key, said.values);
}
