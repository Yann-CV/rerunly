"""Browser coverage uses simulated availability responses, never real inference."""
import shutil
import threading
import time

import pytest
import uvicorn

from rerunly.server import create_app


def test_setup_owns_schedule_and_launch_and_reruns_only_reports_information(tmp_path, monkeypatch):
    playwright = pytest.importorskip('playwright.sync_api')
    from rerunly.config import DEFAULT_SETTINGS
    from rerunly import server as server_module
    monkeypatch.setitem(DEFAULT_SETTINGS, 'claude_projects_dir', str(tmp_path / 'no-claude'))
    monkeypatch.setattr(server_module, 'CLAUDE_SETTINGS', tmp_path / 'claude.json')
    app = create_app(tmp_path / 'ui.db', background=False)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=0, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=shutil.which('google-chrome'))
            try:
                page = browser.new_page()
                errors, actions = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                state = {'config': {'enabled': False, 'interval_hours': 24, 'agents': ['claude', 'codex']},
                         'scan': {'status': 'idle', 'results': []}, 'next_at': None}
                page.route('**/api/models', lambda route: route.fulfill(json=[]))
                page.route('**/api/limits', lambda route: route.fulfill(json=[]))
                def checks(route):
                    request = route.request
                    if request.method != 'GET':
                        actions.append((request.method, request.url, request.post_data_json if request.post_data else None))
                    if request.method == 'PUT':
                        state['config'] = request.post_data_json
                    elif request.url.endswith('/start'):
                        state['scan'] = {'status': 'running', 'results': [], 'current': 'codex: first'}
                    elif request.url.endswith('/cancel'):
                        state['scan'] = {'status': 'cancelled', 'results': []}
                    route.fulfill(json=state)
                page.route('**/api/model-checks**', checks)
                page.goto(f'http://127.0.0.1:{port}/#/reruns')
                summary = page.locator('#model-check-tools')
                summary.get_by_text('No model information available yet.', exact=True).wait_for()
                assert summary.locator('button, form, input, select').count() == 0
                assert not actions
                link = summary.get_by_role('link', name='Recompute or schedule in Setup')
                assert link.get_attribute('href') == '#/setup?jump=model-availability'
                link.click()
                panel = page.locator('#model-availability .model-checks')
                panel.wait_for()
                assert panel.evaluate('el => el.open')
                assert panel.get_by_role('button', name='Check now', exact=True).is_visible()
                form = page.locator('#model-check-config')
                assert not form.locator('[name=enabled]').is_checked()
                form.locator('[value=claude]').uncheck()
                form.locator('[name=interval_hours]').select_option('168')
                form.get_by_role('button', name='Save schedule').click()
                page.wait_for_function("() => document.querySelector('[data-model-check-state]').textContent.includes('Periodic checks are off')")
                panel.get_by_role('button', name='Check now').click()
                page.get_by_role('button', name='Cancel check', exact=True).wait_for()
                assert state['config'] == {'enabled': False, 'interval_hours': 168, 'agents': ['codex']}
                assert page.get_by_role('button', name='Checking…', exact=True).is_disabled()
                page.get_by_role('button', name='Cancel check', exact=True).click()
                page.wait_for_function("() => document.querySelector('[data-model-check-state]').textContent.includes('cancelled')")
                assert page.get_by_role('button', name='Check now').is_enabled()
                state['scan'] = {'status': 'completed', 'finished_at': '2026-10-30T12:00:00Z', 'results': [
                    {'agent': 'codex', 'model': '<model>', 'status': 'listed', 'message': 'Listed by agent; no test prompt sent.',
                     'tokens': 21, 'checked_at': '2026-10-30T12:00:00Z'}]}
                # Enabling periodic checks saves the interval and renders persisted results.
                page.locator('[name=enabled]').check()
                page.get_by_role('button', name='Save schedule').click()
                page.get_by_text('Listed by agent', exact=True).wait_for()
                assert state['config']['enabled'] is True
                assert page.get_by_text('<model>', exact=True).count() == 1
                for width in (360, 768, 1440):
                    page.set_viewport_size({'width': width, 'height': 900})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), page.evaluate("[...document.querySelectorAll('body *')].filter(el=>el.getBoundingClientRect().right>innerWidth+1 && getComputedStyle(el).position!=='fixed' && !el.closest('.table-scroll,.table-wrap')).map(el=>[el.tagName,el.className,el.getBoundingClientRect().width]).slice(0,20)")
                page.goto(f'http://127.0.0.1:{port}/#/reruns')
                summary = page.locator('#model-check-tools')
                summary.get_by_text('1 model listed by your agents.', exact=True).wait_for()
                assert not summary.locator('details').evaluate('el => el.open')
                assert not summary.locator('time').is_visible()
                summary.locator('summary').click()
                assert summary.locator('time').get_attribute('datetime') == '2026-10-30T12:00:00Z'
                assert summary.locator('time').is_visible()
                assert summary.locator('button, form, input, select').count() == 0
                assert len([a for a in actions if a[1].endswith('/start')]) == 1
                assert len([a for a in actions if a[1].endswith('/cancel')]) == 1
                assert not errors
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
