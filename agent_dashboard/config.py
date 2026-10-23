import os
from pathlib import Path

from .pricing import DEFAULT_PRICES

DEFAULT_DB_PATH = Path(
    os.environ.get("AGENT_DASHBOARD_DB", Path.home() / ".local/share/agent-dashboard/dashboard.db")
)
DEFAULT_PORT = 8765

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
}
