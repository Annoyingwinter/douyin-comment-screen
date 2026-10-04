'use strict';
const { spawn } = require('child_process');
const path = require('path');

// 登录态检测（新增能力，不改原脚本）：
// 通过调用新增助手 login_check.py 完成。stdout 中以 "RESULT:" 前缀的 JSON 行为结构化状态。
// 模式:
//   check      -> 检测一次，返回 logged-in / logged-out
//   wait-login -> 未登录时尝试弹出登录窗，轮询等待扫码，返回 logged-in / waiting / timeout
class LoginChecker {
  constructor({ scriptDir, pythonPath, onLine, onStatus }) {
    this.scriptDir = scriptDir;
    this.pythonPath = pythonPath;
    this.onLine = onLine || (() => {});
    this.onStatus = onStatus || (() => {});
    this.child = null;
  }

  get running() {
    return !!this.child;
  }

  run({ mode, profileDir, browserChannel, timeoutSeconds, headless }) {
    return new Promise((resolve) => {
      if (this.child) {
        resolve({ ok: false, error: '登录检测进行中' });
        return;
      }
      const args = [
        path.join(this.scriptDir, 'login_check.py'),
        '--mode', mode || 'check',
        '--profile-dir', profileDir,
        '--browser-channel', browserChannel || 'msedge',
        '--timeout-seconds', String(timeoutSeconds || 300),
      ];
      if (headless) args.push('--headless');

      const child = spawn(this.pythonPath, args, {
        cwd: this.scriptDir,
        env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' },
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      this.child = child;
      let lastResult = null;
      this.onStatus({ state: 'checking', mode });

      const pipe = (stream, kind) => {
        let buf = '';
        stream.on('data', (chunk) => {
          buf += chunk.toString('utf-8');
          const lines = buf.split(/\r?\n/);
          buf = lines.pop();
          for (const l of lines) {
            if (!l) continue;
            if (l.startsWith('RESULT:')) {
              try { lastResult = JSON.parse(l.slice('RESULT:'.length)); } catch (e) { /* ignore */ }
              this.onStatus({ state: 'result', mode, result: lastResult });
            } else {
              this.onLine({ kind, text: l, ts: Date.now() });
            }
          }
        });
        stream.on('end', () => {
          if (!buf) return;
          if (buf.startsWith('RESULT:')) {
            try { lastResult = JSON.parse(buf.slice('RESULT:'.length)); } catch (e) { /* ignore */ }
            this.onStatus({ state: 'result', mode, result: lastResult });
          } else {
            this.onLine({ kind, text: buf, ts: Date.now() });
          }
        });
      };
      pipe(child.stdout, 'stdout');
      pipe(child.stderr, 'stderr');

      child.on('error', (err) => {
        this.child = null;
        this.onStatus({ state: 'error', message: err.message });
        resolve({ ok: false, error: err.message });
      });
      child.on('exit', (code) => {
        this.child = null;
        this.onStatus({ state: 'done', code, result: lastResult });
        const errMsg = lastResult && lastResult.message;
        // exit 0: check 完成(含未登录) 或 wait-login 登录成功; exit 2: wait-login 超时; exit 1: 异常
        resolve({
          ok: code === 0 || code === 2,
          code,
          result: lastResult,
          error: code === 1 ? (errMsg || `Python 退出码 ${code}`) : undefined,
        });
      });
    });
  }

  kill() {
    if (this.child) {
      try { this.child.kill(); } catch (e) { /* ignore */ }
      this.child = null;
    }
  }
}

module.exports = { LoginChecker };
