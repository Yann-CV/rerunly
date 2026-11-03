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
from .tagging import ai_tags, rule_matches, rule_scope, rule_tags
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
        p.answer, json.dumps(sorted(p.files)), json.dumps(p.permissions),
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


# what is kept per prompt, beyond its row: removed with it
PROMPT_TABLES = ("agents", "tags", "tag_exclusions", "tag_suppressed", "prompt_skill_runs", "tag_reference", "replay_queue")


def drop_prompts(conn: sqlite3.Connection, ids: list[str]) -> None:
    """Remove prompts from the history with everything kept about them (no transaction of its own)."""
    for table in PROMPT_TABLES:
        conn.executemany(f"DELETE FROM {table} WHERE prompt_id = ?", [(i,) for i in ids])
    conn.executemany("DELETE FROM rerun_reference WHERE prompt_id = ? OR reference_id = ?", [(i, i) for i in ids])
    conn.executemany("DELETE FROM run_ratings WHERE base_id = ? OR other_id = ?", [(i, i) for i in ids])
    conn.executemany("DELETE FROM prompts WHERE id = ?", [(i,) for i in ids])


def failed_rerun(conn: sqlite3.Connection, session_id: str) -> bool:
    return conn.execute("SELECT 1 FROM replays WHERE session_id = ? AND status = 'failed'", (session_id,)).fetchone() is not None


def drop_failed_rerun(conn: sqlite3.Connection, session_id: str) -> None:
    """A rerun that failed says nothing about its prompt: its prompts are never kept, even imported while it ran."""
    drop_prompts(conn, [r[0] for r in conn.execute("SELECT id FROM prompts WHERE session_id = ?", (session_id,))])


def store_session(conn: sqlite3.Connection, session: SessionRecord, settings: dict) -> None:
    # Parser corrections can change a file's session identity. Remove its previous
    # attribution first, so duplicate-prompt handling cannot strand its prompts there.
    obsolete = [r[0] for r in conn.execute(
        "SELECT id FROM sessions WHERE source=? AND path=? AND id!=?",
        (session.source, session.path, session.id))]
    replaced = [session.id, *obsolete]
    slots = ",".join("?" for _ in replaced)
    old_ids = [r[0] for r in conn.execute(f"SELECT id FROM prompts WHERE session_id IN ({slots})", replaced)]
    conn.executemany("DELETE FROM tags WHERE prompt_id=? AND auto != 0", [(i,) for i in old_ids])
    conn.executemany("DELETE FROM prompt_skill_runs WHERE prompt_id=?", [(i,) for i in old_ids])
    conn.execute(f"DELETE FROM agents WHERE session_id IN ({slots})", replaced)
    conn.execute(f"DELETE FROM prompts WHERE session_id IN ({slots})", replaced)
    conn.executemany("DELETE FROM sessions WHERE id=?", [(sid,) for sid in obsolete])
    _insert(conn, "sessions", (
        session.id, session.source, session.project, session.title, session.path, session.signature,
        session.reported_cost_usd,
    ))
    if failed_rerun(conn, session.id):
        drop_failed_rerun(conn, session.id)  # human tags of prompts imported while it ran
        return
    for prompt in session.prompts:
        # A forked/resumed session replays earlier prompts: keep them in their first session.
        # A prompt the user deleted stays deleted.
        if conn.execute("SELECT 1 FROM prompts WHERE id=? UNION ALL SELECT 1 FROM deleted_prompts WHERE prompt_id=?",
                        (prompt.id, prompt.id)).fetchone():
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
        _restore_suppressed(conn)
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
        if any(rule_scope(r) == "session" for r in rules):
            for session_id, text in session_texts(conn):
                tags = rule_tags(text, rules, "session")
                if tags:
                    ids = [r[0] for r in conn.execute("SELECT id FROM prompts WHERE session_id = ?", (session_id,))]
                    conn.executemany(insert, [(i, tag) for i in ids for tag in tags])
        _curate(conn, rules)


def apply_curation(conn: sqlite3.Connection) -> None:
    """Reapply tag exclusions and priorities without recomputing the rules: enough after a tag is
    added to or removed from some prompts, or a tag's priorities change."""
    rules = get_settings(conn)["tag_rules"]
    if any(r.get("include_tag") for r in rules):
        apply_rules(conn, rules)
        return
    with conn:
        _restore_suppressed(conn)
        _curate(conn)


def _restore_suppressed(conn: sqlite3.Connection) -> None:
    """Put back the tags held back by a priority, so priorities are resolved again from scratch."""
    conn.execute("INSERT OR IGNORE INTO tags SELECT prompt_id, tag, auto FROM tag_suppressed WHERE prompt_id IN (SELECT id FROM prompts)")
    conn.execute("DELETE FROM tag_suppressed")


def _curate(conn: sqlite3.Connection, rules: list[dict] | None = None) -> None:
    """Drop the prompts removed from a tag, then hold back a tag on prompts carrying a tag preferred to it."""
    conn.execute("DELETE FROM tags WHERE auto != 1 AND EXISTS (SELECT 1 FROM tag_exclusions e WHERE e.prompt_id=tags.prompt_id AND e.tag=tags.tag)")
    priorities = {}
    for tag, preferred in conn.execute("SELECT tag, preferred_tag FROM tag_priorities"):
        priorities.setdefault(tag, set()).add(preferred)
    inclusions = {}
    keyword_exclusions = {}
    dependencies = {tag: set(preferred) for tag, preferred in priorities.items()}
    rules = get_settings(conn)["tag_rules"] if rules is None else rules
    for rule in rules:
        if rule.get("exclude") and rule.get("tag"):
            keyword_exclusions.setdefault(rule["tag"], []).append(rule)
            dependencies.setdefault(rule["tag"], set())
        if rule.get("include_tag"):
            inclusions.setdefault(rule["tag"], []).append(rule)
            dependencies.setdefault(rule["tag"], set()).add(rule["include_tag"])
    # Resolve preferred tags first so chains have deterministic behavior.
    ordered, visiting, visited = [], set(), set()

    def visit(tag):
        if tag in visited or tag in visiting:
            return
        visiting.add(tag)
        for preferred in sorted(dependencies.get(tag, ())):
            visit(preferred)
        visiting.remove(tag)
        visited.add(tag)
        ordered.append(tag)

    for tag in sorted(dependencies):
        visit(tag)
    held = "FROM tags WHERE tag=? AND auto != 1 AND prompt_id IN (SELECT prompt_id FROM tags WHERE tag=?)"
    prompt_texts = conn.execute("SELECT id, text, session_id FROM prompts").fetchall() if keyword_exclusions else []
    sessions = session_texts(conn) if any(rule_scope(r) == "session" for rs in keyword_exclusions.values() for r in rs) else []
    for tag in ordered:
        for rule in inclusions.get(tag, ()):
            used = "SELECT prompt_id AS id FROM tags WHERE tag=?"
            if rule_scope(rule) == "session":
                used = "SELECT id FROM prompts WHERE session_id IN (SELECT p.session_id FROM prompts p JOIN tags t ON t.prompt_id=p.id WHERE t.tag=?)"
            conn.execute(f"INSERT OR IGNORE INTO tags(prompt_id,tag,auto) SELECT id, ?, 2 FROM ({used}) WHERE NOT EXISTS (SELECT 1 FROM tag_exclusions e WHERE e.prompt_id=id AND e.tag=?)", (tag, rule["include_tag"], tag))
        for rule in keyword_exclusions.get(tag, ()):
            matching_sessions = {sid for sid, text in sessions if rule_matches(rule, text)} if rule_scope(rule) == "session" else None
            ids = [pid for pid, text, sid in prompt_texts if (sid in matching_sessions if matching_sessions is not None else rule_matches(rule, text))]
            for pid in ids:
                conn.execute("INSERT OR IGNORE INTO tag_suppressed SELECT prompt_id, tag, auto FROM tags WHERE prompt_id=? AND tag=? AND auto!=1", (pid, tag))
                conn.execute("DELETE FROM tags WHERE prompt_id=? AND tag=? AND auto!=1", (pid, tag))
        for preferred in priorities.get(tag, ()):
            conn.execute(f"INSERT OR IGNORE INTO tag_suppressed SELECT prompt_id, tag, auto {held}", (tag, preferred))
            conn.execute(f"DELETE {held}", (tag, preferred))


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
    missing_permissions = {r[0] for r in conn.execute("SELECT s.path FROM sessions s JOIN prompts p ON p.session_id=s.id WHERE p.permissions IS NULL")}
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
        if force or known.get(key) != signature or key in missing_permissions:
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
        for source, key, _signature in candidates:
            if key not in loaded:
                session = source.load(key)
                if session is not None:
                    loaded[key] = session
    updated = 0
    for _source, key, signature in candidates:
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
