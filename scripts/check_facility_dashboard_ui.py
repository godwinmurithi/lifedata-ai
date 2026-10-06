"""Check the facility executive UI with the verified development source rows."""
import json
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
DATA = json.loads(Path('/tmp/lifedata-hf-source.json').read_text())
ID = 'ec298695-1758-4351-b953-f9db38789780'

def main():
    errors = []
    tile_statuses = []
    tile_referrers = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':1440,'height':900})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('response', lambda response: tile_statuses.append(response.status) if 'tile.openstreetmap.org' in response.url else None)
        def route_request(route):
            path = urlparse(route.request.url).path
            if '/static/' in path:
                asset = ROOT / 'lifedata_ai/static' / path.split('/static/',1)[1]
                return route.fulfill(body=asset.read_bytes(),content_type='text/css' if asset.suffix=='.css' else 'application/javascript')
            if path.endswith('/charts/433/') or path.endswith('/charts/434/'):
                return route.fulfill(json={'rows': DATA['survey' if path.endswith('/433/') else 'network']})
            if path == f'/lifedata-ai/api/publications/{ID}/':
                return route.fulfill(json={'id':ID,'title':'Trans Nzoia · Facility intelligence','theme':'light','layout':[], 'charts':[], 'dashboard_id':45,'configuration':{'preset':'facility-network','source_chart_id':433,'network_chart_id':434,'region_label':'Trans Nzoia County','map_bounds':[0.7,34.4,1.4,35.4]}})
            if 'tile.openstreetmap.org' in route.request.url:
                tile_referrers.append(route.request.headers.get('referer'))
                return route.continue_()
            return route.fulfill(body=f'<link rel="stylesheet" href="/static/dashboards/app.css"><div id="lifedata-dashboard" data-publication="{ID}"></div><script src="/static/dashboards/app.js"></script>',content_type='text/html',headers={'Referrer-Policy':'strict-origin-when-cross-origin'})
        page.route('**/*',route_request)
        page.goto('https://sandbox.lifehealth.app/lifedata-ai/dashboards/'+ID+'/')
        expect(page.get_by_test_id('kpi-0')).to_have_text('53')
        expect(page.get_by_test_id('kpi-1')).to_have_text('48')
        expect(page.locator('.leaflet-interactive')).to_have_count(53)
        expect(page.get_by_text('3 coordinates outside default map extent')).to_be_visible()
        page.get_by_role('button', name='Fit all coordinates').click()
        expect(page.get_by_role('button', name='Default view')).to_be_visible()
        expect(page.locator('.leaflet-interactive')).to_have_count(53)
        page.get_by_role('button', name='Default view').click()
        expect(page.locator('.hf-service-pie')).to_have_count(11)
        expect(page.get_by_role('button', name='Maternal & child health: Yes', exact=True).locator('b')).to_have_text('49')
        expect(page.get_by_role('button', name='Maternal & child health: No', exact=True).locator('b')).to_have_text('4')
        page.get_by_role('button', name='Maternal & child health: No', exact=True).click()
        expect(page.get_by_test_id('kpi-0')).to_have_text('4')
        expect(page.locator('.leaflet-interactive')).to_have_count(4)
        expect(page.get_by_role('button', name='Laboratory: Yes', exact=True).locator('b')).to_have_text(str(sum(r['maternal_childhealth']=='n' and r['labaratory']=='y' for r in DATA['survey'])))
        page.get_by_role('button', name='Clear service filter ×').click()
        expect(page.get_by_test_id('kpi-0')).to_have_text('53')
        # Exercise the chart itself, not only its keyboard-accessible controls.
        page.locator('[data-service="hiv_tb"] svg path[fill="#d6e0ed"]').first.click()
        expect(page.get_by_test_id('kpi-0')).to_have_text('10')
        page.get_by_role('button', name='Clear service filter ×').click()
        page.locator('.hf-breakdown').nth(2).locator('svg path[fill="#14a68c"]').first.click()
        expect(page.get_by_test_id('kpi-0')).to_have_text('45')
        expect(page.locator('.leaflet-interactive')).to_have_count(45)
        page.get_by_role('button',name='Reset',exact=True).click()
        for width,height in [(1440,900),(1366,768)]:
            page.set_viewport_size({'width':width,'height':height})
            assert page.evaluate('document.documentElement.scrollHeight <= innerHeight'), 'Dashboard overflows viewport'
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Dashboard overflows horizontally'
            assert page.locator('.hf-service-pie').evaluate_all('(cards) => cards.every(card => { const r=card.getBoundingClientRect(); const p=card.closest(".hf-services").getBoundingClientRect(); return r.bottom <= p.bottom && r.top >= p.top; })'), 'Service pies clipped'
        page.get_by_label('Level',exact=True).select_option('Level 2')
        expect(page.get_by_test_id('kpi-0')).to_have_text('25')
        expect(page.locator('.leaflet-interactive')).to_have_count(25)
        expect(page.get_by_role('button', name='Maternal & child health: Yes', exact=True).locator('b')).to_have_text(str(sum(r['hf_level']=='Level 2' and r['maternal_childhealth']=='y' for r in DATA['survey'])))
        page.get_by_role('button',name='Reset',exact=True).click()
        page.get_by_label('PCN',exact=True).select_option('Endebess PCN')
        expect(page.get_by_test_id('kpi-0')).to_have_text('3')
        expect(page.locator('.leaflet-interactive')).to_have_count(3)
        page.get_by_label('Hub',exact=True).select_option('Endebess')
        expect(page.get_by_test_id('kpi-0')).to_have_text('3')
        page.get_by_label('Ownership',exact=True).select_option('Private')
        expect(page.get_by_test_id('kpi-0')).to_have_text('0')
        expect(page.locator('.leaflet-interactive')).to_have_count(0)
        page.get_by_role('button',name='Reset',exact=True).click()
        page.get_by_label('Find a facility',exact=True).fill('Kitum')
        expect(page.get_by_test_id('kpi-0')).to_have_text('2')
        page.get_by_role('button',name='Reset',exact=True).click()
        page.set_viewport_size({'width':1440,'height':900})
        page.wait_for_timeout(1800)
        page.screenshot(path='/tmp/transnzoia-dashboard.png',full_page=True)
        assert not errors, errors
        assert tile_statuses and 200 in tile_statuses and all(s == 200 for s in tile_statuses), tile_statuses
        assert tile_referrers and all(r == 'https://sandbox.lifehealth.app/' for r in tile_referrers), tile_referrers
        browser.close()
    print('PASS: 11 service pies, slice/bar click cross-filtering, 53 map markers, live tiles HTTP 200 with origin-only referrer, and single desktop viewport')

if __name__=='__main__': main()
