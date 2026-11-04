"""Quota rendering and aggregation must not count current usage once per queued run."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_queue_forecast_adds_impacts_but_not_current_usage():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for frontend checks')
    source = (Path(__file__).parents[1] / 'rerunly/static/app.js').read_text()
    helpers = source[source.index('function windowLabel('):source.index('/** Subscription usage of each connected agent')]
    subprocess.run([node, '-e', '''
const assert = require('node:assert/strict');
const esc = (s) => String(s).replaceAll('<', '&lt;');
const fmtTokens = (s) => String(s);
''' + helpers + r'''
const row = {window_minutes: 300, status: 'estimated', models: [{model:'example', samples:3}],
 additional_percent: 3, additional_percent_low: 2, additional_percent_high: 4,
 projected_percent: 83, projected_percent_low: 82, projected_percent_high: 84};
const run = {agent:'Codex', forecast: {windows:[row]}};
const judgment = {agent:'Codex', kind:'judge', forecast: {windows:[row]}};
const html = queueQuotaForecast([run, run]);
assert.match(html, /\+4.0–8.0 percentage points/);
assert.match(html, /projected 84.0–88.0% used/);
assert.match(queueQuotaForecast([run, judgment]), /\+4.0–8.0 percentage points/);
assert.match(queueQuotaForecast([judgment]), /\+2.0–4.0 percentage points/);
assert.doesNotMatch(html, /6 samples/);
assert.match(queueQuotaForecast([run, {agent:'Codex', forecast:{windows:[]}}]), /Learning/);
assert.doesNotMatch(queueQuotaForecast([run, {agent:'Codex', forecast:{windows:[]}}]), /projected/);
assert.match(quotaForecast({windows:[{...row, projected_percent_high:101}]}), /May exhaust/);
assert.match(quotaForecast({windows:[{...row, may_cross_reset:true}]}), /cross the reported reset/);
assert.match(quotaForecast({windows:[]}), /unavailable/);
assert.match(quotaForecast({windows:[{window_minutes:300, status:'learning',models:[{model:'<model>',samples:1}]}]}), /&lt;model>/);
'''], check=True)
