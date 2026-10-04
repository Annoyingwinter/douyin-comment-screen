'use strict';
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

function normalizeProfileUrl(url) {
  let parsed;
  try {
    parsed = new URL(String(url || '').trim());
  } catch (e) {
    return '';
  }
  const host = parsed.hostname.toLowerCase();
  const allowed = parsed.protocol === 'https:' &&
    (host === 'douyin.com' || host.endsWith('.douyin.com')) &&
    !parsed.username && !parsed.password && parsed.pathname.startsWith('/user/');
  return allowed ? parsed.toString() : '';
}

// 以子进程方式调用 douyin_topic_comment_export.py。
// 停止通过标记文件；主页通过任务专属 JSONL 队列交给现有 Playwright 上下文打开。
class ScraperRunner {
  constructor({ scriptDir, pythonPath, onLine, onStatus, onExit }) {
    this.scriptDir = scriptDir;
    this.pythonPath = pythonPath;
    this.onLine = onLine || (() => {});
    this.onStatus = onStatus || (() => {});
    this.onExit = onExit || (() => {});
    this.child = null;
    this.lastParams = null;
    this.stopping = false;
    this.startedAt = null;
    this.openUrlRequestFile = null;
  }

  get running() {
    return !!this.child;
  }

  buildArgs(p) {
    const args = [path.join(this.scriptDir, 'douyin_topic_comment_export.py')];
    if (p.mode === 'feed') args.push('--feed-mode');
    const push = (flag, value) => {
      if (value !== undefined && value !== null && String(value).trim() !== '') {
        args.push(flag, String(value));
      }
    };
    push('--topic-url', p.topicUrl);
    push('--topic-keyword', p.topicKeyword);
    push('--ips', p.ips);
    push('--sort', p.sort);
    push('--max-videos', p.maxVideos);
    push('--topic-scroll-rounds', p.topicScrollRounds);
    push('--comment-scroll-rounds', p.commentScrollRounds);
    push('--output', p.output);
    push('--store-file', p.storeFile);
    push('--history-file', p.historyFile);
    push('--profile-dir', p.profileDir);
    push('--browser-channel', p.browserChannel);
    push('--pause-seconds', p.pauseSeconds);
    push('--max-comment-age-days', p.maxCommentAgeDays);
    push('--max-run-minutes', p.maxRunMinutes);
    push('--captcha-wait-seconds', p.captchaWaitSeconds);
    push('--ready-timeout-seconds', p.readyTimeoutSeconds);
    push('--stop-flag-file', p.stopFlagFile);
    push('--open-url-request-file', p.openUrlRequestFile || '.douyin_open_url_requests.jsonl');
    if (p.headless) args.push('--headless');
    return args;
  }

  stopFlagPath(p) {
    const flag = (p && p.stopFlagFile) || '.douyin_stop';
    return path.isAbsolute(flag) ? flag : path.join(this.scriptDir, flag);
  }

  openUrlRequestPath(p) {
    const file = (p && p.openUrlRequestFile) || '.douyin_open_url_requests.jsonl';
    return path.isAbsolute(file) ? file : path.join(this.scriptDir, file);
  }

  start(params) {
    if (this.child) throw new Error('任务已在运行');
    const runParams = {
      ...params,
      openUrlRequestFile: `.douyin_open_url_requests_${process.pid}_${Date.now()}.jsonl`,
    };
    this.lastParams = runParams;
    this.stopping = false;
    this.startedAt = Date.now();
    this.openUrlRequestFile = this.openUrlRequestPath(runParams);
    fs.writeFileSync(this.openUrlRequestFile, '', 'utf-8');
    const args = this.buildArgs(runParams);
    const child = spawn(this.pythonPath, args, {
      cwd: this.scriptDir,
      env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' },
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    this.child = child;
    this.onStatus({ state: 'running', pid: child.pid, args });

    const pipe = (stream, kind) => {
      let buf = '';
      stream.on('data', (chunk) => {
        buf += chunk.toString('utf-8');
        const lines = buf.split(/\r?\n/);
        buf = lines.pop();
        for (const l of lines) {
          if (l) this.onLine({ kind, text: l, ts: Date.now() });
        }
      });
      stream.on('end', () => {
        if (buf) this.onLine({ kind, text: buf, ts: Date.now() });
      });
    };
    pipe(child.stdout, 'stdout');
    pipe(child.stderr, 'stderr');

    child.on('error', (err) => {
      this.onLine({ kind: 'stderr', text: `无法启动 Python: ${err.message}`, ts: Date.now() });
      this.onStatus({ state: 'error', message: err.message });
      this.onExit({ code: null, error: err.message, stoppedByUser: false });
      this.child = null;
      this.cleanupOpenUrlRequests();
    });

    child.on('exit', (code, signal) => {
      const wasStopping = this.stopping;
      this.child = null;
      this.onStatus({ state: 'stopped', code, signal, stoppedByUser: wasStopping });
      this.onExit({ code, signal, stoppedByUser: wasStopping });
      this.cleanupOpenUrlRequests();
    });
    return child.pid;
  }

  openProfileUrl(url) {
    if (!this.child || !this.openUrlRequestFile) {
      return { ok: false, error: '抓取任务未运行，无法复用自动化登录态' };
    }
    const normalizedUrl = normalizeProfileUrl(url);
    if (!normalizedUrl) return { ok: false, error: '只允许打开抖音用户主页链接' };

    const request = {
      url: normalizedUrl,
      requestedAt: new Date().toISOString(),
    };
    try {
      fs.appendFileSync(this.openUrlRequestFile, JSON.stringify(request) + '\n', 'utf-8');
      this.onLine({ kind: 'app', text: '已请求在自动化 Edge 新标签打开主页', ts: Date.now() });
      return { ok: true, mode: 'automation-tab' };
    } catch (e) {
      return { ok: false, error: `主页请求写入失败: ${e.message}` };
    }
  }

  cleanupOpenUrlRequests() {
    if (!this.openUrlRequestFile) return;
    try { fs.unlinkSync(this.openUrlRequestFile); } catch (e) { /* ignore */ }
    this.openUrlRequestFile = null;
  }

  // 优雅停止：写标记文件，原脚本会检测到并停止+导出部分结果；15 秒后仍未退出则强杀。
  stop() {
    if (!this.child) return false;
    this.stopping = true;
    const flagPath = this.stopFlagPath(this.lastParams);
    try {
      fs.writeFileSync(flagPath, 'stop', 'utf-8');
    } catch (e) {
      /* 目录不可写时仍尝试 kill */
    }
    this.onLine({ kind: 'app', text: `已写入停止标记 ${flagPath}，等待脚本优雅停止…`, ts: Date.now() });
    setTimeout(() => {
      if (this.child && this.stopping) {
        this.onLine({ kind: 'app', text: '脚本 15 秒内未退出，执行强制结束。', ts: Date.now() });
        try { this.child.kill(); } catch (e) { /* ignore */ }
      }
    }, 15000);
    return true;
  }

  kill() {
    if (this.child) {
      try { this.child.kill(); } catch (e) { /* ignore */ }
      this.child = null;
    }
    this.cleanupOpenUrlRequests();
  }
}

module.exports = { ScraperRunner, normalizeProfileUrl };
