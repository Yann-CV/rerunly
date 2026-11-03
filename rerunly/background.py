"""Run the dashboard detached from the terminal, and start it at login (XDG autostart)."""

import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path

from .config import DEFAULT_DB_PATH

STATE_DIR = DEFAULT_DB_PATH.parent
PID_FILE = STATE_DIR / "tray.pid"
LOG_FILE = STATE_DIR / "tray.log"
ICON = Path(__file__).parent / "static" / "icon.svg"


def tray_command(db_path: Path, port: int) -> list[str]:
    """Absolute paths only: the login session has neither this shell's PATH nor its working directory."""
    return [sys.executable, "-m", "rerunly", "--db", str(Path(db_path).resolve()), "tray", "--port", str(port)]


def autostart_file(name: str = "rerunly") -> Path:
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config / "autostart" / f"{name}.desktop"


# written before the rename to Rerunly; its command no longer exists
LEGACY_AUTOSTART = "agent-dashboard"


def autostart_enabled() -> bool:
    return autostart_file().is_file() or autostart_file(LEGACY_AUTOSTART).is_file()


def set_autostart(enable: bool, db_path: Path, port: int) -> Path:
    autostart_file(LEGACY_AUTOSTART).unlink(missing_ok=True)
    path = autostart_file()
    if not enable:
        path.unlink(missing_ok=True)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Rerunly\n"
        "Comment=Cost and time per prompt of your AI agents, from the status bar\n"
        f"Exec={shlex.join(tray_command(db_path, port))}\n"
        f"Icon={ICON}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
        # leave the session a few seconds to bring up its status bar
        "X-GNOME-Autostart-Delay=5\n"
    )
    return path


def running_pid() -> int | None:
    """The pid of the background dashboard, if it is alive."""
    try:
        pid = int(PID_FILE.read_text())
        os.kill(pid, 0)
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes() if Path("/proc").is_dir() else b"rerunly"
    except (OSError, ValueError):
        return None
    return pid if b"rerunly" in cmdline else None  # a recycled pid is someone else's process


def write_pid() -> None:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def clear_pid() -> None:
    if running_pid() in (None, os.getpid()):
        PID_FILE.unlink(missing_ok=True)


def start(db_path: Path, port: int) -> int:
    """Launch the tray (and its server) detached from this terminal; returns its pid."""
    if pid := running_pid():
        return pid
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with LOG_FILE.open("ab") as log:
        proc = subprocess.Popen(
            tray_command(db_path, port), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,  # survives closing the terminal
        )
    return proc.pid


def stop() -> int | None:
    pid = running_pid()
    if pid:
        os.kill(pid, signal.SIGTERM)
    return pid
