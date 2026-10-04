'use strict';
// 离线验证脚本（不需要 electron，不需要 npm install，仅需本机 Node）:
//   在 app 目录下执行: node scripts/verify.js   （或 npm run verify）
// 检查项:
//   1. 所有 JS 文件语法检查 (node --check)
//   2. parser.js 对真实汇总 MD 的解析（键名/行数/脏数据）
//   3. scraper.js buildArgs 与原脚本 argparse 参数映射
//   4. 关键文件存在性（原脚本、login_check.py、浏览器配置目录）
const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const APP_DIR = path.resolve(__dirname, '..');
const SCRIPT_DIR = path.resolve(APP_DIR, '..');
let failures = 0;

function ok(msg) { console.log('  [PASS] ' + msg); }
function bad(msg) { console.error('  [FAIL] ' + msg); failures += 1; }
function check(cond, msg) { if (cond) ok(msg); else bad(msg); }

console.log('== 1. JS 语法检查 ==');
const jsFiles = [
  'main/index.js', 'main/config.js', 'main/parser.js', 'main/watcher.js',
  'main/scraper.js', 'main/loginchecker.js', 'preload.js', 'renderer/app.js',
];
for (const rel of jsFiles) {
  const f = path.join(APP_DIR, rel);
  if (!fs.existsSync(f)) { bad(`${rel} 不存在`); continue; }
  const r = spawnSync(process.execPath, ['--check', f], { encoding: 'utf-8' });
  check(r.status === 0, `${rel} 语法正确` + (r.status !== 0 ? ' -> ' + (r.stderr || '').trim() : ''));
}

console.log('== 2. parser.js 解析汇总 MD ==');
const mdPath = path.join(SCRIPT_DIR, 'douyin_全话题_去重昵称汇总.md');
const { parseSummaryMd } = require('../main/parser');
{
  // Fresh customer packages have no scan history; use an explicit test fixture.
  const fixture = [
    '- 生成时间: `2026-09-04 12:00:00`',
    '- 本次扫描内容数: `1`',
    '- 本次抓取评论总数: `1`',
    '- 本次筛出评论者数(按昵称去重): `1`',
    '- 全局去重后评论者数(按账号ID): `1`',
    '## 本次筛出明细',
    '| 昵称 | IP | 时间 | 账号链接 | 评论内容 |',
    '| --- | --- | --- | --- | --- |',
    '| 测试用户 | 北京 | 2026-09-04 11:00:00 | [主页](https://www.douyin.com/user/test) | 测试评论 |',
    '## 分次扫描结果',
    '### 批次 1',
    '| 昵称 | IP | 时间 | 账号链接 | 评论内容 | ID键 |',
    '| --- | --- | --- | --- | --- | --- |',
    '| 测试用户 | 北京 | 2026-09-04 11:00:00 | [主页](https://www.douyin.com/user/test) | 测试评论 | uid:test |',
  ].join('\n');
  const parsed = parseSummaryMd(fs.existsSync(mdPath) ? fs.readFileSync(mdPath, 'utf-8') : fixture);
  const meta = parsed.meta || {};
  const keys = ['生成时间', '本次扫描内容数', '本次抓取评论总数', '本次筛出评论者数(按昵称去重)', '全局去重后评论者数(按账号ID)'];
  for (const k of keys) check(k in meta, `meta 键存在: ${k}`);
  check(parsed.batches.length >= 1, `批次解析数 >= 1 (实际 ${parsed.batches.length})`);
  // 脏数据检查: 不应出现整行都是 --- 的分隔行数据
  const dirty = (parsed.current || []).filter((r) => /^-+$/.test(r.nickname || ''));
  check(dirty.length === 0, '无分隔行脏数据混入表格');
  const withProfile = (parsed.current || []).filter((r) => /^https:\/\//.test(r.profileUrl || ''));
  ok(`current 解析行数: ${(parsed.current || []).length}, 含主页链接: ${withProfile.length}`);
  const batchWithRecords = (parsed.batches || []).filter((b) => (b.records || []).length > 0);
  ok(`批次详情表解析: ${batchWithRecords.length} 个批次含明细记录`);

  const oldParsed = parseSummaryMd([
    '## 本次筛出明细',
    '| 昵称 | IP | 账号链接 | 评论内容 |',
    '|---|---|---|---|',
    '| 旧用户 | 北京 | [主页](https://www.douyin.com/user/old) | 旧评论 |',
  ].join('\n'));
  check(oldParsed.current[0].time === '', '兼容旧版无时间列 Markdown');

  const newParsed = parseSummaryMd([
    '## 本次筛出明细',
    '| 昵称 | IP | 时间 | 账号链接 | 评论内容 |',
    '|---|---|---|---|---|',
    '| 新用户 | 天津 | 2026-09-01 12:30:00 | [主页](https://www.douyin.com/user/new) | 新评论 |',
  ].join('\n'));
  check(newParsed.current[0].time === '2026-09-01 12:30:00', '解析新版时间列 Markdown');
}

console.log('== 3. scraper.js 参数映射对照 ==');
const { ScraperRunner, normalizeProfileUrl } = require('../main/scraper');
const sample = {
  topicUrl: 'https://www.douyin.com/search/妈妈?type=general',
  topicKeyword: '妈妈', ips: '北京,河北', sort: 'latest', maxVideos: 50,
  topicScrollRounds: 28, commentScrollRounds: 24,
  output: 'douyin_全话题_去重昵称汇总.md', storeFile: '.douyin_unique_commenters_store.jsonl',
  historyFile: '.douyin_scan_history.jsonl', profileDir: 'C:\\profiles\\edge',
  browserChannel: 'msedge', headless: false, pauseSeconds: 0,
  maxCommentAgeDays: 2, maxRunMinutes: 0, captchaWaitSeconds: 180,
  readyTimeoutSeconds: 90, stopFlagFile: '.douyin_stop',
};
const runner = new ScraperRunner({ scriptDir: SCRIPT_DIR, pythonPath: 'python' });
const args = runner.buildArgs(sample);
const expectedFlags = [
  '--topic-url', '--topic-keyword', '--ips', '--sort', '--max-videos',
  '--topic-scroll-rounds', '--comment-scroll-rounds', '--output', '--store-file',
  '--history-file', '--profile-dir', '--browser-channel', '--pause-seconds',
  '--max-comment-age-days', '--max-run-minutes', '--captcha-wait-seconds',
  '--ready-timeout-seconds', '--stop-flag-file',
  '--open-url-request-file',
];
for (const f of expectedFlags) check(args.includes(f), `参数存在: ${f}`);
check(!args.includes('--headless'), 'headless=false 时不加 --headless');
const hArgs = runner.buildArgs({ ...sample, headless: true });
check(hArgs.includes('--headless'), 'headless=true 时加 --headless');
check(args[args.indexOf('--max-videos') + 1] === '50', '--max-videos 值为 50');
check(args[args.indexOf('--ips') + 1] === '北京,河北', '--ips 值为 北京,河北');

const requestFile = path.join(require('os').tmpdir(), `douyin-open-url-${process.pid}.jsonl`);
runner.child = {};
runner.openUrlRequestFile = requestFile;
fs.writeFileSync(requestFile, '', 'utf-8');
const openOk = runner.openProfileUrl('https://www.douyin.com/user/valid_user');
check(openOk.ok, '合法抖音主页写入自动化标签请求');
const queued = JSON.parse(fs.readFileSync(requestFile, 'utf-8').trim());
check(queued.url === 'https://www.douyin.com/user/valid_user', '主页请求 URL 保持正确');
const openBad = runner.openProfileUrl('https://evil-douyin.com/user/nope');
check(!openBad.ok, '拒绝伪造抖音域名');
check(
  normalizeProfileUrl('https://www.douyin.com/user/ok') === 'https://www.douyin.com/user/ok',
  '主页 URL 白名单规范化正确'
);
runner.child = null;
runner.cleanupOpenUrlRequests();

console.log('== 4. 关键文件存在性 ==');
const must = [
  [path.join(SCRIPT_DIR, 'douyin_topic_comment_export.py'), '原抓取脚本'],
  [path.join(SCRIPT_DIR, 'login_check.py'), '登录检测助手'],
  [path.join(SCRIPT_DIR, '.edge_user_data_clone'), 'Edge 登录配置(目录)'],
];
for (const [p, name] of must) check(fs.existsSync(p), `${name}: ${p}`);

console.log('');
if (failures === 0) {
  console.log('全部通过 ✅');
  process.exit(0);
}
console.error(`共 ${failures} 项失败 ❌`);
process.exit(1);
