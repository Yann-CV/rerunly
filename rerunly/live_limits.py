"""Subscription usage asked from the provider, for an agent that is logged in but reported none locally yet.

Uses the agent's own login (Claude Code's `.credentials.json`, Codex's `auth.json`) read-only: an expired token is
not refreshed, since refreshing rotates the tokens the agent itself relies on. Same shape as the local readings:
{"observed_at", "plan", "windows": [{"used_percent", "window_minutes", "resets_at"}]}, or None."""

import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .statusline import WINDOWS

CACHE_SECONDS = 60
_cache: dict[str, tuple[float, dict | None]] = {}


def _get(url, headers):
    req = urllib.request.Request(url, headers={"User-Agent": "rerunly", **headers})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _cached(key, fetch):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    try:
        value = fetch()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        value = None  # not logged in, offline, or an answer of another shape
    _cache[key] = (time.monotonic(), value)
    return value


def _now():
    return datetime.now(timezone.utc).isoformat()


def _claude():
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    auth = json.loads((home / ".credentials.json").read_text())["claudeAiOauth"]
    if (auth.get("expiresAt") or 0) / 1000 <= time.time():
        return None
    usage = _get("https://api.anthropic.com/api/oauth/usage",
                 {"Authorization": "Bearer " + auth["accessToken"], "anthropic-beta": "oauth-2025-04-20"})
    windows = []
    for key, minutes in WINDOWS.items():
        w = usage.get(key)
        if not isinstance(w, dict) or w.get("utilization") is None:
            continue
        resets = w.get("resets_at")
        resets = datetime.fromisoformat(resets.replace("Z", "+00:00")).timestamp() if isinstance(resets, str) else None
        windows.append({"used_percent": w["utilization"], "window_minutes": minutes, "resets_at": resets})
    return {"observed_at": _now(), "plan": auth.get("subscriptionType"), "windows": windows} if windows else None


def _codex():
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    tokens = json.loads((home / "auth.json").read_text())["tokens"]
    usage = _get("https://chatgpt.com/backend-api/wham/usage",
                 {"Authorization": "Bearer " + tokens["access_token"], "ChatGPT-Account-Id": tokens["account_id"]})
    limits = usage.get("rate_limit") or {}
    windows = [{"used_percent": w["used_percent"], "window_minutes": (w.get("limit_window_seconds") or 0) // 60 or None,
                "resets_at": w.get("reset_at")}
               for w in (limits.get(k) for k in ("primary_window", "secondary_window"))
               if isinstance(w, dict) and w.get("used_percent") is not None]
    return {"observed_at": _now(), "plan": usage.get("plan_type"), "windows": windows} if windows else None


def claude_code():
    return _cached("claude", _claude)


def codex():
    return _cached("codex", _codex)
