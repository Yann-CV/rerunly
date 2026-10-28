"""Claude Code's usage limits: it reports them to its statusline command only, never in its transcripts.

Installing replaces the statusline command with one saving the statusline input in Rerunly's data folder,
then running the user's own command (kept in a script) on the same input: the statusline looks the same.
Removing puts the user's own statusline back."""

import json
import shlex
from datetime import datetime, timezone
from pathlib import Path

from .config import DATA_DIR

DIR = DATA_DIR
MARKER = "rerunly-statusline"
# statusline `rate_limits` keys, and the length of their window in minutes
WINDOWS = {"five_hour": 300, "seven_day": 10080}


def _paths():
    return DIR / "claude-statusline.json", DIR / "claude-statusline.sh", DIR / "claude-statusline-original.json"


def command() -> str:
    data, script, _ = _paths()
    tmp = shlex.quote(str(data)) + ".$$"  # written whole, then renamed: never read half-written
    return (f"input=$(cat); printf '%s' \"$input\" > {tmp} && mv {tmp} {shlex.quote(str(data))}; "
            f"[ -f {shlex.quote(str(script))} ] && printf '%s' \"$input\" | sh {shlex.quote(str(script))}; true # {MARKER}")


def installed(settings: dict) -> bool:
    return MARKER in json.dumps(settings.get("statusLine") or {})


def install(settings: dict, on: bool = True) -> dict:
    """Claude Code settings with the capturing statusline (or, `on=False`, the user's own one back)."""
    _, script, original_file = _paths()
    current = settings.get("statusLine")
    if on and not installed(settings):
        DIR.mkdir(parents=True, exist_ok=True)
        original_file.write_text(json.dumps(current))
        if current and current.get("command"):
            script.write_text(current["command"] + "\n")
        else:
            script.unlink(missing_ok=True)
        settings["statusLine"] = {**(current or {}), "type": "command", "command": command()}
    elif not on and installed(settings):
        original = json.loads(original_file.read_text()) if original_file.exists() else None
        if original:
            settings["statusLine"] = original
        else:
            settings.pop("statusLine", None)
        original_file.unlink(missing_ok=True)
        script.unlink(missing_ok=True)
    return settings


def rate_limits():
    """The usage limits of the last statusline input: {"observed_at", "plan", "windows"}, or None."""
    data, _, _ = _paths()
    try:
        limits = json.loads(data.read_text()).get("rate_limits") or {}
        observed = datetime.fromtimestamp(data.stat().st_mtime, timezone.utc).isoformat()
    except (OSError, ValueError, AttributeError):
        return None
    windows = []
    for key, minutes in WINDOWS.items():
        w = limits.get(key)
        if not isinstance(w, dict) or w.get("used_percentage") is None:
            continue
        resets = w.get("resets_at")
        if isinstance(resets, str):
            try:
                resets = datetime.fromisoformat(resets.replace("Z", "+00:00")).timestamp()
            except ValueError:
                resets = None
        windows.append({"used_percent": w["used_percentage"], "window_minutes": minutes, "resets_at": resets})
    return {"observed_at": observed, "plan": None, "windows": windows} if windows else None
