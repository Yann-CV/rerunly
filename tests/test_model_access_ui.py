import shutil
import subprocess
from pathlib import Path

import pytest


def test_access_filter_keeps_categories_separate_and_requires_explicit_selection():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for frontend checks')
    source = (Path(__file__).parents[1] / 'rerunly/static/app.js').read_text()
    helper = source.split('let modelCatalog = [];', 1)[1].split('function rerunModelAgent(', 1)[0]
    subprocess.run([node, '-e', 'let modelCatalog = [];\n' + helper + r'''
const assert = require('node:assert/strict');
const esc = String;
class Field {
 constructor() { this.value='all'; this.listeners={}; }
 addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
 dispatchEvent(event) { for (const fn of this.listeners[event.type] || []) fn(); }
 setAttribute() {}
}
let filter;
const document = {createElement(tag) {
 const element = new Field();
 if (tag === 'label') { filter=new Field(); element.querySelector=()=>filter; }
 return element;
}};
let accessNote;
const label={firstChild:{textContent:'Model '},before(){},append(note){accessNote=note;}};
class Picker extends Field {
 constructor(values) { super(); this.options=values.map(value=>({value,textContent:value||'Same',dataset:{}})); this.selectedIndex=0; }
 get value() { return this.options?.[this.selectedIndex]?.value || ''; }
 set value(v) { if(this.options) this.selectedIndex=this.options.findIndex(o=>o.value===v); }
 closest() {return label;}
 setCustomValidity(message) { this.error=message; }
}
modelCatalog=[{id:'codex:included',access:'subscription'}, {id:'claude-paid',access:'api'},
 {id:'codex:old',access:'unknown'}];
assert.equal(modelAccess('included'), 'subscription');
assert.equal(modelAccess('missing'), 'unknown');
modelCatalog.push({id:'claude-default',agent:'claude',is_default:true,access:'subscription'});
assert.equal(modelAccess('', null), 'unknown');
assert.equal(modelAccess(''), 'subscription');
const picker=new Picker(['','codex:included','claude-paid','codex:old']);
const binding=bindModelAccess(picker,'codex:old');
assert.equal(picker.value,'');
assert.match(accessNote.textContent, /Subscription inclusion is unverified/);
assert.match(accessNote.textContent, /separate usage credits/);
filter.value='subscription'; filter.dispatchEvent(new Event('change'));
assert.equal(picker.selectedIndex,-1);
assert.match(picker.error,/Choose a model/);
assert.deepEqual(picker.options.filter(o=>!o.hidden).map(o=>o.value),['codex:included']);
picker.value='codex:included'; picker.dispatchEvent(new Event('change'));
assert.equal(picker.error,'');
binding.apply(); binding.apply();
assert.equal(picker.options[1].textContent,'codex:included · Subscription');
filter.value='api'; filter.dispatchEvent(new Event('change'));
assert.equal(picker.selectedIndex,-1);
assert.deepEqual(picker.options.filter(o=>!o.hidden).map(o=>o.value),['claude-paid']);
filter.value='unknown'; filter.dispatchEvent(new Event('change'));
assert.deepEqual(picker.options.filter(o=>!o.hidden).map(o=>o.value),['','codex:old']);
filter.value='all'; filter.dispatchEvent(new Event('change'));
assert.equal(picker.selectedIndex,-1); // Never silently replace a saved choice.
'''], check=True)


def test_rerun_models_ignore_access_and_preserve_agent_and_permission_rules(tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    import re
    import threading
    import time
    import uvicorn
    from rerunly.server import create_app

    app = create_app(tmp_path / 'examples.db', background=False, examples=True)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=0, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=shutil.which('google-chrome'))
            try:
                page = browser.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                catalog = [
                    {'id': 'codex:gpt-6.1-sol', 'agent': 'codex', 'access': 'subscription'},
                    {'id': 'codex:legacy', 'agent': 'codex', 'access': 'unknown'},
                    {'id': 'claude-sonnet-5-5', 'agent': 'claude', 'access': 'api'},
                ]
                page.route('**/api/models', lambda route: route.fulfill(json=catalog))
                def detail(route):
                    response = route.fetch()
                    data = response.json()
                    data['source'] = 'test-fixture'  # Enable only the UI; never launch an agent.
                    route.fulfill(response=response, json=data)
                page.route(re.compile(r'/api/prompts/[^/?]+(?:\?.*)?$'), detail)
                page.goto(f'http://127.0.0.1:{port}/#/prompt/rerunly-example:broad-prompt')
                planner = page.locator('.rerun-planner')
                planner.wait_for()
                assert not planner.evaluate('el => el.open')
                assert page.locator('#compare, #compare-sec, #cmp-cost').count() == 0
                planner.locator(':scope > summary').click()
                page.locator('#replay-form').wait_for()
                page.locator('.replay-impact-estimate').wait_for()
                assert page.evaluate("""() => {
                    const y = s => document.querySelector(s).getBoundingClientRect().top;
                    return y('.replay-head') < y('.replay-current') &&
                        y('.replay-current') < y('#replay-agent') &&
                        y('#replay-agent') < y('.replay-impact');
                }""")
                page.locator('#replay-effort').select_option('high')
                planner.locator(':scope > summary').click()
                assert not page.locator('#replay-form').is_visible()
                planner.locator(':scope > summary').click()
                assert page.locator('#replay-effort').input_value() == 'high'
                for width in (360, 390, 768, 1440):
                    page.set_viewport_size({'width': width, 'height': 900})
                    assert page.evaluate("""() => {
                        const panel = document.querySelector('.rerun-planner').getBoundingClientRect();
                        return [...document.querySelectorAll('#replay-form select, #replay-form input, #replay-form button')]
                            .filter(el => el.getClientRects().length).every(el => {
                                const r = el.getBoundingClientRect();
                                return r.width > 0 && r.left >= panel.left && r.right <= panel.right + 1;
                            });
                    }"""), f'Clipped control at {width}px'
                    help_icon = page.get_by_role('button', name='About estimated usage impact', exact=True)
                    help_icon.scroll_into_view_if_needed()
                    help_icon.hover()
                    tooltip = page.locator('#help-usage-impact')
                    assert tooltip.is_visible()
                    bounds = tooltip.bounding_box()
                    assert 0 <= bounds['x'] and bounds['x'] + bounds['width'] <= width
                    help_icon.focus()
                    help_icon.press('Escape')
                    assert tooltip.is_hidden()
                    help_icon.click()
                    assert tooltip.is_visible()
                    page.locator('#replay-effort').click()
                    assert tooltip.is_hidden()
                recorded = planner.locator('.rerun-recorded')
                recorded.locator(':scope > summary').click()
                recorded.get_by_role('button', name='About recorded configuration and skills').click()
                assert recorded.evaluate('el => el.open')
                page.keyboard.press('Escape')
                assert planner.locator('.model-access-filter, .model-access-note').count() == 0
                assert 'Unverified' not in planner.inner_text()
                # Both subscription and unverified models remain available for the agent.
                assert page.locator('#replay-model').evaluate(
                    'el => [...el.options].some(o => o.value === "codex:legacy" && !o.hidden && !o.disabled)')
                page.locator('#replay-model').select_option('codex:legacy')
                assert page.locator('#replay-model').evaluate('el => el.checkValidity()')
                page.locator('#replay-agent').select_option('claude')
                assert page.locator('#replay-agent').input_value() == 'claude'
                assert page.locator('#replay-permission').input_value() == ''
                assert page.locator('#replay-model').evaluate(
                    'el => [...el.options].every(o => o.value === "claude-sonnet-5-5")')
                page.locator('#replay-model').select_option('claude-sonnet-5-5')
                assert page.locator('#replay-model').evaluate('el => el.checkValidity()')
                page.locator('#replay-judge').select_option('codex:legacy')
                assert page.locator('#replay-judge').input_value() == 'codex:legacy'
                page.locator('#replay-rate').uncheck()
                assert page.locator('#replay-judge').is_disabled()
                assert not errors
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
