'use strict';
const fs = require('fs');
const path = require('path');
const { parseSummaryMd } = require('./parser');

// 观察汇总 MD / 去重库 / 历史文件变化，解析后推送快照。
// 原脚本每抓完一个内容就会重写汇总 MD，因此这里能实现"实时表格"而不改动原脚本。
class DataWatcher {
  constructor() {
    this.watchers = [];
    this.timer = null;
    this.interval = null;
    this.targets = null;
    this.onSnapshot = () => {};
  }

  watch(targets) {
    this.stop();
    this.targets = targets;
    const files = [targets.mdPath, targets.storePath, targets.historyPath].filter(Boolean);
    for (const f of files) {
      try {
        const dir = path.dirname(f);
        if (!fs.existsSync(dir)) continue;
        const base = path.basename(f);
        const w = fs.watch(dir, { persistent: false }, (_eventType, filename) => {
          if (filename && path.basename(String(filename)) !== base) return;
          this._schedule();
        });
        this.watchers.push(w);
      } catch (e) {
        /* 某些目录不支持 watch，靠轮询兜底 */
      }
    }
    // 轮询兜底
    this.interval = setInterval(() => this._schedule(), 5000);
    this._schedule();
  }

  _schedule() {
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.readNow(), 400);
  }

  readNow() {
    const t = this.targets;
    if (!t) return null;
    let mdText = '';
    let mdExists = false;
    try {
      mdText = fs.readFileSync(t.mdPath, 'utf-8');
      mdExists = !!mdText;
    } catch (e) {
      mdExists = false;
    }
    const parsed = parseSummaryMd(mdText);

    let storeCount = 0;
    const timeByProfile = new Map();
    try {
      const storeLines = fs.readFileSync(t.storePath, 'utf-8').split(/\r?\n/).filter((l) => l.trim());
      storeCount = storeLines.length;
      for (const line of storeLines) {
        try {
          const row = JSON.parse(line);
          const profileUrl = String(row.user_home || '').trim();
          const createdAt = String(row.created_at || '').trim();
          if (profileUrl && createdAt && !timeByProfile.has(profileUrl)) {
            timeByProfile.set(profileUrl, createdAt);
          }
        } catch (e) { /* 忽略单行损坏，不影响其余结果 */ }
      }
    } catch (e) { /* 文件不存在 */ }

    // 兼容旧版 Markdown：旧表格没有时间列时，从不改动的 JSONL 去重库补齐。
    for (const row of parsed.current || []) {
      if (!row.time) row.time = timeByProfile.get(row.profileUrl) || '';
    }
    for (const batch of parsed.batches || []) {
      for (const row of batch.records || []) {
        if (!row.time) row.time = timeByProfile.get(row.profileUrl) || '';
      }
    }
    let historyCount = 0;
    try {
      historyCount = fs.readFileSync(t.historyPath, 'utf-8').split(/\r?\n/).filter((l) => l.trim()).length;
    } catch (e) { /* 文件不存在 */ }

    const snapshot = {
      mdExists,
      mdPath: t.mdPath,
      storeCount,
      historyCount,
      meta: parsed.meta,
      current: parsed.current,
      batches: parsed.batches,
      ts: Date.now(),
    };
    try { this.onSnapshot(snapshot); } catch (e) { /* ignore */ }
    return snapshot;
  }

  stop() {
    for (const w of this.watchers) {
      try { w.close(); } catch (e) { /* ignore */ }
    }
    this.watchers = [];
    clearInterval(this.interval);
    clearTimeout(this.timer);
  }
}

module.exports = { DataWatcher };
