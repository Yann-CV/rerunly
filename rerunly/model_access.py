"""Read-only CLI account and model metadata for model picker access filters.

Never infer subscription entitlement from a model's price or historical use. CLI
catalogs report availability, not a promise about remaining quota or extra usage.
"""
import functools
import json
import os
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path

from . import processes


def _command(argv, **kwargs):
    # A scratch folder: the server's own folder could hold project settings and MCP servers to start.
    with tempfile.TemporaryDirectory(prefix='rerunly-account-') as scratch:
        return processes.run(argv, cwd=scratch, timeout=12, **kwargs)


@functools.lru_cache(maxsize=16)
def _account(agent: str, bucket: int, codex_home: str, claude_home: str) -> dict:
    unknown = {"mode": "unknown", "models": [], "plan": None}
    try:
        if agent == 'claude':
            status = json.loads(_command(['claude', 'auth', 'status', '--json']).stdout)
            if not status.get('loggedIn'):
                return unknown
            method = status.get('authMethod')
            if method == 'claude.ai' and status.get('apiProvider') == 'firstParty':
                mode = 'subscription'
            elif method in ('api_key', 'apiKey', 'console') or status.get('apiProvider') in ('bedrock', 'vertex', 'foundry'):
                mode = 'api'
            else:
                return unknown
            # SDK initialization only: no user message, inference, or task execution.
            request = {'type': 'control_request', 'request_id': 'rerunly-models', 'request': {'subtype': 'initialize'}}
            models = []
            try:
                result = _command(['claude', '--print', '--input-format', 'stream-json',
                                   '--output-format', 'stream-json', '--verbose', '--strict-mcp-config',
                                   '--settings', '{"disableAllHooks":true}', '--no-session-persistence'], input=json.dumps(request) + '\n')
                for line in result.stdout.splitlines():
                    event = json.loads(line)
                    response = event.get('response') or {}
                    if event.get('type') == 'control_response' and response.get('request_id') == 'rerunly-models':
                        models = (response.get('response') or {}).get('models') or []
            except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
                pass  # Keep the known billing route even if model discovery fails.
            return {'mode': mode, 'models': models, 'plan': status.get('subscriptionType')}
        home = Path(codex_home)
        config = home / 'config.toml'
        if config.exists():
            settings = tomllib.loads(config.read_text())
            # A custom provider/profile can select a different billing route.
            if settings.get('model_provider', 'openai') != 'openai' or settings.get('profile'):
                return unknown
        if os.environ.get('OPENAI_BASE_URL'):
            return unknown
        result = _command(['codex', 'login', 'status'])
        if result.returncode:
            return unknown
        status = (result.stdout + result.stderr).lower()
        mode = 'subscription' if 'logged in using chatgpt' in status else 'api' if 'logged in using an api key' in status else 'unknown'
        try:
            cache = json.loads((home / 'models_cache.json').read_text())
            models = cache.get('models') or []
            # Old catalogs are insufficient evidence for current subscription access.
            if time.time() - (home / 'models_cache.json').stat().st_mtime > 86400:
                models = []
        except (OSError, ValueError):
            models = []
        return {'mode': mode, 'models': models, 'plan': None}
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        return unknown


def accounts() -> dict:
    homes = (str(Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')),
             str(Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')))
    bucket = int(time.monotonic() // 60)
    return {agent: _account(agent, bucket, *homes) for agent in ('claude', 'codex')}


def annotate(models: list[dict], accounts: dict) -> list[dict]:
    # The live Claude menu can contain models missing from the configured price table.
    models = [dict(model) for model in models]
    known = {m['id'] for m in models}
    claude = accounts.get('claude', {})
    for entry in claude.get('models', []):
        for name in (entry.get('value'), entry.get('resolvedModel')):
            if name and name not in known and name != 'default':
                models.append({'id': name, 'label': name, 'agent': 'claude', 'kind': 'model', 'used': False,
                               'efforts': entry.get('supportedEffortLevels') or [], 'default_effort': None})
                known.add(name)
    for model in models:
        account = accounts.get(model['agent'], {})
        reported = set()
        default = None
        for entry in account.get('models', []):
            if model['agent'] == 'codex':
                if entry.get('visibility') == 'list':
                    reported.add('codex:' + entry.get('slug', ''))
            else:
                reported.update((entry.get('value'), entry.get('resolvedModel')))
                if entry.get('value') == 'default':
                    default = entry.get('resolvedModel')
        mode = account.get('mode', 'unknown')
        # Claude's menu includes credit-only models, even for subscription logins.
        # Presence in that menu is not evidence of inclusion in the subscription.
        listed = model['id'] in reported
        access = 'subscription' if mode == 'subscription' and model['agent'] == 'codex' and listed else 'api' if mode == 'api' else 'unknown'
        model.update(access=access, catalog_listed=listed, account_mode=mode,
                     account_plan=account.get('plan'), is_default=model['id'] == default)
    return models
