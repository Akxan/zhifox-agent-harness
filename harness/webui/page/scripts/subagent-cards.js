  // ---- 交给子 Agent 的卡片 ----------------------------------------------------
  // 主助手调用 agent 工具（把一项任务交给一个自定义 Agent，见 harness/agents/runner.py）时，对话里画的不是普通的工具卡片，
  // 而是这一张：摘要一行写着交给了谁、做什么、它此刻在干什么（在想、在写、在调用哪个工具）；点开能看到任务的原文、
  // 它调用过的每个工具、它交回的回答，以及用了哪个模型、调用了几次、花了多少。
  // 它的进展是一串 subagent 事件（step：start、activity、tool_call、tool_result、auto、note、done），按 run 编号找到这张卡片。
  // "在想 / 在写"只是此刻的样子，不存进会话；重放旧会话时卡片照样画出来，只是不播动画。
  // 它要你确认的操作，确认卡片照常画在对话里（写着是哪个 Agent 在问），不藏在这张卡片里面。

  const subRuns = new Map();  // run 编号 → 这次交出去的任务在页面上的那张卡片

  const SUB_DONE = {
    done: k('做完了'), limit: k('步数用完了，没做完'), refused: k('被模型拒绝了'), empty: k('没有交回内容'),
    failed: k('出错了'), stopped: k('被中断了'), truncated: k('回答被截断了'),
  };

  // 一张卡片（还没有 run 编号：等 start 事件来了再记上）。nested：画在另一张卡片里面（再往下一层的子 Agent）
  function subagentCard(event, nested) {
    const input = event.input && typeof event.input === 'object' ? event.input : {};
    const name = String(input.name || event.agent || ''), task = String(input.task || event.task || '');
    const card = el('details', 'call subagent-card' + (nested ? ' subagent-nested' : ''));
    card.dataset.color = (agentByName(name) || {}).color || '';
    const summary = card.appendChild(el('summary'));
    const status = summary.appendChild(el('span', 'status pending'));
    status.appendChild(tel('span', 'sr-only', k('正在执行')));
    const face = summary.appendChild(agentFace({ name, color: card.dataset.color }));
    summary.appendChild(tel('span', 'subagent-who', k('交给「{agent}」'), { agent: name }));
    summary.appendChild(el('span', 'subagent-task', firstLine(task)));
    const live = summary.appendChild(el('span', 'subagent-live'));
    live.appendChild(el('i'));
    const liveText = live.appendChild(el('span'));
    const outcome = summary.appendChild(el('span', 'tool-outcome'));
    summary.appendChild(icon('chevron', 'chev'));
    const details = card.appendChild(el('div', 'tool-body'));
    details.appendChild(tel('div', 'label', k('交给它的任务')));
    details.appendChild(el('pre', null, task));
    const stepsLabel = details.appendChild(tel('div', 'label', k('它做了什么')));
    const steps = details.appendChild(el('ul', 'subagent-steps'));
    const answerLabel = details.appendChild(tel('div', 'label', k('它交回的回答')));
    const answer = details.appendChild(el('div', 'subagent-answer md'));
    const error = details.appendChild(el('p', 'subagent-error'));
    const meta = details.appendChild(el('p', 'subagent-meta'));
    stepsLabel.hidden = answerLabel.hidden = answer.hidden = error.hidden = meta.hidden = true;
    const sub = { name, card, face, live, liveText, steps, stepsLabel, answer, answerLabel, error, meta, status, run: '', last: null, done: false, nested };
    setSubLive(sub, k('正在准备'));
    if (!nested) add(card);
    return { card, status, outcome, details, summary, sub };
  }

  function setSubLive(sub, key, values) {
    sub.live.className = 'subagent-live';
    retext(sub.liveText, key, values);
  }

  function subagentStep(event) {
    let sub = subRuns.get(event.run);
    if (event.step === 'start') {
      const parent = event.within ? subRuns.get(event.within) : null;
      if (parent) {  // 再往下一层：画在叫它的那张卡片的"它做了什么"里
        sub = subagentCard(event, true).sub;
        parent.stepsLabel.hidden = false;
        parent.steps.appendChild(el('li')).appendChild(sub.card);
      } else if (toolCard && toolCard.sub && !toolCard.sub.run) sub = toolCard.sub;
      else sub = subagentCard(event, false).sub;  // 对应的那次调用没能画出来（不该发生）：也要让人看到
      sub.run = event.run;
      sub.card.dataset.color = sub.face.dataset.color = event.color || '';
      sub.model = event.model || '';
      subRuns.set(event.run, sub);
      return;
    }
    if (!sub) return;
    if (event.step === 'activity') {
      setSubLive(sub, event.doing === 'thinking' ? k('正在思考') : event.doing === 'writing' ? k('正在写回答') : k('正在工作'));
    } else if (event.step === 'tool_call') {
      const item = sub.steps.appendChild(el('li', 'subagent-step'));
      const status = item.appendChild(el('span', 'status pending'));
      status.appendChild(tel('span', 'sr-only', k('正在执行')));
      item.appendChild(el('code', null, String(event.summary || event.name || '')));
      const note = item.appendChild(el('small'));
      sub.last = { item, status, note };
      sub.stepsLabel.hidden = false;
      sub.tool = String(event.name || '');
      setSubLive(sub, k('正在调用 {tool}'), { tool: sub.tool });
      if (isLive()) play(item, [{ opacity: 0, transform: 'translateY(-3px)' }, { opacity: 1, transform: 'none' }], 200);
    } else if (event.step === 'tool_result' && sub.last) {
      const failed = Boolean(event.is_error);
      const { item, status, note } = sub.last;
      status.className = 'status ' + (failed ? 'failed' : 'ok') + (isLive() ? ' pop' : '');
      status.replaceChildren(icon(failed ? 'x' : 'check'), tel('span', 'sr-only', failed ? k('失败') : k('成功')));
      item.classList.toggle('failed', failed);
      const text = typeof event.key === 'string' ? t(event.key, event.values) : String(event.text || '');
      if (failed) note.textContent = firstLine(text);
      item.title = firstLine(text);
      sub.last = null;
      setSubLive(sub, k('正在思考'));
    } else if (event.step === 'auto' && sub.last) {
      sub.last.item.insertBefore(tel('span', 'auto-tag', k('自动允许')), sub.last.note);
    } else if (event.step === 'note') {
      const item = sub.steps.appendChild(el('li', 'subagent-step note'));
      item.appendChild(event.key ? tel('span', null, event.key, event.values) : el('span', null, String(event.text || '')));
      sub.stepsLabel.hidden = false;
    } else if (event.step === 'done') {
      finishSub(sub, event);
    }
  }

  function finishSub(sub, event) {
    sub.done = true;
    const status = event.status in SUB_DONE ? event.status : 'done';
    const bad = ['failed', 'refused', 'empty', 'limit'].includes(status);
    sub.live.className = 'subagent-live done' + (status === 'failed' ? ' failed' : '');
    retext(sub.liveText, SUB_DONE[status]);
    if (sub.last) {  // 停在一个工具调用中间
      sub.last.status.className = 'status stopped';
      sub.last.status.replaceChildren(icon('minus'), tel('span', 'sr-only', k('没有执行完')));
      sub.last = null;
    }
    if (event.answer) {
      sub.answerLabel.hidden = sub.answer.hidden = false;
      const view = markdownView(sub.answer);
      try { if (view) view.update(String(event.answer), true); else throw new Error('no view'); } catch (_) {
        sub.answer.classList.add('md-plain');
        sub.answer.textContent = String(event.answer);
      }
    }
    if (event.error) {
      sub.error.hidden = false;
      const error = event.error;
      if (error && typeof error === 'object' && error.key) retext(sub.error, error.key, error.values);
      else sub.error.textContent = typeof error === 'object' ? String(error.text || '') : String(error);
    }
    // 用了哪个模型、调用了几次、用了几次工具、花了多少（没有价格的模型不写钱）
    const facts = [];
    if (sub.model) facts.push(sub.model);
    if (event.steps) facts.push(event.steps === 1 ? t('调用了 1 次模型') : t('调用了 {n} 次模型', { n: event.steps }));
    if (event.calls) facts.push(event.calls === 1 ? t('用了 1 次工具') : t('用了 {n} 次工具', { n: event.calls }));
    if (typeof event.cost === 'number') facts.push('$' + (event.cost < 0.01 ? event.cost.toFixed(4) : event.cost.toFixed(2)));
    sub.meta.hidden = !facts.length;
    sub.meta.textContent = facts.join(' · ');
    if (sub.nested) {  // 里面那一层没有自己的工具结果来收尾：在这里把状态画完
      sub.status.className = 'status ' + (status === 'stopped' ? 'stopped' : bad ? 'failed' : 'ok');
      sub.status.replaceChildren(icon(status === 'stopped' ? 'minus' : bad ? 'x' : 'check'));
    }
    if (isLive() && status === 'done') announce(t('「{agent}」做完了，回答交给了主助手。', { agent: sub.name }));
  }
