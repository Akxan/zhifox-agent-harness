// ---- 命令的沙箱 ---------------------------------------------------------------
// 设置里"沙箱"一节，和三处共用的"选哪一档"下拉框（这里的设置、定时任务的编辑框、聊天通道向导的最后一步）。
// 沙箱是什么、每一档能碰哪些地方、这台电脑上能不能用，都由助手程序定（harness/sandbox/），话也由它写好、带着原文发来
// （state.sandbox，格式见 harness/webui/sandbox_api.py）：页面只照着画，换语言时就地重译，不另抄一份规矩。
// 选了一档马上保存（/api/sandbox/level），下一条命令就按新的来。额外允许的路径在单独的窗口里管（sandbox-paths.js）。

let sandboxInfo = null;     // 状态里的 sandbox：{level, levels, paths, limit, chars, backend, problem}
let sandboxDrawn = '';
let sandboxSaving = false;  // 正在保存新的一档：这期间下拉框不能再动
const SANDBOX_CHOICES = ['default', 'off', 'workspace', 'network'];  // 任务、通道多一项"跟随设置"（default）

// 程序给的一句话（{text, key, values}）放进一个元素：有原文的照原文译，换语言时就地重译
function sandboxSaid(node, said) {
  if (said && said.key) retext(node, said.key, said.values);
  else { delete node.dataset.i18n; delete node.dataset.i18nValues; node.textContent = said ? said.text || '' : ''; }
  return node;
}

// 一档的名字、说明（程序给的），找不到的（旧的助手程序）是 null
const sandboxLevel = (id) => (sandboxInfo ? sandboxInfo.levels.find((level) => level.id === id) || null : null);
// 值里放程序给的原文，页面的 t() 会连它一起按当前语言译
const sandboxValue = (said) => (said && said.key ? { key: said.key, values: said.values || {} } : (said ? said.text : ''));
// 定时任务、通道选了"跟随设置"时，实际用的是哪一档
const sandboxResolved = (own) => (own === 'default' || !SANDBOX_CHOICES.includes(own) ? (sandboxInfo ? sandboxInfo.level : 'off') : own);

// 把一个 <select> 填成"选哪一档"：withDefault 时第一项是"跟随设置（现在是：……）"。内容没变就不重建（焦点、正在点开的列表都不受影响）
function fillSandboxSelect(select, value, withDefault) {
  if (!sandboxInfo) return;
  const ids = withDefault ? SANDBOX_CHOICES : SANDBOX_CHOICES.slice(1);
  const print = JSON.stringify([ids, sandboxInfo.levels, sandboxInfo.level, language]);
  if (select.dataset.print !== print) {
    select.dataset.print = print;
    select.replaceChildren(...ids.map((id) => {
      const option = el('option');
      option.value = id;
      if (id === 'default') {
        const current = sandboxLevel(sandboxInfo.level);
        retext(option, k('跟随设置（现在是：{level}）'), { level: sandboxValue(current && current.label) });
      } else {
        const level = sandboxLevel(id);
        if (level) sandboxSaid(option, level.label); else option.textContent = id;
      }
      return option;
    }));
  }
  select.value = ids.includes(value) ? value : ids[0];
}

// 下拉框下面那一句：选的这一档是什么意思。"跟随设置"说现在跟着的是哪一档
function sandboxChoiceText(node, value) {
  if (value === 'default') {
    const current = sandboxLevel(sandboxInfo ? sandboxInfo.level : 'off');
    retext(node, k('跟着设置里的那一档走，现在是“{level}”。以后你在设置里改了，这里也跟着变。'), { level: sandboxValue(current && current.label) });
    return;
  }
  const level = sandboxLevel(value);
  sandboxSaid(node, level ? level.text : null);
}

// 选了一档（resolved 是实际用的那一档）、这台电脑上却用不了沙箱：命令一律会被拒绝。在下拉框下面明说，不能让人以为它在沙箱里
function sandboxRefusalNote(box, resolved, what) {
  const problem = sandboxInfo && sandboxInfo.problem;
  box.hidden = !(problem && resolved !== 'off');
  if (box.hidden) return;
  retext(box.querySelector('span'), what, { problem: sandboxValue(problem) });
}

// ---- 设置里的"沙箱"一节 ----

function renderSandbox(info) {
  sandboxInfo = info || null;
  $('sandbox-section').hidden = !sandboxInfo;  // 旧的助手程序没有这一节
  if (!sandboxInfo) return;
  const current = sandboxLevel(sandboxInfo.level);
  if (sandboxInfo.level !== 'off' && sandboxInfo.problem) retext($('sandbox-state'), k('（用不了，命令会被拒绝）'));
  else if (current) retext($('sandbox-state'), k('（{level}）'), { level: sandboxValue(current.label) });
  if (settings.open) drawSandboxSettings();
  if (sandboxDialog.open) drawSandboxPaths();
}

function drawSandboxSettings(force) {
  if (!sandboxInfo) return;
  const info = sandboxInfo;
  const print = JSON.stringify([info, sandboxSaving, language]);
  if (print === sandboxDrawn && !force) return;
  sandboxDrawn = print;
  const select = $('sandbox-level');
  fillSandboxSelect(select, info.level, false);
  select.disabled = sandboxSaving;
  sandboxChoiceText($('sandbox-level-text'), info.level);
  $('sandbox-card').dataset.level = info.level;
  // 这台电脑上能不能用：能用的写是哪一种（macOS 的 sandbox-exec……）和补充的一句；用不了的写原因。
  // 选了一档却用不了：命令全都会被拒绝执行，用醒目的红色，告诉你怎么办
  const problem = $('sandbox-problem');
  problem.hidden = !info.problem;
  problem.classList.toggle('bad', Boolean(info.problem) && info.level !== 'off');
  if (info.problem) {
    const what = info.level !== 'off'
      ? k('现在所有命令都会被拒绝执行，不会悄悄在沙箱之外运行：{problem} 照着做好，或者把上面改成“关”。')
      : k('这台电脑上暂时用不了沙箱：{problem}');
    retext($('sandbox-problem-text'), what, { problem: sandboxValue(info.problem) });
  }
  // 用的是哪一种：sandbox-exec、bwrap 写名字和补充的一句；没有沙箱的系统（Windows……）只写补充的那一句（比如可以在 WSL 里用）
  const backend = $('sandbox-backend'), found = info.backend;
  backend.hidden = !found || (found.id === 'none' && !found.note);
  if (found && found.id !== 'none') {
    const values = { backend: sandboxValue(found.label), note: found.note ? sandboxValue(found.note) : '' };
    retext(backend, found.note ? k('这台电脑用的是 {backend}。{note}') : k('这台电脑用的是 {backend}。'), values);
  } else if (found && found.note) sandboxSaid(backend, found.note);
  const count = info.paths.length;
  if (!count) retext($('sandbox-paths-count'), k('还没有：命令只碰得到工作目录。'));
  else if (count === 1) retext($('sandbox-paths-count'), k('1 条'));
  else retext($('sandbox-paths-count'), k('{n} 条'), { n: count });
}

async function chooseSandboxLevel() {
  const select = $('sandbox-level');
  const level = select.value;
  if (!sandboxInfo || level === sandboxInfo.level || sandboxSaving) return;
  sandboxSaving = true;
  drawSandboxSettings(true);
  const reply = await call('/api/sandbox/level', { level });
  sandboxSaving = false;
  if (!reply.ok) {
    say(reply.error, 'bad');
    drawSandboxSettings(true);  // 放回原来那一档
    select.focus();
    return;
  }
  // 状态马上会到，下拉框照着它画；这里先说一声。选了一档、这台电脑上却用不了的，说清楚命令会被拒绝
  const chosen = sandboxLevel(level);
  const label = chosen ? t(chosen.label.key || chosen.label.text, chosen.label.values) : level;
  const broken = Boolean(sandboxInfo && sandboxInfo.problem) && level !== 'off';
  const said = level === 'off' ? t('沙箱关了：命令照以前那样直接运行。')
    : broken ? t('沙箱改成了“{level}”，可是这台电脑上用不了：修好之前，命令都会被拒绝执行。', { level: label })
      : t('沙箱改成了“{level}”：下一条命令就在里面运行。', { level: label });
  say(said, broken ? 'bad' : 'good');
  announce(said);
  drawSandboxSettings(true);
  select.focus();
}

$('sandbox-level').addEventListener('change', chooseSandboxLevel);
