'use strict';
const assert = require('assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const vm = require('vm');

const appDir = path.resolve(__dirname, '..');
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'douyin-package-test-'));
const userData = path.join(scratch, '用户数据');
const resources = path.join(scratch, 'resources');
const names = ['douyin_topic_comment_export.py', 'login_check.py', 'douyin_comment_link_demo.py'];
try {
  fs.mkdirSync(path.join(resources, 'scripts'), { recursive: true });
  for (const name of names) {
    fs.copyFileSync(path.join(appDir, '..', name), path.join(resources, 'scripts', name));
  }
  const sandbox = {
    module: { exports: {} },
    __dirname: path.join(resources, 'app.asar', 'main'),
    process: { resourcesPath: resources },
    require: (name) => name === 'electron'
      ? { app: { isPackaged: true, getPath: () => userData } }
      : require(name),
  };
  vm.runInNewContext(fs.readFileSync(path.join(appDir, 'main', 'config.js'), 'utf8'), sandbox);
  const { ConfigStore } = sandbox.module.exports;
  const store = new ConfigStore();
  const cfg = store.get();
  assert.equal(cfg.scriptDir, path.join(userData, 'runtime'));
  assert.ok(fs.statSync(cfg.params.profileDir).isDirectory());
  assert.equal(cfg.params.topicUrl, 'https://www.douyin.com/');
  assert.equal(cfg.params.topicKeyword, '');
  for (const name of names) assert.ok(fs.existsSync(path.join(cfg.scriptDir, name)));
  console.log('[PASS] Packaged first run: bundled scripts and writable profile');

  const history = path.join(cfg.scriptDir, '.douyin_scan_history.jsonl');
  const login = path.join(cfg.params.profileDir, 'test-login-marker');
  fs.writeFileSync(history, 'history preserved');
  fs.writeFileSync(login, 'login preserved');
  fs.writeFileSync(path.join(cfg.scriptDir, names[0]), 'old script');
  store.save({ params: { ips: '上海', topicUrl: 'https://www.douyin.com/search/old' } });
  const restarted = new ConfigStore().get();
  assert.equal(restarted.params.ips, '上海');
  assert.equal(restarted.params.topicUrl, 'https://www.douyin.com/');
  assert.equal(fs.readFileSync(history, 'utf8'), 'history preserved');
  assert.equal(fs.readFileSync(login, 'utf8'), 'login preserved');
  assert.equal(fs.readFileSync(path.join(cfg.scriptDir, names[0]), 'utf8'),
    fs.readFileSync(path.join(resources, 'scripts', names[0]), 'utf8'));
  console.log('[PASS] Restart/upgrade refreshes scripts and preserves data/config');

  store.save({ scriptDir: path.join(scratch, 'missing-old-machine-path') });
  assert.equal(new ConfigStore().get().scriptDir, cfg.scriptDir);
  const custom = path.join(scratch, 'custom');
  fs.mkdirSync(custom);
  fs.writeFileSync(path.join(custom, names[0]), 'custom script');
  store.save({ scriptDir: custom });
  assert.equal(new ConfigStore().get().scriptDir, custom);
  console.log('[PASS] Stale script path recovered; valid custom script path retained');

  const build = JSON.parse(fs.readFileSync(path.join(appDir, 'package.json'), 'utf8')).build;
  assert.deepEqual(build.extraResources[0].filter.slice().sort(), names.slice().sort());
  console.log('[PASS] Resource allowlist contains only the three Python scripts');
} finally {
  const parent = path.resolve(os.tmpdir());
  if (path.dirname(path.resolve(scratch)) === parent && path.basename(scratch).startsWith('douyin-package-test-')) {
    fs.rmSync(scratch, { recursive: true, force: true });
  }
}
