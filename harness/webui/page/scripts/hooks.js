// ---- 扩展 · 钩子 -------------------------------------------------------------
// 你的钩子（harness/hooks/）：在固定的时刻运行你自己配置的命令。四个时刻：工具执行之前（PreToolUse，可以拦下、或者要你再
// 确认一次）、工具执行之后（PostToolUse，可以在结果后面补一句话给模型）、你发出消息时（UserPromptSubmit，可以拦下、或者附一段
// 说明）、一轮答完时（Stop，只是通知）。协议和 Claude Code 的钩子一样：标准输入给一份 JSON，退出码和标准输出决定怎么办。
//
// 钩子会在这台电脑上运行命令，所以这一页处处写明这一点：只添加你信得过的命令；各家模型的密钥不会交给钩子；
// 模型没有任何能添加、修改钩子的工具（只有带口令的这个页面、命令行的 harness hooks 和你自己能改 data/hooks.json）。
//
// 列表不在状态里（状态里只有个数和文件有没有问题，见 harness/webui/hooks_api.py 的 summary）：打开这一页、做完一个操作、
// 状态里的个数变了的时候，去取一次 /api/hooks/list。"上一次跑得怎么样"只记在助手程序的内存里，重启以后从头记。
// 参数里像密钥的部分（--token=…）到页面上时已经遮住了（••••）；编辑时没改的原样交回去，表示"用原来的"。
//
// 这一页有三种视图：list 列表、detail 打开一个（它的设置、上一次跑得怎么样、试运行）、form 添加或编辑。
// 分成四个文件：这个是第一个（总说明、共用的状态和小工具），后面依次是 hooks-list.js 列表、hooks-detail.js 打开一个和试运行、
// hooks-form.js 添加和编辑（最后在那里挂到扩展对话框上）。

const hooksPanel = $('panel-hooks');
// 四个时刻的名字和说明（HOOK_EVENTS）、结论的名字（HOOK_RESULTS）和运行记录共用，写在 hook-words.js
const HOOK_TOOL_EVENTS = ['PreToolUse', 'PostToolUse'];  // 只有这两个看 matcher
const HOOK_BLOCKING = ['PreToolUse', 'UserPromptSubmit'];  // 能拦下的（"钩子出错时当成拦下"只对它们有用）
const HOOK_SOURCES = { chat: k('对话'), task: k('定时任务'), channel: k('聊天通道') };
const HOOK_EXAMPLE = 'import json, sys\n\ncall = json.load(sys.stdin)          # PreToolUse\nif "rm -rf" in call["tool_input"].get("command", ""):\n    print("rm -rf is not allowed here", file=sys.stderr)\n    sys.exit(2)                       # 2 = block';
let hooksData = null;          // /api/hooks/list 的回答：{hooks, problem, file, events, sources, groups, timeout, max_timeout, …}
let hooksSummary = '';         // 状态里那一小段（个数、文件的问题）上一次的样子：变了就重新取列表
let hooksView = { kind: 'list' };
let hooksDrawn = '';
let hooksDeleting = null;      // 正在原地确认删除的那个钩子
const hooksPending = new Set();  // 正在等回答的操作（按名字）。这期间那一行的按钮不能再点
let hooksSeq = 0;              // 每取一次列表加一。回来得晚的旧列表靠它认出来、丢掉

const hooksFind = (name) => (hooksData ? hooksData.hooks.find((row) => row.name === name) : null);
const hooksSaid = (said) => (!said ? '' : said.key ? t(said.key, said.values) : String(said.text || ''));
const hooksIsTool = (event) => HOOK_TOOL_EVENTS.includes(event);
// 命令写成一行：程序和参数，有空格的参数加上引号（只是给人看的；真正运行时是一项一项交给系统的，不经过 shell）
const hooksArg = (arg) => (/[\s"']/.test(arg) || arg === '' ? JSON.stringify(arg) : arg);
const hooksCommand = (hook) => (!hook ? '' : [hook.command, ...(hook.args || [])].map(hooksArg).join(' '));

// 给一个元素写上一句"说过的话"（服务器给的 {text, key, values}）：带着原文的，换语言时原地重译
function hooksRetext(node, said) {
  if (said && said.key) return retext(node, said.key, said.values);
  delete node.dataset.i18n;
  delete node.dataset.i18nValues;
  node.textContent = hooksSaid(said);
  return node;
}

// matcher 给人看的样子：功能的名字照界面的语言写（执行命令、读写文件……），工具的名字原样
function hooksMatcherText(matcher) {
  const items = String(matcher || '').split(/[|,]/).map((item) => item.trim()).filter(Boolean);
  if (!items.length || items.includes('*')) return t('全部工具');
  const groups = hooksData ? hooksData.groups : [];
  return items.map((item) => { const group = groups.find((one) => one.id === item); return group ? t(group.label) : item; }).join(t('、'));
}

// "上一次"那一句：怎么样了、用了多久、什么时候（或者"试运行"）；还没跑过的说一声
function hooksLastText(last) {
  if (!last) return t('这次启动以来还没运行过');
  const when = last.test ? t('试运行') : fromNow(last.at);
  return t('上次：{result} · {time} · {when}', { result: t(HOOK_RESULTS[last.result] || HOOK_RESULTS.error), time: traceTime(last.ms), when });
}
const hooksLastKind = (last) => (!last ? '' : hookResultKind(last.result));

function hooksSay(text, kind) {
  const box = $('extensions-message');
  delete box.dataset.i18n;  // 别的页在这里写过原文的话，换语言时会把它换回来、盖掉这句
  delete box.dataset.i18nValues;
  box.textContent = text;
  box.className = 'settings-message ' + (kind || '');
}

// 一段提示。key 是原文（换语言时原地重译）；said 是服务器给的一句话（{text, key, values}）
function hooksNote(kind, key, values, iconName) {
  const box = el('div', 'hooks-note ' + kind);
  box.setAttribute('role', kind === 'bad' ? 'alert' : 'note');
  box.appendChild(icon(iconName || (kind === 'calm' ? 'spark' : kind === 'warn' ? 'shield' : 'alert')));
  box.appendChild(tel('span', null, key, values));
  return box;
}
function hooksSaidNote(kind, said) {
  const box = hooksNote(kind, '');
  hooksRetext(box.lastChild, said);
  return box;
}

function hooksButton(iconName, text, extra) {
  const button = el('button', 'button hooks-button' + (extra ? ' ' + extra : ''));
  button.type = 'button';
  button.append(icon(iconName), tel('span', null, text));
  return button;
}

// 开关：一个 role="switch" 的按钮（和 MCP、通道的一样的样子）
function hooksSwitch(on, name) {
  const button = el('button', 'hooks-switch');
  button.type = 'button';
  button.setAttribute('role', 'switch');
  button.setAttribute('aria-checked', on ? 'true' : 'false');
  const label = t('启用钩子：{name}', { name });
  button.setAttribute('aria-label', label);
  button.title = label;
  return button;
}

// 换一种视图：换掉整页的内容。从别的视图换过来的话，回到顶上、内容淡入；焦点放到 focus 指定的那个元素上
function hooksShow(nodes, kind, focus) {
  const fresh = hooksPanel.dataset.view !== kind;
  const kept = !fresh && hooksPanel.contains(document.activeElement) ? document.activeElement.dataset.focus : null;
  const keptScroll = hooksPanel.scrollTop;
  hooksPanel.dataset.view = kind;
  const wrap = el('div', 'hooks-view');
  wrap.append(...nodes);
  hooksPanel.replaceChildren(wrap);
  if (fresh) {
    hooksPanel.scrollTop = 0;
    play(wrap, [{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }], 240);
  } else {
    hooksPanel.scrollTop = keptScroll;  // 重画：别把正在看的位置弄丢
  }
  const target = focus || kept;
  if (target) {
    const again = hooksPanel.querySelector('[data-focus="' + CSS.escape(target) + '"]');
    if (again) again.focus({ preventScroll: !fresh });
  }
}

// 取一次列表，画出来。focus：画好以后把焦点放到哪儿（见 hooksShow）
async function loadHooks(focus) {
  const mine = ++hooksSeq;
  const reply = await call('/api/hooks/list');
  if (mine !== hooksSeq) return;  // 这期间又取了一次：用后来的那一份
  if (!reply.ok) {
    if (!hooksData) hooksShow([hooksSaidNote('bad', { text: reply.error })], 'error');
    hooksSay(reply.error, 'bad');
    return;
  }
  hooksData = reply.data;
  hooksDrawn = '';
  drawHooks(focus);
}

// 一个 POST 请求，等回答的这段时间把这个钩子标成"忙"（按钮暂时不能点）
async function hooksAct(name, path, data) {
  hooksPending.add(name);
  drawHooks();
  const reply = await call(path, data);
  hooksPending.delete(name);
  drawHooks();
  return reply;
}

// 状态里那一小段：标签上写有几个；这一页正开着、钩子变了（别处改过、换了语言）的话，重新取一次列表。
// 表单开着时不动它（你正在填；表单里的字都记着原文，换语言时原地重译）
function renderHooksState(summary) {
  const data = summary || { count: 0 };
  $('count-hooks').textContent = data.count ? data.count.toLocaleString(LOCALES[language]) : '';
  const print = JSON.stringify([data, language]);
  if (print === hooksSummary) return;
  hooksSummary = print;
  if (extensionsDialog.open && extensionTab === 'hooks' && hooksView.kind !== 'form') loadHooks();
}

function drawHooks(focus) {
  if (!hooksData) return;
  if (hooksView.kind === 'list') drawHooksList(focus);
  else if (hooksView.kind === 'detail') drawHooksDetail(focus);
}
