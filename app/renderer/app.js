'use strict';
const $ = (id) => document.getElementById(id);

const state = {
  running: false,
  startedAt: null,
  maxRunMinutes: 0,
  consoleLines: 0,
  autoScroll: true,
};

const paramIds = {
  mode: 'mode',
  topicUrl: 'topicUrl',
  topicKeyword: 'topicKeyword',
  ips: 'ips',
  sort: 'sort',
  maxVideos: 'maxVideos',
  topicScrollRounds: 'topicScrollRounds',
  commentScrollRounds: 'commentScrollRounds',
  output: 'output',
  storeFile: 'storeFile',
  historyFile: 'historyFile',
  profileDir: 'profileDir',
  browserChannel: 'browserChannel',
  headless: 'headless',
  pauseSeconds: 'pauseSeconds',
  maxCommentAgeDays: 'maxCommentAgeDays',
  maxRunMinutes: 'maxRunMinutes',
  captchaWaitSeconds: 'captchaWaitSeconds',
  readyTimeoutSeconds: 'readyTimeoutSeconds',
  stopFlagFile: 'stopFlagFile',
};
const numIds = new Set([
  'maxVideos', 'topicScrollRounds', 'commentScrollRounds', 'pauseSeconds',
  'maxCommentAgeDays', 'maxRunMinutes', 'captchaWaitSeconds', 'readyTimeoutSeconds',
]);

function fmt(sec) {
  sec = Math.max(0, Math.floor(sec));
  const m = String(Math.floor(sec / 60)).padStart(2, '0');
  const s = String(sec % 60).padStart(2, '0');
  return `${m}:${s}`;
}

function readParams() {
  const p = {};
  for (const [key, id] of Object.entries(paramIds)) {
    const el = $(id);
    if (!el) continue;
    p[key] = el.type === 'checkbox' ? el.checked : el.value;
  }
  for (const id of numIds) p[id] = Number(p[id] || 0);
  return p;
}

function fillForm(params) {
  for (const [key, id] of Object.entries(paramIds)) {
    const el = $(id);
    if (!el) continue;
    if (el.type === 'checkbox') el.checked = !!params[key];
    else el.value = params[key] === undefined || params[key] === null ? '' : params[key];
  }
}

function appendConsole(kind, text) {
  const c = $('console');
  const div = document.createElement('div');
  const time = new Date().toTimeString().slice(0, 8);
  let cls = kind === 'stderr' ? 'stderr' : kind === 'app' ? 'app' : 'stdout';
  if (/验证码|安全验证|滑块|风险提示/.test(String(text))) cls = 'captcha';
  div.className = 'line ' + cls;
  div.textContent = `[${time}] ${text}`;
  c.appendChild(div);
  state.consoleLines += 1;
  if (state.consoleLines > 3000) {
    c.removeChild(c.firstChild);
    state.consoleLines -= 1;
  }
  if (state.autoScroll) c.scrollTop = c.scrollHeight;
}

function flash(msg) {
  $('state').textContent = msg;
  appendConsole('app', msg);
}

function setRunning(running) {
  state.running = running;
  $('btnStart').disabled = running;
  $('btnStop').disabled = !running;
  if (running) {
    state.startedAt = Date.now();
    state.maxRunMinutes = Number($('maxRunMinutes').value || 0);
    $('state').textContent = '运行中';
    $('state').className = 'running';
  } else {
    $('state').textContent = '空闲';
    $('state').className = '';
    $('statDeadline').textContent = '限时: 未启用';
  }
}

function setLoginBadge(result) {
  const b = $('loginBadge');
  const s = result && result.state;
  if (s === 'logged-in') {
    b.textContent = '登录态: 已登录 ✅';
    b.className = 'badge logged-in';
  } else if (s === 'logged-out') {
    b.textContent = '登录态: 未登录 ❌';
    b.className = 'badge logged-out';
  } else if (s === 'checking') {
    b.textContent = '登录态: 检测中…';
    b.className = 'badge checking';
  } else if (s === 'waiting') {
    b.textContent = `扫码登录中… 剩余 ${result.remaining_seconds || 0}s`;
    b.className = 'badge waiting';
  } else if (s === 'timeout') {
    b.textContent = '登录态: 等待超时 ❌';
    b.className = 'badge logged-out';
  } else if (s === 'error') {
    b.textContent = '登录态: 检测异常';
    b.className = 'badge logged-out';
  } else {
    b.textContent = '登录态: 未检测';
    b.className = 'badge unknown';
  }
}

function td(text, cls) {
  const d = document.createElement('td');
  d.textContent = text || '';
  if (cls) d.className = cls;
  return d;
}

function tdLink(url) {
  const d = document.createElement('td');
  if (url) {
    const a = document.createElement('a');
    a.textContent = '打开主页';
    a.href = '#';
    a.addEventListener('click', async (e) => {
      e.preventDefault();
      const res = await window.api.openExternal(url);
      if (!res || !res.ok) flash((res && res.error) || '主页打开失败');
    });
    d.appendChild(a);
  }
  return d;
}

function renderCurrentTable(rows) {
  const tbody = $('resultBody');
  tbody.innerHTML = '';
  for (const r of rows || []) {
    const tr = document.createElement('tr');
    tr.appendChild(td(r.nickname));
    tr.appendChild(td(r.ip, 'ip'));
    tr.appendChild(td(r.time, 'time'));
    tr.appendChild(tdLink(r.profileUrl));
    tr.appendChild(td(r.comment));
    tbody.appendChild(tr);
  }
  $('resultCount').textContent = (rows || []).length ? `(${rows.length})` : '';
}

function renderBatches(batches) {
  const box = $('batches');
  box.innerHTML = '';
  if (!batches || !batches.length) {
    box.innerHTML = '<div class="empty">暂无批次数据（运行一次抓取后显示）</div>';
    return;
  }
  const ordered = [...batches].reverse(); // 最新在前
  for (const b of ordered) {
    const card = document.createElement('details');
    card.className = 'batch';
    const summary = document.createElement('summary');
    summary.textContent =
      `${b.title || ''} · ${b.startTime || '未知时间'} · 关键词:${b.keyword || '-'} · ` +
      `扫描:${b.scanned || 0} · 新增去重:${b.filtered || 0} · ID键:${b.dedupeKeyCount || 0}`;
    card.appendChild(summary);
    const body = document.createElement('div');
    body.className = 'batch-body';
    if (b.entry) {
      const p = document.createElement('div');
      p.className = 'batch-entry';
      p.textContent = `入口: ${b.entry}`;
      body.appendChild(p);
    }
    if (b.records && b.records.length) {
      const table = document.createElement('table');
      table.className = 'data-table';
      table.innerHTML =
        '<thead><tr><th>昵称</th><th>IP</th><th>时间</th><th>账号</th><th>评论内容</th><th>ID键</th></tr></thead>';
      const tbody = document.createElement('tbody');
      for (const r of b.records) {
        const tr = document.createElement('tr');
        tr.appendChild(td(r.nickname));
        tr.appendChild(td(r.ip, 'ip'));
        tr.appendChild(td(r.time, 'time'));
        tr.appendChild(tdLink(r.profileUrl));
        tr.appendChild(td(r.comment));
        tr.appendChild(td(r.dedupeKey, 'key'));
        tbody.appendChild(tr);
      }
      table.appendChild(tbody);
      body.appendChild(table);
    } else {
      const p = document.createElement('div');
      p.className = 'empty';
      p.textContent = '本批次无明细记录（历史摘要已压缩）';
      body.appendChild(p);
    }
    card.appendChild(body);
    box.appendChild(card);
  }
}

function renderSnapshot(s) {
  if (!s) return;
  const meta = s.meta || {};
  $('statScanned').textContent = `已扫内容: ${meta['本次扫描内容数'] || 0}`;
  $('statComments').textContent = `评论总数: ${meta['本次抓取评论总数'] || 0}`;
  $('statFiltered').textContent = `本次筛出: ${meta['本次筛出评论者数(按昵称去重)'] || 0}`;
  $('statGlobal').textContent = `全局去重: ${meta['全局去重后评论者数(按账号ID)'] || s.storeCount || 0}`;
  renderCurrentTable(s.current);
  renderBatches(s.batches);
}

function switchTab(name) {
  document.querySelectorAll('.tab').forEach((b) => {
    b.classList.toggle('active', b.dataset.tab === name);
  });
  document.querySelectorAll('.tab-panel').forEach((p) => {
    p.classList.toggle('active', p.id === 'tab-' + name);
  });
}

async function saveConfig() {
  const params = readParams();
  return window.api.saveConfig({
    scriptDir: $('scriptDir').value,
    pythonPath: $('pythonPath').value,
    params,
  });
}

function applyModeVisibility(mode) {
  const row = $('keywordRow');
  if (row) row.style.display = mode === 'feed' ? 'none' : '';
}

function onModeChange() {
  const mode = $('mode').value;
  applyModeVisibility(mode);
  appendConsole(
    'app',
    mode === 'feed'
      ? '已切换到推荐流模式：程序将自动刷首页推荐视频并抓取评论区'
      : '已切换到搜索模式：按关键词搜索视频后抓取评论'
  );
}

async function onStart() {
  const params = readParams();
  if (params.mode !== 'feed' && !params.topicKeyword.trim()) {
    flash('请填写话题关键词');
    return;
  }
  await saveConfig();
  const res = await window.api.startTask(params);
  if (!res.ok) {
    flash(res.error);
    return;
  }
  appendConsole('app', `任务已启动 (pid ${res.pid})`);
}

async function onStop() {
  const res = await window.api.stopTask();
  if (!res.ok) flash(res.error);
}

async function onCheckLogin() {
  await saveConfig();
  setLoginBadge({ state: 'checking' });
  appendConsole('app', '开始检测登录态…');
  const res = await window.api.checkLogin({});
  if (!res.ok) {
    flash(res.error);
    setLoginBadge({ state: 'error' });
    return;
  }
  const r = res.result || {};
  setLoginBadge(r);
  appendConsole('app', r.state === 'logged-in' ? '检测完成: 已登录' : '检测完成: 未登录');
}

async function onScanLogin() {
  await saveConfig();
  appendConsole('app', '启动扫码登录流程，请在弹出的浏览器窗口中扫码…');
  const res = await window.api.waitLogin({ timeoutSeconds: 300 });
  if (!res.ok) {
    flash(res.error || '登录检测失败');
    setLoginBadge({ state: 'error' });
    return;
  }
  const r = res.result || {};
  setLoginBadge(r);
  if (r.state === 'logged-in') appendConsole('app', '登录成功 ✅');
  else if (r.state === 'timeout') appendConsole('stderr', '登录等待超时');
  else appendConsole('app', '登录流程结束');
}

async function onEnvCheck() {
  appendConsole('app', '检测 Python 环境…');
  const res = await window.api.envCheck();
  if (res.ok) {
    appendConsole('app', `环境正常: ${res.info}`);
    flash('环境正常: ' + res.info);
  } else {
    appendConsole('stderr', `环境异常: ${res.error}`);
    flash('环境异常: ' + res.error);
  }
}

async function pickScriptDir() {
  const d = await window.api.chooseDir();
  if (d) {
    $('scriptDir').value = d;
    await saveConfig();
    const snap = await window.api.refreshData();
    renderSnapshot(snap);
    appendConsole('app', '脚本目录已切换: ' + d);
  }
}

async function pickProfileDir() {
  const d = await window.api.chooseDir();
  if (d) {
    $('profileDir').value = d;
    await saveConfig();
  }
}

async function onGenLocator() {
  await saveConfig();
  appendConsole('app', '生成评论定位清单…');
  const res = await window.api.generateLocator({
    inputCsv: $('locatorCsv').value,
    checkUrl: $('locatorCheckUrl').checked,
  });
  if (res.ok) {
    appendConsole('app', '定位清单生成完成');
    flash('定位清单生成完成');
  } else {
    appendConsole('stderr', `定位清单生成失败: ${res.error || ''}`);
    flash('定位清单生成失败: ' + res.error);
  }
}

function bind() {
  $('paramsForm').addEventListener('submit', (e) => e.preventDefault());
  $('btnStart').addEventListener('click', onStart);
  $('btnStop').addEventListener('click', onStop);
  $('mode').addEventListener('change', onModeChange);
  $('btnCheckLogin').addEventListener('click', onCheckLogin);
  $('btnScanLogin').addEventListener('click', onScanLogin);
  $('btnOpenResult').addEventListener('click', () => window.api.openResult());
  $('btnOpenFolder').addEventListener('click', () => window.api.openFolder());
  $('btnGenLocator').addEventListener('click', onGenLocator);
  $('btnOpenLocator').addEventListener('click', () => window.api.openLocatorHtml());
  $('btnPickScriptDir').addEventListener('click', pickScriptDir);
  $('btnPickProfileDir').addEventListener('click', pickProfileDir);
  $('btnEnvCheck').addEventListener('click', onEnvCheck);

  document.querySelectorAll('.tab').forEach((b) => {
    b.addEventListener('click', () => switchTab(b.dataset.tab));
  });

  const c = $('console');
  c.addEventListener('scroll', () => {
    state.autoScroll = c.scrollTop + c.clientHeight >= c.scrollHeight - 20;
  });

  window.api.onTaskLine((l) => appendConsole(l.kind, l.text));
  window.api.onTaskStatus((s) => {
    if (s && s.state === 'running') setRunning(true);
    else if (s && (s.state === 'stopped' || s.state === 'error')) setRunning(false);
  });
  window.api.onTaskExit((r) => {
    setRunning(false);
    appendConsole('app', `任务结束: code=${r.code}${r.stoppedByUser ? ' (用户停止)' : ''}`);
    setTimeout(async () => renderSnapshot(await window.api.refreshData()), 1200);
  });
  window.api.onData(renderSnapshot);
  window.api.onLoginStatus((s) => {
    if (s && s.result) setLoginBadge(s.result);
    else if (s && s.state === 'checking') setLoginBadge({ state: 'checking' });
  });

  setInterval(() => {
    if (state.running && state.startedAt) {
      const sec = (Date.now() - state.startedAt) / 1000;
      $('statElapsed').textContent = `耗时: ${fmt(sec)}`;
      if (state.maxRunMinutes > 0) {
        $('statDeadline').textContent = `限时: ${fmt(state.maxRunMinutes * 60 - sec)}`;
      }
    }
  }, 1000);
}

async function init() {
  const cfg = await window.api.getConfig();
  $('scriptDir').value = cfg.scriptDir || '';
  $('pythonPath').value = cfg.pythonPath || 'python';
  fillForm(cfg.params || {});
  if (!$('mode').value) $('mode').value = 'search';
  applyModeVisibility($('mode').value);
  appendConsole('app', '应用已就绪。脚本目录: ' + (cfg.scriptDir || '(未设置)'));
  const snap = await window.api.refreshData();
  renderSnapshot(snap);
  bind();
}

init();
