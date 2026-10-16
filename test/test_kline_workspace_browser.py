"""K-line search/watchlist/layout acceptance; isolated browser storage and deterministic APIs.
Run after npm --prefix frontend run build; KLINE_TEST_URL can point to Vite.
"""
import asyncio
import os
from urllib.parse import urlsplit, parse_qs
import pytest
from playwright.async_api import async_playwright, expect
from test.test_kline_today_browser import bars, coverage, intraday, TODAY

BASE = os.environ.get('KLINE_TEST_URL', 'http://127.0.0.1:8000')
CODE = '600519.SH'
HIT = {'code': CODE, 'name': '贵州茅台', 'board': '主板', 'market': 'CN', 'flags': []}
OTHER = {'code': '688981.SH', 'name': '中芯国际', 'board': '科创板', 'market': 'CN', 'flags': []}


async def scenario(case):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
        await page.add_init_script("""if (!sessionStorage.getItem('seeded')) {
          localStorage.setItem('kline.watchlist.v1', JSON.stringify(['000001.SZ']));
          localStorage.setItem('kline.watchlist.names.v1', JSON.stringify({'000001.SZ':'平安银行'}));
          sessionStorage.setItem('seeded','1'); }""")
        errors = []; calls = []; searches = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        async def api(route):
            u = urlsplit(route.request.url); path = u.path; q = parse_qs(u.query)
            calls.append(path)
            payload = {}
            if path == '/api/stocks/search':
                query = q.get('q', [''])[0]; searches.append(q)
                if query == 'broken':
                    await route.fulfill(status=503, json={'detail': 'fixture search outage'}); return
                if query == 'slow':
                    await asyncio.sleep(.8)
                    hits = [HIT]
                elif query in ['600519', '贵州茅台', 'gzmt']: hits = [HIT]
                elif query in ['688981', '中芯国际']: hits = [OTHER]
                elif query == 'two': hits = [HIT, OTHER]
                else: hits = []
                payload = {'query': query, 'count': len(hits), 'results': hits}
            elif path.endswith('/ohlcv'): payload = bars(path.split('/')[-2])
            elif '/intraday/coverage/' in path: payload = coverage()
            elif '/intraday/daily/' in path: payload = {'available': False}
            elif '/intraday/auction/' in path: payload = {'available': False, 'points': []}
            elif path.startswith('/api/intraday/') and path.split('/')[-1].endswith(('.SH','.SZ')):
                payload = intraday(path.split('/')[-1], q.get('date', [TODAY])[0])
            elif path.endswith('/wyckoff'): payload = {'available': False, 'reason': 'fixture'}
            elif path == '/api/stocks': payload = {'stocks': []}
            elif path == '/api/strategies':
                payload = {'strategies': [
                    {'id':'lower','name':'低年化','stage':'production','selection_source':'lower','annual_rank':{'rank':2,'value':.1,'observations':300,'window':'fixture'}},
                    {'id':'live_holdings','name':'实盘镜像','stage':'production','selection_source':'live_holdings'},
                    {'id':'higher','name':'高年化','stage':'production','selection_source':'higher','annual_rank':{'rank':1,'value':.2,'observations':300,'window':'fixture'}}]}
            elif path == '/api/selection':
                source = q.get('source',[''])[0]
                payload = {'source':{'key':source,'trade_date':TODAY},'rows':[
                    {'stock_code':'000001.SZ','stock_name':'平安银行'}]}
            elif '/selection' in path: payload = {'rows':[],'records':[],'results':[]}
            elif path.endswith('/bundle'): payload = {'available':False, 'reason':'fixture'}
            await route.fulfill(status=200, json=payload)
        await page.route('**/api/**', api)
        await page.goto(BASE + '/#/kline?code=000001.SZ')
        await expect(page.locator('.chart-area')).to_be_visible()
        await expect(page.locator('.watchlist .groups .chip')).to_have_count(6)
        field = page.get_by_role('combobox', name='全市场搜索股票')
        async def search(query):
            await field.fill(query)
            await expect(page.locator('.stock-selector .dropdown-item')).not_to_have_count(0)
        def stored(): return page.evaluate("JSON.parse(localStorage.getItem('kline.watchlist.v1'))")
        if case in ['code','name','pinyin','keyboard','mobile']:
            query = {'name':'贵州茅台','pinyin':'gzmt','keyboard':'two'}.get(case, '600519')
            await search(query)
            target = OTHER if case == 'keyboard' else HIT
            if case == 'keyboard': await field.press('ArrowDown'); await field.press('Enter')
            elif case == 'name': await page.get_by_role('option').first.click()
            else: await field.press('Enter')
            await expect(page.locator('.title-bar')).to_contain_text(target['code'])
            assert await stored() == ['000001.SZ'], 'viewing must not auto-add'
            add = page.locator('.search-add'); await expect(add).to_contain_text('加入自选')
            await add.click()
            await expect(page.locator('.watchlist .groups .chip.active')).to_contain_text('我的自选')
            await expect(page.locator('.watchlist [role="status"]')).to_contain_text('已加入')
            await add.click()
            assert await stored() == ['000001.SZ', target['code']]
            await page.reload()
            await page.locator('.watchlist .groups .chip').first.click()
            await expect(page.locator('.watchlist .rows li')).to_have_count(2)
            await expect(page.locator('.watchlist .rows')).to_contain_text(target['name'])
            if case == 'mobile':
                for width in [390, 768, 1440]:
                    await page.set_viewport_size({'width':width,'height':900})
                    await page.wait_for_timeout(250)
                    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                    assert await page.locator('.chart-wrapper').evaluate('(e)=>e.getBoundingClientRect().right <= innerWidth')
                    await expect(page.locator('.search-add')).to_be_visible()
            row = page.locator('.watchlist .rows li').filter(has_text=target['code'])
            await row.click()
            await expect(row).to_have_class('active')
            await row.locator('.star').click()
            assert await stored() == ['000001.SZ']
        elif case == 'empty_error':
            for query, msg in [('no_such_stock','没有匹配'), ('broken','股票搜索暂时失败')]:
                await field.fill(query)
                await expect(page.get_by_role('listbox')).to_contain_text(msg)
                await field.press('Enter')
                assert await stored() == ['000001.SZ']
                assert 'code=000001.SZ' in page.url
            await search('600519'); await field.press('Enter')
            await expect(page.locator('.title-bar')).to_contain_text(CODE)
        elif case == 'race':
            await field.fill('slow')
            while not any(q.get('q') == ['slow'] for q in searches): await page.wait_for_timeout(20)
            await field.fill('688981')
            await expect(page.get_by_role('option')).to_contain_text('中芯国际')
            await page.wait_for_timeout(900)
            await expect(page.get_by_role('option')).to_have_count(1)
            await expect(page.get_by_role('option')).to_contain_text('中芯国际')
            await field.press('Enter')
            await expect(page.locator('.title-bar')).to_contain_text(OTHER['code'])
            # Clearing invalidates a running request too.
            await field.fill('slow'); await page.wait_for_timeout(250); await field.fill('')
            await page.wait_for_timeout(900)
            assert await page.get_by_role('option').count() == 0
        elif case == 'filters':
            await search('600519')
            await page.get_by_label('只主板', exact=True).check()
            await page.wait_for_timeout(150)
            assert searches[-1]['main_board_only'] == ['true']
            await page.get_by_label('含退市/摘牌', exact=True).check()
            await page.wait_for_timeout(150)
            assert searches[-1]['include_delisted'] == ['true']
        elif case == 'layout':
            positions = await page.evaluate('''() => {
              const y=s=>document.querySelector(s).getBoundingClientRect().top;
              return [y('.stock-discovery')<y('.groups'), y('.chart-area')<y('.intraday-panel'),
                y('.intraday-panel')<y('.ai-panel'), y('.ai-panel')<y('.research-panel')]; }''')
            assert all(positions), positions
            assert not any(path.endswith('/wyckoff') for path in calls)
            await page.locator('.groups-toggle').click()
            chips = page.locator('.watchlist .groups .chip')
            assert '实盘镜像' in await chips.nth(2).inner_text()
            assert '高年化' in await chips.nth(3).inner_text()
            for mode in ['annual','default','total','excess','t','name']:
                await page.locator('.group-sort select').select_option(mode)
                assert '实盘镜像' in await chips.nth(2).inner_text()
            await page.locator('.research-panel > summary').click()
            await expect(page.locator('.wyckoff-panel')).to_contain_text('fixture')
            assert sum(path.endswith('/wyckoff') for path in calls) == 1
        assert not errors, errors
        await browser.close()


@pytest.mark.parametrize('case',['code','name','pinyin','keyboard','mobile','empty_error','race','filters','layout'])
def test_kline_workspace(case):
    asyncio.run(scenario(case))
