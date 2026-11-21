// ---- 扩展 · MCP -------------------------------------------------------------
// MCP 服务器给助手接上别人写好的工具。这一页列出接入的服务器和它们的状态（连接中、已连接、失败、停用），
// 可以添加、编辑、删除、启用停用、重启；点开一个，能看到它的工具（每个工具可以单独设成"不用问"）和日志；
// 还可以从别的工具（Claude Desktop、Claude Code、Cursor、VS Code、Codex……）导入，或者粘贴一段 JSON。
//
// 列表跟着状态走：服务器连上了、断开了、工具变了，助手程序都会发一次状态。工具的说明和日志比较长，不放在状态里，
// 点开一个服务器时另外去取（它的 revision 或日志的编号变了就重新取）。
// 密钥（环境变量、请求头的值）从来不会到页面上来，状态里只有名字和最后 4 位。编辑时没有重新填的那一项，
// 交回去的是"保持原来的"（keep），值由助手程序自己从配置文件里拿。
//
// 这一页有几种视图：list 列表、detail 一个服务器的详情、form 添加或编辑、paste 粘贴 JSON、import 从其他工具导入。
// 列表和详情跟着状态重画（内容没变就不动）；表单、粘贴、导入是你正在填的东西，搭好之后只在原地更新，不整个重画。
//
// 这一页分成五个文件，这个是第一个：总说明、共用的状态和小工具。后面依次是 mcp-list.js 列表、
// mcp-detail.js 详情、mcp-form.js 添加和编辑、mcp-import.js 粘贴 JSON 和从其他工具导入（最后在那里挂到扩展对话框上）。

const mcpPanel = $('panel-mcp');
const MCP_STATUS = { connecting: k('连接中'), ready: k('已连接'), failed: k('连接失败'), disabled: k('已停用') };
const MCP_ERA = { modern: k('新一代（无状态，每个请求自带版本）'), legacy: k('老一代（先握手）') };
const MCP_EXAMPLE = '{\n  "mcpServers": {\n    "filesystem": {\n      "command": "npx",\n      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/folder"]\n    }\n  }\n}';
let mcpState = null;           // 状态里的 mcp：{servers, problem, file, started, dropped}
let mcpView = { kind: 'list' };
let mcpDrawn = '';
let mcpDeleting = null;        // 正在原地确认删除的那个服务器
const mcpPending = new Set();  // 正在等回答的操作（按服务器的名字）。这期间那一行的按钮不能再点
let mcpSeq = 0;                // 每取一次详情加一。回来得晚的旧详情靠它认出来、丢掉

const spoken = (said) => (!said ? '' : said.key ? t(said.key, said.values) : String(said.text || ''));
const mcpFind = (name) => (mcpState ? mcpState.servers.find((server) => server.name === name) : null);
const mcpCommand = (config) => (!config ? '' : config.transport === 'http' ? config.url : [config.command, ...(config.args || [])].join(' '));

function mcpSay(text, kind) {
  const box = $('extensions-message');
  delete box.dataset.i18n;  // Skill 一页在这里写过原文的话，换语言时会把它换回来、盖掉这句
  delete box.dataset.i18nValues;
  box.textContent = text;
  box.className = 'settings-message ' + (kind || '');
}

function mcpNote(kind, text, iconName) {
  const box = el('div', 'mcp-note ' + kind);
  box.setAttribute('role', kind === 'bad' ? 'alert' : 'note');
  box.appendChild(icon(iconName || (kind === 'calm' ? 'spark' : 'alert')));
  box.appendChild(el('span', null, text));
  return box;
}

function panelButton(iconName, text, extra) {
  const button = el('button', 'button mcp-button' + (extra ? ' ' + extra : ''));
  button.type = 'button';
  button.append(icon(iconName), tel('span', null, text));
  return button;
}

function mcpSwitch(on, label, values) {
  const button = el('button', 'mcp-switch');
  button.type = 'button';
  button.setAttribute('role', 'switch');
  button.setAttribute('aria-checked', on ? 'true' : 'false');
  button.setAttribute('aria-label', t(label, values));
  button.title = t(label, values);
  return button;
}

// 换一种视图：换掉整页的内容。是从别的视图换过来的话，回到顶上、内容淡入；焦点放到 focus 指定的那个元素上
function mcpShow(nodes, kind, focus) {
  const fresh = mcpPanel.dataset.view !== kind;
  const kept = !fresh && mcpPanel.contains(document.activeElement) ? document.activeElement.dataset.focus : null;
  const keptScroll = mcpPanel.scrollTop;
  mcpPanel.dataset.view = kind;
  const wrap = el('div', 'mcp-view');
  wrap.append(...nodes);
  mcpPanel.replaceChildren(wrap);
  if (fresh) {
    mcpPanel.scrollTop = 0;
    play(wrap, [{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }], 240);
  } else {
    mcpPanel.scrollTop = keptScroll;  // 状态更新时重画：别把正在看的位置弄丢
  }
  const target = focus || kept;
  if (target) {
    const again = mcpPanel.querySelector('[data-focus="' + CSS.escape(target) + '"]');
    if (again) again.focus({ preventScroll: !fresh });
  }
}

// 一个 POST 请求，等回答的这段时间把这个服务器标成"忙"（按钮暂时不能点）
async function mcpAct(name, path, data) {
  if (name) mcpPending.add(name);
  if (name) drawMcp();
  const reply = await call(path, data);
  if (name) mcpPending.delete(name);
  drawMcp();
  return reply;
}

function renderMcpState(data) {
  mcpState = data || null;
  $('count-mcp').textContent = data && data.servers.length ? data.servers.length.toLocaleString(LOCALES[language]) : '';
  if (extensionsDialog.open && extensionTab === 'mcp') drawMcp();
}

function drawMcp() {
  if (!mcpState) return;
  if (mcpView.kind === 'list') drawMcpList();
  else if (mcpView.kind === 'detail') drawMcpDetail();
  else if (mcpView.kind === 'form') syncMcpForm();
}
