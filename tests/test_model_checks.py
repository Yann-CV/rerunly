"""Catalog refreshes must never send inference requests."""
import json
import sys
import threading
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from rerunly import db, model_checks as mc
from rerunly.server import create_app


def wait_finished(checks):
    deadline = time.monotonic() + 3
    while checks.running and time.monotonic() < deadline:
        time.sleep(.01)
    assert not checks.running
    return checks.status()['scan']


def configure(database, **values):
    c = db.connect(database)
    try:
        db.save_settings(c, {'model_checks': {'enabled': False, 'interval_hours': 24, 'agents': ['codex'], **values}})
    finally:
        c.close()


def test_discovery_refreshes_menu_and_deduplicates_without_inference(monkeypatch, tmp_path):
    calls = []
    def command(argv, cwd, cancel, **kwargs):
        calls.append(argv)
        if argv[0] == 'codex':
            assert argv == ['codex', 'debug', 'models']
            return 0, json.dumps({'models': [
                {'slug': 'first', 'visibility': 'list'}, {'slug': 'hidden', 'visibility': 'hide'},
                {'slug': 'first', 'visibility': 'list'}, {'slug': 'bad;command', 'visibility': 'list'}]})
        request = json.loads(kwargs['input_text'])
        # Only the SDK initialize control message, never a user prompt or a query.
        assert request == {'type': 'control_request', 'request_id': 'rerunly-model-check',
                           'request': {'subtype': 'initialize'}}
        assert argv[-1] == '--no-session-persistence'
        assert '--model' not in argv
        return 0, json.dumps({'response': {'request_id': request['request_id'], 'response': {'models': [
            {'value': 'sonnet', 'resolvedModel': 'claude-current'}, {'value': 'opus', 'resolvedModel': 'claude-current'}]}}})
    monkeypatch.setattr(mc, 'run_command', command)
    assert mc.discover('codex', tmp_path, threading.Event()) == ['first']
    assert mc.discover('claude', tmp_path, threading.Event()) == ['claude-current']
    assert len(calls) == 2


def test_worker_reads_catalogs_without_launching_per_model_prompts(monkeypatch, tmp_path):
    database = tmp_path / 'checks.db'
    configure(database, agents=['codex', 'claude'])
    calls = []
    def discover(agent, *args):
        calls.append(agent)
        return ['first', 'second']
    monkeypatch.setattr(mc, 'discover', discover)
    monkeypatch.setattr(mc, 'run_command', lambda *a, **k: pytest.fail('No per-model command or inference is allowed'))
    checks = mc.ModelChecks(database, threading.Event())
    checks.start()
    scan = wait_finished(checks)
    assert calls == ['codex', 'claude']
    assert len(scan['results']) == 4 and scan['method'] == 'catalog'
    assert all(r['status'] == 'listed' for r in scan['results'])
    assert all('No test prompt was sent' in r['message'] for r in scan['results'])


def test_scheduler_is_opt_in_persists_and_does_not_repeat_before_due(monkeypatch, tmp_path):
    database = tmp_path / 'checks.db'
    calls = []
    def discover(*args):
        calls.append(args[0])
        return ['a', 'b']
    monkeypatch.setattr(mc, 'discover', discover)
    checks = mc.ModelChecks(database, threading.Event())
    assert not checks.tick() and not calls
    configure(database, enabled=True)
    assert checks.tick()
    scan = wait_finished(checks)
    assert scan['status'] == 'completed' and len(scan['results']) == 2
    assert not checks.tick() and len(calls) == 1
    restored = mc.ModelChecks(database, threading.Event())
    assert restored.status()['scan'] == scan
    assert restored.status()['next_at']
    monkeypatch.setattr(mc.time, 'time', lambda: datetime.fromisoformat(scan['started_at']).timestamp() + 86401)
    assert restored.tick()
    assert len(wait_finished(restored)['results']) == 2


def test_concurrent_start_and_cancellation_stop_discovery(monkeypatch, tmp_path):
    database = tmp_path / 'checks.db'
    configure(database)
    entered = threading.Event()
    def discover(agent, cwd, cancel):
        entered.set()
        assert cancel.wait(2)
        raise InterruptedError()
    monkeypatch.setattr(mc, 'discover', discover)
    checks = mc.ModelChecks(database, threading.Event())
    assert checks.start() and entered.wait(1)
    assert not checks.start()
    checks.cancel.set()
    scan = wait_finished(checks)
    assert scan['status'] == 'cancelled' and scan['results'] == []


def test_discovery_failure_does_not_claim_models_unavailable(monkeypatch, tmp_path):
    database = tmp_path / 'checks.db'
    configure(database, agents=['codex', 'claude'])
    def discover(agent, *args):
        if agent == 'codex':
            raise ValueError('Could not read catalog')
        return ['first']
    monkeypatch.setattr(mc, 'discover', discover)
    checks = mc.ModelChecks(database, threading.Event())
    checks.start()
    results = wait_finished(checks)['results']
    assert [r['status'] for r in results] == ['discovery_failed', 'listed']


def test_timeout_and_cancel_kill_child_process(tmp_path):
    with pytest.raises(TimeoutError):
        mc.run_command([sys.executable, '-c', 'import time; time.sleep(5)'], tmp_path, threading.Event(), timeout=.1)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(InterruptedError):
        mc.run_command([sys.executable, '-c', 'import time; time.sleep(5)'], tmp_path, cancel)


def test_api_validation_examples_and_default_no_inference(monkeypatch, tmp_path):
    monkeypatch.setattr(mc, 'discover', lambda *a: pytest.fail('No real discovery should start'))
    with TestClient(create_app(tmp_path / 'app.db', background=False)) as client:
        state = client.get('/api/model-checks').json()
        assert state['config']['enabled'] is False and state['scan']['status'] == 'idle'
        config = {'enabled': True, 'agents': ['codex'], 'interval_hours': 24}
        assert client.put('/api/model-checks', json=config).status_code == 200
        assert client.put('/api/model-checks', json={**config, 'interval_hours': 0}).status_code == 400
        assert client.put('/api/model-checks', json={**config, 'agents': []}).status_code == 400
        assert client.put('/api/settings', json={'model_checks': {**config, 'enabled': 'true'}}).status_code == 400
        assert client.post('/examples/api/model-checks/start').status_code == 400
        assert client.post('/examples/api/model-checks/cancel').status_code == 400
        assert client.put('/examples/api/model-checks', json=config).status_code == 400
        assert client.get('/examples/api/model-checks').json()['scan']['status'] == 'idle'
        monkeypatch.setattr(mc.ModelChecks, 'start', lambda self: False)
        assert client.post('/api/model-checks/start').status_code == 200
