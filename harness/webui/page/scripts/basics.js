  const token = new URLSearchParams(location.search).get('token') || '';
  const $ = (id) => document.getElementById(id);
  const root = document.documentElement, body = document.body;
  // 界面要等第一批内容画好才淡入。万一脚本中途出错，也要让界面显示出来，不能留下一个空白页
  window.addEventListener('error', () => body.classList.add('ready'));
