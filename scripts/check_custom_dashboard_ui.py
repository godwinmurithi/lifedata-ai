"""Browser smoke checks with mocked data; no live login or model calls."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PUBLICATION = '11111111-1111-4111-8111-111111111111'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=')


def main() -> None:
    publication = {
        'id': PUBLICATION, 'title': 'Development services', 'theme': 'light',
        'layout': [{'chart_id': 12, 'span': 6, 'height': 320}, {'chart_id': 13, 'span': 6, 'height': 320}],
        'dashboard_id': 4, 'can_edit': True, 'url': f'/lifedata-ai/dashboards/{PUBLICATION}/',
        'charts': [{'id': 12, 'title': 'Visits', 'viz_type': 'table'}, {'id': 13, 'title': 'Results', 'viz_type': 'pie'}],
    }
    patches = []
    uploads = []
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        page.on('pageerror', lambda error: errors.append(str(error)))

        def route_request(route):
            request = route.request
            path = urlparse(request.url).path
            if '/static/' in path:
                asset = ROOT / 'lifedata_ai/static' / path.split('/static/', 1)[1]
                if asset.exists():
                    return route.fulfill(body=asset.read_bytes(), content_type='text/css' if asset.suffix == '.css' else 'application/javascript')
            if path == f'/lifedata-ai/api/publications/{PUBLICATION}/':
                if request.method == 'PATCH':
                    layout = request.post_data_json['layout']
                    publication['layout'] = layout
                    patches.append(layout)
                return route.fulfill(json=publication)
            if '/charts/' in path:
                return route.fulfill(json={'rows': [{'name': 'Dummy A', 'count': 7}, {'name': 'Dummy B', 'count': 12}],
                                           'columns': ['name', 'count'], 'row_limit': 200,
                                           'form_data': {'groupby': ['name'], 'donut': True}})
            if path == '/api/v1/security/csrf_token/':
                return route.fulfill(json={'result': 'dummy-token'})
            if path == '/lifedata-ai/api/chat/':
                uploads.append((request.headers.get('content-type', ''), request.post_data_buffer))
                events = [('message_start', {'conversation_id': PUBLICATION}),
                          ('text_delta', {'text': 'I can see your screenshot.'}),
                          ('published_dashboard', {**publication, 'type': 'published_dashboard'}),
                          ('message_stop', {'stop_reason': 'end_turn', 'conversation_id': PUBLICATION})]
                body = ''.join(f'event: {name}\ndata: {json.dumps(data)}\n\n' for name, data in events)
                return route.fulfill(body=body, content_type='text/event-stream')
            if path == '/lifedata-ai/api/conversations/':
                return route.fulfill(json={'conversations': []})
            if path.startswith('/lifedata-ai/api/'):
                return route.fulfill(json={})
            return route.fulfill(body=f'''<!doctype html><html><head>
<link rel="stylesheet" href="/lifedata-ai/static/dashboards/app.css">
<script defer src="/lifedata-ai/static/dashboards/app.js"></script>
<script defer src="/lifedata-ai/static/panel/panel.js"></script></head>
<body><div id="lifedata-dashboard" data-publication="{PUBLICATION}"></div></body></html>''', content_type='text/html')

        page.route('**/*', route_request)
        page.goto('http://lifedata.test' + publication['url'])
        expect(page.get_by_role('heading', name='Development services')).to_be_visible()
        expect(page.get_by_role('cell', name='Dummy A')).to_be_visible()
        expect(page.locator('canvas')).to_have_count(1)
        page.get_by_role('button', name='Edit layout').click()
        handle = page.get_by_role('button', name='Move Visits')
        handle.focus()
        page.keyboard.press('Space')
        expect(handle).to_have_attribute('aria-pressed', 'true')
        page.keyboard.press('ArrowRight')
        expect(page.get_by_role('status').filter(has_text='over droppable area 13')).to_be_visible()
        page.keyboard.press('Space')
        expect(page.get_by_role('status').filter(has_text='Layout saved')).to_be_visible()
        assert patches and patches[-1][0]['chart_id'] == 13, 'Keyboard drag did not persist the reordered layout'
        source = page.get_by_role('button', name='Move Results').bounding_box()
        target = page.get_by_role('button', name='Move Visits').bounding_box()
        assert source and target
        page.mouse.move(source['x'] + source['width'] / 2, source['y'] + source['height'] / 2)
        page.mouse.down()
        page.mouse.move(target['x'] + target['width'] / 2, target['y'] + target['height'] / 2, steps=15)
        page.mouse.up()
        expect(page.get_by_role('status').filter(has_text='Layout saved')).to_be_visible()
        assert patches[-1][0]['chart_id'] == 12, 'Pointer drag did not persist the layout'
        page.get_by_label('Width of Visits').select_option('12')
        expect(page.get_by_role('status').filter(has_text='Layout saved')).to_be_visible()
        assert next(item for item in patches[-1] if item['chart_id'] == 12)['span'] == 12
        page.screenshot(path='/tmp/lifedata-custom-dashboard.png', full_page=True)

        page.evaluate('window.__lifedataCopilot.open()')
        page.locator('input[type=file]').set_input_files({'name': 'reference.png', 'mimeType': 'image/png', 'buffer': PNG})
        expect(page.locator('.ld-attachment img')).to_be_visible()
        page.locator('#lifedata-ai-input').fill('Build this dashboard from my screenshot')
        page.locator('.ld-send').click()
        expect(page.locator('.ld-messages')).to_contain_text('I can see your screenshot.')
        expect(page.locator('.ld-publication a')).to_have_attribute('href', publication['url'])
        assert uploads and 'multipart/form-data' in uploads[0][0]
        assert b'reference.png' in uploads[0][1] and PNG in uploads[0][1]
        assert b'"dashboard_id":4' in uploads[0][1], 'Custom-page context was not sent to chatbot'
        assert not errors, errors
        browser.close()
    print('PASS: custom charts, pointer/keyboard drag, layout persistence, image upload, publication link and chatbot context')


if __name__ == '__main__':
    main()
