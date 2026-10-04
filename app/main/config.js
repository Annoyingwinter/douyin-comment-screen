'use strict';
const { app } = require('electron');
const fs = require('fs');
const path = require('path');

const SCRIPT_NAME = 'douyin_topic_comment_export.py';
const SCRIPT_FILES = [SCRIPT_NAME, 'login_check.py', 'douyin_comment_link_demo.py'];
const HOME_URL = 'https://www.douyin.com/';

function defaultParams() {
  return {
    mode: 'search',
    topicUrl: HOME_URL,
    topicKeyword: '',
    ips: '北京,河北',
    sort: 'latest',
    maxVideos: 50,
    topicScrollRounds: 28,
    commentScrollRounds: 24,
    output: 'douyin_全话题_去重昵称汇总.md',
    storeFile: '.douyin_unique_commenters_store.jsonl',
    historyFile: '.douyin_scan_history.jsonl',
    profileDir: '',
    browserChannel: 'msedge',
    headless: false,
    pauseSeconds: 0,
    maxCommentAgeDays: 2,
    maxRunMinutes: 0,
    captchaWaitSeconds: 180,
    readyTimeoutSeconds: 90,
    stopFlagFile: '.douyin_stop',
  };
}

class ConfigStore {
  constructor() {
    this.file = path.join(app.getPath('userData'), 'douyin-screen-config.json');
    this.data = this._load();
  }

  _load() {
    const base = { scriptDir: '', pythonPath: 'python', params: defaultParams() };
    try {
      if (fs.existsSync(this.file)) {
        const raw = JSON.parse(fs.readFileSync(this.file, 'utf-8'));
        base.scriptDir = (raw && raw.scriptDir) || '';
        base.pythonPath = (raw && raw.pythonPath) || 'python';
        base.params = { ...defaultParams(), ...((raw && raw.params) || {}) };
      }
    } catch (e) {
      /* corrupted config -> fall back to defaults */
    }
    // Desktop tasks always enter through the homepage, including saved old configs.
    base.params.topicUrl = HOME_URL;
    if (app.isPackaged) {
      // Python and its outputs need real, writable paths outside app.asar/Program Files.
      const runtimeDir = path.join(app.getPath('userData'), 'runtime');
      fs.mkdirSync(runtimeDir, { recursive: true });
      for (const name of SCRIPT_FILES) {
        fs.copyFileSync(path.join(process.resourcesPath, 'scripts', name), path.join(runtimeDir, name));
      }
      if (!base.scriptDir || !fs.existsSync(path.join(base.scriptDir, SCRIPT_NAME))) {
        base.scriptDir = runtimeDir;
      }
    } else if (!base.scriptDir) {
      const devDir = path.resolve(__dirname, '..', '..');
      if (fs.existsSync(path.join(devDir, SCRIPT_NAME))) {
        base.scriptDir = devDir; // dev mode: app/ 的上级
      }
    }
    if (!base.params.profileDir && base.scriptDir) {
      const edgeClone = path.join(base.scriptDir, '.edge_user_data_clone');
      const pwProfile = path.join(base.scriptDir, '.playwright-douyin-profile');
      if (fs.existsSync(edgeClone)) base.params.profileDir = edgeClone;
      else if (fs.existsSync(pwProfile)) base.params.profileDir = pwProfile;
      else base.params.profileDir = edgeClone;
    }
    if (base.params.profileDir) {
      const profilePath = path.resolve(base.scriptDir, base.params.profileDir);
      fs.mkdirSync(profilePath, { recursive: true });
    }
    return base;
  }

  save(partial) {
    const next = { ...this.data };
    if (typeof partial.scriptDir === 'string') next.scriptDir = partial.scriptDir;
    if (typeof partial.pythonPath === 'string') next.pythonPath = partial.pythonPath;
    if (partial.params && typeof partial.params === 'object') {
      next.params = { ...defaultParams(), ...this.data.params, ...partial.params };
    }
    next.params.topicUrl = HOME_URL;
    this.data = next;
    try {
      fs.mkdirSync(path.dirname(this.file), { recursive: true });
      fs.writeFileSync(this.file, JSON.stringify(this.data, null, 2), 'utf-8');
    } catch (e) {
      /* non-fatal */
    }
    return this.data;
  }

  get() {
    return this.data;
  }
}

module.exports = { ConfigStore, defaultParams, SCRIPT_NAME };
