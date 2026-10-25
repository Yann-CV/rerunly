"""Load sessions from every source into the database, re-parsing only changed ones."""

import json
import sqlite3
from collections import Counter
from datetime import datetime

from .db import get_settings
from .pricing import Usage, cost_usd
from .sources.base import AgentRecord, ApiCall, PromptRecord, SessionRecord
from .sources.claude_code import ClaudeCodeSource
from .tagging import ai_tags, rule_tags


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
    ))
    for tag in ai_tags(p):
        conn.execute("INSERT OR IGNORE INTO tags(prompt_id, tag, auto) VALUES (?,?,1)", (p.id, tag))


def store_session(conn: sqlite3.Connection, session: SessionRecord, settings: dict) -> None:
    old_ids = [r[0] for r in conn.execute("SELECT id FROM prompts WHERE session_id=?", (session.id,))]
    conn.executemany("DELETE FROM tags WHERE prompt_id=? AND auto != 0", [(i,) for i in old_ids])
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


def apply_rules(conn: sqlite3.Connection, rules: list[dict]) -> None:
    """Recompute the tags given by auto-tag rules on every prompt."""
    with conn:
        conn.execute("DELETE FROM tags WHERE auto = 2")
        for prompt_id, text in conn.execute("SELECT id, text FROM prompts").fetchall():
            # INSERT OR IGNORE keeps a tag already added by hand (auto = 0) as such.
            conn.executemany(
                "INSERT OR IGNORE INTO tags(prompt_id, tag, auto) VALUES (?,?,2)",
                [(prompt_id, tag) for tag in rule_tags(text, rules)],
            )


def sources_from_settings(settings: dict) -> list:
    return [ClaudeCodeSource(settings["claude_projects_dir"])]


def ingest(conn: sqlite3.Connection, force: bool = False) -> dict:
    """Return counts of scanned and updated sessions."""
    settings = get_settings(conn)
    known = {r["path"]: r["signature"] for r in conn.execute("SELECT path, signature FROM sessions")}
    scanned = updated = 0
    for source in sources_from_settings(settings):
        for key, signature in source.sessions():
            scanned += 1
            if not force and known.get(key) == signature:
                continue
            session = source.load(key)
            if session is None:
                continue
            session.signature = signature
            with conn:
                store_session(conn, session, settings)
            updated += 1
    if updated:
        apply_rules(conn, settings["tag_rules"])
    return {"scanned": scanned, "updated": updated}
