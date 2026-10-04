import argparse
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse

from playwright.sync_api import BrowserContext, Page, sync_playwright


CONTENT_PATH_RE = re.compile(r"^/(video|note)/(?P<id>[0-9A-Za-z_-]{8,40})/?$")
USER_HOME_PATH_RE = re.compile(r"/user/(?P<id>[^/?#]+)")

_RUNTIME_CONTEXT: Optional[BrowserContext] = None
_OPEN_URL_REQUEST_PATH: Optional[Path] = None
_OPEN_URL_REQUEST_OFFSET = 0
_MANUAL_PROFILE_PAGES: List[Page] = []


@dataclass
class CommentRecord:
    topic_url: str
    video_url: str
    aweme_id: str
    comment_id: str
    ip_location: str
    nickname: str
    uid: str
    sec_uid: str
    unique_id: str
    user_home: str
    text: str
    created_at: str
    created_unix: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scrape comments from videos under a Douyin hashtag page, "
            "filter by IP location, and export to one Markdown document."
        )
    )
    parser.add_argument(
        "--topic-url",
        default="https://www.douyin.com/",
        help="Douyin entry URL; defaults to the homepage and searches via the page UI.",
    )
    parser.add_argument(
        "--topic-keyword",
        default="",
        help="Search keyword entered by the user. Required for homepage entry.",
    )
    parser.add_argument(
        "--feed-mode",
        action="store_true",
        help=(
            "Skip keyword search: browse the homepage recommend feed, open each "
            "recommended video's comment panel, and collect commenters by IP location."
        ),
    )
    parser.add_argument(
        "--ips",
        default="北京,河北",
        help="Comma-separated IP locations to keep, e.g. 北京,河北",
    )
    parser.add_argument(
        "--sort",
        choices=["latest", "default"],
        default="latest",
        help="Sort mode on hashtag page before collection",
    )
    parser.add_argument("--max-videos", type=int, default=50, help="Max number of videos to scan")
    parser.add_argument(
        "--topic-scroll-rounds",
        type=int,
        default=28,
        help="Scroll rounds on hashtag page to collect video URLs",
    )
    parser.add_argument(
        "--comment-scroll-rounds",
        type=int,
        default=24,
        help="Scroll rounds in each video page to trigger more comments",
    )
    parser.add_argument(
        "--output",
        default="douyin_全话题_去重昵称汇总.md",
        help="Unified Markdown output file (all topics merged)",
    )
    parser.add_argument(
        "--store-file",
        default=".douyin_unique_commenters_store.jsonl",
        help="Local JSONL store for cross-run/global account-ID dedupe",
    )
    parser.add_argument(
        "--history-file",
        default=".douyin_scan_history.jsonl",
        help="Local JSONL run history; each run is separated into one section in output",
    )
    parser.add_argument(
        "--profile-dir",
        default=".playwright-douyin-profile",
        help="Persistent browser profile dir for login reuse",
    )
    parser.add_argument(
        "--browser-channel",
        choices=["chromium", "msedge", "chrome"],
        default="chromium",
        help="Browser channel for Playwright persistent context",
    )
    parser.add_argument("--headless", action="store_true", help="Run in headless mode")
    parser.add_argument(
        "--pause-seconds",
        type=int,
        default=0,
        help="Wait time after opening topic page. 0 means wait for Enter in terminal.",
    )
    parser.add_argument(
        "--max-comment-age-days",
        type=int,
        default=2,
        help="Keep only comments within N days. Set <=0 to disable this time filter.",
    )
    parser.add_argument(
        "--max-run-minutes",
        type=int,
        default=0,
        help="Stop scraping after N minutes and export partial results. Set <=0 to disable.",
    )
    parser.add_argument(
        "--captcha-wait-seconds",
        type=int,
        default=180,
        help="When captcha/safety check appears, wait up to N seconds for manual completion.",
    )
    parser.add_argument(
        "--ready-timeout-seconds",
        type=int,
        default=90,
        help="Wait up to N seconds for search page content to become ready.",
    )
    parser.add_argument(
        "--stop-flag-file",
        default=".douyin_stop",
        help="Create this file while running to request a graceful manual stop",
    )
    parser.add_argument(
        "--open-url-request-file",
        default=".douyin_open_url_requests.jsonl",
        help="JSONL request queue used by the Electron app to open profile tabs",
    )
    return parser.parse_args()


def normalize_topic_url(url: str) -> str:
    value = (url or "").strip()
    if not value.startswith("http://") and not value.startswith("https://"):
        raise ValueError("--topic-url must start with http:// or https://")
    return value


def validate_topic_domain(url: str) -> None:
    host = urlparse(url).netloc.lower()
    if "douyin.com" not in host:
        raise ValueError("--topic-url must be a douyin.com URL")


def extract_topic_keyword_from_url(url: str) -> str:
    path = unquote(urlparse(url).path or "")
    if "/hashtag/" not in path:
        return ""
    token = path.split("/hashtag/", 1)[1].strip("/")
    return token


def extract_search_keyword_from_url(url: str) -> str:
    path = unquote(urlparse(url).path or "")
    if "/search/" not in path:
        return ""
    token = path.split("/search/", 1)[1].strip("/")
    return token


def normalize_ip_label(value: Any) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    s = s.replace("IP属地", "").replace("ip属地", "")
    s = s.replace("：", ":")
    s = s.strip(" :")
    if s.startswith("中国"):
        s = s.replace("中国", "", 1).strip(" -")
    return s


def parse_target_ips(raw: str) -> Set[str]:
    items = []
    for part in raw.split(","):
        clean = normalize_ip_label(part)
        if clean:
            items.append(clean)
    return set(items)


def sanitize_text(value: Any) -> str:
    text = str(value or "")
    return " ".join(text.replace("\r", " ").replace("\n", " ").split())


def parse_unix_timestamp(raw: Any) -> int:
    try:
        v = int(raw)
    except Exception:
        return 0

    if v > 10_000_000_000:
        v = int(v / 1000)
    return v


def format_timestamp(unix_ts: int) -> str:
    if unix_ts <= 0:
        return ""

    try:
        return datetime.fromtimestamp(unix_ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def parse_timestamp(raw: Any) -> str:
    return format_timestamp(parse_unix_timestamp(raw))


def extract_aweme_id(video_url: str) -> str:
    path = unquote(urlparse((video_url or "").strip()).path or "")
    m = CONTENT_PATH_RE.match(path)
    if not m:
        return ""
    return m.group("id")


def to_absolute_douyin_url(href: str) -> str:
    href = (href or "").strip()
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return "https://www.douyin.com" + href
    return href


def normalize_content_url(href: str) -> str:
    abs_url = to_absolute_douyin_url(href)
    path = unquote(urlparse(abs_url).path or "")
    m = CONTENT_PATH_RE.match(path)
    if not m:
        return ""
    content_type = m.group(1)
    content_id = m.group("id")
    return f"https://www.douyin.com/{content_type}/{content_id}"


def extract_content_url_from_page_url(url: str) -> str:
    u = (url or "").strip()
    normalized = normalize_content_url(u)
    if normalized:
        return normalized

    parsed = urlparse(u)
    query = parse_qs(parsed.query or "")
    modal_ids = query.get("modal_id") or query.get("item_id") or []
    if modal_ids:
        mid = str(modal_ids[0]).strip()
        if mid:
            return f"https://www.douyin.com/video/{mid}"
    return ""


def is_live_href(href: str) -> bool:
    h = (href or "").strip().lower()
    if not h:
        return False
    markers = [
        "/live/",
        "live.douyin.com",
        "room_id=",
        "enter_from=live",
        "type=live",
        "webcast",
    ]
    return any(m in h for m in markers)


def is_live_content_page(page: Page) -> bool:
    url = (page.url or "").lower()
    live_url_markers = [
        "/live/",
        "live.douyin.com",
        "room_id=",
        "enter_from=live",
        "type=live",
    ]
    if any(m in url for m in live_url_markers):
        return True

    try:
        return bool(
            page.evaluate(
                """
                () => {
                  const text = document.body ? (document.body.innerText || '') : '';
                  if (!text) return false;
                  const liveMarks = ['直播中', '正在直播', '直播间', '进入直播间', '开播'];
                  const hasLive = liveMarks.some((m) => text.includes(m));
                  const hasCommentSignals = text.includes('评论') || text.includes('条评论');
                  return hasLive && !hasCommentSignals;
                }
                """
            )
        )
    except Exception:
        return False


def build_fallback_video_url(page: Page, index_hint: int = 0) -> str:
    current = extract_content_url_from_page_url(page.url)
    if current:
        return current
    stamp = int(time.time() * 1000)
    if index_hint > 0:
        return f"https://www.douyin.com/video/unknown_{index_hint}_{stamp}"
    return f"https://www.douyin.com/video/unknown_{stamp}"


def extract_content_urls_from_payload(payload: Any) -> Set[str]:
    found: Set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            aweme_id = node.get("aweme_id")
            if aweme_id is not None:
                aid = str(aweme_id).strip()
                if aid:
                    found.add(f"https://www.douyin.com/video/{aid}")

            for value in node.values():
                if isinstance(value, str):
                    if "/video/" in value or "/note/" in value:
                        u = normalize_content_url(value)
                        if u:
                            found.add(u)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


def wait_for_manual_ready(pause_seconds: int) -> None:
    if pause_seconds > 0:
        print(f"Waiting {pause_seconds}s: complete login on Douyin...")
        from time import sleep

        sleep(pause_seconds)
        return
    # Non-blocking by default: avoid requiring terminal input that may be unavailable.
    print("No fixed wait. Auto-detecting page readiness...")


def is_deadline_reached(deadline: Optional[float]) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _is_allowed_profile_url(url: str) -> bool:
    try:
        parsed = urlparse((url or "").strip())
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and (host == "douyin.com" or host.endswith(".douyin.com"))
        and parsed.username is None
        and parsed.password is None
        and parsed.path.startswith("/user/")
    )


def _automation_pages(context: BrowserContext) -> List[Page]:
    global _MANUAL_PROFILE_PAGES
    alive_manual: List[Page] = []
    for manual_page in _MANUAL_PROFILE_PAGES:
        try:
            if not manual_page.is_closed():
                alive_manual.append(manual_page)
        except Exception:
            continue
    _MANUAL_PROFILE_PAGES = alive_manual
    return [
        page
        for page in list(context.pages)
        if not any(page is manual_page for manual_page in alive_manual)
    ]


def configure_open_url_requests(
    context: Optional[BrowserContext], request_path: Optional[Path]
) -> None:
    global _RUNTIME_CONTEXT, _OPEN_URL_REQUEST_PATH, _OPEN_URL_REQUEST_OFFSET
    global _MANUAL_PROFILE_PAGES
    _RUNTIME_CONTEXT = context
    _OPEN_URL_REQUEST_PATH = request_path
    _OPEN_URL_REQUEST_OFFSET = 0
    _MANUAL_PROFILE_PAGES = []


def process_open_url_requests() -> None:
    global _OPEN_URL_REQUEST_OFFSET
    context = _RUNTIME_CONTEXT
    request_path = _OPEN_URL_REQUEST_PATH
    if context is None or request_path is None or not request_path.exists():
        return

    try:
        size = request_path.stat().st_size
        if size < _OPEN_URL_REQUEST_OFFSET:
            _OPEN_URL_REQUEST_OFFSET = 0
        with request_path.open("rb") as f:
            f.seek(_OPEN_URL_REQUEST_OFFSET)
            payload = f.read()
            _OPEN_URL_REQUEST_OFFSET = f.tell()
    except Exception as exc:
        print(f"Profile tab request read failed: {exc}")
        return

    for raw_line in payload.splitlines():
        try:
            request = json.loads(raw_line.decode("utf-8"))
            url = str(request.get("url") or "").strip()
        except Exception:
            print("Ignored malformed profile tab request.")
            continue
        if not _is_allowed_profile_url(url):
            print("Ignored unsafe profile tab URL.")
            continue

        manual_page: Optional[Page] = None
        try:
            manual_page = context.new_page()
            _MANUAL_PROFILE_PAGES.append(manual_page)
            manual_page.on("popup", lambda popup: _MANUAL_PROFILE_PAGES.append(popup))
            manual_page.goto(url, wait_until="commit", timeout=15_000)
            manual_page.bring_to_front()
            print(f"Opened profile in automation browser tab: {url}")
        except Exception as exc:
            print(f"Failed to open profile in automation browser tab: {exc}")


def is_manual_stop_requested(stop_flag_path: Optional[Path]) -> bool:
    if stop_flag_path is None:
        return False
    try:
        return stop_flag_path.exists()
    except Exception:
        return False


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
                print(
                    "Warning: profile dir still held by browser processes (PID: "
                    + ", ".join(str(x) for x in remaining)
                    + "); close those windows manually and retry"
                )
            return killed
        time.sleep(1.5)


def get_stop_reason(deadline: Optional[float], stop_flag_path: Optional[Path]) -> str:
    process_open_url_requests()
    if is_manual_stop_requested(stop_flag_path):
        return f"manual stop flag detected: {stop_flag_path}"
    if is_deadline_reached(deadline):
        return "time limit reached"
    return ""


def is_captcha_page(page: Page) -> bool:
    try:
        text = page.evaluate("document.body ? document.body.innerText : ''") or ""
    except Exception:
        text = ""
    markers = [
        "验证码",
        "安全验证",
        "请完成验证",
        "滑块",
        "行为验证",
        "风险提示",
    ]
    if any(m in text for m in markers):
        return True
    u = (page.url or "").lower()
    if "captcha" in u or "verify" in u:
        return True
    return False


def wait_for_captcha_clear(
    page: Page,
    max_wait_seconds: int,
    stop_flag_path: Optional[Path] = None,
) -> bool:
    if not is_captcha_page(page):
        return True
    print(f"Captcha detected. Please solve it manually within {max_wait_seconds}s...")
    end_at = time.monotonic() + max_wait_seconds
    while time.monotonic() < end_at:
        if is_manual_stop_requested(stop_flag_path):
            print("Manual stop flag detected while waiting captcha.")
            return False
        page.wait_for_timeout(2000)
        if not is_captcha_page(page):
            print("Captcha cleared, continue.")
            return True
    print("Captcha not cleared in time. Stop current run safely.")
    return False


def is_search_page_ready(page: Page) -> bool:
    try:
        info = page.evaluate(
            """
            () => {
              const text = document.body ? (document.body.innerText || '') : '';
              const hasTabs = text.includes('综合') && text.includes('视频');
              const hasSearchInput = !!document.querySelector('[data-e2e="searchbar-input"],input[type="search"],input[placeholder*="搜索"]');
              const bodyLen = text.length;
              return { hasTabs, hasSearchInput, bodyLen };
            }
            """
        )
        if not isinstance(info, dict):
            return False
        # Single-column search has no “图文/相关搜索” labels; those are not readiness signals.
        return bool(info.get("hasTabs")) and bool(info.get("hasSearchInput")) and int(info.get("bodyLen", 0)) > 80
    except Exception:
        return False


def wait_for_search_page_ready(page: Page, timeout_seconds: int) -> bool:
    end_at = time.monotonic() + max(1, timeout_seconds)
    while time.monotonic() < end_at:
        if is_search_page_ready(page):
            return True
        page.wait_for_timeout(1200)
    return False


def close_extra_blank_pages(context: BrowserContext, keep_page: Page) -> None:
    for p in _automation_pages(context):
        if p is keep_page:
            continue
        try:
            u = (p.url or "").strip().lower()
            if u in {"", "about:blank", "edge://newtab/", "edge://newtab"}:
                p.close()
        except Exception:
            continue


def pick_primary_page(context: BrowserContext) -> Optional[Page]:
    pages = _automation_pages(context)
    if not pages:
        return None

    def rank(page: Page) -> int:
        url = (page.url or "").lower()
        if "douyin.com/search" in url:
            return 0
        if "douyin.com" in url and "about:blank" not in url:
            return 1
        if url in {"", "about:blank", "edge://newtab", "edge://newtab/"}:
            return 9
        return 5

    pages.sort(key=rank)
    return pages[0]


def recover_topic_page(context: BrowserContext, page: Optional[Page], entry_url: str) -> Page:
    current = page
    if current is None or current.is_closed():
        current = pick_primary_page(context)
        if current is None:
            current = context.new_page()

    url = (current.url or "").strip().lower()
    if url in {"", "about:blank", "edge://newtab", "edge://newtab/"}:
        candidate = pick_primary_page(context)
        if candidate is not None and candidate is not current:
            current = candidate

    url = (current.url or "").strip().lower()
    if url in {"", "about:blank", "edge://newtab", "edge://newtab/"}:
        current.goto(entry_url, wait_until="domcontentloaded", timeout=60_000)

    try:
        current.bring_to_front()
    except Exception:
        pass
    close_extra_blank_pages(context, current)
    return current


def _try_exit_live_room_ui(page: Page) -> bool:
    if not is_live_content_page(page):
        return True

    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(360)
    except Exception:
        pass
    if not is_live_content_page(page):
        return True

    try_click_first(
        page,
        [
            "[aria-label*='关闭']",
            "[aria-label*='返回']",
            "button:has-text('关闭')",
            "button:has-text('退出')",
            "a:has-text('返回')",
            "[data-e2e*='close']",
            "[data-e2e*='back']",
            "text=关闭",
            "text=返回",
            "text=退出",
        ],
        "live exit",
    )
    for _ in range(3):
        page.wait_for_timeout(260)
        if not is_live_content_page(page):
            return True

    try:
        page.go_back(wait_until="domcontentloaded", timeout=12_000)
        page.wait_for_timeout(700)
    except Exception:
        pass
    return not is_live_content_page(page)


def recover_search_page_from_live(
    context: BrowserContext,
    page: Page,
    entry_url: str,
    topic_keyword: str,
    ready_timeout_seconds: int,
) -> Page:
    current = page
    if not is_live_content_page(current):
        return current

    print("Live room detected, trying to exit and return to search page...")
    for _ in range(3):
        if _try_exit_live_room_ui(current):
            break

    if is_live_content_page(current):
        if len(_automation_pages(context)) > 1:
            try:
                current.close()
            except Exception:
                pass
            replacement = pick_primary_page(context)
            if replacement is None:
                replacement = context.new_page()
            current = replacement
        else:
            try:
                current.goto(entry_url, wait_until="domcontentloaded", timeout=60_000)
            except Exception:
                pass

    current = recover_topic_page(context, current, entry_url)
    current_path = unquote(urlparse(current.url).path or "")
    if "/search/" not in current_path:
        current.goto(entry_url, wait_until="domcontentloaded", timeout=60_000)
        current = recover_topic_page(context, current, entry_url)

    if topic_keyword and not ensure_search_keyword_on_page(current, topic_keyword):
        raise RuntimeError("Failed to restore the requested search keyword.")
    if wait_for_search_page_ready(current, max(12, ready_timeout_seconds)):
        prepare_search_video_page(current)
        wait_for_search_cards_ready(current, timeout_seconds=max(12, ready_timeout_seconds))
    return current


def ensure_topic_page_valid(topic_page: Page, expected_keyword: str) -> None:
    expected_keyword = (expected_keyword or "").strip()
    invalid_markers = [
        "你要观看的话题不存在",
        "话题不存在",
        "内容不存在",
    ]

    for _ in range(6):
        current_url = topic_page.url
        current_path = unquote(urlparse(current_url).path or "")

        body_text = ""
        try:
            body_text = topic_page.evaluate("document.body ? document.body.innerText : ''") or ""
        except Exception:
            body_text = ""

        for marker in invalid_markers:
            if marker in body_text:
                raise RuntimeError(f"Topic page invalid: {marker}. Please provide a valid hashtag URL.")

        if "/hashtag/" not in current_path:
            raise RuntimeError(f"Current page is not a hashtag page: {current_url}")

        if expected_keyword:
            if (
                expected_keyword in current_path
                or expected_keyword in body_text
                or f"#{expected_keyword}" in body_text
            ):
                return
        else:
            return

        topic_page.wait_for_timeout(1200)

    raise RuntimeError(
        "Unable to confirm expected hashtag on page. "
        "Please open the exact hashtag URL in browser and rerun."
    )


def switch_to_topic_page_from_search(topic_page: Page, topic_keyword: str) -> str:
    topic_keyword = (topic_keyword or "").strip()
    if not topic_keyword:
        raise RuntimeError("topic keyword is empty, cannot resolve hashtag from search page.")

    try_click_first(
        topic_page,
        [
            "[role='tab']:has-text('话题')",
            "button:has-text('话题')",
            "a:has-text('话题')",
            "text=话题",
        ],
        "search topic tab",
    )
    topic_page.wait_for_timeout(1200)

    hashtag_href = topic_page.evaluate(
        """
        (kw) => {
          const nodes = Array.from(document.querySelectorAll('a[href*="/hashtag/"]'));
          const visible = (el) => {
            const r = el.getBoundingClientRect();
            const s = window.getComputedStyle(el);
            return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
          };
          const exact = nodes.find((n) => visible(n) && (n.innerText || '').replace(/^#/, '').trim() === kw);
          if (exact) return exact.getAttribute('href') || '';
          const fuzzy = nodes.find((n) => visible(n) && (n.innerText || '').includes(kw));
          if (fuzzy) return fuzzy.getAttribute('href') || '';
          return '';
        }
        """,
        topic_keyword,
    )

    if isinstance(hashtag_href, str) and hashtag_href.strip():
        target_url = to_absolute_douyin_url(hashtag_href)
        topic_page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
        topic_page.wait_for_timeout(1200)
        return topic_page.url

    # Fallback: attempt direct hashtag URL constructed from keyword.
    target_url = f"https://www.douyin.com/hashtag/{quote(topic_keyword)}"
    topic_page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
    topic_page.wait_for_timeout(1200)
    return topic_page.url


def prepare_search_video_page(search_page: Page) -> None:
    # Jingxuan search may remember single-column playback. Collection needs result cards.
    try:
        multi_column = search_page.get_by_text("多列", exact=True)
        if multi_column.count():
            multi_column.first.click(timeout=3000, no_wait_after=True)
            search_page.wait_for_timeout(1200)
    except Exception:
        pass
    # Search mode: keep on 综合 feed (do not force 视频 tab).
    try_click_first(
        search_page,
        [
            "[role='tab']:has-text('综合')",
            "button:has-text('综合')",
            "a:has-text('综合')",
            "text=综合",
        ],
        "search general tab",
    )
    search_page.wait_for_timeout(1200)

    try_click_first(
        search_page,
        [
            "[role='tab']:has-text('最新发布')",
            "[role='tab']:has-text('最新')",
            "button:has-text('最新发布')",
            "button:has-text('最新')",
            "a:has-text('最新发布')",
            "a:has-text('最新')",
            "text=最新发布",
            "text=最新",
        ],
        "search latest sort",
    )
    search_page.wait_for_timeout(1200)


def ensure_search_keyword_on_page(search_page: Page, keyword: str) -> bool:
    keyword = (keyword or "").strip()
    if not keyword:
        return False

    def matching_results() -> bool:
        return extract_search_keyword_from_url(search_page.url) == keyword

    if matching_results():
        return True

    # Only target search controls; never type into an arbitrary input (e.g. login).
    selectors = [
        "input[type='search']:visible",
        "input[placeholder*='搜索']:visible",
        "input[aria-label*='搜索']:visible",
        "input[data-e2e='searchbar-input']:visible",
    ]
    field = None
    for attempt in range(12):
        for selector in selectors:
            loc = search_page.locator(selector)
            if loc.count():
                field = loc.first
                break
        if field is not None:
            break
        if attempt in {0, 4, 8}:
            for selector in ["button:has-text('搜索'):visible", "[role='button'][aria-label='搜索']:visible", "a:has-text('搜索'):visible"]:
                try:
                    control = search_page.locator(selector)
                    if control.count():
                        control.first.click(timeout=2000)
                        break
                except Exception:
                    continue
        search_page.wait_for_timeout(500)
    if field is None:
        return False

    # The homepage can render its input before its search handler is hydrated.
    for _ in range(2):
        try:
            field.click(timeout=3000)
            field.fill(keyword, timeout=3000)
            field.press("Enter", timeout=3000)
            # A matching input alone does not prove the search was submitted.
            search_page.wait_for_url(
                lambda url: extract_search_keyword_from_url(str(url)) == keyword,
                timeout=15000,
                wait_until="domcontentloaded",
            )
            return matching_results()
        except Exception:
            search_page.wait_for_timeout(1000)
    return False


def try_click_first(page: Page, selectors: List[str], label: str) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() == 0:
                continue
            loc.first.click(timeout=2500)
            page.wait_for_timeout(1200)
            print(f"Clicked {label}: {sel}")
            return True
        except Exception:
            continue
    return False


def select_latest_sort(topic_page: Page) -> bool:
    selectors = [
        "[role='tab']:has-text('最新发布')",
        "[role='tab']:has-text('最新')",
        "button:has-text('最新发布')",
        "button:has-text('最新')",
        "a:has-text('最新发布')",
        "a:has-text('最新')",
        "text=最新发布",
        "text=最新",
    ]
    clicked = try_click_first(topic_page, selectors, "latest sort")
    if not clicked:
        # Fallback: walk visible clickable nodes and click by text content.
        result = topic_page.evaluate(
            """
            () => {
              const targets = ['最新发布', '最新'];
              const nodes = Array.from(document.querySelectorAll('a,button,div,span,[role="tab"]'));
              const isVisible = (el) => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
              };
              for (const text of targets) {
                const exact = nodes.find((n) => isVisible(n) && (n.innerText || '').trim() === text);
                if (exact) { exact.click(); return `exact:${text}`; }
              }
              for (const text of targets) {
                const fuzzy = nodes.find((n) => isVisible(n) && (n.innerText || '').includes(text));
                if (fuzzy) { fuzzy.click(); return `contains:${text}`; }
              }
              return '';
            }
            """
        )
        if isinstance(result, str) and result:
            print(f"Clicked latest sort by JS fallback: {result}")
            clicked = True
    if clicked:
        topic_page.wait_for_timeout(1500)
        topic_page.evaluate("window.scrollTo(0, 0)")
        topic_page.wait_for_timeout(400)
    return clicked


def _resolve_content_page_after_click(page: Page, pages_before: int) -> Optional[Page]:
    context = page.context
    end_at = time.monotonic() + 4
    while time.monotonic() < end_at:
        for p in reversed(_automation_pages(context)):
            try:
                if p.is_closed():
                    continue
            except Exception:
                continue
            if extract_content_url_from_page_url(p.url):
                try:
                    p.bring_to_front()
                except Exception:
                    pass
                close_extra_blank_pages(context, p)
                return p
        if len(_automation_pages(context)) <= pages_before and extract_content_url_from_page_url(page.url):
            close_extra_blank_pages(context, page)
            return page
        page.wait_for_timeout(250)
    return None


def get_first_visible_search_card_href(page: Page) -> str:
    try:
        href = page.evaluate(
            """
            () => {
              const maxX = window.innerWidth - 260;
              const minX = 140;
              const minY = 140;
              const anchors = Array.from(
                document.querySelectorAll("a[href*='modal_id='],a[href*='/video/'],a[href*='/note/']")
              );
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 120 &&
                       r.height > 120 &&
                       r.left >= minX &&
                       r.right <= maxX &&
                       r.top >= minY &&
                       s.display !== 'none' &&
                       s.visibility !== 'hidden';
              };
              const sorted = anchors
                .filter(visible)
                .sort((a, b) => {
                  const ra = a.getBoundingClientRect();
                  const rb = b.getBoundingClientRect();
                  if (Math.abs(ra.top - rb.top) > 3) return ra.top - rb.top;
                  return ra.left - rb.left;
                });
              if (!sorted.length) return '';
              return sorted[0].getAttribute('href') || '';
            }
            """
        )
        return str(href or "").strip()
    except Exception:
        return ""


def get_top_left_search_card_target(page: Page, skip_live: bool = True) -> Dict[str, Any]:
    try:
        obj = page.evaluate(
            """
            (skipLive) => {
              const maxX = window.innerWidth - 260;
              const minX = 120;
              const minY = 150;
              const maxY = window.innerHeight - 110;

              const anchorNodes = Array.from(
                document.querySelectorAll(
                  "a[href*='modal_id='],a[href*='/video/'],a[href*='/note/']"
                )
              );
              const nodes = Array.from(document.querySelectorAll("article,li,div,a"));
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 130 &&
                       r.height > 180 &&
                       r.width < 460 &&
                       r.left >= minX &&
                       r.right <= maxX &&
                       r.top >= minY &&
                       r.top <= maxY &&
                       s.display !== 'none' &&
                       s.visibility !== 'hidden';
              };
              const hrefOf = (el) => {
                if (!el) return '';
                const direct = el.getAttribute && (el.getAttribute('href') || '');
                if (direct) return direct;
                const parentA = el.closest && el.closest('a[href]');
                if (parentA) return parentA.getAttribute('href') || '';
                const childA = el.querySelector && el.querySelector('a[href]');
                if (childA) return childA.getAttribute('href') || '';
                return '';
              };
              const isLiveText = (txt) => {
                const t = (txt || '').toLowerCase();
                return t.includes('直播') || t.includes('live') || t.includes('正在播') || t.includes('开播');
              };

              const anchorCards = anchorNodes
                .map((el) => {
                  if (!visible(el)) return null;
                  const r = el.getBoundingClientRect();
                  const href = hrefOf(el);
                  if (!href) return null;
                  const box = (el.closest && el.closest('article,li,div')) || el;
                  const txt = (box.innerText || el.innerText || '').trim();
                  if (!txt) return null;
                  return {
                    x: Math.floor(r.left + r.width * 0.5),
                    y: Math.floor(r.top + Math.min(r.height * 0.45, 300)),
                    top: r.top,
                    left: r.left,
                    href,
                    live: isLiveText(txt),
                  };
                })
                .filter(Boolean);

              const fallbackCards = nodes
                .map((el) => {
                  if (!visible(el)) return null;
                  const r = el.getBoundingClientRect();
                  const href = hrefOf(el);
                  if (!href) return null;
                  const txt = (el.innerText || '').trim();
                  if (!txt) return null;
                  return {
                    x: Math.floor(r.left + r.width * 0.5),
                    y: Math.floor(r.top + Math.min(r.height * 0.45, 300)),
                    top: r.top,
                    left: r.left,
                    href,
                    live: isLiveText(txt),
                  };
                })
                .filter(Boolean);

              const cards = (anchorCards.length ? anchorCards : fallbackCards)
                .sort((a, b) => {
                  if (Math.abs(a.top - b.top) > 2) return a.top - b.top;
                  return a.left - b.left;
                });

              if (skipLive) {
                const nonLive = cards.find((c) => !c.live);
                if (nonLive) return nonLive;
              }
              if (!cards.length) return { href: '', x: 0, y: 0 };
              return cards[0];
            }
            """,
            skip_live,
        )
        if not isinstance(obj, dict):
            return {"href": "", "x": 0, "y": 0, "live": False}
        href = str(obj.get("href") or "").strip()
        x = int(obj.get("x") or 0)
        y = int(obj.get("y") or 0)
        live = bool(obj.get("live"))
        if is_live_href(href):
            live = True
        return {"href": href, "x": x, "y": y, "live": live}
    except Exception:
        return {"href": "", "x": 0, "y": 0, "live": False}


def get_visible_search_card_hrefs(page: Page, limit: int = 30) -> List[str]:
    try:
        hrefs = page.evaluate(
            """
            (maxCount) => {
              const maxX = window.innerWidth - 260;
              const minX = 140;
              const minY = 140;
              const anchors = Array.from(
                document.querySelectorAll("a[href*='modal_id='],a[href*='/video/'],a[href*='/note/']")
              );
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 120 &&
                       r.height > 120 &&
                       r.left >= minX &&
                       r.right <= maxX &&
                       r.top >= minY &&
                       s.display !== 'none' &&
                       s.visibility !== 'hidden';
              };
              const sorted = anchors
                .filter(visible)
                .sort((a, b) => {
                  const ra = a.getBoundingClientRect();
                  const rb = b.getBoundingClientRect();
                  if (Math.abs(ra.top - rb.top) > 3) return ra.top - rb.top;
                  return ra.left - rb.left;
                })
                .map((a) => a.getAttribute('href') || '')
                .filter(Boolean);
              return sorted.slice(0, Math.max(1, maxCount));
            }
            """,
            max(1, limit),
        )
        if not isinstance(hrefs, list):
            return []
        out: List[str] = []
        for h in hrefs:
            hs = str(h or "").strip()
            if hs:
                out.append(hs)
        return out
    except Exception:
        return []


def wait_for_search_cards_ready(page: Page, timeout_seconds: int = 35) -> bool:
    end_at = time.monotonic() + max(1, timeout_seconds)
    while time.monotonic() < end_at:
        hrefs = get_visible_search_card_hrefs(page, limit=8)
        if hrefs:
            return True
        try:
            ok = bool(
                page.evaluate(
                    """
                    () => {
                      const maxX = window.innerWidth - 260;
                      const nodes = Array.from(document.querySelectorAll('article,li,div,a'));
                      const visible = (el) => {
                        const r = el.getBoundingClientRect();
                        const s = window.getComputedStyle(el);
                        return r.width > 150 && r.height > 200 &&
                               r.left >= 120 && r.right <= maxX &&
                               r.top >= 120 &&
                               s.display !== 'none' && s.visibility !== 'hidden';
                      };
                      const cards = nodes.filter((el) => {
                        if (!visible(el)) return false;
                        const t = (el.innerText || '').trim();
                        return t.includes('@') || t.includes('图文') || t.includes('评论');
                      });
                      return cards.length >= 1;
                    }
                    """
                )
            )
            if ok:
                return True
        except Exception:
            pass
        page.wait_for_timeout(700)
    return False


def open_next_unseen_search_card(
    page: Page,
    seen_urls: Set[str],
    deadline: Optional[float] = None,
    stop_flag_path: Optional[Path] = None,
) -> Optional[Page]:
    for _ in range(6):
        if get_stop_reason(deadline, stop_flag_path):
            return None

        top_target = get_top_left_search_card_target(page, skip_live=True)
        top_href = str(top_target.get("href") or "").strip()
        top_x = int(top_target.get("x") or 0)
        top_y = int(top_target.get("y") or 0)
        top_live = bool(top_target.get("live")) or is_live_href(top_href)
        if top_href and top_x > 0 and top_y > 0 and not top_live:
            abs_top = to_absolute_douyin_url(top_href)
            top_candidate = normalize_content_url(abs_top) or extract_content_url_from_page_url(abs_top)
            if not top_candidate or top_candidate not in seen_urls:
                try:
                    pages_before = len(_automation_pages(page.context))
                    page.mouse.click(top_x, top_y, delay=35)
                    page.wait_for_timeout(900)
                    target_page = _resolve_content_page_after_click(page, pages_before)
                    if target_page is not None:
                        return target_page
                except Exception:
                    pass

        hrefs = get_visible_search_card_hrefs(page, limit=36)
        for href in hrefs:
            if is_live_href(href):
                continue
            abs_href = to_absolute_douyin_url(href)
            candidate = normalize_content_url(abs_href) or extract_content_url_from_page_url(abs_href)
            if candidate and candidate in seen_urls:
                continue
            opened = force_open_first_search_card_by_href(page, href)
            if opened is not None:
                return opened
        try:
            page.mouse.move(1200, 700)
            page.mouse.wheel(0, 1350)
            page.wait_for_timeout(700)
        except Exception:
            pass
    return None


def force_open_first_search_card_by_href(page: Page, href: str) -> Optional[Page]:
    href = (href or "").strip()
    if not href:
        return None
    if is_live_href(href):
        return None

    pages_before = len(_automation_pages(page.context))
    try:
        clicked = page.evaluate(
            """
            (targetHref) => {
              const maxX = window.innerWidth - 260;
              const minX = 140;
              const minY = 140;
              const nodes = Array.from(document.querySelectorAll("a[href]"));
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 120 && r.height > 120 &&
                       r.left >= minX && r.right <= maxX && r.top >= minY &&
                       s.display !== 'none' && s.visibility !== 'hidden';
              };
              const candidates = nodes.filter((n) => visible(n) && (n.getAttribute('href') || '') === targetHref)
                .sort((a, b) => {
                  const ra = a.getBoundingClientRect();
                  const rb = b.getBoundingClientRect();
                  if (Math.abs(ra.top - rb.top) > 3) return ra.top - rb.top;
                  return ra.left - rb.left;
                });
              if (!candidates.length) return false;
              candidates[0].click();
              return true;
            }
            """,
            href,
        )
        if bool(clicked):
            page.wait_for_timeout(1000)
            target_page = _resolve_content_page_after_click(page, pages_before)
            if target_page is not None:
                return target_page
    except Exception:
        pass

    abs_href = to_absolute_douyin_url(href)
    target_url = normalize_content_url(abs_href) or extract_content_url_from_page_url(abs_href) or abs_href
    try:
        page.goto(target_url, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(1000)
        target_page = _resolve_content_page_after_click(page, pages_before)
        if target_page is not None:
            return target_page
    except Exception:
        pass
    return None


def open_first_search_card(page: Page) -> Optional[Page]:
    if extract_content_url_from_page_url(page.url):
        return page

    # Current Jingxuan cards have clickable covers, without href links.
    try:
        covers = page.locator(".videoImage:visible")
        if covers.count():
            pages_before = len(_automation_pages(page.context))
            covers.first.click(timeout=4000, no_wait_after=True)
            target_page = _resolve_content_page_after_click(page, pages_before)
            if target_page is not None:
                return target_page
    except Exception:
        pass

    try:
        page.bring_to_front()
    except Exception:
        pass
    close_extra_blank_pages(page.context, page)

    top_target = get_top_left_search_card_target(page, skip_live=True)
    top_href = str(top_target.get("href") or "").strip()
    top_x = int(top_target.get("x") or 0)
    top_y = int(top_target.get("y") or 0)
    top_live = bool(top_target.get("live")) or is_live_href(top_href)
    if top_x > 0 and top_y > 0 and not top_live:
        try:
            pages_before = len(_automation_pages(page.context))
            page.mouse.click(top_x, top_y, delay=35)
            page.wait_for_timeout(900)
            target_page = _resolve_content_page_after_click(page, pages_before)
            if target_page is not None:
                if is_live_content_page(target_page):
                    print("Skip live card at first position.")
                else:
                    return target_page
                if target_page is not page:
                    try:
                        target_page.close()
                    except Exception:
                        pass
                    try:
                        page.bring_to_front()
                    except Exception:
                        pass
                else:
                    try:
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(300)
                    except Exception:
                        pass
        except Exception:
            pass

    first_href = top_href or get_first_visible_search_card_href(page)
    if first_href:
        if is_live_href(first_href):
            first_href = ""
    if first_href:
        forced = force_open_first_search_card_by_href(page, first_href)
        if forced is not None:
            if is_live_content_page(forced):
                print("Skip live card opened by href.")
                if forced is not page:
                    try:
                        forced.close()
                    except Exception:
                        pass
                    try:
                        page.bring_to_front()
                    except Exception:
                        pass
                else:
                    try:
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(300)
                    except Exception:
                        pass
            else:
                return forced

    for sel in ["a[href*='modal_id=']", "a[href*='/video/']", "a[href*='/note/']"]:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                pages_before = len(_automation_pages(page.context))
                loc.first.click(timeout=2500)
                page.wait_for_timeout(1200)
                target_page = _resolve_content_page_after_click(page, pages_before)
                if target_page is not None:
                    if is_live_content_page(target_page):
                        print("Skip live card from selector click.")
                        if target_page is not page:
                            try:
                                target_page.close()
                            except Exception:
                                pass
                            try:
                                page.bring_to_front()
                            except Exception:
                                pass
                        else:
                            try:
                                page.keyboard.press("Escape")
                                page.wait_for_timeout(300)
                            except Exception:
                                pass
                    else:
                        return target_page
        except Exception:
            pass

    try:
        pages_before = len(_automation_pages(page.context))
        clicked = page.evaluate(
            """
            () => {
              const maxX = window.innerWidth - 260;
              const minX = 150;
              const minY = 180;
              const nodes = Array.from(document.querySelectorAll('a,div,article,li'));
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 160 && r.height > 220 &&
                       r.left >= minX && r.right <= maxX && r.top >= minY &&
                       s.display !== 'none' && s.visibility !== 'hidden';
              };
              const cards = nodes
                .filter((el) => visible(el) && ((el.innerText || '').includes('图文') || (el.innerText || '').includes('@')))
                .sort((a, b) => {
                  const ra = a.getBoundingClientRect();
                  const rb = b.getBoundingClientRect();
                  if (Math.abs(ra.top - rb.top) > 3) return ra.top - rb.top;
                  return ra.left - rb.left;
                });
              if (!cards.length) return false;
              cards[0].click();
              return true;
            }
            """
        )
        if clicked:
            page.wait_for_timeout(1200)
            target_page = _resolve_content_page_after_click(page, pages_before)
            if target_page is not None:
                if is_live_content_page(target_page):
                    print("Skip live card from JS click.")
                    if target_page is not page:
                        try:
                            target_page.close()
                        except Exception:
                            pass
                        try:
                            page.bring_to_front()
                        except Exception:
                            pass
                    else:
                        try:
                            page.keyboard.press("Escape")
                            page.wait_for_timeout(300)
                        except Exception:
                            pass
                else:
                    return target_page
    except Exception:
        pass

    for x, y in [(320, 430), (600, 430), (880, 430), (1160, 430)]:
        try:
            pages_before = len(_automation_pages(page.context))
            page.mouse.click(x, y, delay=40)
            page.wait_for_timeout(1100)
            target_page = _resolve_content_page_after_click(page, pages_before)
            if target_page is not None:
                if is_live_content_page(target_page):
                    print("Skip live card from coordinate fallback.")
                    if target_page is not page:
                        try:
                            target_page.close()
                        except Exception:
                            pass
                        try:
                            page.bring_to_front()
                        except Exception:
                            pass
                    else:
                        try:
                            page.keyboard.press("Escape")
                            page.wait_for_timeout(300)
                        except Exception:
                            pass
                else:
                    return target_page
        except Exception:
            continue
    return None


def go_next_modal_content(page: Page) -> bool:
    def marker(u: str) -> str:
        return extract_content_url_from_page_url(u) or (u or "")

    before = marker(page.url)
    actions = [
        lambda: page.keyboard.press("ArrowRight"),
        lambda: page.keyboard.press("ArrowDown"),
        lambda: page.keyboard.press("PageDown"),
    ]

    for action in actions:
        try:
            action()
        except Exception:
            continue
        for _ in range(12):
            page.wait_for_timeout(280)
            after = marker(page.url)
            if after and after != before:
                return True

    # Fallback: scroll on right side to avoid toggling play/pause on video center.
    try:
        view = page.viewport_size or {"width": 1440, "height": 900}
        page.mouse.move(int(view["width"] * 0.86), int(view["height"] * 0.58))
        page.mouse.wheel(0, 1350)
    except Exception:
        return False
    for _ in range(12):
        page.wait_for_timeout(280)
        after = marker(page.url)
        if after and after != before:
            return True
    return False


def open_first_search_card_with_retry(
    page: Page,
    *,
    max_attempts: int = 8,
    deadline: Optional[float] = None,
    stop_flag_path: Optional[Path] = None,
) -> Optional[Page]:
    try:
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(400)
    except Exception:
        pass
    wait_for_search_cards_ready(page, timeout_seconds=12)

    for _ in range(max(1, max_attempts)):
        if get_stop_reason(deadline, stop_flag_path):
            return None
        try:
            page.bring_to_front()
        except Exception:
            pass
        close_extra_blank_pages(page.context, page)

        target_page = open_first_search_card(page)
        if target_page is not None:
            return target_page
        # Keep staying at top while retrying first-card open.
        try:
            page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass
        page.wait_for_timeout(800)
    return None


def scrape_comments_from_current_content(
    page: Page,
    topic_url: str,
    video_url: str,
    comment_scroll_rounds: int,
    deadline: Optional[float] = None,
    captcha_wait_seconds: int = 120,
    stop_flag_path: Optional[Path] = None,
) -> List[CommentRecord]:
    aweme_id = extract_aweme_id(video_url)
    if not aweme_id:
        aweme_id = extract_aweme_id(extract_content_url_from_page_url(page.url))

    captured: List[CommentRecord] = []
    seen_keys: Set[Tuple[str, str, str]] = set()

    def on_response(resp: Any) -> None:
        url = resp.url
        if "douyin.com" not in url:
            return
        if "comment/list" not in url:
            return
        try:
            data = resp.json()
        except Exception:
            return

        for item in iter_comment_objects(data):
            record = extract_comment_record(
                topic_url=topic_url,
                video_url=video_url,
                aweme_id=aweme_id,
                comment_obj=item,
            )
            if not record:
                continue
            key = (record.comment_id, record.nickname, record.text)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            captured.append(record)

    page.on("response", on_response)
    try:
        if not wait_for_captcha_clear(page, captcha_wait_seconds, stop_flag_path):
            return captured

        panel_ready = is_comment_panel_visible(page) or open_comment_panel(page)
        if not panel_ready:
            print("  -> warning: comment panel open failed, will retry opening and avoid feed scrolling")

        idle_rounds = 0
        last_count = 0
        for _ in range(max(1, comment_scroll_rounds)):
            stop_reason = get_stop_reason(deadline, stop_flag_path)
            if stop_reason:
                print(f"Stop during comment scrolling: {stop_reason}.")
                break
            if not wait_for_captcha_clear(page, captcha_wait_seconds, stop_flag_path):
                break

            if not panel_ready:
                panel_ready = open_comment_panel(page)
                if not panel_ready:
                    idle_rounds += 1
                    page.wait_for_timeout(600)
                    if idle_rounds >= 6:
                        break
                    continue

            scrolled_panel = scroll_comment_area_once(page)
            if not scrolled_panel:
                # Do not scroll main page; try reopening panel instead.
                panel_ready = open_comment_panel(page)
                page.wait_for_timeout(420)
                if not panel_ready:
                    idle_rounds += 1
                    if idle_rounds >= 6:
                        break
                    continue

            page.wait_for_timeout(650)
            current_count = len(captured)
            if current_count == last_count:
                idle_rounds += 1
            else:
                idle_rounds = 0
                last_count = current_count
            if idle_rounds >= 6:
                break

        page.wait_for_timeout(900)
        return captured
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass


def is_comment_panel_visible(page: Page) -> bool:
    try:
        return bool(
            page.evaluate(
                """
                () => {
                  const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 20 && r.height > 20 && s.display !== 'none' && s.visibility !== 'hidden';
                  };

                  const input = document.querySelector(
                    "textarea[placeholder*='评论'],input[placeholder*='评论'],div[contenteditable='true'][data-placeholder*='评论'],div[contenteditable='true']"
                  );
                  if (input && visible(input)) return true;

                  const vw = window.innerWidth;
                  const vh = window.innerHeight;
                  const nodes = Array.from(document.querySelectorAll('div,section,aside'));
                  for (const el of nodes) {
                    if (!visible(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.left < vw * 0.56) continue;
                    if (r.width < 240 || r.height < 220) continue;
                    if (r.top > vh - 60 || r.bottom < 60) continue;
                    const t = (el.innerText || '').slice(0, 2000);
                    if (!t) continue;
                    const hasCommentWord = t.includes('评论') || t.includes('条评论') || t.includes('回复');
                    const hasCommentInput =
                      t.includes('留下你的精彩评论') ||
                      t.includes('写评论') ||
                      t.includes('全部评论') ||
                      t.includes('最热') ||
                      t.includes('最新');
                    if (hasCommentWord && hasCommentInput) return true;
                  }
                  return false;
                }
                """
            )
        )
    except Exception:
        return False


def click_comment_icon_fallback(page: Page) -> bool:
    try:
        points = page.evaluate(
            """
            () => {
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 12 && r.height > 12 && s.display !== 'none' && s.visibility !== 'hidden';
              };
              const vw = window.innerWidth;
              const vh = window.innerHeight;
              const nodes = Array.from(document.querySelectorAll('button,a,[role="button"],div,span'));
              const candidates = [];
              for (const el of nodes) {
                if (!visible(el)) continue;
                const r = el.getBoundingClientRect();
                if (r.left < vw * 0.72 || r.left > vw - 6) continue;
                if (r.top < vh * 0.18 || r.top > vh * 0.92) continue;
                const txt = (el.innerText || el.textContent || '').trim();
                const aria = ((el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '')).trim();
                const cls = (el.className || '').toString().toLowerCase();
                let score = 0;
                if (txt.includes('评论') || txt.includes('条评论')) score += 7;
                if (aria.includes('评论')) score += 7;
                if (cls.includes('comment')) score += 2;
                if (el.querySelector && el.querySelector('svg')) score += 1;
                if (/^\\d+(\\.\\d+)?[万wW]?$/.test(txt)) score += 1;
                if (r.top >= vh * 0.52 && r.top <= vh * 0.86) score += 1;
                if (score <= 0) continue;
                candidates.push({
                  x: Math.floor(r.left + r.width / 2),
                  y: Math.floor(r.top + r.height / 2),
                  score,
                  top: r.top,
                });
              }
              candidates.sort((a, b) => {
                if (b.score !== a.score) return b.score - a.score;
                return a.top - b.top;
              });
              return candidates.slice(0, 10);
            }
            """
        )
        if isinstance(points, list):
            for p in points:
                if not isinstance(p, dict):
                    continue
                x = int(p.get("x") or 0)
                y = int(p.get("y") or 0)
                if x <= 0 or y <= 0:
                    continue
                try:
                    page.mouse.click(x, y, delay=22)
                    page.wait_for_timeout(380)
                    if is_comment_panel_visible(page):
                        return True
                except Exception:
                    continue
    except Exception:
        pass

    view = page.viewport_size or {"width": 1440, "height": 900}
    x_candidates = [int(view["width"] * 0.80), int(view["width"] * 0.84), int(view["width"] * 0.88)]
    y_candidates = [int(view["height"] * 0.52), int(view["height"] * 0.60), int(view["height"] * 0.68), int(view["height"] * 0.76)]
    for x in x_candidates:
        for y in y_candidates:
            try:
                page.mouse.click(x, y, delay=25)
                page.wait_for_timeout(450)
                if is_comment_panel_visible(page):
                    return True
            except Exception:
                continue
    return False


def click_comment_count_text_fallback(page: Page) -> bool:
    try:
        points = page.evaluate(
            """
            () => {
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 8 && r.height > 8 && s.display !== 'none' && s.visibility !== 'hidden';
              };
              const vw = window.innerWidth;
              const vh = window.innerHeight;
              const regex = /^\\d+(\\.\\d+)?[万wW]?$/;
              const nodes = Array.from(document.querySelectorAll('div,span,a,button'));
              const nums = [];
              for (const el of nodes) {
                if (!visible(el)) continue;
                const r = el.getBoundingClientRect();
                if (r.left < vw * 0.72 || r.left > vw - 6) continue;
                if (r.top < vh * 0.18 || r.top > vh * 0.94) continue;
                const t = (el.innerText || el.textContent || '').trim();
                if (!t || !regex.test(t)) continue;
                nums.push({
                  x: Math.floor(r.left + r.width / 2),
                  y: Math.floor(r.top + r.height / 2),
                  top: r.top,
                  text: t,
                });
              }
              nums.sort((a, b) => a.top - b.top);
              if (!nums.length) return [];
              // Most pages show like count first, comment count second.
              const ordered = [];
              if (nums.length >= 2) ordered.push(nums[1]);
              for (const n of nums) ordered.push(n);
              // de-dup by coordinate
              const seen = new Set();
              const out = [];
              for (const n of ordered) {
                const key = `${n.x},${n.y}`;
                if (seen.has(key)) continue;
                seen.add(key);
                out.push(n);
                if (out.length >= 8) break;
              }
              return out;
            }
            """
        )
        if not isinstance(points, list):
            return False
        for p in points:
            if not isinstance(p, dict):
                continue
            x = int(p.get("x") or 0)
            y = int(p.get("y") or 0)
            if x <= 0 or y <= 0:
                continue
            try:
                page.mouse.click(x, y, delay=18)
                page.wait_for_timeout(420)
                if is_comment_panel_visible(page):
                    return True
            except Exception:
                continue
    except Exception:
        return False
    return False


def open_comment_panel(page: Page) -> bool:
    # First playback shows a full-screen instructional overlay over the action rail.
    try:
        tutorial = page.get_by_text("我知道了", exact=True)
        if tutorial.count() and tutorial.first.is_visible():
            tutorial.first.click(timeout=2500)
            page.wait_for_timeout(300)
    except Exception:
        pass
    if is_comment_panel_visible(page):
        return True

    selectors = [
        "[data-e2e='feed-comment-icon']:visible",
        "[data-e2e*='comment']",
        "[data-e2e*='comment-icon']",
        "[data-e2e*='comment-btn']",
        "[aria-label*='评论']",
        "[role='button'][aria-label*='评论']",
        "div[aria-label*='评论']",
    ]
    if try_click_first(page, selectors, "comment panel"):
        for _ in range(5):
            page.wait_for_timeout(260)
            if is_comment_panel_visible(page):
                return True

    # JS fallback for pages where comment button has dynamic classes.
    try:
        ok = page.evaluate(
            """
            () => {
              const vw = window.innerWidth;
              const nodes = Array.from(document.querySelectorAll('button,a,div,span'));
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 10 && r.height > 10 && s.display !== 'none' && s.visibility !== 'hidden';
              };
              for (const el of nodes) {
                if (!visible(el)) continue;
                const r = el.getBoundingClientRect();
                if (r.left < vw * 0.65) continue; // restrict to right action rail
                const t = (el.innerText || el.textContent || '').trim();
                if (!t) continue;
                if ((t.includes('评论') || t.includes('条评论')) && t.length <= 24) {
                  el.click();
                  return true;
                }
              }
              return false;
            }
            """
        )
        if bool(ok):
            for _ in range(5):
                page.wait_for_timeout(260)
                if is_comment_panel_visible(page):
                    return True
    except Exception:
        pass

    # Safer fallback: click right-rail comment count text; avoid icon guesses that may like content.
    if click_comment_count_text_fallback(page):
        return True
    return False


def scroll_comment_area_once(page: Page) -> bool:
    # Scroll right-side comment container only; never scroll the main feed here.
    try:
        return bool(
            page.evaluate(
                """
                () => {
                  const nodes = Array.from(document.querySelectorAll('*'));
                  const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 260 && r.height > 160 &&
                           r.bottom > 40 && r.top < window.innerHeight - 40 &&
                           s.display !== 'none' && s.visibility !== 'hidden';
                  };

                  const scrollables = nodes.filter((el) => {
                    if (!visible(el)) return false;
                    const r = el.getBoundingClientRect();
                    if (r.left < window.innerWidth * 0.56) return false;
                    const s = window.getComputedStyle(el);
                    const overflowY = s.overflowY || '';
                    if (!['auto', 'scroll', 'overlay'].includes(overflowY)) return false;
                    return el.scrollHeight > el.clientHeight + 100;
                  });

                  if (scrollables.length > 0) {
                    scrollables.sort((a, b) =>
                      (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight)
                    );
                    const target = scrollables[0];
                    const before = target.scrollTop;
                    target.scrollTop += 1100;
                    return target.scrollTop !== before;
                  }
                  return false;
                }
                """
            )
        )
    except Exception:
        return False


def is_feed_video_present(page: Page) -> bool:
    """首页推荐流里是否有可播放的视频元素。"""
    try:
        return bool(
            page.evaluate(
                """
                () => {
                  if (document.querySelector('[data-e2e="feed-active-video"]')) return true;
                  for (const v of document.querySelectorAll('video')) {
                    const r = v.getBoundingClientRect();
                    if (r.width > 300 && r.height > 300) return true;
                  }
                  return false;
                }
                """
            )
        )
    except Exception:
        return False


def get_feed_active_aweme_id(page: Page) -> str:
    """从推荐流当前活动项的 data-e2e-vid 属性读取 aweme_id（可能为空）。"""
    try:
        value = page.evaluate(
            """
            () => {
              const active = document.querySelector('[data-e2e="feed-active-video"]');
              if (!active) return '';
              return active.getAttribute('data-e2e-vid') || '';
            }
            """
        )
        return str(value or "").strip()
    except Exception:
        return ""


def get_feed_video_signature(page: Page) -> str:
    """当前推荐流视频的签名，用于判断切换视频是否成功。"""
    try:
        value = page.evaluate(
            """
            () => {
              const active = document.querySelector('[data-e2e="feed-active-video"]');
              if (active) {
                const vid = active.getAttribute('data-e2e-vid') || '';
                if (vid) return 'vid:' + vid;
              }
              let best = '';
              let bestArea = 0;
              for (const v of document.querySelectorAll('video')) {
                const r = v.getBoundingClientRect();
                const area = r.width * r.height;
                if (r.width > 300 && r.height > 300 && area > bestArea) {
                  bestArea = area;
                  best = v.src || '';
                }
              }
              return best ? 'src:' + best : '';
            }
            """
        )
        return str(value or "").strip()
    except Exception:
        return ""


def wait_for_feed_ready(page: Page, timeout_seconds: int) -> bool:
    end_at = time.monotonic() + max(1, timeout_seconds)
    while time.monotonic() < end_at:
        if is_feed_video_present(page):
            return True
        page.wait_for_timeout(1200)
    return False


def close_feed_comment_panel(page: Page) -> bool:
    """关闭推荐流右侧评论面板；切换视频前必须关闭，否则滚轮只会滚动评论。"""
    if not is_comment_panel_visible(page):
        return True
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        if not is_comment_panel_visible(page):
            return True
    except Exception:
        pass
    try:
        icon = page.locator("[data-e2e='feed-comment-icon']:visible")
        if icon.count():
            icon.first.click(timeout=2000)
            page.wait_for_timeout(450)
            if not is_comment_panel_visible(page):
                return True
    except Exception:
        pass
    return not is_comment_panel_visible(page)


def advance_feed_video(page: Page) -> bool:
    """滚动滚轮/按方向键切换到下一个推荐视频，并用签名验证切换成功。"""
    close_feed_comment_panel(page)
    view = page.viewport_size or {"width": 1440, "height": 900}
    before = get_feed_video_signature(page)
    mid_x = int(view["width"] * 0.35)

    def try_wheel(delta: int, y_ratio: float) -> None:
        page.mouse.move(mid_x, int(view["height"] * y_ratio))
        page.mouse.wheel(0, delta)

    attempts = [
        ("wheel", lambda: try_wheel(1600, 0.55)),
        ("key", lambda: page.keyboard.press("ArrowDown")),
        ("wheel-big", lambda: try_wheel(3000, 0.45)),
    ]
    for _name, action in attempts:
        try:
            action()
        except Exception:
            continue
        page.wait_for_timeout(1500)
        after = get_feed_video_signature(page)
        if after and after != before:
            return True
    return False


def scrape_comments_from_feed_video(
    page: Page,
    topic_url: str,
    comment_scroll_rounds: int,
    deadline: Optional[float] = None,
    captcha_wait_seconds: int = 120,
    stop_flag_path: Optional[Path] = None,
) -> Tuple[List[CommentRecord], str]:
    """抓取推荐流当前视频的评论。

    首页推荐流的页面 URL 固定不变，aweme_id 改从每条 comment/list 请求的
    查询参数中提取。返回 (评论记录列表, 该视频的 aweme_id)。
    """
    captured: List[CommentRecord] = []
    seen_keys: Set[Tuple[str, str, str]] = set()
    current_aweme = {"id": ""}

    def record_video_url(aid: str) -> str:
        resolved = aid or current_aweme["id"]
        return f"https://www.douyin.com/video/{resolved}" if resolved else ""

    def on_response(resp: Any) -> None:
        url = resp.url
        if "douyin.com" not in url:
            return
        if "comment/list" not in url:
            return
        aid = ""
        try:
            query = parse_qs(urlparse(url).query or "")
            aid = str((query.get("aweme_id") or [""])[0]).strip()
        except Exception:
            aid = ""
        if aid:
            current_aweme["id"] = aid
        try:
            data = resp.json()
        except Exception:
            return

        for item in iter_comment_objects(data):
            record = extract_comment_record(
                topic_url=topic_url,
                video_url=record_video_url(aid),
                aweme_id=aid or current_aweme["id"],
                comment_obj=item,
            )
            if not record:
                continue
            key = (record.comment_id, record.nickname, record.text)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            captured.append(record)

    page.on("response", on_response)
    try:
        if not wait_for_captcha_clear(page, captcha_wait_seconds, stop_flag_path):
            return captured, current_aweme["id"]

        panel_ready = is_comment_panel_visible(page) or open_comment_panel(page)
        if not panel_ready:
            print("  -> warning: comment panel open failed, will retry opening")

        idle_rounds = 0
        last_count = 0
        for _ in range(max(1, comment_scroll_rounds)):
            stop_reason = get_stop_reason(deadline, stop_flag_path)
            if stop_reason:
                print(f"Stop during comment scrolling: {stop_reason}.")
                break
            if not wait_for_captcha_clear(page, captcha_wait_seconds, stop_flag_path):
                break

            if not panel_ready:
                panel_ready = open_comment_panel(page)
                if not panel_ready:
                    idle_rounds += 1
                    page.wait_for_timeout(600)
                    if idle_rounds >= 6:
                        break
                    continue

            scrolled_panel = scroll_comment_area_once(page)
            if not scrolled_panel:
                # 只滚动右侧评论容器；滚动主区域会切换视频，必须避免。
                panel_ready = open_comment_panel(page)
                page.wait_for_timeout(420)
                if not panel_ready:
                    idle_rounds += 1
                    if idle_rounds >= 6:
                        break
                    continue

            page.wait_for_timeout(650)
            current_count = len(captured)
            if current_count == last_count:
                idle_rounds += 1
            else:
                idle_rounds = 0
                last_count = current_count
            if idle_rounds >= 6:
                break

        page.wait_for_timeout(900)
        return captured, current_aweme["id"]
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass


def collect_video_urls(
    topic_page: Page,
    max_videos: int,
    scroll_rounds: int,
    deadline: Optional[float] = None,
    known_urls: Optional[Set[str]] = None,
    stop_at_first: bool = False,
    reset_to_top: bool = True,
) -> List[str]:
    seen: Set[str] = known_urls if known_urls is not None else set()
    ordered: List[str] = []

    def on_search_response(resp: Any) -> None:
        url = resp.url
        if "douyin.com" not in url:
            return
        if "/aweme/v1/web/" not in url and "search" not in url:
            return
        try:
            data = resp.json()
        except Exception:
            return
        urls = extract_content_urls_from_payload(data)
        for u in urls:
            content_url = normalize_content_url(u)
            if not content_url:
                continue
            if content_url in seen:
                continue
            seen.add(content_url)
            ordered.append(content_url)
            if stop_at_first:
                return

    if reset_to_top:
        topic_page.evaluate("window.scrollTo(0, 0)")
        topic_page.wait_for_timeout(600)
    topic_page.on("response", on_search_response)

    try:
        for _ in range(scroll_rounds):
            if is_deadline_reached(deadline):
                print("Time limit reached while collecting content links.")
                break

            if len(ordered) >= max_videos:
                break

            count_before_round = len(ordered)
            hrefs: List[str] = topic_page.eval_on_selector_all(
                "a[href*='/video/'], a[href*='/note/']",
                """
                els => {
                  const maxX = window.innerWidth - 260; // exclude right-side panel
                  return els
                    .map(e => {
                      const href = e.getAttribute('href') || '';
                      const r = e.getBoundingClientRect();
                      const s = window.getComputedStyle(e);
                      const visible = r.width > 80 && r.height > 80 && s.display !== 'none' && s.visibility !== 'hidden';
                      return { href, x: r.left, y: r.top, visible };
                    })
                    .filter(x => x.visible && x.x >= 0 && x.x <= maxX && x.y >= 0)
                    .map(x => x.href);
                }
                """,
            )

            for href in hrefs:
                content_url = normalize_content_url(href)
                if not content_url:
                    continue
                if content_url in seen:
                    continue
                seen.add(content_url)
                ordered.append(content_url)
                if stop_at_first:
                    return ordered[:1]
                if len(ordered) >= max_videos:
                    break

            # Fallback: if href extraction found nothing this round, click visible card positions.
            if len(ordered) == count_before_round:
                _collect_by_click_fallback(
                    topic_page=topic_page,
                    seen=seen,
                    ordered=ordered,
                    max_videos=max_videos,
                )
                if stop_at_first and ordered:
                    return ordered[:1]

            if len(ordered) >= max_videos:
                break

            topic_page.mouse.move(1200, 700)
            topic_page.mouse.wheel(0, 2200)
            topic_page.wait_for_timeout(900)
        return ordered[:max_videos]
    finally:
        try:
            topic_page.remove_listener("response", on_search_response)
        except Exception:
            pass


def _collect_by_click_fallback(
    topic_page: Page,
    seen: Set[str],
    ordered: List[str],
    max_videos: int,
) -> None:
    # Click likely card centers in the masonry grid.
    x_points = [350, 620, 890, 1160]
    y_points = [430, 760]
    start_url = topic_page.url

    for y in y_points:
        for x in x_points:
            if len(ordered) >= max_videos:
                return
            try:
                href_at_point = topic_page.evaluate(
                    """
                    ({x, y}) => {
                      const el = document.elementFromPoint(x, y);
                      if (!el) return '';
                      const direct = el.closest && el.closest('a[href]');
                      if (direct) return direct.getAttribute('href') || '';
                      if (el.querySelector) {
                        const nested = el.querySelector('a[href]');
                        if (nested) return nested.getAttribute('href') || '';
                      }
                      return '';
                    }
                    """,
                    {"x": x, "y": y},
                )
                if isinstance(href_at_point, str) and href_at_point:
                    candidate = normalize_content_url(href_at_point)
                    if candidate and candidate not in seen:
                        seen.add(candidate)
                        ordered.append(candidate)
                        continue

                pages_before = len(_automation_pages(topic_page.context))
                topic_page.mouse.click(x, y, delay=40)
                topic_page.wait_for_timeout(1100)

                # Some cards may open in new tab/page; close it after extracting URL.
                automation_pages_after = _automation_pages(topic_page.context)
                pages_after = len(automation_pages_after)
                target_page = topic_page
                if pages_after > pages_before:
                    target_page = automation_pages_after[-1]
                    try:
                        target_page.bring_to_front()
                    except Exception:
                        pass

                candidate = normalize_content_url(target_page.url)
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    ordered.append(candidate)

                if target_page is not topic_page:
                    try:
                        target_page.close()
                    except Exception:
                        pass
                    try:
                        topic_page.bring_to_front()
                    except Exception:
                        pass
                else:
                    # Same-tab navigation case.
                    if topic_page.url != start_url:
                        try:
                            topic_page.go_back(wait_until="domcontentloaded", timeout=20_000)
                            topic_page.wait_for_timeout(700)
                        except Exception:
                            try:
                                topic_page.goto(start_url, wait_until="domcontentloaded", timeout=20_000)
                                topic_page.wait_for_timeout(700)
                            except Exception:
                                pass
                close_extra_blank_pages(topic_page.context, topic_page)
            except Exception:
                continue


def iter_comment_objects(node: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(node, dict):
        comments = node.get("comments")
        if isinstance(comments, list):
            for item in comments:
                if isinstance(item, dict):
                    yield item
        for value in node.values():
            yield from iter_comment_objects(value)
    elif isinstance(node, list):
        for item in node:
            yield from iter_comment_objects(item)


def extract_comment_record(
    *,
    topic_url: str,
    video_url: str,
    aweme_id: str,
    comment_obj: Dict[str, Any],
) -> Optional[CommentRecord]:
    text = sanitize_text(comment_obj.get("text") or comment_obj.get("content"))
    if not text:
        return None

    comment_id = str(comment_obj.get("cid") or comment_obj.get("comment_id") or "").strip()

    user = comment_obj.get("user") if isinstance(comment_obj.get("user"), dict) else {}
    nickname = sanitize_text(user.get("nickname"))
    uid = str(user.get("uid") or "").strip()
    sec_uid = str(user.get("sec_uid") or "").strip()
    unique_id = str(user.get("unique_id") or user.get("short_id") or "").strip()

    ip_location = normalize_ip_label(
        comment_obj.get("ip_label")
        or comment_obj.get("ip_location")
        or comment_obj.get("ip_label_text")
        or user.get("ip_label")
        or ""
    )

    raw_created = (
        comment_obj.get("create_time")
        or comment_obj.get("create_time_ms")
        or comment_obj.get("create_timestamp")
        or 0
    )
    created_unix = parse_unix_timestamp(raw_created)
    created_at = format_timestamp(created_unix)

    user_home = f"https://www.douyin.com/user/{sec_uid}" if sec_uid else ""

    return CommentRecord(
        topic_url=topic_url,
        video_url=video_url,
        aweme_id=aweme_id,
        comment_id=comment_id,
        ip_location=ip_location,
        nickname=nickname,
        uid=uid,
        sec_uid=sec_uid,
        unique_id=unique_id,
        user_home=user_home,
        text=text,
        created_at=created_at,
        created_unix=created_unix,
    )


def scrape_comments_from_video(
    context: BrowserContext,
    topic_url: str,
    video_url: str,
    comment_scroll_rounds: int,
    deadline: Optional[float] = None,
    captcha_wait_seconds: int = 120,
    stop_flag_path: Optional[Path] = None,
) -> List[CommentRecord]:
    page = context.new_page()
    aweme_id = extract_aweme_id(video_url)

    captured: List[CommentRecord] = []
    seen_keys: Set[Tuple[str, str, str]] = set()

    def on_response(resp: Any) -> None:
        url = resp.url
        if "comment/list" not in url:
            return
        if "douyin.com" not in url:
            return
        try:
            data = resp.json()
        except Exception:
            return

        for item in iter_comment_objects(data):
            record = extract_comment_record(
                topic_url=topic_url,
                video_url=video_url,
                aweme_id=aweme_id,
                comment_obj=item,
            )
            if not record:
                continue
            key = (record.comment_id, record.nickname, record.text)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            captured.append(record)

    try:
        page.on("response", on_response)
        page.goto(video_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(1600)
        if not wait_for_captcha_clear(page, captcha_wait_seconds, stop_flag_path):
            return captured

        opened = open_comment_panel(page)
        if not opened:
            print("  -> warning: comment panel not explicitly opened, fallback to generic scrolling")

        for _ in range(comment_scroll_rounds):
            stop_reason = get_stop_reason(deadline, stop_flag_path)
            if stop_reason:
                print(f"Stop during comment scrolling: {stop_reason}.")
                break
            scrolled_panel = scroll_comment_area_once(page)
            if not scrolled_panel:
                page.mouse.move(1200, 680)
                page.mouse.wheel(0, 1200)
            page.wait_for_timeout(700)

        page.wait_for_timeout(1200)
        return captured
    finally:
        try:
            page.close()
        except Exception:
            pass


def dedupe_records(records: List[CommentRecord]) -> List[CommentRecord]:
    seen: Set[Tuple[str, str, str]] = set()
    out: List[CommentRecord] = []
    for r in records:
        key = (r.video_url, r.comment_id, r.text)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def extract_sec_uid_from_user_home(user_home: str) -> str:
    raw = sanitize_text(user_home)
    if not raw:
        return ""
    try:
        parsed = urlparse(raw if raw.startswith("http") else f"https://www.douyin.com{raw}")
        path = unquote(parsed.path or "")
    except Exception:
        path = raw
    m = USER_HOME_PATH_RE.search(path)
    if not m:
        return ""
    return sanitize_text(m.group("id"))


def build_commenter_dedupe_key(
    *,
    sec_uid: str = "",
    uid: str = "",
    unique_id: str = "",
    user_home: str = "",
    nickname: str = "",
) -> str:
    sec = sanitize_text(sec_uid) or extract_sec_uid_from_user_home(user_home)
    if sec:
        return f"sec_uid:{sec}"
    uid_s = sanitize_text(uid)
    if uid_s:
        return f"uid:{uid_s}"
    unique_id_s = sanitize_text(unique_id)
    if unique_id_s:
        return f"unique_id:{unique_id_s}"
    user_home_s = sanitize_text(user_home)
    if user_home_s:
        return f"user_home:{user_home_s}"
    nickname_s = sanitize_text(nickname)
    if nickname_s:
        return f"nickname:{nickname_s}"
    return ""


def commenter_dedupe_key(r: CommentRecord) -> str:
    return build_commenter_dedupe_key(
        sec_uid=r.sec_uid,
        uid=r.uid,
        unique_id=r.unique_id,
        user_home=r.user_home,
        nickname=r.nickname,
    )


def dedupe_by_nickname(records: List[CommentRecord]) -> List[CommentRecord]:
    # Kept function name for compatibility; actual dedupe key is account identity.
    seen: Set[str] = set()
    out: List[CommentRecord] = []
    for r in records:
        key = commenter_dedupe_key(r)
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def record_to_dict(r: CommentRecord) -> Dict[str, Any]:
    return {
        "topic_url": r.topic_url,
        "video_url": r.video_url,
        "aweme_id": r.aweme_id,
        "comment_id": r.comment_id,
        "ip_location": r.ip_location,
        "nickname": r.nickname,
        "uid": r.uid,
        "sec_uid": r.sec_uid,
        "unique_id": r.unique_id,
        "user_home": r.user_home,
        "text": r.text,
        "created_at": r.created_at,
        "created_unix": r.created_unix,
    }


def dict_to_record(d: Dict[str, Any]) -> Optional[CommentRecord]:
    if not isinstance(d, dict):
        return None
    nickname = sanitize_text(d.get("nickname"))
    text = sanitize_text(d.get("text"))
    if not nickname or not text:
        return None
    return CommentRecord(
        topic_url=sanitize_text(d.get("topic_url")),
        video_url=sanitize_text(d.get("video_url")),
        aweme_id=sanitize_text(d.get("aweme_id")),
        comment_id=sanitize_text(d.get("comment_id")),
        ip_location=sanitize_text(d.get("ip_location")),
        nickname=nickname,
        uid=sanitize_text(d.get("uid")),
        sec_uid=sanitize_text(d.get("sec_uid")),
        unique_id=sanitize_text(d.get("unique_id")),
        user_home=sanitize_text(d.get("user_home")),
        text=text,
        created_at=sanitize_text(d.get("created_at")),
        created_unix=parse_unix_timestamp(d.get("created_unix") or 0),
    )


def load_store_records(store_path: Path) -> List[CommentRecord]:
    if not store_path.exists():
        return []
    out: List[CommentRecord] = []
    for line in store_path.read_text(encoding="utf-8").splitlines():
        row = line.strip()
        if not row:
            continue
        try:
            obj = json.loads(row)
        except Exception:
            continue
        record = dict_to_record(obj)
        if record is not None:
            out.append(record)
    return dedupe_by_nickname(out)


def save_store_records(store_path: Path, records: List[CommentRecord]) -> None:
    unique_records = dedupe_by_nickname(records)
    lines = [json.dumps(record_to_dict(r), ensure_ascii=False) for r in unique_records]
    store_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def merge_unique_by_nickname(
    existing: List[CommentRecord],
    incoming: List[CommentRecord],
) -> List[CommentRecord]:
    merged: List[CommentRecord] = []
    seen: Set[str] = set()
    for r in existing + incoming:
        key = commenter_dedupe_key(r)
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        merged.append(r)
    return merged


def build_run_entry(
    *,
    run_started_at: str,
    topic_keyword: str,
    topic_url: str,
    scanned_count: int,
    run_total_count: int,
    run_filtered_count: int,
    dedupe_keys: List[str],
    records: Optional[List[CommentRecord]] = None,
) -> Dict[str, Any]:
    history_records: List[Dict[str, Any]] = []
    if records:
        history_records = [record_to_dict(r) for r in dedupe_by_nickname(records)]
    return {
        "run_started_at": sanitize_text(run_started_at),
        "topic_keyword": sanitize_text(topic_keyword),
        "topic_url": sanitize_text(topic_url),
        "scanned_count": int(scanned_count),
        "run_total_count": int(run_total_count),
        "run_filtered_count": int(run_filtered_count),
        "dedupe_keys": [sanitize_text(k) for k in dedupe_keys if sanitize_text(k)],
        "records": history_records,
    }


def parse_run_entry(obj: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(obj, dict):
        return None
    run_started_at = sanitize_text(obj.get("run_started_at"))
    topic_keyword = sanitize_text(obj.get("topic_keyword"))
    topic_url = sanitize_text(obj.get("topic_url"))
    try:
        scanned_count = int(obj.get("scanned_count") or 0)
        run_total_count = int(obj.get("run_total_count") or 0)
        run_filtered_count = int(obj.get("run_filtered_count") or 0)
    except Exception:
        scanned_count = 0
        run_total_count = 0
        run_filtered_count = 0

    dedupe_keys: List[str] = []
    keys_raw = obj.get("dedupe_keys")
    if isinstance(keys_raw, list):
        for k in keys_raw:
            ks = sanitize_text(k)
            if ks:
                dedupe_keys.append(ks)

    records: List[Dict[str, Any]] = []
    records_raw = obj.get("records")
    if isinstance(records_raw, list):
        for it in records_raw:
            if not isinstance(it, dict):
                continue
            r = dict_to_record(it)
            if r is None:
                continue
            records.append(record_to_dict(r))

    # Backward compatibility for history rows where only records or only keys exist.
    if not dedupe_keys and records:
        for it in records:
            key = build_commenter_dedupe_key(
                sec_uid=sanitize_text(it.get("sec_uid")),
                uid=sanitize_text(it.get("uid")),
                unique_id=sanitize_text(it.get("unique_id")),
                user_home=sanitize_text(it.get("user_home")),
                nickname=sanitize_text(it.get("nickname")),
            )
            if key:
                dedupe_keys.append(key)
    dedupe_keys = sorted(set(dedupe_keys))

    if records:
        by_key: Dict[str, Dict[str, Any]] = {}
        for it in records:
            key = build_commenter_dedupe_key(
                sec_uid=sanitize_text(it.get("sec_uid")),
                uid=sanitize_text(it.get("uid")),
                unique_id=sanitize_text(it.get("unique_id")),
                user_home=sanitize_text(it.get("user_home")),
                nickname=sanitize_text(it.get("nickname")),
            )
            if not key:
                continue
            if key not in by_key:
                by_key[key] = it
        records = [by_key[k] for k in sorted(by_key.keys())]

    return {
        "run_started_at": run_started_at,
        "topic_keyword": topic_keyword,
        "topic_url": topic_url,
        "scanned_count": scanned_count,
        "run_total_count": run_total_count,
        "run_filtered_count": run_filtered_count,
        "dedupe_keys": dedupe_keys,
        "records": records,
    }


def load_scan_history(history_path: Path) -> List[Dict[str, Any]]:
    if not history_path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for line in history_path.read_text(encoding="utf-8").splitlines():
        row = line.strip()
        if not row:
            continue
        try:
            obj = json.loads(row)
        except Exception:
            continue
        entry = parse_run_entry(obj)
        if entry is not None:
            out.append(entry)
    return out


def append_scan_history(history_path: Path, entry: Dict[str, Any]) -> None:
    clean = parse_run_entry(entry)
    if clean is None:
        return
    line = json.dumps(clean, ensure_ascii=False)
    with history_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def save_scan_history_entries(history_path: Path, entries: List[Dict[str, Any]]) -> None:
    lines: List[str] = []
    for it in entries:
        clean = parse_run_entry(it)
        if clean is None:
            continue
        lines.append(json.dumps(clean, ensure_ascii=False))
    history_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def refresh_history_records_from_store(
    history_path: Path,
    history_entries: List[Dict[str, Any]],
    store_records: List[CommentRecord],
) -> List[Dict[str, Any]]:
    if not history_entries:
        return history_entries
    if not store_records:
        return history_entries

    key_to_record: Dict[str, Dict[str, Any]] = {}
    for r in store_records:
        key = commenter_dedupe_key(r)
        if key and key not in key_to_record:
            key_to_record[key] = record_to_dict(r)

    changed = False
    refreshed: List[Dict[str, Any]] = []
    for raw in history_entries:
        entry = parse_run_entry(raw)
        if entry is None:
            continue
        records = entry.get("records") if isinstance(entry.get("records"), list) else []
        if not records:
            keys = entry.get("dedupe_keys") if isinstance(entry.get("dedupe_keys"), list) else []
            recovered: List[Dict[str, Any]] = []
            for k in keys:
                row = key_to_record.get(sanitize_text(k))
                if row is not None:
                    recovered.append(row)
            if recovered:
                entry["records"] = recovered
                changed = True
        refreshed.append(entry)

    if changed:
        save_scan_history_entries(history_path, refreshed)

    return refreshed


def dedupe_history_entries_across_runs(
    history_path: Path,
    history_entries: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not history_entries:
        return history_entries

    seen_global: Set[str] = set()
    normalized: List[Dict[str, Any]] = []
    changed = False

    for raw in history_entries:
        entry = parse_run_entry(raw)
        if entry is None:
            continue

        keys_raw = entry.get("dedupe_keys") if isinstance(entry.get("dedupe_keys"), list) else []
        keys = [sanitize_text(k) for k in keys_raw if sanitize_text(k)]

        records_raw = entry.get("records") if isinstance(entry.get("records"), list) else []
        record_by_key: Dict[str, Dict[str, Any]] = {}
        for it in records_raw:
            if not isinstance(it, dict):
                continue
            key = history_record_dedupe_key(it)
            if key and key not in record_by_key:
                record_by_key[key] = it

        new_keys: List[str] = []
        new_records: List[Dict[str, Any]] = []

        for k in keys:
            if k in seen_global:
                continue
            seen_global.add(k)
            new_keys.append(k)
            row = record_by_key.get(k)
            if row is not None:
                new_records.append(row)

        for k, row in record_by_key.items():
            if k in seen_global:
                continue
            seen_global.add(k)
            new_keys.append(k)
            new_records.append(row)

        old_count = int(entry.get("run_filtered_count") or 0)
        if old_count != len(new_keys):
            changed = True
        if keys != new_keys:
            changed = True
        if len(records_raw) != len(new_records):
            changed = True

        entry["dedupe_keys"] = new_keys
        entry["records"] = new_records
        entry["run_filtered_count"] = len(new_keys)
        normalized.append(entry)

    if changed:
        save_scan_history_entries(history_path, normalized)
    return normalized


def filter_by_ip(records: List[CommentRecord], target_ips: Set[str]) -> List[CommentRecord]:
    if not target_ips:
        return records
    out: List[CommentRecord] = []
    for r in records:
        if normalize_ip_label(r.ip_location) in target_ips:
            out.append(r)
    return out


def filter_by_age(records: List[CommentRecord], max_age_days: int) -> List[CommentRecord]:
    if max_age_days <= 0:
        return records
    cutoff_unix = int(datetime.now().timestamp()) - max_age_days * 24 * 3600
    out: List[CommentRecord] = []
    for r in records:
        # If time is missing/invalid, skip to keep only recent comments.
        if r.created_unix <= 0:
            continue
        if r.created_unix >= cutoff_unix:
            out.append(r)
    return out


def escape_md(text: str) -> str:
    return (
        sanitize_text(text)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def history_record_dedupe_key(row: Dict[str, Any]) -> str:
    return build_commenter_dedupe_key(
        sec_uid=sanitize_text(row.get("sec_uid")),
        uid=sanitize_text(row.get("uid")),
        unique_id=sanitize_text(row.get("unique_id")),
        user_home=sanitize_text(row.get("user_home")),
        nickname=sanitize_text(row.get("nickname")),
    )


def write_markdown(
    output_path: Path,
    topic_url: str,
    sort_mode: str,
    target_ips: Set[str],
    scanned_video_urls: List[str],
    all_records: List[CommentRecord],
    current_filtered_records: List[CommentRecord],
    global_records: List[CommentRecord],
    max_comment_age_days: int,
    history_entries: Optional[List[Dict[str, Any]]] = None,
    current_run_entry: Optional[Dict[str, Any]] = None,
    expand_last_history: bool = False,
) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines: List[str] = []

    lines.append("# 抖音评论去重汇总")
    lines.append("")
    lines.append(f"- 生成时间: `{now}`")
    lines.append(f"- 最近一次运行话题链接: `{topic_url}`")
    lines.append(f"- 最近一次运行排序模式: `{sort_mode}`")
    lines.append(f"- 筛选IP: `{','.join(sorted(target_ips)) if target_ips else 'none'}`")
    if max_comment_age_days > 0:
        lines.append(f"- 评论时间范围: `近{max_comment_age_days}天`")
    else:
        lines.append("- 评论时间范围: `未限制`")
    lines.append(f"- 本次扫描内容数: `{len(scanned_video_urls)}`")
    lines.append(f"- 本次抓取评论总数: `{len(all_records)}`")
    lines.append(f"- 本次筛出评论者数(按昵称去重): `{len(current_filtered_records)}`")
    lines.append(f"- 全局去重后评论者数(按账号ID): `{len(global_records)}`")
    lines.append("")

    lines.append("## 本次筛出明细")
    lines.append("")
    lines.append("| 昵称 | IP | 时间 | 账号链接 | 评论内容 |")
    lines.append("|---|---|---|---|---|")

    for r in current_filtered_records:
        user_home = f"[主页]({r.user_home})" if r.user_home else ""
        lines.append(
            "| "
            + " | ".join(
                [
                    escape_md(r.nickname),
                    escape_md(r.ip_location),
                    escape_md(r.created_at),
                    user_home,
                    escape_md(r.text),
                ]
            )
            + " |"
        )

    lines.append("")

    run_blocks: List[Dict[str, Any]] = []
    run_sources: List[str] = []
    if history_entries:
        for item in history_entries:
            entry = parse_run_entry(item)
            if entry is not None:
                run_blocks.append(entry)
                run_sources.append("history")
    if current_run_entry:
        entry = parse_run_entry(current_run_entry)
        if entry is not None:
            run_blocks.append(entry)
            run_sources.append("current")

    if run_blocks:
        lines.append("## 分次扫描结果")
        lines.append("")
        if current_run_entry is not None:
            lines.append("历史批次已压缩为摘要；当前运行批次保留完整去重ID键。")
        elif expand_last_history:
            lines.append("历史批次已压缩为摘要；最新完成批次保留完整去重ID键。")
        else:
            lines.append("历史批次已压缩为摘要，仅保留最少去重信息（账号ID键）。")
        lines.append("")
        for idx, run in enumerate(run_blocks, start=1):
            lines.append(f"### 批次 {idx}")
            lines.append(f"- 开始时间: `{escape_md(run.get('run_started_at') or '')}`")
            lines.append(f"- 关键词: `{escape_md(run.get('topic_keyword') or '')}`")
            lines.append(f"- 入口: `{escape_md(run.get('topic_url') or '')}`")
            lines.append(f"- 扫描内容数: `{int(run.get('scanned_count') or 0)}`")
            lines.append(f"- 本批新增去重人数: `{int(run.get('run_filtered_count') or 0)}`")
            keys = run.get("dedupe_keys") if isinstance(run.get("dedupe_keys"), list) else []
            lines.append(f"- 去重ID键数量: `{len(keys)}`")
            run_records = run.get("records") if isinstance(run.get("records"), list) else []
            is_current = run_sources[idx - 1] == "current"
            is_last_history = (
                run_sources[idx - 1] == "history"
                and idx == len(run_blocks)
                and current_run_entry is None
            )
            show_all_keys = is_current or (expand_last_history and is_last_history)
            if keys and show_all_keys:
                lines.append("- 去重ID键(全部):")
                for k in keys:
                    lines.append(f"  - `{escape_md(str(k))}`")
            elif keys:
                sample = ", ".join([f"`{escape_md(str(k))}`" for k in keys[:5]])
                lines.append(f"- ID样本(前5): {sample}")
            if run_records and show_all_keys:
                lines.append("- 本批完整详情:")
                lines.append("")
                lines.append("| 昵称 | IP | 时间 | 账号链接 | 评论内容 | ID键 |")
                lines.append("|---|---|---|---|---|---|")
                for it in run_records:
                    if not isinstance(it, dict):
                        continue
                    nick = escape_md(str(it.get("nickname") or ""))
                    ip = escape_md(str(it.get("ip_location") or ""))
                    created_at = escape_md(str(it.get("created_at") or ""))
                    home = str(it.get("user_home") or "").strip()
                    user_home = f"[主页]({home})" if home else ""
                    text = escape_md(str(it.get("text") or ""))
                    key = escape_md(history_record_dedupe_key(it))
                    lines.append(
                        f"| {nick} | {ip} | {created_at} | {user_home} | {text} | `{key}` |"
                    )
            elif show_all_keys:
                lines.append("- 本批完整详情: `无`")
            lines.append("")
            lines.append("---")
            lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")


def persist_progress_markdown(
    *,
    output_path: Path,
    store_path: Path,
    history_entries: List[Dict[str, Any]],
    run_started_at: str,
    topic_keyword: str,
    topic_url: str,
    sort_mode: str,
    target_ips: Set[str],
    scanned_video_urls: List[str],
    all_records: List[CommentRecord],
    global_records: List[CommentRecord],
    run_baseline_keys: Set[str],
    max_comment_age_days: int,
) -> Tuple[int, int, int, Dict[str, Any]]:
    deduped_all = dedupe_records(all_records)
    run_filtered = filter_by_ip(deduped_all, target_ips)
    run_filtered = filter_by_age(run_filtered, max_comment_age_days)
    run_filtered_unique = dedupe_by_nickname(run_filtered)

    existing_keys: Set[str] = set(run_baseline_keys)
    run_new_unique: List[CommentRecord] = []
    run_seen: Set[str] = set()
    for r in run_filtered_unique:
        key = commenter_dedupe_key(r)
        if not key:
            continue
        if key in existing_keys or key in run_seen:
            continue
        run_seen.add(key)
        run_new_unique.append(r)
    run_dedupe_keys = sorted(run_seen)

    merged_global = merge_unique_by_nickname(global_records, run_new_unique)
    global_records.clear()
    global_records.extend(merged_global)
    save_store_records(store_path, global_records)

    current_run_entry = build_run_entry(
        run_started_at=run_started_at,
        topic_keyword=topic_keyword,
        topic_url=topic_url,
        scanned_count=len(scanned_video_urls),
        run_total_count=len(deduped_all),
        run_filtered_count=len(run_new_unique),
        dedupe_keys=run_dedupe_keys,
        records=run_new_unique,
    )

    write_markdown(
        output_path=output_path,
        topic_url=topic_url,
        sort_mode=sort_mode,
        target_ips=target_ips,
        scanned_video_urls=scanned_video_urls,
        all_records=deduped_all,
        current_filtered_records=run_filtered_unique,
        global_records=global_records,
        max_comment_age_days=max_comment_age_days,
        history_entries=history_entries,
        current_run_entry=current_run_entry,
    )
    return len(deduped_all), len(run_new_unique), len(global_records), current_run_entry


def main() -> int:
    args = parse_args()

    entry_url = normalize_topic_url(args.topic_url)
    validate_topic_domain(entry_url)
    topic_keyword = (args.topic_keyword or "").strip()
    if not topic_keyword:
        topic_keyword = extract_topic_keyword_from_url(entry_url) or extract_search_keyword_from_url(entry_url)
    if not topic_keyword and not args.feed_mode:
        raise ValueError(
            "Please provide --topic-keyword."
        )
    if not topic_keyword:
        topic_keyword = "推荐流"

    target_ips = parse_target_ips(args.ips)
    output_path = Path(args.output)
    store_path = Path(args.store_file)
    history_path = Path(args.history_file)
    stop_flag_path = Path(args.stop_flag_file) if (args.stop_flag_file or "").strip() else None
    open_url_request_path = (
        Path(args.open_url_request_file) if (args.open_url_request_file or "").strip() else None
    )
    resolved_topic_url = entry_url
    entry_path = unquote(urlparse(entry_url).path or "")
    homepage_entry = entry_path in {"", "/"}
    use_search_mode = homepage_entry or "/search/" in entry_path
    global_records = load_store_records(store_path)
    run_baseline_keys: Set[str] = {commenter_dedupe_key(r) for r in global_records if commenter_dedupe_key(r)}
    history_entries = load_scan_history(history_path)
    history_entries = refresh_history_records_from_store(history_path, history_entries, global_records)
    history_entries = dedupe_history_entries_across_runs(history_path, history_entries)
    run_started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print("Entry URL:", entry_url)
    print("Mode:", "recommend-feed" if args.feed_mode else "search/hashtag")
    print("Topic keyword:", topic_keyword)
    print("Sort:", args.sort)
    print("IP filter:", ",".join(sorted(target_ips)) if target_ips else "none")
    if args.max_comment_age_days > 0:
        print("Comment age filter:", f"last {args.max_comment_age_days} days")
    else:
        print("Comment age filter: disabled")
    if args.max_run_minutes > 0:
        print("Run time limit:", f"{args.max_run_minutes} minutes")
    else:
        print("Run time limit: disabled")
    print("Ready timeout:", f"{args.ready_timeout_seconds}s")
    print("Global unique store:", str(store_path))
    print("Loaded global unique commenters:", len(global_records))
    print("Run history file:", str(history_path))
    print("Loaded history batches:", len(history_entries))
    if stop_flag_path is not None:
        print("Manual stop flag file:", str(stop_flag_path))
        print(f"Manual stop usage: create file `{stop_flag_path}` while running.")
        try:
            if stop_flag_path.exists():
                stop_flag_path.unlink()
                print("Removed stale manual stop flag before run.")
        except Exception as e:
            print(f"Warning: failed to clear stale stop flag: {e}")

    deadline: Optional[float] = None
    if args.max_run_minutes > 0:
        deadline = time.monotonic() + args.max_run_minutes * 60

    video_urls: List[str] = []
    all_records: List[CommentRecord] = []
    last_run_entry: Dict[str, Any] = {}

    with sync_playwright() as p:
        context: Optional[BrowserContext] = None
        topic_page: Optional[Page] = None
        try:
            killed = ensure_profile_free(args.profile_dir)
            if killed:
                print(f"Cleared leftover browser processes holding the profile dir: {sorted(set(killed))}")
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
                print(f"Browser launch failed ({launch_err}); cleaning profile holders and retrying once...")
                ensure_profile_free(args.profile_dir)
                context = p.chromium.launch_persistent_context(**launch_kwargs)
            configure_open_url_requests(context, open_url_request_path)

            topic_page = pick_primary_page(context)
            if topic_page is None:
                topic_page = context.new_page()
            topic_page = recover_topic_page(context, topic_page, entry_url)

            current_url = (topic_page.url or "")
            should_navigate = True
            if use_search_mode and not homepage_entry and "douyin.com/search" in current_url.lower():
                current_kw = extract_search_keyword_from_url(current_url)
                if current_kw == topic_keyword:
                    print("Reusing existing search tab with matching keyword.")
                    should_navigate = False
                else:
                    print(
                        "Existing search tab keyword mismatch, navigate to target URL: "
                        f"current='{current_kw or 'unknown'}' target='{topic_keyword}'"
                    )

            if should_navigate:
                topic_page.goto(entry_url, wait_until="domcontentloaded", timeout=60_000)
            topic_page = recover_topic_page(context, topic_page, entry_url)
            wait_for_manual_ready(args.pause_seconds)
            if not wait_for_captcha_clear(topic_page, args.captcha_wait_seconds, stop_flag_path):
                raise RuntimeError("Captcha unresolved on entry page.")
            if args.feed_mode:
                print("Feed mode: waiting for the homepage recommend feed to be ready...")
                if not wait_for_feed_ready(topic_page, max(10, args.ready_timeout_seconds)):
                    raise RuntimeError(
                        f"Recommend feed not ready within {args.ready_timeout_seconds}s. "
                        "Please make sure the logged-in homepage shows the video feed and retry."
                    )
            elif use_search_mode:
                print("Clicking page search and entering the requested keyword...")
                if not ensure_search_keyword_on_page(topic_page, topic_keyword):
                    raise RuntimeError("Search submission failed. Check the homepage search field and retry.")
                if not wait_for_captcha_clear(topic_page, args.captcha_wait_seconds, stop_flag_path):
                    raise RuntimeError("Captcha unresolved after search submission.")
                if not wait_for_search_page_ready(topic_page, args.ready_timeout_seconds):
                    raise RuntimeError(
                        f"Search page not ready within {args.ready_timeout_seconds}s. "
                        "Check VPN/network and try again."
                    )
            elif not wait_for_search_page_ready(topic_page, args.ready_timeout_seconds):
                raise RuntimeError(
                    f"Search page not ready within {args.ready_timeout_seconds}s. "
                    "Check VPN/network and try again."
                )

            if args.feed_mode:
                resolved_topic_url = topic_page.url
                print("Recommend feed ready:", resolved_topic_url)
            elif use_search_mode:
                print("Search mode enabled: stay on search results page (no hashtag navigation).")
                print(f"Search keyword confirmed: {topic_keyword}")
                prepare_search_video_page(topic_page)
                topic_page = recover_topic_page(context, topic_page, entry_url)
                if not wait_for_search_cards_ready(topic_page, timeout_seconds=max(12, args.ready_timeout_seconds)):
                    print("Warning: search cards not fully ready yet; continue with click-retry flow.")
                resolved_topic_url = topic_page.url
            else:
                current_path = unquote(urlparse(topic_page.url).path or "")
                if "/search/" in current_path:
                    print("Detected search page, resolving to hashtag page...")
                    resolved_topic_url = switch_to_topic_page_from_search(topic_page, topic_keyword)
                else:
                    resolved_topic_url = topic_page.url

                ensure_topic_page_valid(topic_page, topic_keyword)
                resolved_topic_url = topic_page.url
                print("Resolved hashtag page:", resolved_topic_url)

                if args.sort == "latest":
                    clicked = select_latest_sort(topic_page)
                    if not clicked:
                        print("Warning: Could not auto-click 'latest'. Please switch manually and rerun.")
                    else:
                        print("Sort switched to latest.")

            def persist_now(tag: str) -> None:
                nonlocal last_run_entry
                total_count, filtered_count, global_count, run_entry = persist_progress_markdown(
                    output_path=output_path,
                    store_path=store_path,
                    history_entries=history_entries,
                    run_started_at=run_started_at,
                    topic_keyword=topic_keyword,
                    topic_url=resolved_topic_url,
                    sort_mode=args.sort,
                    target_ips=target_ips,
                    scanned_video_urls=video_urls,
                    all_records=all_records,
                    global_records=global_records,
                    run_baseline_keys=run_baseline_keys,
                    max_comment_age_days=args.max_comment_age_days,
                )
                last_run_entry = run_entry
                print(
                    f"Progress saved ({tag}): "
                    f"scanned={len(video_urls)} total={total_count} run_filtered={filtered_count} global_unique={global_count}"
                )

            # Reset output at run start so old records are cleared immediately.
            persist_now("init")
            process_open_url_requests()

            print("Processing contents from first card onward...")

            if args.feed_mode:
                print("Feed mode: opening comments for each recommended video...")
                stalled_rounds = 0
                while len(video_urls) < max(1, args.max_videos):
                    topic_page = recover_topic_page(context, topic_page, entry_url)
                    stop_reason = get_stop_reason(deadline, stop_flag_path)
                    if stop_reason:
                        print(f"Stop before next feed video: {stop_reason}.")
                        break
                    if not wait_for_captcha_clear(topic_page, args.captcha_wait_seconds, stop_flag_path):
                        print("Captcha unresolved on feed page, stop this run.")
                        break

                    if not is_feed_video_present(topic_page):
                        stalled_rounds += 1
                        if stalled_rounds >= 6:
                            print("Recommend feed not showing videos for several rounds, stop this run.")
                            break
                        topic_page.wait_for_timeout(1200)
                        continue

                    idx = len(video_urls) + 1
                    active_aweme = get_feed_active_aweme_id(topic_page)
                    print(
                        f"[{idx}] Scraping comments from recommend feed video"
                        + (f": https://www.douyin.com/video/{active_aweme}" if active_aweme else " (id unknown yet)")
                    )
                    video_url = f"https://www.douyin.com/video/{active_aweme}" if active_aweme else ""
                    try:
                        comments, response_aweme = scrape_comments_from_feed_video(
                            page=topic_page,
                            topic_url=entry_url,
                            comment_scroll_rounds=max(1, args.comment_scroll_rounds),
                            deadline=deadline,
                            captcha_wait_seconds=max(30, args.captcha_wait_seconds),
                            stop_flag_path=stop_flag_path,
                        )
                        if response_aweme and not video_url:
                            video_url = f"https://www.douyin.com/video/{response_aweme}"
                        print(f"  -> fetched {len(comments)} comments")
                        all_records.extend(comments)
                    except Exception as e:
                        print(f"  -> failed: {e}")
                    if not video_url:
                        video_url = build_fallback_video_url(topic_page, idx)
                    video_urls.append(video_url)
                    stalled_rounds = 0
                    persist_now(f"feed {len(video_urls)}")

                    if len(video_urls) >= max(1, args.max_videos):
                        break
                    if get_stop_reason(deadline, stop_flag_path):
                        break

                    if not advance_feed_video(topic_page):
                        stalled_rounds += 1
                        print(f"  -> feed did not advance (attempt {stalled_rounds}/5).")
                        if stalled_rounds >= 5:
                            print(
                                "Cannot advance the recommend feed, stop this run. "
                                "You may also swipe the browser window manually; "
                                "comments opened there are still captured."
                            )
                            break
                    else:
                        stalled_rounds = 0
            elif use_search_mode:
                opened_page = open_first_search_card_with_retry(
                    topic_page,
                    max_attempts=max(3, min(10, args.topic_scroll_rounds)),
                    deadline=deadline,
                    stop_flag_path=stop_flag_path,
                )
                if opened_page is None:
                    stop_reason = get_stop_reason(deadline, stop_flag_path)
                    if stop_reason:
                        print(f"Stop before entering first content: {stop_reason}.")
                        opened_page = topic_page
                    else:
                        raise RuntimeError(
                            "Cannot open first search result card. "
                            "Please ensure search results are visible on page and rerun."
                        )
                elif is_live_content_page(opened_page):
                    opened_page = recover_search_page_from_live(
                        context=context,
                        page=opened_page,
                        entry_url=entry_url,
                        topic_keyword=topic_keyword,
                        ready_timeout_seconds=args.ready_timeout_seconds,
                    )
                    reopened_first = open_first_search_card_with_retry(
                        opened_page,
                        max_attempts=max(2, min(8, args.topic_scroll_rounds)),
                        deadline=deadline,
                        stop_flag_path=stop_flag_path,
                    )
                    if reopened_first is not None:
                        opened_page = reopened_first
                topic_page = opened_page

                seen_urls: Set[str] = set()
                stalled_rounds = 0
                while len(video_urls) < max(1, args.max_videos):
                    topic_page = recover_topic_page(context, topic_page, entry_url)
                    if is_live_content_page(topic_page):
                        topic_page = recover_search_page_from_live(
                            context=context,
                            page=topic_page,
                            entry_url=entry_url,
                            topic_keyword=topic_keyword,
                            ready_timeout_seconds=args.ready_timeout_seconds,
                        )
                    stop_reason = get_stop_reason(deadline, stop_flag_path)
                    if stop_reason:
                        print(f"Stop before next content: {stop_reason}.")
                        break
                    if not wait_for_captcha_clear(topic_page, args.captcha_wait_seconds, stop_flag_path):
                        print("Captcha unresolved on content page, stop this run.")
                        break

                    current_path = unquote(urlparse(topic_page.url).path or "")
                    if "/search/" in current_path and not extract_content_url_from_page_url(topic_page.url):
                        reopened = open_next_unseen_search_card(
                            topic_page,
                            seen_urls,
                            deadline,
                            stop_flag_path,
                        )
                        if reopened is not None:
                            topic_page = reopened
                        else:
                            stalled_rounds += 1
                            if stalled_rounds >= 5:
                                print("Stayed on search page too long without opening content, stop this run.")
                                break
                            topic_page.wait_for_timeout(900)
                            continue

                    current_url = extract_content_url_from_page_url(topic_page.url)
                    if not current_url:
                        if is_live_content_page(topic_page):
                            topic_page = recover_search_page_from_live(
                                context=context,
                                page=topic_page,
                                entry_url=entry_url,
                                topic_keyword=topic_keyword,
                                ready_timeout_seconds=args.ready_timeout_seconds,
                            )
                            stalled_rounds = 0
                            continue

                        # Not live: try opening comment panel first before giving up.
                        comment_opened = open_comment_panel(topic_page)
                        if comment_opened:
                            current_url = build_fallback_video_url(topic_page, len(video_urls) + 1)
                            print("No modal URL but comment panel opened, continue scraping this content.")
                        else:
                            # If modal disappeared, try reopen from search grid once.
                            reopened = open_next_unseen_search_card(
                                topic_page,
                                seen_urls,
                                deadline,
                                stop_flag_path,
                            )
                            if reopened is None:
                                stalled_rounds += 1
                                if stalled_rounds >= 5:
                                    print("Cannot stay in content modal, stop this run.")
                                    break
                                topic_page.wait_for_timeout(900)
                                continue
                            topic_page = reopened
                            current_url = extract_content_url_from_page_url(topic_page.url)

                    if not current_url:
                        stalled_rounds += 1
                        if stalled_rounds >= 5:
                            print("No valid content URL in modal, stop this run.")
                            break
                        continue

                    if current_url in seen_urls:
                        moved = go_next_modal_content(topic_page)
                        if not moved:
                            reopened = open_next_unseen_search_card(
                                topic_page,
                                seen_urls,
                                deadline,
                                stop_flag_path,
                            )
                            if reopened is not None:
                                topic_page = reopened
                                stalled_rounds = 0
                                continue
                            stalled_rounds += 1
                            if stalled_rounds >= 5:
                                print("Cannot move to next content, stop this run.")
                                break
                            topic_page.wait_for_timeout(900)
                            continue
                        continue

                    stalled_rounds = 0
                    seen_urls.add(current_url)
                    video_urls.append(current_url)
                    idx = len(video_urls)
                    print(f"[{idx}] Scraping comments from current modal: {current_url}")
                    try:
                        comments = scrape_comments_from_current_content(
                            page=topic_page,
                            topic_url=resolved_topic_url,
                            video_url=current_url,
                            comment_scroll_rounds=max(1, args.comment_scroll_rounds),
                            deadline=deadline,
                            captcha_wait_seconds=max(30, args.captcha_wait_seconds),
                            stop_flag_path=stop_flag_path,
                        )
                        print(f"  -> fetched {len(comments)} comments")
                        all_records.extend(comments)
                    except Exception as e:
                        print(f"  -> failed: {e}")
                    finally:
                        persist_now(f"content {idx}")

                    if len(video_urls) >= max(1, args.max_videos):
                        break
                    if get_stop_reason(deadline, stop_flag_path):
                        break

                    moved = go_next_modal_content(topic_page)
                    if not moved:
                        print("Reached end of visible modal feed or next jump failed.")
                        break
            else:
                known_urls: Set[str] = set()
                idle_rounds = 0
                while len(video_urls) < max(1, args.max_videos):
                    topic_page = recover_topic_page(context, topic_page, entry_url)
                    stop_reason = get_stop_reason(deadline, stop_flag_path)
                    if stop_reason:
                        print(f"Stop before next content: {stop_reason}.")
                        break
                    if not wait_for_captcha_clear(topic_page, args.captcha_wait_seconds, stop_flag_path):
                        print("Captcha unresolved on topic page, stop this run.")
                        break

                    next_urls = collect_video_urls(
                        topic_page=topic_page,
                        max_videos=1,
                        scroll_rounds=max(1, min(2, args.topic_scroll_rounds)),
                        deadline=deadline,
                        known_urls=known_urls,
                        stop_at_first=True,
                        reset_to_top=(len(video_urls) == 0),
                    )

                    if not next_urls:
                        idle_rounds += 1
                        topic_page.mouse.move(1200, 700)
                        topic_page.mouse.wheel(0, 1800)
                        topic_page.wait_for_timeout(800)
                        if idle_rounds >= 6:
                            print("No new content found for several rounds, stop this run.")
                            break
                        continue

                    idle_rounds = 0
                    video_url = next_urls[0]
                    video_urls.append(video_url)
                    idx = len(video_urls)
                    print(f"[{idx}] Scraping comments from content: {video_url}")
                    try:
                        comments = scrape_comments_from_video(
                            context=context,
                            topic_url=resolved_topic_url,
                            video_url=video_url,
                            comment_scroll_rounds=max(1, args.comment_scroll_rounds),
                            deadline=deadline,
                            captcha_wait_seconds=max(30, args.captcha_wait_seconds),
                            stop_flag_path=stop_flag_path,
                        )
                        print(f"  -> fetched {len(comments)} comments")
                        all_records.extend(comments)
                    except Exception as e:
                        print(f"  -> failed: {e}")
                    finally:
                        persist_now(f"content {idx}")

            print("Processed contents:", len(video_urls))
        finally:
            configure_open_url_requests(None, None)
            if topic_page is not None:
                try:
                    if not topic_page.is_closed():
                        topic_page.close()
                except Exception:
                    pass
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass

    run_total, run_filtered, global_unique, final_run_entry = persist_progress_markdown(
        output_path=output_path,
        store_path=store_path,
        history_entries=history_entries,
        run_started_at=run_started_at,
        topic_keyword=topic_keyword,
        topic_url=resolved_topic_url,
        sort_mode=args.sort,
        target_ips=target_ips,
        scanned_video_urls=video_urls,
        all_records=all_records,
        global_records=global_records,
        run_baseline_keys=run_baseline_keys,
        max_comment_age_days=args.max_comment_age_days,
    )

    # Commit this run as one separated batch in history only when it has meaningful data.
    should_commit_history = (
        int(final_run_entry.get("scanned_count") or 0) > 0
        or int(final_run_entry.get("run_filtered_count") or 0) > 0
    )
    if should_commit_history:
        append_scan_history(history_path, final_run_entry)
        history_entries.append(final_run_entry)
    else:
        print("Skip history append: this run has no scanned/filtered data.")
    write_markdown(
        output_path=output_path,
        topic_url=resolved_topic_url,
        sort_mode=args.sort,
        target_ips=target_ips,
        scanned_video_urls=video_urls,
        all_records=dedupe_records(all_records),
        current_filtered_records=filter_by_age(
            dedupe_by_nickname(filter_by_ip(dedupe_records(all_records), target_ips)),
            args.max_comment_age_days,
        ),
        global_records=global_records,
        max_comment_age_days=args.max_comment_age_days,
        history_entries=history_entries,
        current_run_entry=None,
        expand_last_history=True,
    )

    print(f"Done: {output_path}")
    print("Current run total comments:", run_total)
    print("Current run filtered comments:", run_filtered)
    print("Global unique commenters (ID dedupe):", global_unique)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
