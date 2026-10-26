"""Load sessions from every source into the database, re-parsing only changed ones."""

import json
import sqlite3
from collections import Counter
from datetime import datetime

from .db import get_settings
from .pricing import Usage, cost_usd
from .sources.base import AgentRecord, ApiCall, PromptRecord, SessionRecord
from .sources.claude_code import ClaudeCodeSource
from .sources.codex import CodexSource
from .tagging import ai_tags, rule_scope, rule_tags
from .versions import body_hash, resolve


def _ms_between(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))  # noqa: E731
    return int((parse(end) - parse(start)).total_seconds() * 1000)


def _summarize(calls: list[ApiCall], prices: dict) -> tuple[Usage, float, Counter, set[str]]:
    usage, cost, models, unpriced = Usage(), 0.0, Counter(), set()
    for call in calls:
        usage.add(call.usage)
        models[call.model] += 1
        c = cost_usd(call.model, call.usage, prices, call.fast)
        if c is None:
            unpriced.add(call.model)
        else:
            cost += c
    return usage, cost, models, unpriced


def _usage_cols(u: Usage) -> tuple:
    return (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens, u.web_search_requests)


def _insert(conn: sqlite3.Connection, table: str, values: tuple) -> None:
    conn.execute(f"INSERT OR REPLACE INTO {table} VALUES ({','.join('?' * len(values))})", values)


def _insert_agent(conn: sqlite3.Connection, agent: AgentRecord, session_id: str, prices: dict) -> tuple[int, float]:
    usage, cost, models, _ = _summarize(agent.calls, prices)
    _insert(conn, "agents", (
        agent.id, agent.prompt_id, session_id, agent.agent_type, agent.description,
        agent.started_at, agent.ended_at, _ms_between(agent.started_at, agent.ended_at),
        *_usage_cols(usage), usage.total_tokens, cost,
        len(agent.calls), sum(agent.tools.values()), json.dumps(agent.tools), json.dumps(models),
        agent.persona, agent.skill,
    ))
    return usage.total_tokens, cost


def _insert_prompt(conn: sqlite3.Connection, p: PromptRecord, source: str, settings: dict) -> None:
    prices = settings["prices"]
    usage, cost, models, unpriced = _summarize(p.calls, prices)
    agent_tokens, agent_cost, end = 0, 0.0, p.ended_at
    for agent in p.agents:
        tokens, c = _insert_agent(conn, agent, p.session_id, prices)
        agent_tokens += tokens
        agent_cost += c
        _, _, _, agent_unpriced = _summarize(agent.calls, prices)
        unpriced |= agent_unpriced
        if agent.ended_at and (not end or agent.ended_at > end):
            end = agent.ended_at
    _insert(conn, "prompts", (
        p.id, p.session_id, source, p.project, p.cwd, p.git_branch, p.command, p.text,
        p.started_at, end, _ms_between(p.started_at, end), p.active_ms or None,
        *_usage_cols(usage), cost, len(p.calls), sum(p.tools.values()),
        json.dumps(p.tools), json.dumps(models), json.dumps(sorted(unpriced)),
        len(p.agents), agent_tokens, agent_cost,
        usage.total_tokens + agent_tokens, cost + agent_cost,
        p.harness, p.harness_version, p.effort, p.requested_model,
        p.answer, json.dumps(sorted(p.files)),
    ))
    for run in p.skill_runs:
        h = _store_skill_version(conn, run, p.started_at)
        conn.execute("INSERT OR IGNORE INTO prompt_skill_runs(prompt_id, skill, hash, args) VALUES (?,?,?,?)", (p.id, run.name, h, run.args))
    for tag in ai_tags(p):
        conn.execute("INSERT OR IGNORE INTO tags(prompt_id, tag, auto) VALUES (?,?,1)", (p.id, tag))


def _store_skill_version(conn: sqlite3.Connection, run, used_at: str) -> str:
    """Keep the skill's snapshot; find what restores it once, or again while it was not committed."""
    h = body_hash(run.body)
    row = conn.execute("SELECT git_state, origin FROM skill_versions WHERE hash = ?", (h,)).fetchone()
    if row and (row[1] != "git" or row[0] == "committed"):
        return h
    v = resolve(run.base_dir, run.body, used_at)
    conn.execute(
        "INSERT OR REPLACE INTO skill_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (h, run.name, run.base_dir, run.body, v["origin"], v["git_repo"], v["git_path"], v["git_commit"], v["git_state"],
         v["plugin"], v["plugin_version"], v["bundled_version"], datetime.now().isoformat(timespec="seconds")),
    )
    return h


def store_session(conn: sqlite3.Connection, session: SessionRecord, settings: dict) -> None:
    old_ids = [r[0] for r in conn.execute("SELECT id FROM prompts WHERE session_id=?", (session.id,))]
    conn.executemany("DELETE FROM tags WHERE prompt_id=? AND auto != 0", [(i,) for i in old_ids])
    conn.executemany("DELETE FROM prompt_skill_runs WHERE prompt_id=?", [(i,) for i in old_ids])
    conn.execute("DELETE FROM agents WHERE session_id=?", (session.id,))
    conn.execute("DELETE FROM prompts WHERE session_id=?", (session.id,))
    _insert(conn, "sessions", (
        session.id, session.source, session.project, session.title, session.path, session.signature,
        session.reported_cost_usd,
    ))
    for prompt in session.prompts:
        # A forked/resumed session replays earlier prompts: keep them in their first session.
        if conn.execute("SELECT 1 FROM prompts WHERE id=?", (prompt.id,)).fetchone():
            continue
        _insert_prompt(conn, prompt, session.source, settings)
    _link_replay(conn, session.id)


RERUN_TAG = "rerun"


def _link_replay(conn: sqlite3.Connection, session_id: str) -> None:
    """A session launched as a rerun is studied on its own: every prompt of it gets the `rerun` tag (an AI
    tag, so it can be filtered on or left out of the history), and none of the original's tags."""
    if conn.execute("SELECT 1 FROM replays WHERE session_id = ?", (session_id,)).fetchone():
        conn.execute(
            "INSERT OR IGNORE INTO tags(prompt_id, tag, auto) SELECT id, ?, 1 FROM prompts WHERE session_id = ?",
            (RERUN_TAG, session_id),
        )


def apply_rules(conn: sqlite3.Connection, rules: list[dict]) -> None:
    """Recompute the tags given by auto-tag rules on every prompt."""
    with conn:
        conn.execute("DELETE FROM tags WHERE auto = 2")
        # INSERT OR IGNORE keeps a tag already added by hand (auto = 0) as such.
        insert = "INSERT OR IGNORE INTO tags(prompt_id, tag, auto) VALUES (?,?,2)"
        for prompt_id, text in conn.execute("SELECT id, text FROM prompts").fetchall():
            conn.executemany(insert, [(prompt_id, tag) for tag in rule_tags(text, rules)])
        # skill rules: the prompts where the agent used the skill (its AI tag), or every prompt of their sessions
        for r in rules:
            if not (r.get("tag") and r.get("skill")):
                continue
            used = "SELECT prompt_id AS id FROM tags WHERE tag = ? AND auto = 1"
            if rule_scope(r) == "session":
                used = (
                    "SELECT id FROM prompts WHERE session_id IN "
                    "(SELECT p.session_id FROM prompts p JOIN tags t ON t.prompt_id = p.id WHERE t.tag = ? AND t.auto = 1)"
                )
            conn.execute(
                f"INSERT OR IGNORE INTO tags(prompt_id, tag, auto) SELECT ids.id, ?, 2 FROM ({used}) ids",
                (r["tag"], f"skill:{r['skill']}"),
            )
        if not any(rule_scope(r) == "session" for r in rules):
            return
        for session_id, text in session_texts(conn):
            tags = rule_tags(text, rules, "session")
            if tags:
                ids = [r[0] for r in conn.execute("SELECT id FROM prompts WHERE session_id = ?", (session_id,))]
                conn.executemany(insert, [(i, tag) for i in ids for tag in tags])


def session_texts(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Each session's title and prompts, the text session-scoped rules match."""
    return conn.execute(
        "SELECT s.id, COALESCE(s.title, '') || char(10) || COALESCE(GROUP_CONCAT(p.text, char(10)), '') "
        "FROM sessions s LEFT JOIN prompts p ON p.session_id = s.id GROUP BY s.id"
    ).fetchall()


def sources_from_settings(settings: dict) -> list:
    sources = []
    if settings.get("claude_enabled", True):
        sources.append(ClaudeCodeSource(settings["claude_projects_dir"]))
    if settings.get("codex_enabled", True):
        sources.append(CodexSource(settings["codex_sessions_dir"]))
    return sources


def ingest(conn: sqlite3.Connection, force: bool = False) -> dict:
    """Return counts of scanned and updated sessions."""
    settings = get_settings(conn)
    known = {r["path"]: r["signature"] for r in conn.execute("SELECT path, signature FROM sessions")}
    from .price_discovery import discover_prices
    from .replay import codex_models

    candidates = [(source, key, signature) for source in sources_from_settings(settings) for key, signature in source.sessions()]
    loaded = {}
    models = set()
    for table in ("prompts", "agents"):
        for row in conn.execute(f"SELECT models FROM {table}"):
            models.update(json.loads(row[0] or "{}"))
    if settings.get("codex_enabled", True):
        models.update(codex_models())
    models.add(settings.get("judge_model", ""))
    for source, key, signature in candidates:
        if force or known.get(key) != signature:
            session = source.load(key)
            if session is not None:
                loaded[key] = session
                for prompt in session.prompts:
                    models.update(call.model for call in prompt.calls)
                    for agent in prompt.agents:
                        models.update(call.model for call in agent.calls)
    if discover_prices(conn, models, settings):
        settings = get_settings(conn)
        # Reparse old sessions too: aggregate DB rows cannot reconstruct all cache TTL usage.
        for source, key, signature in candidates:
            if key not in loaded:
                session = source.load(key)
                if session is not None:
                    loaded[key] = session
    updated = 0
    for source, key, signature in candidates:
        session = loaded.get(key)
        if session is None:
            continue
        session.signature = signature
        with conn:
            store_session(conn, session, settings)
        updated += 1
    if updated:
        apply_rules(conn, settings["tag_rules"])
    return {"scanned": len(candidates), "updated": updated}
