'use strict';
// Launch an actual packaged app with an isolated profile; never start a scrape.
const assert = require('assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const exe = path.resolve(process.argv[2] || 'dist/win-unpacked/抖音评论筛查.exe');
const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'douyin-smoke-'));
let child;
let socket;
async function main() {
  const env = { ...process.env };
  delete env.ELECTRON_RUN_AS_NODE;
  let stderr = '';
  child = spawn(exe, [`--user-data-dir=${userData}`, '--remote-debugging-port=0'], {
    windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'], env,
  });
  child.stderr.on('data', (chunk) => { stderr = (stderr + chunk.toString()).slice(-3000); });
  let launchError;
  child.on('error', (error) => { launchError = error; });
  const portFile = path.join(userData, 'DevToolsActivePort');
  const deadline = Date.now() + 45000;
  let page;
  while (Date.now() < deadline) {
    if (launchError) throw launchError;
    if (child.exitCode !== null) throw new Error(`EXE exited early (${child.exitCode}): ${stderr}`);
    if (fs.existsSync(portFile)) {
      const port = fs.readFileSync(portFile, 'utf8').split(/\r?\n/)[0];
      try {
        const pages = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
        page = pages.find((p) => p.type === 'page' && p.url.includes('index.html'));
        if (page) break;
      } catch (_) { /* Wait for the local debugging endpoint. */ }
    }
    await delay(200);
  }
  assert.ok(page, 'Packaged renderer must load within 45 seconds');
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', reject, { once: true });
  });
  const result = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('Renderer IPC timed out')), 10000);
    socket.addEventListener('message', (event) => {
      const data = JSON.parse(event.data);
      if (data.id !== 1) return;
      clearTimeout(timeout);
      if (data.error || data.result.exceptionDetails) reject(new Error(JSON.stringify(data)));
      else resolve(data.result.result.value);
    });
    socket.send(JSON.stringify({ id: 1, method: 'Runtime.evaluate', params: {
      expression: '(async () => {if(document.readyState!=="complete") await new Promise(r=>window.addEventListener("load",r,{once:true})); return {title:document.title,config:await window.api.getConfig(),snapshot:await window.api.refreshData(),keywordPlaceholder:document.getElementById("topicKeyword").placeholder,entryReadonly:document.getElementById("topicUrl").readOnly};})()',
      awaitPromise: true, returnByValue: true,
    } }));
  });
  assert.equal(result.title, '抖音评论筛查');
  assert.equal(result.config.scriptDir, path.join(userData, 'runtime'));
  assert.ok(fs.statSync(result.config.params.profileDir).isDirectory());
  for (const name of ['douyin_topic_comment_export.py', 'login_check.py', 'douyin_comment_link_demo.py']) {
    assert.ok(fs.existsSync(path.join(result.config.scriptDir, name)));
  }
  assert.equal(result.config.params.topicUrl, 'https://www.douyin.com/');
  assert.equal(result.config.params.topicKeyword, '');
  assert.equal(result.keywordPlaceholder, '');
  assert.equal(result.entryReadonly, true);
  assert.equal(fs.existsSync(path.join(result.config.scriptDir, '.douyin_scan_history.jsonl')), false);
  console.log('[PASS] Actual packaged EXE: renderer, preload IPC, fresh profile, bundled scripts, empty history');
  console.log('EXE: ' + exe);
  socket.send(JSON.stringify({ id: 2, method: 'Runtime.evaluate', params: { expression: 'window.close()' } }));
  await delay(1000);
}
main().catch((error) => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ id: 3, method: 'Runtime.evaluate', params: { expression: 'window.close()' } }));
    await delay(1000);
    socket.close();
  }
  if (child && child.exitCode === null) child.kill();
  console.log('Isolated smoke-test data: ' + userData);
});
