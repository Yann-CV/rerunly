"""Shared browsing toolbars keep search, filters, and actions in consistent rows."""
import shutil
import threading
import time
from urllib.parse import urlencode

import pytest
import uvicorn

from rerunly.server import create_app


def test_browse_toolbar_rows_and_stable_layout_switch(tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    app = create_app(tmp_path / 'examples.db', background=False, examples=True)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=0, log_level='error'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(.05)
        base = f'http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}/'
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=shutil.which('google-chrome'))
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1100})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                for route, search_id in [
                    ('/prompts', 'prompt-search'),
                    ('/tag/example:skills', 'prompt-search'),
                    ('/sessions', 'session-search'),
                    ('/tags', 'tags-search'),
                ]:
                    # Full navigation makes each assertion wait for the new view.
                    page.goto(base + '#' + route)
                    page.reload()
                    toolbar = page.locator('.browse-toolbar')
                    toolbar.locator('#' + search_id).wait_for()
                    assert toolbar.locator('[data-toggle-reruns]').count() == 0
                    search = toolbar.locator(':scope > .search-box').bounding_box()
                    filters = toolbar.locator('.browse-filters').bounding_box()
                    actions = toolbar.locator('.browse-actions').bounding_box()
                    assert search['y'] + search['height'] <= filters['y']
                    assert filters['y'] + filters['height'] <= actions['y']
                    assert abs(search['width'] - filters['width']) < 1
                    # Desktop filters occupy one row.
                    children = toolbar.locator('.browse-filters > *').all()
                    centers = [b['y'] + b['height'] / 2 for c in children if (b := c.bounding_box())]
                    assert max(centers) - min(centers) < 1
                    if search_id != 'tags-search':
                        period = toolbar.get_by_role('group', name='Period', exact=True).bounding_box()
                        assert abs(period['x'] - filters['x']) < 1
                page.goto(base + '#/sessions')
                page.locator('#session-search').wait_for()
                page.locator('#pop-category-example summary').click()
                with page.expect_response(lambda r: '/api/sessions?' in r.url):
                    page.locator('[data-category-tag="example:skills"]').click()
                page.reload()
                page.wait_for_function("() => sessState.categoryTags.example?.includes('example:skills') && sessState.items.length > 0")
                assert 'tags=example%3Askills' in page.url
                page.locator('.active-filters [data-clear="category:example"]').click()
                page.wait_for_function("() => Object.keys(sessState.categoryTags).length === 0")
                page.locator('#pop-category-tag summary').click()
                page.locator('#pop-category-tag .menu-search').fill('rerunly-examples')
                with page.expect_response(lambda r: '/api/sessions?' in r.url):
                    page.locator('[data-category-tag="rerunly-examples"]').click()
                page.locator('.active-filters [data-clear="all"]').click()
                page.wait_for_function("() => !sessState.tag && Object.keys(sessState.categoryTags).length === 0")
                page.goto(base + '#/prompts')
                page.locator('#prompt-search').wait_for()
                # Category menus intersect, while the general Tag menu has no prefixed tags.
                tags = page.request.get(base + 'api/prompts/rerunly-example:review-skill-v2').json()['tags']
                selected = [next(t['tag'] for t in tags if t['tag'].startswith(prefix)) for prefix in ['model:', 'skill:']]
                page.locator('#pop-category-tag summary').click()
                offered = page.locator('#pop-category-tag [data-category-tag]').evaluate_all("buttons => buttons.map(b => b.dataset.categoryTag)")
                assert offered and all(':' not in tag for tag in offered)
                assert 'rerun' in offered
                page.locator('#pop-category-tag summary').click()
                for tag in selected:
                    cat = tag.split(':')[0]
                    page.locator(f'#pop-category-{cat} summary').click()
                    with page.expect_response(lambda r: '/api/prompts?' in r.url):
                        page.locator(f'[data-category-tag="{tag}"]').click()
                # Menu counts follow the other filters and match what selecting a value would list.
                page.wait_for_function("() => listState.tagCounts && listState.categoryTags.skill?.length === 1")
                model_menu = page.locator('#pop-category-model')
                model_menu.locator('summary').click()
                unselected = model_menu.locator('[role=checkbox][aria-checked=false]').first
                shown = unselected.locator('.mi-count').inner_text()
                query = [('tags', selected[1]), ('tags', unselected.get_attribute('data-category-tag'))]
                expected = page.request.get(base + 'api/prompts?' + urlencode(query)).json()['total']
                assert shown == str(expected)
                assert ('no-match' in unselected.get_attribute('class')) == (expected == 0)
                model_menu.locator('summary').click()
                # Keep the menu open and select another value in the same category.
                model_menu.locator('summary').click()
                other = model_menu.locator('[role=checkbox]').evaluate_all("buttons => buttons.map(b => b.dataset.categoryTag)")
                other = next(tag for tag in other if tag != selected[0])
                with page.expect_response(lambda r: '/api/prompts?' in r.url):
                    model_menu.locator(f'[data-category-tag="{other}"]').click()
                assert model_menu.get_attribute('open') is not None
                assert model_menu.locator('[aria-checked=true]').count() == 2
                page.reload()
                page.wait_for_function("() => listState.categoryTags.model?.length === 2 && listState.items.length > 0")
                for item in page.evaluate('listState.items'):
                    names = {t['tag'] for t in item['tags']}
                    assert selected[1] in names
                    assert names.intersection([selected[0], other])
                model_menu.locator('summary').click()
                with page.expect_response(lambda r: '/api/prompts?' in r.url):
                    model_menu.locator(f'[data-category-tag="{other}"]').click()
                assert model_menu.locator('[aria-checked=true]').count() == 1
                with page.expect_response(lambda r: '/api/prompts?' in r.url):
                    model_menu.locator('[data-category-tag=""]').click()
                page.wait_for_function("() => !listState.categoryTags.model && listState.categoryTags.skill?.length === 1")
                page.locator('.active-filters [data-clear="all"]').click()
                page.wait_for_function("() => Object.keys(listState.categoryTags).length === 0")
                for width in [1440, 1024, 768, 390]:
                    page.set_viewport_size({'width': width, 'height': 1100})
                    page.get_by_role('button', name='List', exact=True).click()
                    switch = page.get_by_role('group', name='Layout', exact=True)
                    before = switch.bounding_box()
                    for label in ['By tag', 'List']:
                        page.get_by_role('button', name=label, exact=True).click()
                        after = switch.bounding_box()
                        assert abs(before['x'] - after['x']) < 1
                        assert abs(before['y'] - after['y']) < 1
                for route in ['/prompts', '/tag/example:skills']:
                    for width in [1440, 390]:
                        page.set_viewport_size({'width': width, 'height': 900})
                        page.goto(base + '#' + route)
                        page.reload()
                        page.get_by_role('button', name='Select prompts', exact=True).click()
                        page.locator('.psel').first.click()
                        # Extend the list to exercise scrolling even with the small example dataset.
                        page.locator('#prompt-results').evaluate("el => el.style.minHeight = '3000px'")
                        page.locator('#select-bar').evaluate("el => window.scrollTo(0, el.getBoundingClientRect().top + scrollY + 500)")
                        selection = page.get_by_role('region', name='Selection', exact=True)
                        bounds = selection.bounding_box()
                        nav = page.locator('.topbar').bounding_box()
                        assert bounds['y'] >= nav['y'] + nav['height']
                        assert bounds['y'] + bounds['height'] <= 900
                        assert selection.get_by_role('button', name='Tag 1 selected…', exact=True).is_enabled()
                        selection.get_by_role('button', name='Done', exact=True).click()
                        assert selection.count() == 0
                assert not errors
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
