// ---- 要不要先问你 --------------------------------------------------------
// 助手改文件、执行命令之前默认每次都问。这里可以放开一部分或者全部。
// 现在是哪一档由助手程序说了算（它在状态里告诉页面），页面只负责显示和提出修改。

const APPROVAL = { ask: k('每次确认'), edits: k('自动允许改文件'), all: k('全部自动允许') };
const approvalDialog = $('approval-dialog');
let approval = 'ask';

function renderApproval(mode) {
  const button = $('approval');
  button.hidden = !(mode in APPROVAL);  // 助手程序是旧版本、没有这个功能时，不显示这个开关
  if (button.hidden) return;
  approval = mode;
  button.className = 'approval ' + mode;
  retext($('approval-label'), APPROVAL[mode]);
  for (const option of document.querySelectorAll('#approval-options .option')) {
    option.classList.toggle('current', option.dataset.mode === mode);
    option.setAttribute('aria-pressed', String(option.dataset.mode === mode));
  }
}

function openApproval() {
  $('approval-warning').hidden = true;
  approvalDialog.showModal();
}

async function setApproval(mode) {
  const reply = await call('/api/approval', { mode });
  if (!reply.ok) { toast(reply.error); return; }
  approvalDialog.close();
  toast(mode === 'ask' ? t('已恢复为每次确认。') : t('已开启：{mode}。重启程序后会恢复为每次确认。', { mode: t(APPROVAL[mode]) }));
}

$('approval').addEventListener('click', openApproval);
$('close-approval').addEventListener('click', () => approvalDialog.close());
for (const option of document.querySelectorAll('#approval-options .option')) {
  option.addEventListener('click', () => {
    const mode = option.dataset.mode;
    if (mode === approval) { approvalDialog.close(); return; }
    // 全部放开之前多一步：把风险说清楚，要再点一次才生效
    if (mode === 'all') { $('approval-warning').hidden = false; $('approval-confirm').focus(); return; }
    setApproval(mode);
  });
}
$('approval-confirm').addEventListener('click', () => setApproval('all'));
$('approval-cancel').addEventListener('click', () => { $('approval-warning').hidden = true; });
