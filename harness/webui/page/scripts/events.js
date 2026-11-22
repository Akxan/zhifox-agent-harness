// ---- 每种事件怎么画 ------------------------------------------------------
// 服务器推来的每一种事件（文字、思考、工具调用和结果、确认请求、状态……）在 handlers 里各有一个函数，
// 收到事件就按它的类型调用对应的那个（连上事件流的地方在 start.js）。

function finishTool(kind, text) {
  const { card, status, outcome } = toolCard;
  if (toolCard.sub) text = '';  // 交给子 Agent 的卡片：结果写在卡片里（它的回答、做了什么），这一行不再重复
  status.className = 'status ' + kind + (isLive() ? ' pop' : '');
  status.replaceChildren(icon(kind === 'ok' ? 'check' : kind === 'failed' ? 'x' : 'minus'),
                         tel('span', 'sr-only', kind === 'ok' ? k('成功') : kind === 'failed' ? k('失败') : k('没有执行完')));
  card.classList.toggle('failed', kind === 'failed');
  // 成功了有对勾就够了；出错时把原因直接写在这一行上，不用点开。"没有执行完"是页面自己的话，要能跟着换语言
  if (kind === 'stopped' && text) retext(outcome, text); else outcome.textContent = kind === 'ok' ? '' : text;
  toolCard = null;
}

const handlers = {
  reset() {
    const focused = log.contains(document.activeElement);  // 焦点在要被清掉的内容里：清完交给输入框，不然按键没人接
    dropTurnTools();
    dropFileChanges();
    log.replaceChildren();
    if (focused) input.focus({ preventScroll: true });
    block = null;
    toolCard = null;
    subRuns.clear();
    confirms.clear();
    forgetTraces();
    busy = stopping = false;
    replaying = unsettled = true;
    refreshStatus();
  },

  state(event) {
    state = event;
    if (event.language in LOCALES && event.language !== language) applyLanguage(event.language);
    const current = event.providers.find((provider) => provider.id === event.provider);
    $('workspace').textContent = event.workspace;
    // 还没有密钥时，右上角这个按钮直接写明要做什么（服务器的提示里说的"右上角的设置"就是它）
    if (current.has_key) $('model-label').replaceChildren(el('span', 'provider-name', current.label + ' · '), document.createTextNode(event.model));
  else $('model-label').textContent = t('设置 API 密钥');
    $('model').classList.toggle('no-key', !current.has_key);
    $('title').textContent = event.title || t('新对话');
    renderAgents(event.agents);   // 先于会话列表：列表里的小标记要用 Agent 的颜色
    renderChannelsState(event.channels);  // 也先于会话列表：在聊天通道里聊的那几段要写平台的名字（不然刷新后写成小写的 telegram）
    renderPersona(event.persona);
    document.title = (event.title ? event.title + ' · ' : '') + t('智狐');
    $('setup').hidden = current.has_key;
    $('cards').hidden = !current.has_key;
    $('setup-text').textContent = t('还没有设置 {provider} 的 API 密钥，填好之后就可以开始对话。', { provider: current.label });

    renderConversations();
    renderTodos(event.todos);
    renderMemories(event.memories);
    renderSkills(event.skills);
    renderTasks(event.tasks, event.reminders);
    syncMediaTab(event.media);
    retext($('prompt-state'), event.prompt_custom ? k('（已改过）') : k('（内置的）'));
    renderUsage(event);
    renderSpend(event.spend);
    renderCapabilities(event.capabilities);
    renderMcpState(event.mcp);
    renderHooksState(event.hooks);  // 你的钩子：扩展标签上的个数；那一页开着的话，钩子变了就重新取一次列表
    renderBrowser(event.browser);
    renderComputer(event.computer);
    if (settings.open) renderSettings();
    // 换模型的面板：开着的话重新取一次目录（换了模型、填了密钥、这一轮聊完了……）；第一次先取好，点开时马上就有
    if (modelsFlyout.isOpen() || !modelsCatalog) refreshModels();
    renderApproval(event.approval);
    renderCommands(event.commands);
    renderSandbox(event.sandbox);  // 命令的沙箱（见 sandbox.js）
    renderEndpoints(event.endpoints);  // 设置里的"自定义模型接口"（见 endpoints.js）

    if (replaying) {  // 重放的旧内容到这里就画完了
      replaying = false;
      refreshStatus();  // 重放期间没有刷新过，这里按最终的状态刷新一次
      if (synced) settle();
    }
    renderTurnTools();  // "编辑""重新生成"摆在最后一轮上（见 rewind.js）
  },

  user(event) {
    endBlock();  // 上一段回复停在 block_end 上（见 pauseBlock）：到这里一定结束了。平时 turn_end 已经收过尾
    const attachments = Array.isArray(event.attachments) ? event.attachments : [];
    if (attachments.length) add(attachmentRow(attachments, event.at));
    if (event.text || !attachments.length) add(el('div', 'msg user', event.text));
  },
  // 重放旧会话时不在每一轮都刷新"正在工作"：一段 150 轮的会话会把 body.busy 来回切换 300 次，
  // 而 Chrome 在用户真的在输入框里打过字之后，每切换一次就同步重算一遍整页的样式（实测卡 1.4 秒）。
  // 重放是在同一个任务里画完的，中间状态本来就看不到；画完之后按最终状态刷新一次（见 state）
  turn_start() { busy = true; stopping = false; if (!replaying) { refreshStatus(); renderTurnTools(); } },

  turn_end() {
    endBlock();
    if (toolCard) finishTool('stopped', k('没有执行完'));  // 这一轮结束时工具还没给出结果（比如被中断了）
    busy = stopping = false;
    if (!replaying) { refreshStatus(); renderTurnTools(); }
  },

  thinking(event) { stream('thinking', event.text); },
  text(event) { stream('text', event.text); },
  cite(event) { noteCite(event); },  // 刚写完的那段文字引用了网页（见 citations.js）
  block_end() { pauseBlock(); },  // 先停一下，不马上收尾（为什么见 streaming.js 的 pauseBlock）

  notice(event) {
    endBlock();
    const node = el('div', 'notice ' + event.level);
    if (event.level === 'error') node.appendChild(icon('alert'));
    if (event.level === 'tool') node.appendChild(icon('globe'));
    // 助手程序给了原文（key）的提示，换语言时也能跟着换；没给的（比如旧的会话记录）照原样显示
    node.appendChild(event.key ? tel('span', null, event.key, event.values) : el('span', null, String(event.text)));
    add(node);
  },

  tool_call(event) {
    endBlock();
    if (toolCard) finishTool('stopped', '');
    if (event.name === 'agent') { toolCard = subagentCard(event); return; }  // 交给子 Agent：画成一张专门的卡片（见下面"交给子 Agent 的卡片"）
    // 浏览器、电脑的一步：卡片下面还要放一张小截图（见"浏览器"、"操作整台电脑"两节）。大图各有各的接口，记在 shots 里
    const shots = isBrowserCall(event.name) ? 'browser' : isComputerCall(event.name) ? 'computer' : '';
    const browserStep = Boolean(shots);
    const card = el('details', shots ? 'call ' + shots + '-call' : 'call');
    const summary = card.appendChild(el('summary'));
    const status = summary.appendChild(el('span', 'status pending'));
    status.appendChild(tel('span', 'sr-only', k('正在执行')));
    // 摘要的开头是工具名，后面是参数。分开放，名字用粗体
    const name = typeof event.name === 'string' && event.summary.startsWith(event.name) ? event.name : '';
    summary.appendChild(el('span', 'tool-name', name || event.summary));
    summary.appendChild(el('span', 'tool-args', name ? event.summary.slice(name.length).trim() : ''));
    const outcome = summary.appendChild(el('span', 'tool-outcome'));
    summary.appendChild(icon('chevron', 'chev'));
    const details = card.appendChild(el('div', 'tool-body'));
    details.appendChild(tel('div', 'label', k('参数')));
    details.appendChild(el('pre', null, JSON.stringify(event.input, null, 2)));
    const holder = browserStep ? el('div', 'browser-step') : null;
    if (holder) holder.appendChild(card);
    add(holder || card);
    toolCard = { card, status, outcome, details, summary, holder, shots };
  },

  // 这次调用本来要问用户，但用户打开了"自动允许"，所以没有问。在卡片上标出来，记录里看得出；
  // 只读的命令、你自己加的命令写明为什么（event.why，见 approval-modes.js 的 autoTag）
  auto_allowed(event) {
    if (!toolCard || toolCard.summary.querySelector('.auto-tag')) return;
    toolCard.summary.insertBefore(tel('span', 'auto-tag', autoTag(event && event.why)), toolCard.summary.lastChild);
  },

  tool_result(event) {
    // 对应的那次调用没画出来（比如那条事件坏了）：结果也要让人看到，不能悄悄丢掉
    if (!toolCard) handlers.tool_call({ name: '', summary: t('工具结果（对应的调用没能显示）'), input: null });
    // 交给子 Agent 的卡片：它交回的内容已经画在卡片里了；只有它没能说完（比如还没开始就出错了）时，才把结果原样摆出来
    if (!toolCard.sub || !toolCard.sub.done) {
      toolCard.details.appendChild(tel('div', 'label', k('结果')));
      toolCard.details.appendChild(el('pre', null, event.text));
    }
    if (toolCard.holder && typeof event.thumb === 'string' && THUMB_URL.test(event.thumb)) toolCard.holder.appendChild(browserThumb(event));
    finishTool(event.is_error ? 'failed' : 'ok', firstLine(event.text));
  },

  confirm(event) {
    endBlock();
    const card = el('div', 'confirm');
    const head = card.appendChild(el('div', 'confirm-head'));
    head.appendChild(icon('shield'));
    const heading = head.appendChild(tel('span', null, k('需要你确认')));
    // 说明分两截：前半句是页面的话，后半句是助手程序给的。程序给了原文（key）的话，换语言时它也能跟着换
    const what = card.appendChild(el('pre'));
    if (event.agent) {  // 是子 Agent 在问：写明是哪一个（带着它的颜色），它的卡片上也写着"等你确认"
      const who = what.appendChild(tel('span', 'confirm-who', k('「{agent}」想要'), { agent: event.agent }));
      who.dataset.color = (agentByName(event.agent) || {}).color || '';
      if (toolCard && toolCard.sub && !toolCard.sub.done) setSubLive(toolCard.sub, k('等你确认'));
    } else what.appendChild(tel('span', null, k('助手想要')));
    what.appendChild(event.key ? tel('span', null, event.key, event.values) : el('span', null, String(event.description).trimEnd()));
    const actions = card.appendChild(el('div', 'confirm-actions'));
    const allow = actions.appendChild(tel('button', 'button primary', k('允许')));
    // 操作整台电脑：可以多选一个"这段对话里……不再问"（截图一种，点击、打字一种；只管这段对话，见 computer.js 的 computerTrust）
    const trust = computerTrust(event.offer, actions);
    const deny = actions.appendChild(tel('button', 'button', k('拒绝')));
    const reason = actions.appendChild(el('input', 'field reason'));
    allow.type = deny.type = 'button';
    reason.type = 'text';
    tattr(reason, 'placeholder', k('拒绝时可以告诉助手原因（可选）'));
    tattr(reason, 'aria-label', k('拒绝的原因'));
    if (!$('approval').hidden && !event.always) {  // 助手程序支持"自动允许"时，在这里放一个直达的入口（换哪一档都照样要问的不放）
      const fewer = actions.appendChild(tel('button', 'link', k('不想每次都问？')));
      fewer.type = 'button';
      fewer.addEventListener('click', openApproval);
    }
    if (trust) computerTrustNote(event.offer, trust, actions, event.id);

    const answer = async (allowed) => {
      const hadFocus = card.contains(document.activeElement);
      allow.disabled = deny.disabled = true;
      if (trust) trust.disabled = true;
      const sent = await post('/api/confirm', { id: event.id, allow: allowed, feedback: allowed ? '' : reason.value });
      if (!sent) { allow.disabled = deny.disabled = false; if (trust) trust.disabled = false; }
      else if (hadFocus) input.focus({ preventScroll: true });
    };
    allow.addEventListener('click', () => answer(true));
    deny.addEventListener('click', () => answer(false));
    if (trust) trust.addEventListener('click', async () => {
      trust.disabled = true;
      const reply = await call('/api/computer/trust', { what: event.offer, confirm: event.id });
      if (!reply.ok) { trust.disabled = false; toast(reply.error); return; }
      answer(true);
    });
    reason.addEventListener('keydown', (key) => {
      if (key.key === 'Enter' && !key.isComposing && key.keyCode !== 229) { key.preventDefault(); answer(false); }
    });

    if (isLive()) card.classList.add('alert');
    add(card);
    announce(t('需要你确认') + '：' + what.textContent);
    confirms.set(event.id, { card, actions, heading });
    refreshStatus();
  },

  subagent(event) { subagentStep(event); },
  trace(event) { traceTurn(event); },  // 一轮结束时的运行记录：在这一轮上放一个入口（见 trace.js）
  file_changes(event) { fileChangesLine(event); },  // 这一轮改了哪些文件（见 file-changes.js）
  file_undo(event) { fileUndoLine(event); },        // 撤回、重做了哪几个文件

  confirm_done(event) {
    const pending = confirms.get(event.confirm);
    if (!pending) return;
    // 子 Agent 问的那一次答复了：它的卡片上换回"正在调用"
    if (event.allowed && toolCard && toolCard.sub && !toolCard.sub.done && toolCard.sub.tool) setSubLive(toolCard.sub, k('正在调用 {tool}'), { tool: toolCard.sub.tool });
    const outcome = el('span', 'outcome' + (event.allowed ? ' allowed' : event.stopped ? '' : ' denied'));
    outcome.appendChild(icon(event.allowed ? 'check' : event.stopped ? 'minus' : 'x'));
    outcome.appendChild(tel('span', null, event.allowed ? k('已允许') : event.stopped ? k('已中断') : k('已拒绝')));
    pending.actions.replaceChildren(outcome);
    retext(pending.heading, k('需要确认的操作'));
    pending.card.classList.add('done');
    pending.card.classList.remove('alert');
    confirms.delete(event.confirm);
    refreshStatus();
  },
};
