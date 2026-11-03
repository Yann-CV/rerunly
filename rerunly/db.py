import contextvars
import json
import os
import sqlite3
import zlib
from contextlib import contextmanager
from pathlib import Path

from .config import DATA_DIR, DEFAULT_SETTINGS

SCHEMA = """
CREATE TABLE IF NOT EXISTS model_check_state (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
-- Only local usage readings and token counts, never authentication data.
CREATE TABLE IF NOT EXISTS usage_observations (
    agent TEXT PRIMARY KEY,
    observed_at TEXT NOT NULL,
    scope TEXT NOT NULL,
    windows TEXT NOT NULL,
    records TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_samples (
    id INTEGER PRIMARY KEY,
    agent TEXT NOT NULL,
    scope TEXT NOT NULL,
    model TEXT NOT NULL,
    window_minutes INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    tokens INTEGER NOT NULL,
    percentage REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_samples_model ON usage_samples(agent, scope, model, window_minutes);

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

-- Prompts taken off a tag: `auto` is how they had it (-1 = unknown legacy assignment,
-- restored by hand), `example` whether they
-- serve as examples of what does not belong to the tag.
CREATE TABLE IF NOT EXISTS tag_exclusions (
    prompt_id TEXT NOT NULL, tag TEXT NOT NULL,
    auto INTEGER NOT NULL DEFAULT -1, example INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (prompt_id, tag)
);
CREATE TABLE IF NOT EXISTS tag_priorities (
    tag TEXT NOT NULL, preferred_tag TEXT NOT NULL,
    PRIMARY KEY (tag, preferred_tag)
);
CREATE TABLE IF NOT EXISTS tag_suppressed (
    prompt_id TEXT NOT NULL, tag TEXT NOT NULL, auto INTEGER NOT NULL,
    PRIMARY KEY (prompt_id, tag)
);

-- When the user created a human tag (by hand or with keywords), and what it is for. AI tags have
-- no row: their creation date is their first prompt's.
CREATE TABLE IF NOT EXISTS tag_meta (
    tag TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    description TEXT
);

-- Every version of a skill the agent loaded, by the hash of its instructions: the snapshot itself
-- and, when it can be found, what restores it (git commit, plugin version, bundling harness).
CREATE TABLE IF NOT EXISTS skill_versions (
    hash TEXT PRIMARY KEY,
    skill TEXT NOT NULL,
    base_dir TEXT,
    body TEXT NOT NULL,
    origin TEXT,
    git_repo TEXT, git_path TEXT, git_commit TEXT, git_state TEXT,
    plugin TEXT, plugin_version TEXT, bundled_version TEXT,
    resolved_at TEXT
);

-- The skill versions each prompt ran, with the arguments they were called with.
CREATE TABLE IF NOT EXISTS prompt_skill_runs (
    prompt_id TEXT NOT NULL,
    skill TEXT NOT NULL,
    hash TEXT NOT NULL,
    args TEXT,
    PRIMARY KEY (prompt_id, skill, hash)
);

-- Prompts the user deleted from the history: the import skips them, so they stay deleted.
CREATE TABLE IF NOT EXISTS deleted_prompts (
    prompt_id TEXT PRIMARY KEY,
    deleted_at TEXT NOT NULL
);

-- Replays launched from the dashboard: the session id given to Claude Code, and what it replays.
CREATE TABLE IF NOT EXISTS replays (
    session_id TEXT PRIMARY KEY,
    prompt_id TEXT NOT NULL,
    model TEXT, effort TEXT, harness_version TEXT,
    launched_at TEXT NOT NULL
);

-- Reruns waiting to be launched from the Reruns page, with the settings to launch them with.
CREATE TABLE IF NOT EXISTS replay_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_id TEXT NOT NULL,
    model TEXT, effort TEXT, same_version INTEGER NOT NULL DEFAULT 0,
    added_at TEXT NOT NULL
);

-- Claude's rating of how much a run's output (other_id) differs from another's (base_id).
CREATE TABLE IF NOT EXISTS run_ratings (
    base_id TEXT NOT NULL,
    other_id TEXT NOT NULL,
    status TEXT NOT NULL,             -- rating, done, failed
    score INTEGER, verdict TEXT, quality TEXT, look INTEGER, summary TEXT, differences TEXT,
    model TEXT, cost_usd REAL, rated_at TEXT, error TEXT,
    PRIMARY KEY (base_id, other_id)
);

-- Completed judge attempts are kept when a pair is judged again.
CREATE TABLE IF NOT EXISTS judge_rating_history (
    id INTEGER PRIMARY KEY, base_id TEXT NOT NULL, other_id TEXT NOT NULL,
    score INTEGER, verdict TEXT, quality TEXT, look INTEGER, summary TEXT, differences TEXT,
    model TEXT, cost_usd REAL, rated_at TEXT, input_hash TEXT
);
CREATE INDEX IF NOT EXISTS judge_rating_history_pair ON judge_rating_history(base_id, other_id);

-- Human assessments are context for future judgments of the same pair and inputs.
CREATE TABLE IF NOT EXISTS judge_feedback (
    base_id TEXT NOT NULL, other_id TEXT NOT NULL, input_hash TEXT NOT NULL,
    model TEXT NOT NULL, judge_score INTEGER NOT NULL, human_score INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
    PRIMARY KEY (base_id, other_id)
);

-- The run of a tag (yours or an AI tag) every other run of it is compared with.
CREATE TABLE IF NOT EXISTS rerun_reference (
    prompt_id TEXT PRIMARY KEY,
    reference_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tag_reference (
    tag TEXT PRIMARY KEY,
    prompt_id TEXT NOT NULL
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
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # SQLite otherwise creates a new database using the process umask, often 0644.
    # Preserve permissions of existing databases, including deliberately shared ones.
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    conn = sqlite3.connect(path, check_same_thread=False)
    opened = _scope_connections.get()
    if opened is not None:
        opened.append(conn)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    if conn.execute("PRAGMA user_version").fetchone()[0] != _schema_version():
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.execute(f"PRAGMA user_version = {_schema_version()}")
    return conn


_scope_connections: contextvars.ContextVar[list | None] = contextvars.ContextVar("rerunly_connections", default=None)


@contextmanager
def connection_scope():
    """Close every connection opened inside: one request, or one pass of a background thread."""
    opened = []
    token = _scope_connections.set(opened)
    try:
        yield
    finally:
        _scope_connections.reset(token)
        for conn in opened:
            conn.close()


class ConnectionScope:
    """ASGI wrapper giving each request its connection scope."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        with connection_scope():
            await self.app(scope, receive, send)


def _schema_version() -> int:
    """Changes by itself with the schema, so a new table or column can never be forgotten."""
    return zlib.crc32((SCHEMA + repr(ADDED_COLUMNS)).encode()) & 0x7FFFFFFF


def tighten_permissions(db_path: Path) -> None:
    """Prompts and answers belong to this user: close Rerunly's own folder to everyone else.

    Installations made before new files became private may still be readable by others. A database
    placed elsewhere with `--db` keeps the permissions it was given."""
    if db_path.resolve().parent != DATA_DIR.resolve():
        return
    for path in (DATA_DIR, *DATA_DIR.iterdir()):
        if path.is_symlink():
            continue
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            path.chmod(mode & 0o700)


# Columns added after the first release: (table, column, type). Appended at the end of the
# table, so inserts listing values in table order keep working.
ADDED_COLUMNS = [
    ("run_ratings", "duration_ms", "INTEGER"), ("run_ratings", "total_tokens", "INTEGER"),
    ("judge_rating_history", "duration_ms", "INTEGER"), ("judge_rating_history", "total_tokens", "INTEGER"),
    ("run_ratings", "origin", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("judge_rating_history", "origin", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("replay_queue", "kind", "TEXT NOT NULL DEFAULT 'prompt'"),
    ("replay_queue", "other_prompt_id", "TEXT"),
    ("replay_queue", "keep_terminal_open", "INTEGER"),
    ("replays", "keep_terminal_open", "INTEGER"),
    ("replay_queue", "cwd", "TEXT"),
    ("agents", "persona", "TEXT"), ("agents", "skill", "TEXT"), ("tag_meta", "description", "TEXT"),
    ("tag_exclusions", "auto", "INTEGER NOT NULL DEFAULT -1"), ("tag_exclusions", "example", "INTEGER NOT NULL DEFAULT 1"),
    # what ran a prompt, to run it again (inserted in this order, after the original columns)
    ("prompts", "harness", "TEXT"), ("prompts", "harness_version", "TEXT"), ("prompts", "effort", "TEXT"),
    ("prompts", "requested_model", "TEXT"),
    # the Claude Code release a queued rerun runs with: "" installed, "same" its own, or a version
    ("replay_queue", "release", "TEXT"),
    # what a prompt produced: its final answer, and the files it edited (JSON list)
    ("prompts", "answer", "TEXT"), ("prompts", "files", "TEXT"),
    ("prompts", "permissions", "TEXT"),
    # how a rerun ran: "terminal" (interactive) or "background" (unattended), and how it ended
    ("replays", "mode", "TEXT"), ("replays", "status", "TEXT"), ("replays", "result", "TEXT"),
    ("replays", "denials", "TEXT"), ("replays", "cost_usd", "REAL"), ("replays", "ended_at", "TEXT"),
    ("replays", "pid", "INTEGER"), ("replays", "log_path", "TEXT"), ("replays", "skills", "TEXT"),
    ("replays", "permission", "TEXT"), ("replays", "budget_usd", "REAL"),
    ("replay_queue", "mode", "TEXT"), ("replay_queue", "permission", "TEXT"), ("replay_queue", "budget_usd", "REAL"),
    ("replay_queue", "restore_skills", "INTEGER"),
    # rate the rerun against its original once imported (1, the default) or not (0)
    ("replays", "auto_rate", "INTEGER"), ("replay_queue", "auto_rate", "INTEGER"),
    ("run_ratings", "input_hash", "TEXT"),
    # the LLM judging the rerun ("" = the default judge of the Setup page)
    ("replays", "judge_model", "TEXT"), ("replay_queue", "judge_model", "TEXT"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, kind in ADDED_COLUMNS:
        if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


def _sql_list(values) -> str:
    """SQL literals for a view, which cannot take parameters. Values SQLite would refuse are left out, so an old
    bad setting cannot break every page."""
    usable = [v for v in values if isinstance(v, str) and "\x00" not in v] if isinstance(values, list) else []
    return ", ".join("'" + v.replace("'", "''") + "'" for v in usable)


def create_prompt_view(conn: sqlite3.Connection, prefer_official: bool, settings: dict | None = None) -> None:
    """Temporary view `pv`: prompts plus the cost to display, without the projects and sessions the
    settings exclude from the history (the pages read prompts through it).

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
    where = []
    if settings and settings.get("excluded_projects"):
        where.append(f"p.project NOT IN ({_sql_list(settings['excluded_projects'])})")
    if settings and settings.get("excluded_sessions"):
        where.append(f"p.session_id NOT IN ({_sql_list(settings['excluded_sessions'])})")
    if settings and settings.get("excluded_tags"):
        # prompts carrying an excluded AI tag: a skill or subagent type the agent used, a model, ...
        where.append(f"p.id NOT IN (SELECT prompt_id FROM tags WHERE auto = 1 AND tag IN ({_sql_list(settings['excluded_tags'])}))")
    if settings and settings.get("excluded_your_tags"):
        # prompts carrying one of your excluded tags, added by hand or by a rule
        where.append(f"p.id NOT IN (SELECT prompt_id FROM tags WHERE auto IN (0, 2) AND tag IN ({_sql_list(settings['excluded_your_tags'])}))")
    conn.execute("DROP VIEW IF EXISTS temp.pv")
    conn.execute(
        f"CREATE TEMP VIEW pv AS SELECT {', '.join(select)} "
        "FROM prompts p LEFT JOIN otel_by_prompt o ON o.prompt_id = p.id"
        + (f" WHERE {' AND '.join(where)}" if where else "")
    )


def get_settings(conn: sqlite3.Connection) -> dict:
    settings = dict(DEFAULT_SETTINGS)
    manual_prices = {}
    for row in conn.execute("SELECT key, value FROM settings"):
        settings[row["key"]] = json.loads(row["value"])
        if row["key"] == "prices":
            manual_prices = settings["prices"]
    settings["prices"] = {**DEFAULT_SETTINGS["prices"], **settings["auto_prices"], **manual_prices}
    return settings


def save_settings(conn: sqlite3.Connection, values: dict) -> None:
    with conn:
        for key, value in values.items():
            if key in DEFAULT_SETTINGS:
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value)),
                )
