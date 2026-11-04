"""Queue drafts preserve missing permissions; immediate launches still require them."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_queue_button_preserves_draft_permissions_and_reports_request_errors():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for frontend checks')
    source = (Path(__file__).parents[1] / 'rerunly/static/app.js').read_text()
    settings = source.split('  const settings = (draft = false) => {', 1)[1].split('  const modelSelect', 1)[0]
    handler = source.split('  const queueButton = document.getElementById("queue-replay");', 1)[1].split('  if (form) form.onsubmit', 1)[0]
    subprocess.run([node, '-e', '''
const assert = require('node:assert/strict');
class StaleNavigation extends Error {}
const fields = Object.fromEntries(['replay-permission', 'replay-model', 'replay-effort', 'replay-release',
 'replay-mode', 'replay-budget', 'replay-skills', 'replay-rate', 'replay-judge', 'replay-status'].map(id => [id, {value:'', checked:true}]));
fields['replay-status'].hidden = true;
const document = {getElementById: id => fields[id]};
const p = {id:'example'};
const queueButton = {disabled:false};
const settings = (draft = false) => {
''' + settings + '''
let request;
let api = async (path, options) => { request = options.body; return {added:1,queued:1}; };
''' + handler + '''
(async () => {
 assert.throws(() => settings(), /Choose permissions/);
 assert.equal(settings(true).permission, '');
 await queueButton.onclick();
 assert.equal(request.permission, '');
 assert.deepEqual(request.prompt_ids, ['example']);
 assert.equal(fields['replay-status'].hidden, false);
 assert.match(fields['replay-status'].innerHTML, /Added to the queue/);
 assert.equal(queueButton.disabled, false);
 api = async () => { throw new Error('Connection unavailable'); };
 await queueButton.onclick();
 assert.equal(fields['replay-status'].textContent, 'Connection unavailable');
 assert.equal(queueButton.disabled, false);
})();
'''], check=True)


def test_queue_settings_stay_open_until_permission_issue_is_resolved():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for frontend checks')
    source = (Path(__file__).parents[1] / 'rerunly/static/app.js').read_text()
    helper = source.split('function showQueueConfigIssues(', 1)[1].split('function editQueuedRun(', 1)[0]
    handler = source.split('  dialog.querySelector("form").onsubmit = async (event) => {', 1)[1].split('  dialog.showModal();', 1)[0]
    subprocess.run([node, '-e', '''
const assert = require('node:assert/strict');
const esc = s => String(s).replaceAll('<', '&lt;');
class StaleNavigation extends Error {}
const q = {id:7};
const resumeIds = null;
let validationReady = true, saving = false;
const button = {disabled:false};
const status = {hidden:true};
const list = {hidden:true};
const permission = {value:'same', invalid:false, focused:false,
 setAttribute(){this.invalid=true;}, removeAttribute(){this.invalid=false;}, focus(){this.focused=true;}};
const model = {value:'edited-model'};
const form = {};
const dialog = {closed:false, isConnected:true, close(){this.closed=true;},
 querySelector: selector => ({'form':form, '[type="submit"]':button,
 '#queue-config-status':status, '#queue-config-issues':list,
 '[name="permission"]':permission})[selector] || null,
 querySelectorAll: () => [permission]};
const FormData = class { *[Symbol.iterator]() { yield ['permission',permission.value]; yield ['model',model.value]; } };
let views = 0;
const viewReruns = async () => {views++;};
let api = async () => ({needs_input:true, issues:[{field:'permission',
 message:'Original permissions were not recorded. Choose explicit permissions.'}]});
function showQueueConfigIssues(''' + helper + '''
form.onsubmit = async (event) => {
''' + handler + '''
(async () => {
 const event = {preventDefault(){},currentTarget:form};
 await form.onsubmit(event);
 assert.equal(dialog.closed,false);
 assert.equal(views,0);
 assert.equal(permission.invalid,true);
 assert.equal(permission.focused,true);
 assert.equal(status.hidden,false);
 assert.equal(list.hidden,false);
 assert.match(list.innerHTML,/Choose explicit permissions/);
 assert.equal(model.value,'edited-model');
 assert.equal(button.disabled,true);
 validationReady = true;
 permission.value = 'ask';
 api = async () => ({needs_input:false, issues:[]});
 await form.onsubmit(event);
 assert.equal(dialog.closed,true);
 assert.equal(views,1);
})();
'''], check=True)


def test_grouped_history_preserves_attempt_links_settings_and_missing_originals():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend checks")
    source = (Path(__file__).parents[1] / "rerunly/static/app.js").read_text()
    helpers = "function groupRerunHistory(" + source.split("function groupRerunHistory(", 1)[1].split("function rerunBudgetWarning", 1)[0]
    subprocess.run([node, "-e", r"""
const assert = require('node:assert/strict');
const esc = s => String(s).replaceAll('<','&lt;').replaceAll('"','&quot;');
const fmtDate = String, fmtAgo = String, fmtCost = n => '$'+n, fmtMs = String;
const plural = (n, label) => `${n} ${label}${n === 1 ? '' : 's'}`;
const rerunSettings = () => 'all settings', rerunStatusHtml = x => x.status;
const judgeBadge = () => '', refDelta = () => '', duration = () => 0;
const rerunState = {historyOpen:true, historyGroups:new Map()};
""" + helpers + r"""
const original = {id:'original',text:'Same <request>',project:'demo',requested_model:'codex:test',harness:'Codex',effort:'high',total_cost_usd:1};
const first = {status:'done',session_id:'first',prompt_id:'original',original,launched_at:'2026-01-01',mode:'terminal',effort:'low',model:'codex:other',
 replay:{id:'result',text:'Imported request',total_cost_usd:2}};
const second = {...first,session_id:'second',launched_at:'2026-01-02',status:'failed',replay:null,result:'<failure>'};
const unrelated = {...first,prompt_id:'unrelated',session_id:'third',original:{...original,id:'unrelated'}};
const groups = groupRerunHistory([first,second,unrelated]);
assert.equal(groups.length,2);
assert.deepEqual(groups[0].runs.map(x=>x.session_id), ['second','first']);
assert.equal(groups[1].runs.length,1); // Identical text must not merge distinct prompts.
let html = rerunHistoryHtml([first,second]);
assert.match(html, /id="rerun-history" open/);
assert.equal((html.match(/class="rh-group"/g)||[]).length,1);
assert.match(html, /href="#\/rerun\/first"/);
assert.match(html, /href="#\/rerun\/second"/);
assert.match(html, /href="#\/prompt\/result"/);
assert.match(html, /href="#\/prompt\/original"/);
assert.match(html, /Compare with original/);
assert.match(html, /class="rh-compare-all" href="#\/rerun-comparison\/original"/);
assert.match(html, /Compare all reruns/);
assert.match(html, /Codex/);
assert.match(html, /low effort/);
assert.match(html, /&lt;failure>/);
assert.doesNotMatch(html, /Same <request>/);
rerunState.historyOpen = false;
rerunState.historyGroups.set('original',false);
html = rerunHistoryHtml([first,second]);
assert.doesNotMatch(html, /id="rerun-history" open/);
assert.doesNotMatch(html, /data-history-group="original" open/);
html = rerunHistoryHtml([{...second, original:null}]);
assert.match(html, /Original prompt unavailable/);
assert.match(html, /href="#\/rerun\/second"/);
assert.match(rerunHistoryHtml([]), /Your next experiment starts above/);
"""], check=True)


def test_queue_budget_warnings_cover_over_cap_and_unenforced_budgets():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend checks")
    source = (Path(__file__).parents[1] / "rerunly/static/app.js").read_text()
    helper = source.split("function rerunBudgetWarning(", 1)[1].split("function rerunEstimateTotal", 1)[0]
    subprocess.run([node, "-e", r"""
const assert = require('node:assert/strict');
const fmtCost = n => '$' + n.toFixed(2);
function rerunBudgetWarning(""" + helper + r"""
const q = {mode:'background', agent:'Claude Code', budget_usd:3, estimate:{cost_usd:6}};
assert.match(rerunBudgetWarning(q), /Estimated cost \$6.00 exceeds the \$3.00/);
assert.match(rerunBudgetWarning(q), /stop before completing/);
assert.equal(rerunBudgetWarning({...q, budget_usd:6}), '');
assert.equal(rerunBudgetWarning({...q, budget_usd:null}), '');
assert.equal(rerunBudgetWarning({...q, estimate:null}), '');
assert.match(rerunBudgetWarning({...q, mode:'terminal'}), /not enforced in terminal mode/);
assert.match(rerunBudgetWarning({...q, agent:'Codex'}), /not enforced in Codex/);
"""], check=True)
    assert 'role="alert">${esc(rerunBudgetWarning(q))}' in source
    assert 'picked.filter((q) => q.kind !== "judge").map(rerunBudgetWarning)' in source


def test_queue_cards_keep_configuration_actions_and_unavailable_estimates():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend checks")
    source = (Path(__file__).parents[1] / "rerunly/static/app.js").read_text()
    helper = "function rerunQueueCard(" + source.split("function rerunQueueCard(", 1)[1].split("async function viewReruns", 1)[0]
    subprocess.run([node, "-e", r"""
const assert = require('node:assert/strict');
const esc = s => String(s).replaceAll('<','&lt;').replaceAll('"','&quot;');
const fmtDate = String, fmtAgo = String, fmtCost = String, fmtMs = String, fmtTokens = String;
const rerunSettings = () => 'settings', quotaForecast = () => 'forecast';
const rerunEstimateCost = () => 'Cost unavailable', rerunEstimateTotal = () => ({});
const rerunBudgetWarning = () => 'Budget warning';
const rerunState = {picked:new Set([7])};
""" + helper + r"""
let html = rerunQueueCard({id:7,agent:'Codex',model:'codex:test',mode:'terminal',
  needs_input:true, issues:[{message:'<permissions>'}],prompt:{id:'prompt',text:'<request>',project:'demo'}},0);
assert.match(html, /rq-card rr-needs-input selected/);
assert.match(html, /data-rr-pick="7" checked/);
assert.match(html, /href="#\/prompt\/prompt"/);
assert.match(html, /&lt;request>/);
assert.match(html, /&lt;permissions>/);
assert.match(html, /data-rr-config="7">Complete configuration/);
assert.match(html, /data-rr-launch-one="7"/);
assert.match(html, /data-rr-remove="7"/);
assert.match(html, /Cost unavailable/);
assert.match(html, /role="alert">Budget warning/);
html = rerunQueueCard({id:8,needs_input:false,mode:'background',prompt:null},1);
assert.match(html, /outside the current history/);
assert.match(html, /data-rr-config="8">Configure/);
assert.doesNotMatch(html, /class="rr-input-needed/);
"""], check=True)
