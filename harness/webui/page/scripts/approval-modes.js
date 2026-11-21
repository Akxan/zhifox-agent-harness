// ---- 要不要先问你：几档的名字和说明 ------------------------------------------------
// 对话里的开关（approval.js）、定时任务的编辑器（tasks-editor.js）、聊天通道向导的最后一步（channels-steps.js）三处选的是同一套，
// 名字和说明只写在这里一处，三处都照着它画。几档是什么意思由助手程序定（harness/permissions.py 的 MODES），
// 顺序也一样：从严到松。只读的命令怎么判断见 harness/commands/。

const APPROVAL = { ask: k('每次确认'), readonly: k('自动允许只读命令'), edits: k('自动允许改文件'), all: k('全部自动允许') };
const APPROVAL_TEXT = {
  ask: k('改文件、执行命令、读工作目录之外的文件，每一次都先问你。最稳妥，也是默认的做法。'),
  readonly: k('只是看的命令（ls、cat、grep、df、git status、看日志……）和读文件不再问。会改动东西的命令、改文件，以及密钥和助手自己的数据，仍然每次问。'),
  edits: k('创建和修改工作目录里的文件不再问。执行命令、读工作目录之外的文件仍然每次问。'),
  all: k('执行命令、改文件、读工作目录之外的文件，都不再问。打开了“操作整台电脑”的话，点击、打字也不再问（危险的组合键照样问）。'),
};
const APPROVAL_ORDER = Object.keys(APPROVAL);
// 没问你就放行的那一步上标的小字。只读的命令、你自己加的命令、这段对话里说过不再问的电脑操作写得更具体
// （为什么不问，见 harness/permissions.py 的 allowance）
const AUTO_TAGS = { readonly: k('自动允许（只读）'), listed: k('自动允许（你加的命令）'), trusted: k('自动允许（这段对话里不再问）') };
const autoTag = (why) => AUTO_TAGS[why] || k('自动允许');

// Windows 上"只读的命令"一条都不认（命令是 PowerShell，见 harness/commands/__init__.py），这一档只剩"读文件不问"，说明照实写
const READONLY_ON_WINDOWS = k('读文件（包括工作目录之外的）不再问。Windows 上命令在 PowerShell 里运行，还认不出哪些只是看，所以每条命令仍然都问；改文件、密钥和助手自己的数据也照常问。');
const approvalText = (mode) => (mode === 'readonly' && serverSystem === 'windows' ? READONLY_ON_WINDOWS : APPROVAL_TEXT[mode]);

// 一个选项：名字、说明、选中时的勾。className 和 data 属性由用它的地方定（三处的样子各自写在自己的样式里）
function approvalChoice(className, mode, data) {
  const button = el('button', className);
  button.type = 'button';
  button.dataset[data] = mode;
  const text = tel('span', 'approval-choice-text', approvalText(mode));
  text.dataset.approvalMode = mode;
  button.append(tel('b', null, APPROVAL[mode]), text, icon('check'));
  return button;
}

// 知道了助手程序在哪种电脑上：已经画好的说明照着重写（通道向导每次打开时重画，自然就是对的）
whenServerKnown(() => {
  for (const text of document.querySelectorAll('.approval-choice-text[data-approval-mode="readonly"]')) retext(text, approvalText('readonly'));
});

// 对话里的开关和任务编辑器的选项写在页面的骨架里是空的，在这里一次画好（通道的向导每次打开时自己画）
$('approval-options').replaceChildren(...APPROVAL_ORDER.map((mode) => approvalChoice('option', mode, 'mode')));
$('task-approval-options').replaceChildren(...APPROVAL_ORDER.map((mode) => {
  const choice = approvalChoice('task-approval-option', mode, 'approval');
  choice.setAttribute('role', 'radio');
  return choice;
}));
