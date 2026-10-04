"""Offline regression checks for homepage search; no login or network required."""
import ast
from pathlib import Path
from urllib.parse import urlparse, unquote, quote

source = Path(__file__).resolve().parents[2] / 'douyin_topic_comment_export.py'
tree = ast.parse(source.read_text(encoding='utf-8-sig'))
names = {'extract_search_keyword_from_url', 'ensure_search_keyword_on_page'}
selected = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[])
scope = {'Page': object, 'urlparse': urlparse, 'unquote': unquote}
exec(compile(selected, str(source), 'exec'), scope)
search = scope['ensure_search_keyword_on_page']


class Control:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector
        self.first = self

    def count(self):
        if self.selector.startswith('input'):
            return int(self.page.revealed and self.page.has_field)
        return int(not self.page.revealed and self.page.has_field)

    def click(self, **kwargs):
        self.page.actions.append('click')
        self.page.revealed = True

    def fill(self, value, **kwargs):
        self.page.value = value
        self.page.actions.append(('fill', value))

    def press(self, key, **kwargs):
        self.page.actions.append(('press', key))
        if self.page.submit_works:
            self.page.url = 'https://www.douyin.com/search/' + quote(self.page.value)


class Page:
    def __init__(self, url='https://www.douyin.com/', revealed=True, has_field=True, submit_works=True):
        self.url, self.revealed, self.has_field, self.submit_works = url, revealed, has_field, submit_works
        self.value = '用户关键词'
        self.actions = []

    def locator(self, selector):
        return Control(self, selector)

    def wait_for_timeout(self, value):
        pass

    def wait_for_url(self, predicate, **kwargs):
        if not predicate(self.url):
            raise TimeoutError('Search did not navigate')


page = Page()
assert search(page, '用户关键词')
assert page.actions == ['click', ('fill', '用户关键词'), ('press', 'Enter')]
print('[PASS] Homepage clicks search field and submits user keyword, even if input already matches')
page = Page(revealed=False)
assert search(page, '用户关键词') and page.actions.count('click') == 2
print('[PASS] Collapsed search control is opened before typing')
page = Page(url='https://www.douyin.com/search/old')
assert search(page, '用户关键词') and 'old' not in page.url
print('[PASS] Old search is replaced by the requested keyword')
assert not search(Page(submit_works=False), '用户关键词')
assert not search(Page(has_field=False), '用户关键词')
page = Page()
assert not search(page, '   ') and not page.actions
print('[PASS] Failed submission, missing search field, and blank keyword fail closed')
page = Page(url='https://www.douyin.com/search/' + quote('用户关键词'))
assert search(page, '用户关键词') and not page.actions
print('[PASS] Matching result page can be reused during recovery')
