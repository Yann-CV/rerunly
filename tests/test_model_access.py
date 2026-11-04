import json
import subprocess
from types import SimpleNamespace

import pytest

from rerunly import model_access


@pytest.fixture(autouse=True)
def clear_accounts():
    model_access._account.cache_clear()
    yield
    model_access._account.cache_clear()


def test_subscription_uses_account_catalog_not_prices_or_history():
    models = [{'id': 'codex:current', 'agent': 'codex'}, {'id': 'codex:old', 'agent': 'codex'},
              {'id': 'codex:hidden', 'agent': 'codex'}, {'id': 'claude-old', 'agent': 'claude'}]
    accounts = {'codex': {'mode': 'subscription', 'models': [
        {'slug': 'current', 'visibility': 'list', 'supported_in_api': True},
        {'slug': 'hidden', 'visibility': 'hide', 'supported_in_api': True}]},
        'claude': {'mode': 'subscription', 'plan': 'pro', 'models': [
            {'value': 'default', 'resolvedModel': 'claude-current'},
            {'value': 'sonnet', 'resolvedModel': 'claude-current', 'supportedEffortLevels': ['low', 'high']}]}}
    result = {m['id']: m for m in model_access.annotate(models, accounts)}
    assert result['codex:current']['access'] == 'subscription'
    assert result['codex:old']['access'] == result['codex:hidden']['access'] == 'unknown'
    assert result['claude-old']['access'] == 'unknown'
    assert result['sonnet']['access'] == 'unknown'
    assert result['sonnet']['catalog_listed']
    assert result['claude-current']['is_default']
    assert result['claude-current']['account_plan'] == 'pro'
    assert 'access' not in models[0]  # Caller-owned catalog stays intact.


def test_claude_menu_does_not_prove_subscription_inclusion_for_credit_only_models():
    account = {'claude': {'mode': 'subscription', 'plan': 'pro', 'models': [
        {'value': 'fable', 'resolvedModel': 'claude-fable-5'},
        {'value': 'default', 'resolvedModel': 'claude-fable-5'},
    ]}}
    result = {m['id']: m for m in model_access.annotate(
        [{'id': 'claude-fable-5', 'agent': 'claude'}], account)}
    for name in ('fable', 'claude-fable-5'):
        assert result[name]['access'] == 'unknown'
        assert result[name]['catalog_listed'] is True
        assert result[name]['account_mode'] == 'subscription'
    assert result['claude-fable-5']['is_default']


def test_api_label_depends_on_active_login():
    models = [{'id': 'codex:current', 'agent': 'codex'}, {'id': 'claude-current', 'agent': 'claude'}]
    result = model_access.annotate(models, {'codex': {'mode': 'api'}, 'claude': {'mode': 'unknown'}})
    assert [m['access'] for m in result] == ['api', 'unknown']


def test_claude_discovery_sends_only_initialize_and_disables_hooks(monkeypatch, tmp_path):
    calls = []
    def command(argv, **kwargs):
        calls.append((argv, kwargs))
        if 'status' in argv:
            return SimpleNamespace(stdout=json.dumps({'loggedIn': True, 'authMethod': 'claude.ai',
                                                     'apiProvider': 'firstParty', 'subscriptionType': 'pro'}))
        request = json.loads(kwargs['input'])
        assert request['type'] == 'control_request'
        assert request['request'] == {'subtype': 'initialize'}
        assert json.loads(argv[argv.index('--settings') + 1]) == {'disableAllHooks': True}
        return SimpleNamespace(stdout=json.dumps({'type': 'control_response', 'response': {
            'request_id': request['request_id'], 'response': {'models': [{'value': 'sonnet'}]}}}))
    monkeypatch.setattr(model_access, '_command', command)
    account = model_access._account('claude', 1, str(tmp_path), str(tmp_path))
    assert account == {'mode': 'subscription', 'plan': 'pro', 'models': [{'value': 'sonnet'}]}
    assert len(calls) == 2


def test_catalog_failure_preserves_known_api_billing(monkeypatch, tmp_path):
    def command(argv, **kwargs):
        if 'status' in argv:
            return SimpleNamespace(stdout='{"loggedIn":true,"authMethod":"api_key"}')
        raise subprocess.TimeoutExpired(argv, 12)
    monkeypatch.setattr(model_access, '_command', command)
    assert model_access._account('claude', 1, str(tmp_path), str(tmp_path))['mode'] == 'api'


def test_codex_uses_recent_catalog_and_rejects_custom_provider(monkeypatch, tmp_path):
    monkeypatch.delenv('OPENAI_BASE_URL', raising=False)
    monkeypatch.setattr(model_access, '_command', lambda *args, **kwargs: SimpleNamespace(
        stdout='', stderr='Logged in using ChatGPT', returncode=0))
    cache = tmp_path / 'models_cache.json'
    cache.write_text('{"models":[{"slug":"current","visibility":"list"}]}')
    account = model_access._account('codex', 1, str(tmp_path), str(tmp_path))
    assert account['mode'] == 'subscription' and account['models'][0]['slug'] == 'current'
    import os
    os.utime(cache, (0, 0))
    assert model_access._account('codex', 2, str(tmp_path), str(tmp_path))['models'] == []
    (tmp_path / 'config.toml').write_text('model_provider = "custom"\n')
    assert model_access._account('codex', 3, str(tmp_path), str(tmp_path))['mode'] == 'unknown'


def test_missing_cli_is_unverified(monkeypatch, tmp_path):
    def missing(*args, **kwargs):
        raise FileNotFoundError()
    monkeypatch.setattr(model_access, '_command', missing)
    assert model_access._account('claude', 1, str(tmp_path), str(tmp_path))['mode'] == 'unknown'


def test_example_catalog_does_not_inspect_personal_account_access(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from rerunly.server import create_app
    from rerunly import replay
    monkeypatch.setattr(replay, 'agent_models', lambda *args: [{'id': 'illustrative', 'agent': 'claude'}])
    def forbidden():
        pytest.fail('Example app must not discover personal account access')
    monkeypatch.setattr(model_access, 'accounts', forbidden)
    with TestClient(create_app(tmp_path / 'examples.db', background=False, examples=True)) as client:
        response = client.get('/api/models')
        assert response.status_code == 200
        assert response.json()[0]['access'] == 'unknown'
