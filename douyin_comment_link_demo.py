import argparse
import csv
import html
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List

DEFAULT_INPUT = Path("douyin_comments_input.csv")
OUTPUT_CSV = Path("douyin_comment_targets_demo.csv")
OUTPUT_HTML = Path("douyin_comment_targets_demo.html")
TARGET_IP = {"北京", "河北"}
VIDEO_URL_PATTERN = re.compile(r"^https?://(www\.)?douyin\.com/video/(\d{15,22})/?$")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def write_input_template(path: Path) -> None:
    template_rows = [
        {
            "ip_location": "北京",
            "nickname": "示例用户A",
            "comment_text": "这里放真实评论内容",
            "created_at": "2026-02-22 10:41:00",
            "video_url": "https://www.douyin.com/video/这里换成真实ID",
            "comment_id": "这里填真实comment_id(可留空)",
        },
        {
            "ip_location": "河北",
            "nickname": "示例用户B",
            "comment_text": "这里放真实评论内容",
            "created_at": "2026-02-22 11:03:00",
            "video_url": "https://www.douyin.com/video/这里换成真实ID",
            "comment_id": "这里填真实comment_id(可留空)",
        },
    ]
    fieldnames = [
        "ip_location",
        "nickname",
        "comment_text",
        "created_at",
        "video_url",
        "comment_id",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(template_rows)


def normalize_url(url: str) -> str:
    return (url or "").strip()


def check_url_status(url: str) -> str:
    m = VIDEO_URL_PATTERN.match(url)
    if not m:
        return "pattern_invalid"

    request = urllib.request.Request(
        url=url,
        headers={"User-Agent": USER_AGENT},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            code = getattr(resp, "status", 200)
            if 200 <= code < 300:
                # Douyin often returns a shell page with 200 even when content is unavailable.
                # Treat as unverified instead of confirmed-valid.
                return f"http_{code}_unverified"
            return f"http_{code}"
    except urllib.error.HTTPError as e:
        return f"http_{e.code}"
    except urllib.error.URLError:
        return "network_error"
    except TimeoutError:
        return "timeout"


def load_rows(input_path: Path, do_check: bool) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []

    with input_path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {"ip_location", "nickname", "comment_text", "created_at", "video_url"}
        missing = [k for k in required if k not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"Input CSV missing required columns: {', '.join(missing)}")

        for c in reader:
            ip_location = (c.get("ip_location") or "").strip()
            if ip_location not in TARGET_IP:
                continue

            comment_text = (c.get("comment_text") or "").strip()
            video_url = normalize_url(c.get("video_url") or "")
            locate_keyword = comment_text[:24]
            comment_id = (c.get("comment_id") or "").strip()
            url_status = check_url_status(video_url) if do_check else "unchecked"

            rows.append(
                {
                    "ip_location": ip_location,
                    "nickname": (c.get("nickname") or "").strip(),
                    "comment_text": comment_text,
                    "created_at": (c.get("created_at") or "").strip(),
                    "video_url": video_url,
                    "comment_id": comment_id,
                    "url_status": url_status,
                    "locate_keyword": locate_keyword,
                }
            )
    return rows


def write_output_csv(rows: List[Dict[str, str]]) -> None:
    fieldnames = [
        "ip_location",
        "nickname",
        "comment_text",
        "created_at",
        "video_url",
        "comment_id",
        "url_status",
        "locate_keyword",
    ]
    with OUTPUT_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_output_html(rows: List[Dict[str, str]]) -> None:
    html_lines = [
        "<!doctype html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8" />',
        '<meta name="viewport" content="width=device-width, initial-scale=1" />',
        "<title>抖音评论定位清单</title>",
        "<style>",
        "body{font-family:Segoe UI,Microsoft YaHei,sans-serif;margin:20px;background:#f7fafc;color:#111827}",
        "table{border-collapse:collapse;width:100%;background:#fff}",
        "th,td{border:1px solid #e5e7eb;padding:10px;font-size:14px;vertical-align:top}",
        "th{background:#f3f4f6;text-align:left}",
        "a{color:#2563eb;text-decoration:none}",
        "a:hover{text-decoration:underline}",
        ".tip{margin:0 0 12px 0;padding:10px;background:#ecfeff;border:1px solid #bae6fd}",
        ".bad{color:#b91c1c;font-weight:600}",
        ".warn{color:#92400e;font-weight:600}",
        ".ok{color:#166534;font-weight:600}",
        "</style>",
        "</head>",
        "<body>",
        "<h2>抖音评论定位清单</h2>",
        "<p class=\"tip\">说明：这里是可点击的视频链接，不是单条评论永久链接。状态为 http_2xx_unverified 仅表示页面可访问，不代表视频一定存在；需手动打开确认。</p>",
        "<table>",
        "<tr><th>IP</th><th>昵称</th><th>评论</th><th>时间</th><th>打开视频</th><th>链接状态</th><th>定位关键词</th></tr>",
    ]

    for r in rows:
        safe_ip = html.escape(r["ip_location"])
        safe_nickname = html.escape(r["nickname"])
        safe_comment = html.escape(r["comment_text"])
        safe_created_at = html.escape(r["created_at"])
        safe_video_url = html.escape(r["video_url"])
        safe_status = html.escape(r["url_status"])
        safe_keyword = html.escape(r["locate_keyword"])
        if r["url_status"].startswith("http_2"):
            status_class = "warn"
        elif r["url_status"] == "pattern_invalid":
            status_class = "bad"
        else:
            status_class = "bad"

        html_lines.append(
            "<tr>"
            f"<td>{safe_ip}</td>"
            f"<td>{safe_nickname}</td>"
            f"<td>{safe_comment}</td>"
            f"<td>{safe_created_at}</td>"
            f"<td><a href=\"{safe_video_url}\" target=\"_blank\" rel=\"noreferrer\">打开视频</a></td>"
            f"<td class=\"{status_class}\">{safe_status}</td>"
            f"<td>{safe_keyword}</td>"
            "</tr>"
        )

    html_lines.extend(["</table>", "</body>", "</html>"])
    OUTPUT_HTML.write_text("\n".join(html_lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从真实评论CSV生成可点击视频链接清单（按北京/河北筛选）。"
    )
    parser.add_argument(
        "--input",
        default=str(DEFAULT_INPUT),
        help="输入CSV路径，默认 douyin_comments_input.csv",
    )
    parser.add_argument(
        "--check-url",
        action="store_true",
        help="启用链接状态检测（会访问网络）。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)

    if not input_path.exists():
        write_input_template(input_path)
        print(f"Missing input CSV. Template created: {input_path}")
        print("请先填入真实 video_url 后再运行脚本。")
        return 1

    try:
        rows = load_rows(input_path=input_path, do_check=args.check_url)
    except ValueError as e:
        print(str(e))
        return 2

    write_output_csv(rows)
    write_output_html(rows)
    print(f"Generated: {OUTPUT_CSV}")
    print(f"Generated: {OUTPUT_HTML}")
    print(f"Rows exported: {len(rows)}")
    if args.check_url:
        unverified = [r for r in rows if r["url_status"].startswith("http_2")]
        bad = [r for r in rows if not r["url_status"].startswith("http_2")]
        print(f"Rows with 2xx but unverified existence: {len(unverified)}")
        print(f"Rows with invalid/error status: {len(bad)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
