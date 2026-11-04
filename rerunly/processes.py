"""Run an agent CLI to completion, and take its whole process group down when it times out."""

import os
import signal
import subprocess


def run(argv: list[str], *, cwd=None, input: str | None = None, timeout: float) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        # Only the program's name: the arguments can hold a whole prompt, and this message gets stored.
        raise subprocess.TimeoutExpired(argv[0], timeout) from None
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def write_atomic(path, text: str, *, mode: int | None = None) -> None:
    """Replace contents atomically, following symlinks and preserving the mode unless explicitly private."""
    target = path.resolve()
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY,
                 mode if mode is not None else target.stat().st_mode & 0o777 if target.exists() else 0o600)
    if mode is not None:
        os.fchmod(fd, mode)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
