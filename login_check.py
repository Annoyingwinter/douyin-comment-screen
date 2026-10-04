"""抖音登录态检测助手（供 Electron 应用调用，不修改原抓取脚本 douyin_topic_comment_export.py）。

用法:
  python login_check.py --mode check      --profile-dir <dir> [--browser-channel msedge] [--timeout-seconds 60]
  python login_check.py --mode wait-login --profile-dir <dir> [--browser-channel msedge] [--timeout-seconds 300]

行为:
  check      -> 打开抖音首页检测一次登录态，立即返回 logged-in / logged-out
  wait-login -> 未登录时尝试弹出登录窗，轮询等待扫码，返回 logged-in / waiting / timeout

输出协议: 所有结构化状态以 "RESULT:<json>" 一行输出，其余行为普通日志。
退出码: 0=完成(check 完成或 wait-login 登录成功)；2=wait-login 超时；1=异常。
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import List

from playwright.sync_api import sync_playwright


def emit(payload: dict) -> None:
    print("RESULT:" + json.dumps(payload, ensure_ascii=False), flush=True)


def log(message: str) -> None:
    print(message, flush=True)


def _list_profile_browser_pids(profile_dir: str) -> List[int]:
    """枚举命令行引用了 profile_dir 的 Edge/Chrome 进程 PID（仅 Windows 有效）。"""
    if not profile_dir:
        return []
    ps_script = (
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' OR Name='chrome.exe' OR Name='chromium.exe'\" "
        "| Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains($env:DOUYIN_PROFILE_DIR.ToLower()) } "
        "| Select-Object -ExpandProperty ProcessId | ConvertTo-Json -Compress"
    )
    try:
        env = {**os.environ, "DOUYIN_PROFILE_DIR": profile_dir}
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=20,
        )
        raw = (result.stdout or "").strip()
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, list):
            return [int(x) for x in data if str(x).strip().isdigit()]
        if str(data).strip().isdigit():
            return [int(data)]
        return []
    except Exception:
        return []


def ensure_profile_free(profile_dir: str, wait_seconds: float = 12.0) -> List[int]:
    """结束占用浏览器配置目录的残留进程，返回清理过的 PID 列表。

    Chromium 系浏览器对 user-data-dir 做单例：目录已被占用时，新进程会把启动
    请求移交给旧进程后立即退出(exitCode=0)，Playwright 随即报
    "Target page, context or browser has been closed"。抓取结束或『打开主页』
    拉起的自动化浏览器经常残留，因此每次启动前先清理。只匹配命令行包含该
    配置目录路径的进程，不会影响用户自己日常使用的浏览器。
    """
    try:
        resolved = str(Path(profile_dir).resolve())
    except Exception:
        resolved = str(profile_dir or "")

    killed: List[int] = []
    deadline = time.monotonic() + wait_seconds
    while True:
        pids = _list_profile_browser_pids(resolved)
        if not pids:
            return killed
        for pid in pids:
            if pid not in killed:
                killed.append(pid)
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    timeout=10,
                )
            except Exception:
                pass
        if time.monotonic() >= deadline:
            remaining = _list_profile_browser_pids(resolved)
            if remaining:
                log(
                    "警告: 配置目录仍被浏览器进程占用 (PID: "
                    + ", ".join(str(x) for x in remaining)
                    + ")，请手动关闭对应窗口后重试"
                )
            return killed
        time.sleep(1.5)


def read_cookie_markers(context) -> dict:
    try:
        cookies = {c["name"]: c["value"] for c in context.cookies()}
    except Exception:
        cookies = {}
    return {
        "sessionid": bool(cookies.get("sessionid")),
        "passport_auth_status": cookies.get("passport_auth_status") == "1",
        "passport_csrf_token": bool(cookies.get("passport_csrf_token")),
    }


def read_dom_markers(page) -> dict:
    try:
        return page.evaluate(
            """
            () => {
              const text = document.body ? (document.body.innerText || '') : '';
              const nodes = Array.from(document.querySelectorAll('button,a,div,span'));
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
              };
              const headerLogin = nodes.some((el) => {
                if (!visible(el)) return false;
                const r = el.getBoundingClientRect();
                if (r.top > 160) return false;
                return (el.innerText || '').trim() === '登录';
              });
              const avatar = !!document.querySelector(
                'img[class*="avatar"], img[src*="avatar"], [data-e2e*="avatar"], [class*="Avatar"], [data-e2e*="Avatar"]'
              );
              return {
                headerLogin: headerLogin,
                avatar: avatar,
                hasUserMenu: text.includes('我的主页') || text.includes('创作者中心') || text.includes('退出登录'),
                bodyLen: text.length,
              };
            }
            """
        )
    except Exception:
        return {"headerLogin": False, "avatar": False, "hasUserMenu": False, "bodyLen": 0}


def judge(cookie_m: dict, dom_m: dict) -> bool:
    if cookie_m.get("sessionid") or dom_m.get("hasUserMenu"):
        return True
    if dom_m.get("avatar") and not dom_m.get("headerLogin") and dom_m.get("bodyLen", 0) > 200:
        return True
    return False


def open_home_and_wait(context, timeout_seconds: int = 45):
    page = context.pages[0] if context.pages else context.new_page()
    try:
        page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        log(f"打开抖音首页异常: {e}")
    for _ in range(max(3, int(timeout_seconds))):
        page.wait_for_timeout(1000)
        dom = read_dom_markers(page)
        if dom.get("bodyLen", 0) > 200:
            break
    return page


def click_login_button(page) -> bool:
    try:
        return bool(
            page.evaluate(
                """
                () => {
                  const nodes = Array.from(document.querySelectorAll('button,a,div,span'));
                  const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                  };
                  for (const el of nodes) {
                    if (!visible(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.top > 160) continue;
                    if ((el.innerText || '').trim() === '登录') {
                      el.click();
                      return true;
                    }
                  }
                  return false;
                }
                """
            )
        )
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="抖音登录态检测助手")
    parser.add_argument("--mode", choices=["check", "wait-login"], required=True)
    parser.add_argument("--profile-dir", required=True)
    parser.add_argument("--browser-channel", choices=["chromium", "msedge", "chrome"], default="msedge")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    deadline = time.monotonic() + max(30, args.timeout_seconds)
    last_emit = 0.0

    try:
        with sync_playwright() as p:
            log(f"启动浏览器(通道={args.browser_channel}, 配置={args.profile_dir})...")
            killed = ensure_profile_free(args.profile_dir)
            if killed:
                log(f"已清理占用配置目录的残留浏览器进程: {sorted(set(killed))}")
            launch_kwargs = dict(
                user_data_dir=args.profile_dir,
                headless=args.headless,
                channel=None if args.browser_channel == "chromium" else args.browser_channel,
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1440, "height": 900},
            )
            try:
                context = p.chromium.launch_persistent_context(**launch_kwargs)
            except Exception as launch_err:
                log(f"浏览器启动失败({launch_err})，再次清理残留进程后重试一次…")
                ensure_profile_free(args.profile_dir)
                context = p.chromium.launch_persistent_context(**launch_kwargs)
            try:
                page = open_home_and_wait(context)
                cookie_m = read_cookie_markers(context)
                dom_m = read_dom_markers(page)
                logged = judge(cookie_m, dom_m)
                emit(
                    {
                        "state": "logged-in" if logged else "logged-out",
                        "mode": args.mode,
                        "cookies": cookie_m,
                        "dom": dom_m,
                    }
                )

                if logged:
                    log("已处于登录状态。")
                    return 0

                if args.mode == "check":
                    log("未检测到登录态。可点击『扫码登录』在浏览器窗口中完成登录。")
                    return 0

                # wait-login 模式: 尝试打开登录弹窗并轮询
                log("尝试打开登录弹窗，请在浏览器窗口中扫码…")
                if not click_login_button(page):
                    log("未找到『登录』按钮，将直接轮询登录态；也可在浏览器窗口手动登录。")
                page.wait_for_timeout(1500)

                while time.monotonic() < deadline:
                    try:
                        cookie_m = read_cookie_markers(context)
                        dom_m = read_dom_markers(page)
                    except Exception:
                        cookie_m, dom_m = {}, {}
                    if judge(cookie_m, dom_m):
                        emit(
                            {
                                "state": "logged-in",
                                "mode": args.mode,
                                "cookies": cookie_m,
                                "dom": dom_m,
                            }
                        )
                        log("登录成功。")
                        return 0
                    now = time.monotonic()
                    if now - last_emit >= 3.0:
                        last_emit = now
                        emit(
                            {
                                "state": "waiting",
                                "remaining_seconds": int(deadline - now),
                                "sessionid": bool(cookie_m.get("sessionid")),
                            }
                        )
                    page.wait_for_timeout(1200)

                emit({"state": "timeout", "mode": args.mode, "remaining_seconds": 0})
                log("等待登录超时。")
                return 2
            finally:
                try:
                    context.close()
                except Exception:
                    pass
    except Exception as e:
        emit({"state": "error", "message": str(e)})
        log(f"异常: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
