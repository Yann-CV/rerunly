import json
import sqlite3
from pathlib import Path

from .config import DEFAULT_SETTINGS

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    project TEXT,
    title TEXT,
    path TEXT NOT NULL,
    signature TEXT,
    reported_cost_usd REAL
);

-- Usage columns hold what the main agent spent; agent_* columns hold its subagents.
CREATE TABLE IF NOT EXISTS prompts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    source TEXT NOT NULL,
    project TEXT,
    cwd TEXT,
    git_branch TEXT,
    command TEXT,
    text TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    wall_ms INTEGER,
    active_ms INTEGER,
    input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
    cache_write_tokens INTEGER, web_search_requests INTEGER,
    cost_usd REAL,
    api_calls INTEGER,
    tool_calls INTEGER,
    tools TEXT,
    models TEXT,
    unpriced_models TEXT,
    agent_count INTEGER,
    agent_tokens INTEGER,
    agent_cost_usd REAL,
    total_tokens INTEGER,
    total_cost_usd REAL
);
CREATE INDEX IF NOT EXISTS prompts_started ON prompts(started_at);
CREATE INDEX IF NOT EXISTS prompts_session ON prompts(session_id);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    prompt_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    agent_type TEXT,
    description TEXT,
    started_at TEXT,
    ended_at TEXT,
    wall_ms INTEGER,
    input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
    cache_write_tokens INTEGER, web_search_requests INTEGER,
    total_tokens INTEGER,
    cost_usd REAL,
    api_calls INTEGER,
    tool_calls INTEGER,
    tools TEXT,
    models TEXT,
    persona TEXT,
    skill TEXT
);
CREATE INDEX IF NOT EXISTS agents_prompt ON agents(prompt_id);

-- auto: 0 = human tag added by hand, 1 = AI tag derived from the transcript,
-- 2 = human tag from an auto-tag rule. Values 1 and 2 are recomputed on every ingestion.
CREATE TABLE IF NOT EXISTS tags (
    prompt_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    auto INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (prompt_id, tag)
);
CREATE INDEX IF NOT EXISTS tags_tag ON tags(tag);

-- When the user created a human tag (by hand or with keywords), and what it is for. AI tags have
-- no row: their creation date is their first prompt's.
CREATE TABLE IF NOT EXISTS tag_meta (
    tag TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    description TEXT
);

-- One row per `claude_code.api_request` OpenTelemetry event: Claude Code's own cost figures.
CREATE TABLE IF NOT EXISTS otel_requests (
    id TEXT PRIMARY KEY,
    prompt_id TEXT,
    session_id TEXT,
    timestamp TEXT,
    model TEXT,
    query_source TEXT,
    agent_name TEXT,
    cost_usd REAL,
    duration_ms INTEGER,
    input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS otel_prompt ON otel_requests(prompt_id);

CREATE VIEW IF NOT EXISTS otel_by_prompt AS
SELECT prompt_id, SUM(cost_usd) cost_usd, SUM(duration_ms) api_ms, COUNT(*) requests
FROM otel_requests WHERE prompt_id IS NOT NULL GROUP BY prompt_id;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# Columns added after the first release: (table, column, type). Appended at the end of the
# table, so inserts listing values in table order keep working.
ADDED_COLUMNS = [("agents", "persona", "TEXT"), ("agents", "skill", "TEXT"), ("tag_meta", "description", "TEXT")]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, kind in ADDED_COLUMNS:
        if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


def create_prompt_view(conn: sqlite3.Connection, prefer_official: bool) -> None:
    """Temporary view `pv`: prompts plus the cost to display.

    `total_cost_usd` becomes Claude Code's own cost (OpenTelemetry) when it covers every API
    call of the prompt, the price-table estimate otherwise; `estimated_cost_usd`,
    `official_cost_usd` and `cost_source` say which one was used."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(prompts)")]
    covered = (
        "o.requests >= p.api_calls + (SELECT COALESCE(SUM(a.api_calls), 0) FROM agents a WHERE a.prompt_id = p.id)"
    )
    official = f"CASE WHEN {covered} THEN o.cost_usd END"
    shown = f"COALESCE({official}, p.total_cost_usd)" if prefer_official else "p.total_cost_usd"
    select = [f"p.{c}" for c in cols if c != "total_cost_usd"] + [
        f"{shown} AS total_cost_usd",
        "p.total_cost_usd AS estimated_cost_usd",
        f"{official} AS official_cost_usd",
        "o.api_ms AS api_ms",
        f"CASE WHEN {int(prefer_official)} AND ({official}) IS NOT NULL THEN 'claude-code' ELSE 'prices' END AS cost_source",
    ]
    conn.execute("DROP VIEW IF EXISTS temp.pv")
    conn.execute(
        f"CREATE TEMP VIEW pv AS SELECT {', '.join(select)} "
        "FROM prompts p LEFT JOIN otel_by_prompt o ON o.prompt_id = p.id"
    )


def get_settings(conn: sqlite3.Connection) -> dict:
    settings = dict(DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        settings[row["key"]] = json.loads(row["value"])
    return settings


def save_settings(conn: sqlite3.Connection, values: dict) -> None:
    with conn:
        for key, value in values.items():
            if key in DEFAULT_SETTINGS:
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value)),
                )
