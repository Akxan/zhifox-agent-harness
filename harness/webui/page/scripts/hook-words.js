// ---- 钩子的几个说法 ------------------------------------------------------------
// 四个时刻（事件）给人看的名字和说明、一个钩子这一次的结论（放行、拦下……）。"扩展 · 钩子"那一页（hooks.js 往后几块）
// 和运行记录（trace-chart.js）都照着这里写，所以放在它们两个前面，同一种说法只写在这一处。
// 时刻的真名（PreToolUse……）照原样写在旁边：配置文件和脚本里用的是它（格式见 harness/hooks/__init__.py）。

const HOOK_EVENTS = {
  PreToolUse: { label: k('工具执行之前'), help: k('确认过了、马上要执行一个工具的时候。钩子可以拦下它（模型会看到原因），或者要你再确认一次；不能改参数，也不能让本来要问你的不问。') },
  PostToolUse: { label: k('工具执行之后'), help: k('工具做完了。钩子可以补一小段话，接在这次的结果后面交给模型（比如"格式化过了"）。') },
  UserPromptSubmit: { label: k('你发出消息时'), help: k('你的消息交给模型之前。钩子可以拦下它（不发给模型、不花钱），或者附一段说明一起交给模型。') },
  Stop: { label: k('一轮答完时'), help: k('只是通知：跑一遍格式化、记一笔日志、发个通知。你点了停止、出错了的那一轮不跑。') },
};
const HOOK_RESULTS = { ok: k('放行'), blocked: k('拦下'), ask: k('再问你'), error: k('出错'), timeout: k('超时'), stopped: k('停下') };
// 结论的轻重：ok 绿、要当心的琥珀、出错的红（页面上标签的颜色；每个标签上另有文字，不只靠颜色）
const hookResultKind = (result) => (result === 'ok' ? 'ok' : result === 'blocked' || result === 'ask' || result === 'stopped' ? 'warn' : 'bad');

// "工具执行之前（PreToolUse）"。认不出来的时刻照原样写
function hookEventText(event) {
  const known = HOOK_EVENTS[event];
  return known ? t('{label}（{event}）', { label: { key: known.label, values: {} }, event }) : String(event || '');
}
