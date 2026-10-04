import json
import os
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import douyin_topic_comment_export as scraper
from playwright.sync_api import sync_playwright
cfg = json.loads((Path(os.environ['APPDATA'])/'douyin-screen-electron/douyin-screen-config.json').read_text(encoding='utf-8-sig'))
pw = sync_playwright().start()
context = pw.chromium.launch_persistent_context(cfg['params']['profileDir'],channel='msedge',headless=False,args=['--disable-blink-features=AutomationControlled'],viewport={'width':1440,'height':900})
page = context.pages[0] if context.pages else context.new_page()
page.goto('https://www.douyin.com/',wait_until='domcontentloaded')
print('SEARCH',scraper.ensure_search_keyword_on_page(page,cfg['params']['topicKeyword']),flush=True)
page.wait_for_timeout(4000)
print('URL',page.url,flush=True)
