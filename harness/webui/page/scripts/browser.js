  // ---- 浏览器 --------------------------------------------------------------
  // 助手可以操作一个智狐自己打开的 Chrome（见 harness/browser/）。这一节管页面上和它有关的三样东西：
  //   顶栏上的提示   浏览器开着时出现："浏览器正在被智狐控制"（正在操作）或"智狐的浏览器开着"，旁边一个按钮停下并关掉它
  //   对话里的一步   浏览器的工具卡片下面一张小截图（事件里带着的 thumb），点开在查看器里看大图（按 shot 编号去取）
  //   设置里的一节   开关、要不要窗口、Chrome 在哪儿、配置文件夹（在访达中显示 / 清除，清除先在原地确认）
  // 开没开、在不在运行、找没找到 Chrome、截图留几天……都由助手程序在状态里给（state.browser），页面不另算一份。

  let browserStatus = null;      // 状态里最新的那一份
  let browserDrawn = '';         // 设置里那一节上一次按什么画的（没变就不重画，保留焦点和输入框里写了一半的路径）
  let browserConfirming = false; // 配置文件夹那一行正在原地确认"清除"

  const isBrowserCall = (name) => typeof name === 'string' && /^browser[._]/.test(name);
  const browserShotDays = () => (browserStatus && browserStatus.shot_days) || 30;

  function browserThumb(event) {
    // 查看器顶上写的是这一步做了什么（browser.navigate https://…），翻看几张截图时分得清是哪一步
    const step = toolCard && toolCard.summary ? toolCard.summary.querySelector('.tool-name').textContent + ' ' + toolCard.summary.querySelector('.tool-args').textContent : '';
    const button = el('button', 'browser-thumb');
    button.type = 'button';
    tattr(button, 'title', k('看大图'));
    tattr(button, 'aria-label', k('看这一步的截图'));
    const image = button.appendChild(el('img'));
    image.alt = '';
    image.decoding = 'async';
    image.src = event.thumb;
    const shot = typeof event.shot === 'string' && /^[0-9a-f]{64}$/.test(event.shot) ? event.shot : '';
    const source = toolCard && toolCard.shots === 'computer' ? 'computer' : 'browser';  // 大图问哪个接口要
    const fallback = source === 'computer' ? t('电脑截图') : t('浏览器截图');
    button.addEventListener('click', () => openViewer([{ kind: 'image', name: step.trim() || fallback, thumb: event.thumb, shot, source, download: 'screenshot.jpg' }], 0, button));
    if (isLive()) play(button, [{ opacity: 0, transform: 'translateY(6px) scale(.97)' }, { opacity: 1, transform: 'none' }], 280);
    return button;
  }

  function renderBrowser(status, force) {
    if (!status) return;
    browserStatus = status;
    // 顶栏上的提示
    const live = $('browser-live');
    const wasHidden = live.hidden;
    live.hidden = !status.running;
    live.classList.toggle('busy', Boolean(status.busy));
    retext($('browser-live-text'), status.busy ? k('浏览器正在被智狐控制') : k('智狐的浏览器开着'));
    const stop = $('browser-live-stop');
    // 按钮里一个图标、一句话；窄窗口里只留图标（说明在 title 和 aria-label 里）
    stop.replaceChildren(icon(status.busy ? 'stop' : 'x'), tel('span', 'browser-live-label', status.busy ? k('停止') : k('关闭')));
    tattr(stop, 'title', status.busy ? k('停下浏览器正在做的事，并关掉它') : k('关掉智狐的浏览器（下次要用时会自动打开）'));
    tattr(stop, 'aria-label', status.busy ? k('停下浏览器正在做的事，并关掉它') : k('关掉智狐的浏览器（下次要用时会自动打开）'));
    if (wasHidden && !live.hidden && !isLive()) live.style.animation = 'none';  // 重放旧内容时不播入场动画
    else if (!live.hidden) live.style.animation = '';
    if (settings.open) drawBrowserSettings(force);
  }

  $('browser-live-stop').addEventListener('click', async () => {
    const button = $('browser-live-stop');
    button.disabled = true;
    const reply = await call('/api/browser/stop', {});
    button.disabled = false;
    toast(reply.ok ? t('已经停下并关掉了浏览器。') : reply.error);
  });

  function browserSay(text, kind) { say(text, kind); }  // 设置窗口开着：提示写在窗口里（浮在页面上的会被暗底挡住）

  async function setBrowser(changes, done) {
    const reply = await call('/api/browser/settings', changes);
    if (!reply.ok) { browserSay(reply.error, 'bad'); return false; }
    browserSay(reply.warning || done, reply.warning ? 'bad' : 'good');
    return true;
  }

  function drawBrowserSettings(force) {
    const status = browserStatus;
    if (!status) return;
    const print = JSON.stringify([status, language, browserConfirming]);
    if (!force && print === browserDrawn) return;
    browserDrawn = print;

    const enabled = $('browser-enabled');
    enabled.setAttribute('aria-checked', String(Boolean(status.enabled)));
    $('browser-card').classList.toggle('on', Boolean(status.enabled));
    retext($('browser-state'), !status.enabled ? k('（关着）') : status.running ? (status.busy ? k('（正在操作）') : k('（开着，正在运行）')) : k('（开着，用到时才启动）'));

    const headless = $('browser-headless');
    headless.setAttribute('aria-checked', String(Boolean(status.headless)));
    headless.disabled = Boolean(status.headless_forced);
    retext($('browser-headless-note'), status.headless_forced ? k('这台电脑没有屏幕（比如在服务器上），只能不显示窗口。')
      : status.headless_setting ? k('浏览器在后台运行，你看不到它，截图照样有。') : k('会弹出一个单独的浏览器窗口，你可以看着它操作。'));

    const found = $('browser-chrome');
    found.classList.toggle('bad', !status.chrome);
    if (status.chrome) { delete found.dataset.i18n; found.textContent = status.chrome; }
    else retext(found, status.path ? k('设置里填的路径不对：找不到这个 Chrome。') : k('没有找到 Chrome。装一个 Chrome，或者在下面填上它的路径。'));
    const path = $('browser-path');
    if (document.activeElement !== path && path.dataset.saved !== status.path) { path.value = status.path || ''; path.dataset.saved = status.path || ''; }

    const error = status.error;
    if (error && status.enabled) {
      // 上一次打开失败的原因（程序给了原文的话，换语言时跟着换）
      found.classList.add('bad');
      if (error.key) retext(found, error.key, error.values); else found.textContent = String(error.text || '');
    }
    retext($('browser-cost'), k('用 Claude 时，浏览器的工具每次请求要多发六千多个 token 的说明书（命中缓存时很便宜）；每张截图大约 {tokens} 个 token。浏览器闲置 {minutes} 分钟会自动关掉，登录状态留在配置文件夹里。'),
      { tokens: Number(status.shot_tokens || 0).toLocaleString(LOCALES[language]), minutes: status.idle_minutes || 10 });
    drawBrowserProfile(status);
  }

  // 配置文件夹那一行：和会话、记忆、Skill 的行一样，悬停时右边浮出按钮（在访达中显示、清除），清除先在原地确认
  function drawBrowserProfile(status) {
    const row = $('browser-profile');
    const hadFocus = row.contains(document.activeElement) ? document.activeElement.dataset.focus : '';
    if (browserConfirming) {
      const confirm = el('div', 'browser-confirm');
      confirm.appendChild(tel('span', null, k('清除以后，在这个浏览器里登录过的网站都要重新登录，缓存和历史记录也没有了。浏览器开着的话会先关掉。')));
      const yes = confirm.appendChild(tel('button', 'mini danger', k('清除')));
      const no = confirm.appendChild(tel('button', 'mini', k('取消')));
      yes.type = no.type = 'button';
      yes.dataset.focus = 'yes';
      no.dataset.focus = 'no';
      const back = () => { browserConfirming = false; drawBrowserSettings(true); const trash = row.querySelector('[data-focus="trash"]'); if (trash) trash.focus(); };
      no.addEventListener('click', back);
      confirm.addEventListener('keydown', (key) => { if (key.key === 'Escape') { key.preventDefault(); key.stopPropagation(); back(); } });
      yes.addEventListener('click', async () => {
        yes.disabled = no.disabled = true;
        const reply = await call('/api/browser/clear', {});
        browserConfirming = false;
        browserSay(reply.ok ? t('配置文件夹已经清除。') : reply.error, reply.ok ? 'good' : 'bad');
        drawBrowserSettings(true);
      });
      row.replaceChildren(confirm);
      (row.querySelector('[data-focus="' + (hadFocus || 'no') + '"]') || no).focus();
      return;
    }
    const main = el('div', 'browser-profile-main');
    const glyph = main.appendChild(el('span', 'browser-profile-glyph'));
    glyph.appendChild(icon('folder'));
    const words = main.appendChild(el('div', 'browser-profile-words'));
    words.appendChild(tel('span', 'browser-profile-name', k('配置文件夹')));
    const where = words.appendChild(el('span', 'browser-profile-path', status.profile));
    where.title = status.profile;
    const actions = el('div', 'browser-profile-actions');
    const reveal = actions.appendChild(iconButton('folder', revealLabel()));
    reveal.dataset.focus = 'reveal';
    reveal.addEventListener('click', async () => {
      const reply = await call('/api/browser/reveal', {});
      if (!reply.ok) browserSay(reply.error, 'bad');
      else if (state && state.file_manager === false) browserSay(t('文件在：{path}', { path: reply.data.path || status.profile }), 'good');
    });
    const trash = actions.appendChild(iconButton('trash', k('清除配置文件夹')));
    trash.classList.add('danger-hover');
    trash.dataset.focus = 'trash';
    trash.disabled = !status.profile_exists;
    if (!status.profile_exists) tattr(trash, 'title', k('还没有用过浏览器，没有可以清除的'));
    trash.addEventListener('click', () => { browserConfirming = true; drawBrowserSettings(true); });
    row.replaceChildren(main, actions);
    if (hadFocus) { const again = row.querySelector('[data-focus="' + hadFocus + '"]'); if (again && !again.disabled) again.focus(); }
  }

  $('browser-enabled').addEventListener('click', async () => {
    const on = $('browser-enabled').getAttribute('aria-checked') !== 'true';
    await setBrowser({ enabled: on }, on ? t('浏览器已打开：助手用到它的时候才会启动。') : t('浏览器已关闭。'));
  });
  $('browser-headless').addEventListener('click', async () => {
    const on = $('browser-headless').getAttribute('aria-checked') !== 'true';
    await setBrowser({ headless: on }, on ? t('以后浏览器在后台运行，不显示窗口。') : t('以后浏览器会显示窗口。'));
  });
  async function saveBrowserPath() {
    const path = $('browser-path');
    const saved = await setBrowser({ path: path.value.trim() }, path.value.trim() ? t('Chrome 的路径已保存。') : t('已改回自动查找 Chrome。'));
    if (saved) path.dataset.saved = path.value.trim();
  }
  $('browser-path-save').addEventListener('click', saveBrowserPath);
  $('browser-path').addEventListener('keydown', (key) => {
    if (key.key === 'Enter' && !key.isComposing) { key.preventDefault(); saveBrowserPath(); }
  });
