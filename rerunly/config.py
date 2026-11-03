import math
import os
from pathlib import Path

from .pricing import DEFAULT_PRICES

DATA_DIR = Path.home() / ".local/share/rerunly"
LEGACY_DATA_DIR = Path.home() / ".local/share/agent-dashboard"  # Rerunly's folder before the rename
DEFAULT_DB_PATH = Path(os.environ.get("RERUNLY_DB") or os.environ.get("AGENT_DASHBOARD_DB") or DATA_DIR / "dashboard.db")
DEFAULT_PORT = 8765


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def migrate_legacy_data() -> bool:
    """Move the data kept under the tool's former name (agent-dashboard) to Rerunly's folder, once."""
    if LEGACY_DATA_DIR.is_dir() and not DATA_DIR.exists():
        LEGACY_DATA_DIR.rename(DATA_DIR)
        return True
    return False

# Settings stored in the database, editable from the Setup page.
DEFAULT_SETTINGS = {
    "claude_enabled": True,
    "codex_enabled": True,
    "model_checks": {"enabled": False, "interval_hours": 24, "agents": ["claude", "codex"]},
    "claude_projects_dir": str(claude_home() / "projects"),
    "codex_sessions_dir": str(Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"),
    "prices": DEFAULT_PRICES,
    "auto_price_lookup": True,
    "auto_prices": {},
    "price_catalogs": {},
    "price_provenance": {},
    "price_lookup_missing": [],
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
    # Projects, session ids and AI tags (skills `skill:x`, subagent types `agent:x`, models, ...) left out of the
    # history: their prompts are still imported, shown nowhere.
    "excluded_projects": [],
    "excluded_sessions": [],
    "excluded_tags": [],
    # your tags (by hand or from auto-tag rules) left out the same way: kept apart from AI tags of the same name
    "excluded_your_tags": [],
    # the model rating how much a rerun's output differs from its original ("" = Claude Code's default)
    "judge_model": "claude-haiku-5-5",
    # its effort level ("" = the model's default)
    "judge_effort": "",
}


def _is_text(value) -> bool:
    return isinstance(value, str) and "\x00" not in value


def _is_number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _is_text_list(value) -> bool:
    return isinstance(value, list) and all(_is_text(item) for item in value)


def _is_price_table(value) -> bool:
    return isinstance(value, dict) and all(
        _is_text(model) and isinstance(price, dict) and {"input", "output", "cache_read"} <= set(price)
        and all(_is_number(price[k]) and price[k] >= 0 for k in ("input", "output", "cache_read"))
        for model, price in value.items())


def _is_rule_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(rule, dict) and _is_text(rule.get("tag")) for rule in value)


_SETTING_CHECKS = {
    "claude_enabled": lambda v: isinstance(v, bool),
    "codex_enabled": lambda v: isinstance(v, bool),
    "auto_price_lookup": lambda v: isinstance(v, bool),
    "claude_projects_dir": _is_text,
    "codex_sessions_dir": _is_text,
    "prices": _is_price_table,
    "auto_prices": _is_price_table,
    "price_catalogs": lambda v: isinstance(v, dict),
    "price_provenance": lambda v: isinstance(v, dict),
    "price_lookup_missing": _is_text_list,
    "similarity_threshold": lambda v: _is_number(v) and 0 <= v <= 1,
    "tag_rules": _is_rule_list,
    "auto_ingest_interval": lambda v: type(v) is int and v >= 0,
    "cost_source": lambda v: v in ("claude-code", "prices"),
    "excluded_projects": _is_text_list,
    "excluded_sessions": _is_text_list,
    "excluded_tags": _is_text_list,
    "excluded_your_tags": _is_text_list,
    "judge_model": _is_text,
    "judge_effort": _is_text,
}


def invalid_settings(values: dict) -> list[str]:
    """The known settings whose value could not work: saving one could break every page."""
    return [key for key, check in _SETTING_CHECKS.items() if key in values and not check(values[key])]
