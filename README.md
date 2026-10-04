# 抖音评论筛查工具 (douyin-comment-screen)

[![GitHub Stars](https://img.shields.io/github/stars/Annoyingwinter/douyin-comment-screen?style=social)](https://github.com/Annoyingwinter/douyin-comment-screen/stargazers)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Windows%2010%2B-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Electron](https://img.shields.io/badge/electron-33-47848F)

基于 Electron + Python(Playwright) 的抖音评论采集与 IP 属地筛查桌面应用。开箱即用，扫码登录后一键运行。

两种抓取模式：

- **搜索模式**：输入关键词，程序自动搜索视频并逐个采集评论区；
- **推荐流模式**：不搜关键词，程序自动"刷"抖音首页的系统推荐视频流，逐个打开评论区采集。

采集到的评论按 **IP 属地**（如"北京,河北"）筛选，按昵称/账号 ID 去重，实时展示在界面表格中，
并汇总导出 Markdown、记录批次历史。评论数据来自抖音 `comment/list` 网络接口捕获，非页面 DOM 猜测。

> 仅供学习与技术研究，请在遵守抖音用户协议与当地法律法规的前提下使用；
> 登录需使用本人账号，请控制抓取频率，勿用于批量骚扰或商业爬取。

## 目录结构

```
douyin-comment-screen/
├── douyin_topic_comment_export.py   # 核心抓取脚本（搜索/推荐流两种模式，输出 MD）
├── login_check.py                   # 登录态检测助手（check / wait-login）
├── douyin_comment_link_demo.py      # 评论定位清单辅助脚本
├── 启动客户端.bat                    # 开发模式启动入口
├── 使用说明.txt                      # 最终用户简明手册
└── app/                             # Electron 桌面应用
    ├── main/                        # 主进程（任务调度、IPC、文件监听）
    ├── preload.js                   # 上下文桥（安全暴露 API）
    ├── renderer/                    # 界面（参数表单、控制台、结果表格、批次历史）
    └── scripts/                     # 离线自检脚本（node scripts/verify.js）
```

## 快速开始（最终用户）

1. 安装 Python 3.10+ 与 Playwright：`pip install playwright`
2. 运行 `dist/DouyinScreen-版本-x64-nsis.exe` 安装（或用 portable 版免安装）
3. 启动应用 → "检测登录" → 弹出浏览器扫码登录抖音
4. 选择抓取模式、填写 IP 属地 → "开始抓取"
5. 结果在"本次结果"页签查看，或点"打开结果MD"

## 开发模式

```bash
# 1. 准备 Python 依赖
pip install playwright

# 2. 安装 Electron 依赖（国内网络会走 .npmrc 里的 npmmirror 镜像）
cd app
npm install

# 3. 启动
cd ..
启动客户端.bat
# 或: cd app && npm start
```

首次运行需要扫码登录一次；登录态持久化在本地浏览器配置目录，之后免登录。

## 打包发布

```bash
cd app
npm run dist      # 输出 NSIS 安装包 + portable 版到 app/dist/
npm run verify    # 离线自检（语法 / MD 解析 / 参数映射 / 关键文件）
```

## 数据与隐私

所有运行数据都保存在本机 `%APPDATA%\douyin-screen-electron\`：

| 文件 | 内容 |
|---|---|
| `runtime\.edge_user_data_clone\` | 自动化浏览器登录态（含抖音 Cookie） |
| `runtime\.douyin_unique_commenters_store.jsonl` | 跨批次账号去重库 |
| `runtime\.douyin_scan_history.jsonl` | 批次历史 |
| `runtime\douyin_*.md` | 汇总结果 |
| `douyin-screen-config.json` | 应用配置 |

这些文件**永远不会**被 git 追踪（见 `.gitignore`）。本仓库不含任何登录凭据、扫描结果或个人路径。

## 稳定性设计

- **配置目录占用自愈**：启动浏览器前自动清理占用 `.edge_user_data_clone` 的残留
  Edge/Chrome 进程（Chromium 单例机制会导致 "Target page, context or browser has been closed"），
  启动失败再清理重试一次；
- **验证码人工兜底**：检测到滑块/验证码时暂停等待人工完成；
- **优雅停止**：写停止标记文件 → 导出部分结果 → 超时强杀；
- **进程隔离**：Electron 主进程只做调度，抓取全部在 Python 子进程中，崩溃不影响界面；
- **增量持久化**：每抓完一个视频立即落盘，中断不丢已采集数据。

## License

MIT
