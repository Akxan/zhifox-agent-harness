// ---- 定时任务 · 详情 --------------------------------------------------------
// 点开一个任务：它要做什么、什么时候跑、动手之前要不要先问你，以及历次运行（每一次都能打开它存下的那段会话）。
// 底下一排是立即运行、暂停或恢复、编辑、删除（删除先在原地确认）。

function openDetail(id) {
  detailId = id;
  detailConfirm = false;
  detailMessage = '';
  detailDrawn = '';
  drawDetail();
  if (!taskDetail.open) taskDetail.showModal();
  syncTaskTimer();
}

function drawDetail() {
  const task = tasks.find((item) => item.id === detailId);
  if (!task) {
    if (taskDetail.open && detailId) { detailId = null; taskDetail.close(); toast(t('这个任务已经不在了。')); }
    return;
  }
  const print = JSON.stringify([task, language, detailConfirm, detailMessage]);
  if (print === detailDrawn) return;
  detailDrawn = print;
  $('task-detail-title').textContent = task.title;
  $('task-detail-sub').textContent = (task.kind === 'agent' ? t('让助手去做') : t('只提醒我')) + ' · ' + scheduleText(task.schedule);

  const parts = [];
  const section = (title) => { const node = el('section'); node.appendChild(el('h3', null, title)); parts.push(node); return node; };
  if (task.running) section(t('正在运行')).appendChild(progressBlock(task));
  section(t('下次运行')).appendChild(el('p', 'task-next', !task.enabled ? t('已暂停，不会运行。')
    : task.next_run ? momentText(task.next_run) + ' · ' + fromNow(task.next_run) : t('不会再运行了')));
  if (task.kind === 'agent' && task.approval in APPROVAL) {
    const line = section(t('动手之前要不要先问你')).appendChild(el('p', 'task-mode-line'));
    line.appendChild(modeChip(task.approval));
    line.appendChild(el('span', null, task.approval === 'all' ? t('任务运行时多半没人看着：选这一项，执行命令、改文件（允许了任务操作电脑的话，还有点击、打字）都不会再等你批准。只给你完全信任的指令用。')
      : task.approval === 'edits' ? t('改工作目录里的文件直接做；执行命令等别的操作，停下来等你批准。')
        : task.approval === 'readonly' ? t('只是看的命令和读文件直接做；会改动东西的命令、改文件，停下来等你批准。')
        : t('要确认的操作会停下来，等你在任务列表里批准。')));
  }
  if (task.prompt) section(task.kind === 'agent' ? t('让助手做什么') : t('提醒的内容')).appendChild(el('pre', null, task.prompt));
  const history = section(t('历次运行'));
  if (!task.runs || !task.runs.length) history.appendChild(el('p', 'hint', t('还没有运行过。')));
  else {
    const list = history.appendChild(el('ol', 'runs'));
    for (const run of task.runs) list.appendChild(runItem(task, run));
  }
  const keep = taskDetail.contains(document.activeElement) ? document.activeElement.id || document.activeElement.dataset.role : '';
  $('task-detail-body').replaceChildren(...parts);
  drawDetailFoot(task);
  if (keep) { const again = $(keep) || taskDetail.querySelector(`[data-role="${keep}"]`); if (again) again.focus(); }
}

function runItem(task, run) {
  const item = el('li', 'run');
  const row = item.appendChild(el('div', 'row'));
  const status = runStatus(task, run);
  row.appendChild(el('span', 'chip ' + status, t(RUN_STATUS[status] || status)));
  row.appendChild(el('span', null, momentText(run.started_at)));
  if (run.catch_up) row.appendChild(el('span', 'chip', run.missed > 1 ? t('补跑（错过了 {n} 次）', { n: run.missed }) : t('补跑')));
  if (run.manual) row.appendChild(el('span', 'chip', t('手动运行')));
  if (run.finished_at && run.finished_at > run.started_at) row.appendChild(el('span', null, t('用时 {time}', { time: durationText(run.finished_at - run.started_at) })));
  if (run.steps) row.appendChild(el('span', null, t('{n} 步', { n: run.steps })));
  if (task.kind === 'agent' && typeof run.cost === 'number') row.appendChild(el('span', null, costText(run.cost)));
  if (run.conversation) {
    const open = row.appendChild(el('button', 'mini open', t('打开对话')));
    open.type = 'button';
    open.addEventListener('click', () => openRunConversation(run.conversation));
  }
  if (run.summary) item.appendChild(el('p', null, run.summary));
  if (run.error) item.appendChild(el('p', 'bad', run.error));
  return item;
}

async function openRunConversation(id) {
  if (busy) { toast(t(BUSY_HINT)); return; }
  const reply = await call('/api/conversation/open', { id });
  if (!reply.ok) { toast(reply.error); return; }
  taskDetail.close();
  setNavTab('chats', false, true);
  if (overlayNav.matches) setNav(false, false);
}

function drawDetailFoot(task) {
  const words = el('p', 'memory-message' + (detailMessage ? ' bad' : ''), detailConfirm ? t('删除这个任务？以前运行留下的会话不会删。') : detailMessage);
  words.setAttribute('role', 'status');
  const nodes = [words];
  const button = (id, label, iconName, className) => {
    const node = el('button', 'button' + (className ? ' ' + className : ''));
    node.id = id;
    node.type = 'button';
    if (iconName) node.appendChild(icon(iconName));
    node.appendChild(document.createTextNode(label));
    nodes.push(node);
    return node;
  };
  if (detailConfirm) {
    button('task-detail-yes', t('删除'), '', 'danger').addEventListener('click', (click) => deleteTask(task, click.currentTarget));
    button('task-detail-no', t('取消'), '', 'cancel').addEventListener('click', () => { detailConfirm = false; drawDetail(); $('task-detail-delete').focus(); });
  } else {
    const run = button('task-detail-run', t('立即运行'), 'play');
    run.disabled = Boolean(task.running);
    run.addEventListener('click', () => runTask(task));
    button('task-detail-toggle', task.enabled ? t('暂停') : t('恢复')).addEventListener('click', async () => {
      const reply = await call('/api/task/toggle', { id: task.id, enabled: !task.enabled });
      detailMessage = reply.ok ? '' : reply.error;
      drawDetail();
    });
    button('task-detail-edit', t('编辑'), 'edit').addEventListener('click', () => { taskDetail.close(); openEditor(task); });
    button('task-detail-delete', t('删除'), 'trash').addEventListener('click', () => {
      detailConfirm = true;
      drawDetail();
      $('task-detail-no').focus();  // 默认停在"取消"上
    });
  }
  $('task-detail-foot').replaceChildren(...nodes);
}

taskDetail.addEventListener('close', () => {
  const id = detailId;
  detailId = null;
  detailConfirm = false;
  detailDrawn = '';
  $('task-detail-body').replaceChildren();
  syncTaskTimer();
  if (id && navTab === 'tasks' && (!document.activeElement || document.activeElement === body)) focusTask(id);
});
$('close-task-detail').addEventListener('click', () => taskDetail.close());
