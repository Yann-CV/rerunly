"""Periodic model-menu refreshes through installed agents, without inference requests."""
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import db, replay

TIMEOUT = 20
INTERVALS = (6, 24, 168)


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def run_command(argv, cwd, cancel, *, input_text=None, timeout=TIMEOUT):
    """Run `argv` until it ends, `cancel` is set or `timeout` passes, then kill its whole process group: its
    exit code and the tail of its output (with its errors when it failed), for the caller to classify."""
    with tempfile.TemporaryFile(mode='w+') as output, tempfile.TemporaryFile(mode='w+') as errors:
        process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE if input_text else subprocess.DEVNULL,
                                   stdout=output, stderr=errors, text=True, start_new_session=True)
        if input_text and process.stdin:
            try:
                process.stdin.write(input_text)
                process.stdin.close()
            except BrokenPipeError:
                pass
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if cancel.wait(0.1):
                    raise InterruptedError('Check cancelled')
                if time.monotonic() >= deadline:
                    raise TimeoutError('Agent did not finish before the time limit')
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        output.seek(0, 2)
        output.seek(max(0, output.tell() - 128_000))
        text = output.read()
        if process.returncode:
            errors.seek(0, 2)
            errors.seek(max(0, errors.tell() - 32_000))
            text += '\n' + errors.read()
        return process.returncode, text


def discover(agent, cwd, cancel):
    """The models `agent` offers, from the machine-readable catalogs behind /model: /model is never sent as a prompt."""
    if agent == 'codex':
        code, text = run_command(['codex', 'debug', 'models'], cwd, cancel, timeout=20)
        if code:
            raise ValueError('Could not refresh the Codex catalog. Check login and CLI version.')
        data = json.loads(text)
        entries = data if isinstance(data, list) else data.get('models', [])
        models = [entry.get('slug', '') for entry in entries if entry.get('visibility') == 'list']
    else:
        request = {'type': 'control_request', 'request_id': 'rerunly-model-check',
                   'request': {'subtype': 'initialize'}}
        code, text = run_command(['claude', '--print', '--input-format', 'stream-json',
            '--output-format', 'stream-json', '--verbose', '--strict-mcp-config',
            '--settings', '{"disableAllHooks":true}', '--no-session-persistence'], cwd, cancel,
            input_text=json.dumps(request) + '\n', timeout=20)
        models = []
        for line in text.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            response = event.get('response') or {}
            if response.get('request_id') == request['request_id']:
                for entry in (response.get('response') or {}).get('models') or []:
                    name = entry.get('resolvedModel') or entry.get('value')
                    if name and name != 'default':
                        models.append(name)
        if code or not models:
            raise ValueError('Could not refresh the Claude catalog. Check login and CLI version.')
    models = list(dict.fromkeys(m for m in models if replay.MODEL_RE.fullmatch(m)))
    if not models:
        raise ValueError('The agent returned no models to test.')
    return models



class ModelChecks:
    def __init__(self, database: Path, stop: threading.Event):
        self.database, self.stop = database, stop
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.running = False
        c = db.connect(database)
        try:
            state = self._read(c)
            if state.get('status') == 'running':
                state.update(status='interrupted', finished_at=now(), current=None)
                self._write(c, state)
        finally:
            c.close()

    @staticmethod
    def _read(c):
        row = c.execute('SELECT value FROM model_check_state WHERE id=1').fetchone()
        return json.loads(row['value']) if row else {'status': 'idle', 'results': []}

    @staticmethod
    def _write(c, state):
        with c:
            c.execute('INSERT INTO model_check_state(id,value) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value',
                      (json.dumps(state),))

    def status(self):
        c = db.connect(self.database)
        try:
            state = self._read(c)
            config = db.get_settings(c)['model_checks']
            next_at = None
            if config['enabled'] and state.get('started_at'):
                next_at = datetime.fromtimestamp(datetime.fromisoformat(state['started_at']).timestamp() + config['interval_hours'] * 3600, timezone.utc).isoformat(timespec='seconds')
            return {'config': config, 'scan': state, 'next_at': next_at}
        finally:
            c.close()

    def start(self):
        with self.lock:
            if self.running:
                return False
            c = db.connect(self.database)
            try:
                settings = db.get_settings(c)
                agents = [a for a in settings['model_checks']['agents'] if settings[a + '_enabled']]
                if not agents:
                    raise ValueError('Choose at least one connected agent in Setup and in the check settings.')
                self.cancel.clear()
                self._write(c, {'status': 'running', 'method': 'catalog', 'started_at': now(), 'results': [], 'current': None})
            finally:
                c.close()
            self.running = True
            threading.Thread(target=self._run, args=(agents,), daemon=True).start()
            return True

    def _run(self, agents):
        c = db.connect(self.database)
        state = self._read(c)
        try:
            with tempfile.TemporaryDirectory(prefix='rerunly-model-check-') as scratch:
                for agent in agents:
                    if self.cancel.is_set() or self.stop.is_set():
                        break
                    state['current'] = f'{agent}: refreshing model catalog'
                    self._write(c, state)
                    try:
                        models = discover(agent, scratch, self.cancel)
                    except InterruptedError:
                        break
                    except (OSError, ValueError, TypeError, AttributeError, TimeoutError):
                        state['results'].append({'agent': agent, 'model': None, 'status': 'discovery_failed',
                            'checked_at': now(), 'message': 'Catalog refresh failed. Check agent login, connection and CLI version.'})
                        self._write(c, state)
                        continue
                    for model in models:
                        if self.cancel.is_set() or self.stop.is_set():
                            break
                        state['results'].append({'agent': agent, 'model': model, 'checked_at': now(),
                            'status': 'listed', 'message': 'Listed in the agent’s model menu. No test prompt was sent; runtime access and subscription inclusion are not verified.'})
                    self._write(c, state)
            state['status'] = 'cancelled' if self.cancel.is_set() or self.stop.is_set() else 'completed'
        except Exception:
            state['status'] = 'failed'
        finally:
            state.update(finished_at=now(), current=None)
            self._write(c, state)
            c.close()
            with self.lock:
                self.running = False

    def tick(self):
        status = self.status()
        config, state = status['config'], status['scan']
        if not config['enabled'] or self.stop.is_set():
            return False
        if state.get('started_at') and time.time() - datetime.fromisoformat(state['started_at']).timestamp() < config['interval_hours'] * 3600:
            return False
        return self.start()

    def loop(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: S110
                pass  # Keep the scheduler alive; a manual check can report agent failures.
            self.stop.wait(30)
