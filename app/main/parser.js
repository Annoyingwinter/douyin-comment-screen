'use strict';

// 解析 douyin_topic_comment_export.py 生成的汇总 Markdown，结构与脚本 write_markdown 输出保持一致：
//   meta: 顶部 "- key: `value`" 元数据
//   current: "## 本次筛出明细" 表格 -> [{nickname, ip, profileUrl, comment}]
//   batches: "## 分次扫描结果" 下每个 "### 批次 N" -> {title, startTime, keyword, entry, scanned, filtered, dedupeKeyCount, records}
// 单元格中脚本对 "|" 做了 "\|" 转义，分隔符 " | " 安全。

function unescapeCell(s) {
  return String(s || '').replace(/\\\|/g, '|').replace(/\\\\/g, '\\').trim();
}

function splitRow(line) {
  const s = line.trim();
  if (!s.startsWith('|')) return null;
  const inner = s.slice(1, s.endsWith('|') ? -1 : s.length);
  return inner.split(' | ').map(unescapeCell);
}

function extractProfileUrl(cell) {
  const m = /\[主页\]\(([^)\s]+)\)/.exec(cell || '');
  return m ? m[1] : '';
}

const META_RE = /^-\s+(.+?):\s*`(.*)`\s*$/;

function parseSummaryMd(text) {
  const lines = String(text || '').split(/\r?\n/);
  const meta = {};
  const current = [];
  const batches = [];
  let headerSeen = false;
  let section = null;
  let curBatch = null;
  let inCurrentTable = false;
  let inBatchTable = false;
  let currentHasTime = false;
  let batchHasTime = false;

  for (const raw of lines) {
    const line = raw.trim();

    if (line.startsWith('## ')) {
      headerSeen = true;
      inCurrentTable = false;
      inBatchTable = false;
      if (line === '## 本次筛出明细') section = 'current';
      else if (line === '## 分次扫描结果') section = 'batches';
      else section = 'other';
      continue;
    }

    if (line === '---' || line === '') {
      inCurrentTable = false;
      inBatchTable = false;
      continue;
    }

    if (line.startsWith('### 批次')) {
      curBatch = { title: line.replace(/^###\s+/, ''), records: [] };
      batches.push(curBatch);
      inBatchTable = false;
      continue;
    }

    if (line.startsWith('|')) {
      const cells = splitRow(line);
      if (!cells) continue;
      if (cells.every((c) => /^-{2,}$/.test(c))) continue; // 跳过分隔行 |---|---|---|
      const key = cells.join('|');
      if (key === '昵称|IP|账号链接|评论内容') {
        inCurrentTable = true;
        inBatchTable = false;
        currentHasTime = false;
        continue;
      }
      if (key === '昵称|IP|时间|账号链接|评论内容') {
        inCurrentTable = true;
        inBatchTable = false;
        currentHasTime = true;
        continue;
      }
      if (key === '昵称|IP|账号链接|评论内容|ID键') {
        inBatchTable = true;
        inCurrentTable = false;
        batchHasTime = false;
        continue;
      }
      if (key === '昵称|IP|时间|账号链接|评论内容|ID键') {
        inBatchTable = true;
        inCurrentTable = false;
        batchHasTime = true;
        continue;
      }
      if (inCurrentTable && section === 'current' && cells.length >= (currentHasTime ? 5 : 4)) {
        current.push({
          nickname: cells[0],
          ip: cells[1],
          time: currentHasTime ? cells[2] : '',
          profileUrl: extractProfileUrl(cells[currentHasTime ? 3 : 2]),
          comment: cells[currentHasTime ? 4 : 3],
        });
      } else if (inBatchTable && curBatch && cells.length >= (batchHasTime ? 6 : 5)) {
        curBatch.records.push({
          nickname: cells[0],
          ip: cells[1],
          time: batchHasTime ? cells[2] : '',
          profileUrl: extractProfileUrl(cells[batchHasTime ? 3 : 2]),
          comment: cells[batchHasTime ? 4 : 3],
          dedupeKey: cells[batchHasTime ? 5 : 4].replace(/`/g, ''),
        });
      }
      continue;
    }

    const mm = META_RE.exec(line);
    if (mm) {
      const k = mm[1].trim();
      const v = mm[2];
      if (!headerSeen) {
        meta[k] = v;
      } else if (section === 'batches' && curBatch) {
        if (k === '开始时间') curBatch.startTime = v;
        else if (k === '关键词') curBatch.keyword = v;
        else if (k === '入口') curBatch.entry = v;
        else if (k === '扫描内容数') curBatch.scanned = v;
        else if (k === '本批新增去重人数') curBatch.filtered = v;
        else if (k === '去重ID键数量') curBatch.dedupeKeyCount = v;
      }
      continue;
    }
  }

  return { meta, current, batches };
}

module.exports = { parseSummaryMd };
