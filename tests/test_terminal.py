import json
import os
from pathlib import Path
import pty
import select
import signal
import sys
import time

import pytest

from rerunly.terminal import CompletionWatcher


@pytest.mark.parametrize('user_event', [False, True])
def test_codex_completion_requires_matching_rerun_and_finished_turn(tmp_path, user_event):
    watcher = CompletionWatcher(str(tmp_path), 'ours', True)
    path = tmp_path / 'rollout.jsonl'
    completed = json.dumps({'type': 'event_msg', 'payload': {'type': 'task_complete'}}) + '\n'
    path.write_text(completed)
    assert not watcher.complete()
    with path.open('a') as stream:
        text = '<rerunly-replay id="ours"/>\nRequest'
        event = ({'type': 'event_msg', 'payload': {'type': 'user_message', 'message': text}} if user_event else
                 {'type': 'response_item', 'payload': {'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}})
        stream.write(json.dumps(event) + '\n')
        stream.write('{"type":"event_msg","payload":')
    assert not watcher.complete()
    with path.open('a') as stream:
        stream.write('{"type":"task_complete"}}\n')
    assert watcher.complete()


@pytest.mark.parametrize('codex', [False, True])
@pytest.mark.parametrize('keep_open', [False, True])
def test_terminal_exits_after_answer_and_keeps_approval_input(tmp_path, codex, keep_open):
    script = Path(__file__).parents[1] / 'rerunly/terminal.py'
    transcript = tmp_path / ('rollout.jsonl' if codex else 'ours.jsonl')
    fake = '''
import json, sys, time
from pathlib import Path
path = Path(sys.argv[1])
codex = sys.argv[2] == 'true'
print('approve?', flush=True)
assert input() == 'approved'
print('final answer', flush=True)
with path.open('w') as stream:
    if codex:
        stream.write(json.dumps({'type':'response_item','payload':{'role':'user','content':[
            {'text':'<rerunly-replay id="ours"/>\\nTask'}]}}) + '\\n')
        event = {'type':'event_msg','payload':{'type':'task_complete'}}
    else:
        event = {'type':'system','subtype':'turn_duration'}
    stream.write(json.dumps(event) + '\\n')
time.sleep(60)
'''
    child, fd = pty.fork()
    if child == 0:
        os.execv(sys.executable, [sys.executable, str(script), str(tmp_path), 'ours',
                 str(codex).lower(), str(keep_open).lower(), '--status-file', str(tmp_path / 'status.json'), sys.executable, '-c', fake,
                 str(transcript), str(codex).lower()])
    output = b''
    approved = entered = False
    deadline = time.monotonic() + 12
    try:
        while time.monotonic() < deadline:
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
                if b'approve?' in output and not approved:
                    # No completion yet: the terminal must still accept approvals.
                    assert not transcript.exists()
                    os.write(fd, b'approved\n')
                    approved = True
                if b'Press Enter' in output and not entered:
                    assert keep_open
                    assert os.waitpid(child, os.WNOHANG) == (0, 0)
                    os.write(fd, b'\n')
                    entered = True
        else:
            pytest.fail(f'Terminal did not exit: {output!r}')
        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0, output.decode(errors='replace')
        assert b'final answer' in output
        assert entered == keep_open
        assert json.loads((tmp_path / 'status.json').read_text())['state'] == 'done'
    finally:
        os.close(fd)
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
