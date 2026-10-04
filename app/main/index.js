'use strict';
const { app, BrowserWindow, ipcMain, dialog, shell, screen } = require('electron');
const path = require('path');
const fs = require('fs');
const { ConfigStore, SCRIPT_NAME } = require('./config');
const { ScraperRunner, normalizeProfileUrl } = require('./scraper');
const { DataWatcher } = require('./watcher');
const { LoginChecker } = require('./loginchecker');

let win = null;
let configStore = null;
let runner = null;
let loginChecker = null;
const watcher = new DataWatcher();

function writeStartupLog(event, details = '') {
  try {
    const logPath = path.join(app.getPath('userData'), 'startup.log');
    fs.mkdirSync(path.dirname(logPath), { recursive: true });
    const safeDetails = String(details || '').replace(/[\r\n]+/g, ' ').slice(0, 1200);
    const suffix = safeDetails ? ` ${safeDetails}` : '';
    fs.appendFileSync(
      logPath,
      `[${new Date().toISOString()}] pid=${process.pid} ${event}${suffix}\n`,
      'utf-8'
    );
  } catch (_e) {
    // 诊断日志失败不能阻止应用启动。
  }
}

process.on('uncaughtExceptionMonitor', (error) => {
  writeStartupLog('uncaught-exception', error && (error.stack || error.message || error));
});

process.on('unhandledRejection', (reason) => {
  writeStartupLog('unhandled-rejection', reason && (reason.stack || reason.message || reason));
});

function send(channel, payload) {
  if (win && !win.isDestroyed()) {
    win.webContents.send(channel, payload);
  }
}

function resolvePathMaybeRelative(baseDir, p) {
  if (!p) return '';
  return path.isAbsolute(p) ? p : path.join(baseDir, p);
}

function emitTaskLine(line) {
  send('task:line', line);
}

function mdTargetPath() {
  const cfg = configStore.get();
  return resolvePathMaybeRelative(cfg.scriptDir, cfg.params.output || 'douyin_全话题_去重昵称汇总.md');
}

function storeTargetPath() {
  const cfg = configStore.get();
  return resolvePathMaybeRelative(cfg.scriptDir, cfg.params.storeFile || '.douyin_unique_commenters_store.jsonl');
}

function historyTargetPath() {
  const cfg = configStore.get();
  return resolvePathMaybeRelative(cfg.scriptDir, cfg.params.historyFile || '.douyin_scan_history.jsonl');
}

function configuredBrowserExecutable(channel) {
  const local = process.env.LOCALAPPDATA || '';
  const programFiles = process.env.PROGRAMFILES || '';
  const programFilesX86 = process.env['PROGRAMFILES(X86)'] || '';
  const candidates = channel === 'chrome'
    ? [
        path.join(programFiles, 'Google', 'Chrome', 'Application', 'chrome.exe'),
        path.join(programFilesX86, 'Google', 'Chrome', 'Application', 'chrome.exe'),
        path.join(local, 'Google', 'Chrome', 'Application', 'chrome.exe'),
      ]
    : [
        path.join(programFilesX86, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
        path.join(programFiles, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
        path.join(local, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
      ];
  return candidates.find((candidate) => candidate && fs.existsSync(candidate)) || '';
}

function openProfileUrlWhenIdle(url) {
  const normalizedUrl = normalizeProfileUrl(url);
  if (!normalizedUrl) return { ok: false, error: '只允许打开抖音用户主页链接' };
  const cfg = configStore.get();
  const channel = cfg.params.browserChannel || 'msedge';
  if (!['msedge', 'chrome'].includes(channel)) {
    return { ok: false, error: '当前浏览器通道无法独立启动登录配置，请先运行抓取任务' };
  }
  const profileDir = resolvePathMaybeRelative(cfg.scriptDir, cfg.params.profileDir);
  if (!profileDir || !fs.existsSync(profileDir)) {
    return { ok: false, error: `浏览器配置目录不存在: ${profileDir}` };
  }
  const executable = configuredBrowserExecutable(channel);
  if (!executable) return { ok: false, error: `找不到已安装的 ${channel} 浏览器` };

  try {
    const { spawn } = require('child_process');
    const child = spawn(
      executable,
      [`--user-data-dir=${profileDir}`, '--new-tab', normalizedUrl],
      { detached: true, stdio: 'ignore', windowsHide: false }
    );
    child.unref();
    return { ok: true, mode: 'profile-browser' };
  } catch (e) {
    return { ok: false, error: `浏览器启动失败: ${e.message}` };
  }
}

function startWatcher() {
  watcher.stop();
  const cfg = configStore.get();
  watcher.onSnapshot = (snap) => send('data:snapshot', snap);
  watcher.watch({
    mdPath: mdTargetPath(),
    storePath: storeTargetPath(),
    historyPath: historyTargetPath(),
  });
  return watcher.readNow();
}

function validateEnv(cfg) {
  if (!cfg.scriptDir || !fs.existsSync(path.join(cfg.scriptDir, SCRIPT_NAME))) {
    return `脚本目录无效: 找不到 ${SCRIPT_NAME}。请在左侧『环境设置』中选择包含该脚本的目录。`;
  }
  const profileDir = resolvePathMaybeRelative(cfg.scriptDir, cfg.params.profileDir);
  if (!profileDir || !fs.existsSync(profileDir)) {
    return `浏览器配置目录不存在: ${profileDir}。请选择 .edge_user_data_clone 目录。`;
  }
  const isFeedMode = cfg.params.mode === 'feed';
  if (!isFeedMode && !String(cfg.params.topicKeyword || '').trim()) {
    return '请填写话题关键词。';
  }
  return '';
}

function runPythonProbe(pythonPath) {
  return new Promise((resolve) => {
    const { spawn } = require('child_process');
    const code = 'import sys; import playwright; print("PYTHON_OK " + sys.version.split()[0])';
    let child;
    try {
      child = spawn(pythonPath, ['-c', code], {
        env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (e) {
      resolve({ ok: false, error: e.message });
      return;
    }
    let out = '';
    let err = '';
    child.stdout.on('data', (d) => { out += d.toString('utf-8'); });
    child.stderr.on('data', (d) => { err += d.toString('utf-8'); });
    child.on('error', (e) => resolve({ ok: false, error: `无法启动 Python (${pythonPath}): ${e.message}` }));
    child.on('exit', (code) => {
      if (code === 0 && out.includes('PYTHON_OK')) {
        resolve({ ok: true, info: out.trim() });
      } else if (code === 0) {
        resolve({ ok: false, error: 'Python 可用但未安装 playwright。请执行: pip install playwright' });
      } else {
        resolve({ ok: false, error: (err || out).trim() || `Python 退出码 ${code}` });
      }
    });
  });
}

// 运行辅助脚本（如 douyin_comment_link_demo.py），输出转发到控制台
function runHelper(pythonPath, args, cwd) {
  return new Promise((resolve) => {
    const { spawn } = require('child_process');
    let child;
    try {
      child = spawn(pythonPath, args, {
        cwd,
        env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' },
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (e) {
      resolve({ ok: false, error: e.message });
      return;
    }
    const pipe = (stream, kind) => {
      let buf = '';
      stream.on('data', (chunk) => {
        buf += chunk.toString('utf-8');
        const lines = buf.split(/\r?\n/);
        buf = lines.pop();
        for (const l of lines) {
          if (l) emitTaskLine({ kind, text: l, ts: Date.now() });
        }
      });
      stream.on('end', () => { if (buf) emitTaskLine({ kind, text: buf, ts: Date.now() }); });
    };
    let stderrTail = '';
    const errCollect = (chunk) => {
      stderrTail = (stderrTail + chunk.toString('utf-8')).slice(-800);
    };
    pipe(child.stdout, 'stdout');
    pipe(child.stderr, 'stderr');
    child.stderr.on('data', errCollect);
    child.on('error', (e) => resolve({ ok: false, error: e.message }));
    child.on('exit', (code) => {
      if (code === 0) resolve({ ok: true, code });
      else resolve({ ok: false, code, error: stderrTail.trim() || `退出码 ${code}` });
    });
  });
}

function registerIpc() {
  ipcMain.handle('config:get', () => configStore.get());

  ipcMain.handle('config:save', (_e, cfg) => {
    const saved = configStore.save(cfg || {});
    startWatcher();
    return saved;
  });

  ipcMain.handle('task:start', (_e, params) => {
    if (runner && runner.running) return { ok: false, error: '任务已在运行' };
    if (loginChecker && loginChecker.running) return { ok: false, error: '登录检测进行中，请稍候' };
    configStore.save({ params: params || {} });
    const cfg = configStore.get();
    const err = validateEnv(cfg);
    if (err) return { ok: false, error: err };
    try {
      runner = new ScraperRunner({
        scriptDir: cfg.scriptDir,
        pythonPath: cfg.pythonPath,
        onLine: emitTaskLine,
        onStatus: (s) => send('task:status', s),
        onExit: (r) => send('task:exit', r),
      });
      startWatcher();
      const profileDir = resolvePathMaybeRelative(cfg.scriptDir, cfg.params.profileDir);
      const pid = runner.start({ ...cfg.params, profileDir });
      return { ok: true, pid };
    } catch (e) {
      return { ok: false, error: String(e.message || e) };
    }
  });

  ipcMain.handle('task:stop', () => {
    if (runner && runner.running) return { ok: runner.stop() };
    return { ok: false, error: '没有运行中的任务' };
  });

  ipcMain.handle('login:check', async (_e, opts) => {
    if (runner && runner.running) return { ok: false, error: '任务运行中，无法检测登录' };
    if (loginChecker && loginChecker.running) return { ok: false, error: '登录检测进行中，请稍候' };
    const cfg = configStore.get();
    const profileDir = resolvePathMaybeRelative(
      cfg.scriptDir,
      (opts && opts.profileDir) || cfg.params.profileDir
    );
    if (!fs.existsSync(profileDir)) {
      return { ok: false, error: `浏览器配置目录不存在: ${profileDir}` };
    }
    loginChecker = new LoginChecker({
      scriptDir: cfg.scriptDir,
      pythonPath: cfg.pythonPath,
      onLine: emitTaskLine,
      onStatus: (s) => send('login:status', s),
    });
    return loginChecker.run({
      mode: (opts && opts.mode) || 'check',
      profileDir,
      browserChannel: cfg.params.browserChannel,
      timeoutSeconds: (opts && opts.timeoutSeconds) || 300,
      headless: !!(opts && opts.headless),
    });
  });

  ipcMain.handle('result:open', () => {
    const p = mdTargetPath();
    if (fs.existsSync(p)) shell.openPath(p);
    return { ok: true, path: p };
  });

  ipcMain.handle('folder:open', () => {
    const cfg = configStore.get();
    if (cfg.scriptDir) shell.openPath(cfg.scriptDir);
    return { ok: true, path: cfg.scriptDir };
  });

  ipcMain.handle('external:open', async (_e, url) => {
    if (runner && runner.running) return runner.openProfileUrl(url);
    return openProfileUrlWhenIdle(url);
  });

  ipcMain.handle('dialog:chooseDir', async () => {
    const r = await dialog.showOpenDialog(win, { properties: ['openDirectory'] });
    return r.canceled ? '' : r.filePaths[0];
  });

  ipcMain.handle('dialog:chooseFile', async (_e, filters) => {
    const r = await dialog.showOpenDialog(win, { properties: ['openFile'], filters: filters || [] });
    return r.canceled ? '' : r.filePaths[0];
  });

  ipcMain.handle('env:check', () => runPythonProbe(configStore.get().pythonPath));

  ipcMain.handle('data:refresh', () => watcher.readNow() || null);

  ipcMain.handle('locator:generate', (_e, opts) => {
    if (runner && runner.running) return { ok: false, error: '任务运行中，无法生成清单' };
    if (loginChecker && loginChecker.running) return { ok: false, error: '登录检测进行中，请稍候' };
    const cfg = configStore.get();
    const args = [path.join(cfg.scriptDir, 'douyin_comment_link_demo.py')];
    args.push('--input', (opts && opts.inputCsv) || 'douyin_comments_input.csv');
    if (opts && opts.checkUrl) args.push('--check-url');
    return runHelper(cfg.pythonPath, args, cfg.scriptDir);
  });

  ipcMain.handle('locator:open', () => {
    const cfg = configStore.get();
    const p = path.join(cfg.scriptDir, 'douyin_comment_targets_demo.html');
    if (fs.existsSync(p)) shell.openPath(p);
    return { ok: true, path: p };
  });
}

function createWindow() {
  win = new BrowserWindow({
    width: 1480,
    height: 960,
    minWidth: 1200,
    minHeight: 760,
    title: '抖音评论筛查',
    webPreferences: {
      preload: path.join(__dirname, '..', 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.removeMenu();
  win.on('unresponsive', () => writeStartupLog('window-unresponsive'));
  win.on('responsive', () => writeStartupLog('window-responsive'));
  win.on('closed', () => { win = null; });
  win.webContents.on('render-process-gone', (_event, details) => {
    writeStartupLog('renderer-gone', JSON.stringify(details || {}));
  });
  win.webContents.on('did-fail-load', (_event, code, description, url) => {
    writeStartupLog('renderer-load-failed', `code=${code} description=${description} url=${url}`);
  });
  win.loadFile(path.join(__dirname, '..', 'renderer', 'index.html'))
    .then(() => writeStartupLog('window-loaded'))
    .catch((error) => writeStartupLog('window-load-error', error && error.message));
  return win;
}

function bringWindowToFront() {
  if (!win || win.isDestroyed()) {
    writeStartupLog('window-recreated');
    createWindow();
    return;
  }

  if (win.isMinimized()) win.restore();
  if (!win.isVisible()) win.show();

  const bounds = win.getBounds();
  const visibleOnScreen = screen.getAllDisplays().some(({ workArea }) => {
    const width = Math.min(bounds.x + bounds.width, workArea.x + workArea.width)
      - Math.max(bounds.x, workArea.x);
    const height = Math.min(bounds.y + bounds.height, workArea.y + workArea.height)
      - Math.max(bounds.y, workArea.y);
    return width >= 120 && height >= 80;
  });
  if (!visibleOnScreen) {
    const { workArea } = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
    const x = Math.round(workArea.x + Math.max(0, (workArea.width - bounds.width) / 2));
    const y = Math.round(workArea.y + Math.max(0, (workArea.height - bounds.height) / 2));
    win.setPosition(x, y);
    writeStartupLog('window-recentered', `x=${x} y=${y}`);
  }

  win.show();
  win.focus();
  win.moveTop();

  // Windows 有时拒绝后台进程抢焦点；短暂置顶可可靠唤回旧实例。
  if (process.platform === 'win32') {
    win.setAlwaysOnTop(true);
    setTimeout(() => {
      if (!win || win.isDestroyed()) return;
      win.setAlwaysOnTop(false);
      win.focus();
    }, 250);
  }
  writeStartupLog('window-focused');
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  writeStartupLog('single-instance-lock-denied');
  app.quit();
} else {
  writeStartupLog('single-instance-lock-acquired');
  app.on('second-instance', () => {
    writeStartupLog('second-instance-request');
    bringWindowToFront();
  });

  app.whenReady().then(() => {
    configStore = new ConfigStore();
    registerIpc();
    createWindow();
    startWatcher(); // 首次启动也要读取已有结果，否则表格/批次/统计为空
  });

  app.on('window-all-closed', () => {
    app.quit();
  });

  app.on('before-quit', () => {
    writeStartupLog('before-quit');
    watcher.stop();
    if (runner) runner.kill();
    if (loginChecker) loginChecker.kill();
  });
}
