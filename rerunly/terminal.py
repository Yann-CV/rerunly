"""Watch one terminal rerun's transcript and exit its interactive CLI after the turn.

Executed by absolute path so terminal launches also work outside the package directory.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import termios
import time


class CompletionWatcher:
    def __init__(self, root: str, session_id: str, codex: bool):
        self.root = Path(root)
        self.session_id = session_id
        self.codex = codex
        self.started = time.time()
        self.offsets = {}
        self.linked = set()

    def complete(self) -> bool:
        pattern = '*.jsonl' if self.codex else f'{self.session_id}.jsonl'
        paths = tuple(self.linked) if self.linked else self.root.rglob(pattern)
        for path in paths:
            try:
                if path.stat().st_mtime < self.started:
                    continue
                if not self.codex:
                    self.linked.add(path)  # Found: no need to search the whole transcript tree again.
                with path.open() as stream:
                    stream.seek(self.offsets.get(path, 0))
                    while line := stream.readline():
                        try:
                            event = json.loads(line)
                        except ValueError:
                            # A writer may not have finished the final JSON record yet.
                            break
                        self.offsets[path] = stream.tell()
                        if self.codex:
                            payload = event.get('payload') or {}
                            text = None
                            if event.get('type') == 'event_msg' and payload.get('type') == 'user_message':
                                text = payload.get('message', '')
                            elif event.get('type') == 'response_item' and payload.get('role') == 'user':
                                text = "\n".join(block.get("text", "") for block in payload.get("content", []) if isinstance(block, dict))
                            if text is not None and text.startswith(f'<rerunly-replay id="{self.session_id}"/>'):
                                self.linked.add(path)
                            if path in self.linked and event.get('type') == 'event_msg' and payload.get('type') in (
                                'task_complete', 'turn_complete', 'task_completed'
                            ):
                                return True
                        elif event.get('type') == 'system' and event.get('subtype') == 'turn_duration':
                            return True
            except (OSError, UnicodeError):
                continue
        return False


def run(argv: list[str], watcher: CompletionWatcher, keep_open: bool, status_file: str = '') -> int:
    # Give the CLI its own foreground process group so approvals keep using the real
    # terminal, and shutdown signals reach only this rerun (including npx children).
    tty = sys.stdin.fileno()
    attributes = termios.tcgetattr(tty)
    foreground = os.tcgetpgrp(tty)
    previous = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    child = None
    completed = False
    def record(state, detail=''):
        if status_file:
            try:
                path = Path(status_file)
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_suffix('.tmp')
                temp.write_text(json.dumps({'state': state, 'pid': os.getpid(), 'detail': detail}))
                temp.replace(path)
            except OSError:
                pass
    record('running')
    try:
        child = subprocess.Popen(argv, process_group=0)
        os.tcsetpgrp(tty, child.pid)
        os.killpg(child.pid, signal.SIGCONT)
        while child.poll() is None:
            if watcher.complete():
                completed = True
                # The completion record is persisted; allow the CLI's final render
                # to finish before terminating its interactive input loop.
                try:
                    child.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
                break
            time.sleep(0.25)
    except Exception as error:
        record('failed', str(error))
        raise
    finally:
        os.tcsetpgrp(tty, foreground)
        termios.tcsetattr(tty, termios.TCSADRAIN, attributes)
        signal.signal(signal.SIGTTOU, previous)
    status = 0 if completed else child.returncode
    record('done' if completed else 'failed', 'Turn completed.' if completed else f'Agent exited with status {status} before a completed turn was recorded.')
    if keep_open:
        input(f'\nRerun finished (exit {status}). Press Enter to close this terminal.\n')
    return status if status >= 0 else 128 - status


if __name__ == '__main__':
    root, session_id, codex, keep_open, *command = sys.argv[1:]
    status_file = ''
    if command[:1] == ['--status-file']:
        status_file, command = command[1], command[2:]
    raise SystemExit(run(command, CompletionWatcher(root, session_id, codex == 'true'), keep_open == 'true', status_file))
