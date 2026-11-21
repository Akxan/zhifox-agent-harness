const nav = $('nav'), side = $('side'), main = $('main'), sheet = $('sheet'), viewport = $('viewport');
const chat = $('chat'), log = $('log'), empty = $('empty'), starters = $('starters'), working = $('working');
const composer = $('composer'), input = $('input'), sendButton = $('send'), toBottom = $('to-bottom');
const banner = $('banner'), newButton = $('new'), search = $('search');
const rows = $('conversation-rows'), pill = $('pill');
const settings = $('settings'), pickProvider = $('pick-provider'), pickModel = $('pick-model');

let busy = false;          // 是否有一轮对话正在进行
let stopping = false;      // 用户点了"停止"，还在等这一轮收尾
let sending = false;       // 发送消息的请求还没有回来（防止连按两次回车发出两条）
let sentAt = 0;            // 上一条消息是什么时候发出去的（防止双击发送按钮时第二下变成"停止"）
let state = null;          // 助手程序发来的最新状态：当前模型、各家的情况、用量……
let block = null;          // 正在逐字输出的那一段：{kind, box, holder, label, node, text}
let toolCard = null;       // 最近一次工具调用的卡片，等它的结果来填
const confirms = new Map(); // 还没答复的确认卡片

// 动画只给"此刻正在发生"的内容。旧内容（刷新页面后补发的、打开旧会话时重放的）直接画出来，
// 画完之后整段一起淡入一次。下面三个标记用来分辨这两种情况。
let synced = false;        // 刚连上事件流时，服务器先把已有的内容补发一遍；补发完了才算"同步好了"
let replaying = false;     // 收到 reset 之后、下一个状态之前的内容，都是重放出来的旧内容
let unsettled = true;      // 有一批旧内容刚画完，还没有收尾（滚到底、整段淡入）
let jump = null;           // 从搜索结果打开一段会话后，要滚到的那处命中：{id, kind, find}
const isLive = () => synced && !replaying;

const BUSY_HINT = k('请先等这一轮对话结束，或者点“停止”。');
