import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import douyin_topic_comment_export as scraper
from playwright.sync_api import sync_playwright

cfg = json.loads((Path(os.environ['APPDATA']) / 'douyin-screen-electron/douyin-screen-config.json').read_text(encoding='utf-8-sig'))
out = ROOT.parent / '验证结果'
out.mkdir(exist_ok=True)
with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(cfg['params']['profileDir'], channel='msedge', headless=False,
        args=['--disable-blink-features=AutomationControlled'], viewport={'width':1440,'height':900})
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto('https://www.douyin.com/', wait_until='domcontentloaded')
        print('SEARCH_SUBMITTED', scraper.ensure_search_keyword_on_page(page, cfg['params']['topicKeyword']), flush=True)
        page.wait_for_timeout(8000)
        try:
            page.get_by_text('多列', exact=True).click(timeout=3000)
            page.wait_for_timeout(2500)
            print('MULTI_COLUMN', True, flush=True)
        except Exception as e:
            print('MULTI_COLUMN', type(e).__name__, flush=True)
        print('URL', page.url, flush=True)
        print('READY', scraper.is_search_page_ready(page), flush=True)
        print('TEXT', page.locator('body').inner_text()[:2400], flush=True)
        print('DOM', json.dumps(page.evaluate('''() => ({
          inputs:[...document.querySelectorAll('input')].map(e=>({type:e.type,placeholder:e.placeholder,value:e.value})),
          contentLinks:[...document.querySelectorAll('a[href*="/video/"],a[href*="/note/"]')].slice(0,5).map(e=>e.getAttribute('href')),
          e2e:[...new Set([...document.querySelectorAll('[data-e2e]')].map(e=>e.getAttribute('data-e2e')))].slice(0,50)
        })'''), ensure_ascii=False), flush=True)
        page.screenshot(path=str(out/'search-page.png'), full_page=False)
        scraper.prepare_search_video_page(page)
        opened = scraper.open_first_search_card_with_retry(page, max_attempts=2)
        print('OPENED', opened.url if opened else None, flush=True)
        if opened:
            def response(resp):
                if 'comment' in resp.url and 'douyin.com' in resp.url:
                    try:
                        data = resp.json()
                        print('COMMENT_RESPONSE', resp.url.split('?')[0], resp.status, list(data)[:12], 'COUNT', len(data.get('comments') or []), flush=True)
                    except Exception:
                        pass
            opened.on('response', response)
            print('COMMENT_PANEL', scraper.open_comment_panel(opened), flush=True)
            opened.wait_for_timeout(3000)
            print('OPEN_TEXT', opened.locator('body').inner_text()[:3500], flush=True)
            print('OPEN_E2E', opened.evaluate('''() => [...new Set([...document.querySelectorAll('[data-e2e]')].map(e=>e.getAttribute('data-e2e')))]'''), flush=True)
            opened.screenshot(path=str(out/'comment-page.png'), full_page=False)
    finally:
        context.close()
