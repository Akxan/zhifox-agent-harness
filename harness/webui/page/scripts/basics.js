// ---- 最基本的几样 --------------------------------------------------------
// 主脚本的各块（scripts/ 里的文件）按 page/__init__.py 里 SCRIPTS 的顺序接在一起，放进同一个函数里，
// 共用一个作用域：这里定义的名字，后面每一块都能直接用。这一块排在最前面，放的是谁都要用的几样：
// 页面的口令（每个请求都要带）、按 id 找元素的 $、<html> 和 <body>。

const token = new URLSearchParams(location.search).get('token') || '';
const $ = (id) => document.getElementById(id);
const root = document.documentElement, body = document.body;
// 界面要等第一批内容画好才淡入。万一脚本中途出错，也要让界面显示出来，不能留下一个空白页
window.addEventListener('error', () => body.classList.add('ready'));
