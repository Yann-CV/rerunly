import os
from pathlib import Path

from .pricing import DEFAULT_PRICES

DATA_DIR = Path.home() / ".local/share/rerunly"
LEGACY_DATA_DIR = Path.home() / ".local/share/agent-dashboard"  # Rerunly's folder before the rename
DEFAULT_DB_PATH = Path(os.environ.get("RERUNLY_DB") or os.environ.get("AGENT_DASHBOARD_DB") or DATA_DIR / "dashboard.db")
DEFAULT_PORT = 8765


def migrate_legacy_data() -> bool:
    """Move the data kept under the tool's former name (agent-dashboard) to Rerunly's folder, once."""
    if LEGACY_DATA_DIR.is_dir() and not DATA_DIR.exists():
        LEGACY_DATA_DIR.rename(DATA_DIR)
        return True
    return False

# Settings stored in the database, editable from the Setup page.
DEFAULT_SETTINGS = {
    "claude_projects_dir": str(Path.home() / ".claude/projects"),
    "prices": DEFAULT_PRICES,
    # Two prompts are "similar" when the Jaccard similarity of their word sets reaches this.
    "similarity_threshold": 0.6,
    # User rules adding human tags automatically, either
    #   {"tag": "name", "keywords": ["word", ...], "match": "all" | "any"}  (case-insensitive)
    #   {"tag": "name", "pattern": "regex"}
    "tag_rules": [],
    # Seconds between automatic re-ingestions while the server runs (0 disables).
    "auto_ingest_interval": 60,
    # "claude-code": use the cost Claude Code reports through OpenTelemetry when it covers the
    # whole prompt, the price table otherwise. "prices": always use the price table.
    "cost_source": "claude-code",
    # Projects, session ids and AI tags (skills `skill:x`, subagent types `agent:x`) left out of the
    # history: their prompts are still imported, shown nowhere.
    "excluded_projects": [],
    "excluded_sessions": [],
    "excluded_tags": [],
}
