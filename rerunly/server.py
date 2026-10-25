import json
import os
from datetime import datetime, timezone
import shutil
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import background as bg, db, otel
from .config import DEFAULT_DB_PATH, DEFAULT_PORT
from .ingest import apply_rules, ingest
from .tagging import rule_matches, similarity

STATIC = Path(__file__).parent / "static"
CLAUDE_SETTINGS = Path.home() / ".claude/settings.json"
HOOK_MARKER = "rerunly"
LEGACY_HOOK_MARKERS = ("agent-dashboard",)  # hooks installed before the rename to Rerunly
COMPARE_SCOPES = ("similar", "tag", "similar_tag")
SPARK_POINTS = 24


class FreshStaticFiles(StaticFiles):
    """Browsers revalidate every load (cheap 304s via ETag), so an update shows on the next reload."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _tag(tag: str, auto: int) -> dict:
    """auto: 0 = added by hand, 1 = AI (derived from the transcript), 2 = from a user rule."""
    return {"tag": tag, "kind": "ai" if auto == 1 else "human", "from_rule": auto == 2}


def _record_creation(c, tag: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    with c:
        c.execute("INSERT OR IGNORE INTO tag_meta(tag, created_at) VALUES (?, ?)", (tag, now))


def _row(r) -> dict:
    d = dict(r)
    for key in ("tools", "models", "unpriced_models"):
        if key in d and d[key]:
            d[key] = json.loads(d[key])
    return d


def create_app(db_path: Path = DEFAULT_DB_PATH, port: int = DEFAULT_PORT, background: bool = True) -> FastAPI:
    ingest_lock = threading.Lock()
    stop = threading.Event()

    def conn():
        c = db.connect(db_path)
        db.create_prompt_view(c, db.get_settings(c)["cost_source"] == "claude-code")
        return c

    def run_ingest(force: bool = False) -> dict:
        with ingest_lock:
            c = conn()
            try:
                return ingest(c, force=force)
            finally:
                c.close()

    def loop():
        while not stop.is_set():
            try:
                run_ingest()
            except Exception as e:  # keep the server alive on a malformed transcript
                print(f"[rerunly] ingestion failed: {e}")
            c = conn()
            interval = db.get_settings(c)["auto_ingest_interval"]
            c.close()
            stop.wait(interval if interval > 0 else 3600)

    @asynccontextmanager
    async def lifespan(_app):
        if background:
            threading.Thread(target=loop, daemon=True).start()
        yield
        stop.set()

    app = FastAPI(title="Rerunly", lifespan=lifespan)
    app.mount("/static", FreshStaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    # ---- ingestion & settings -------------------------------------------------

    @app.post("/api/ingest")
    def api_ingest(force: bool = False):
        return run_ingest(force)

    @app.get("/api/settings")
    def get_settings():
        c = conn()
        return db.get_settings(c)

    @app.put("/api/settings")
    def put_settings(values: dict = Body(...)):
        c = conn()
        db.save_settings(c, values)
        settings = db.get_settings(c)
        if "tag_rules" in values:
            apply_rules(c, settings["tag_rules"])
        return settings

    # ---- OpenTelemetry receiver (Claude Code's own cost per API request) -------

    @app.post("/v1/logs")
    async def otlp_logs(request: Request):
        if "json" not in request.headers.get("content-type", ""):
            raise HTTPException(415, "set OTEL_EXPORTER_OTLP_LOGS_PROTOCOL=http/json")
        otel.store(conn(), await request.json())
        return {}

    # ---- setup: wiring Claude Code to the dashboard ---------------------------

    def hook_command() -> str:
        return f"curl -s -m 2 -X POST http://127.0.0.1:{port}/api/ingest >/dev/null 2>&1 || true # {HOOK_MARKER}"

    def read_claude_settings() -> dict:
        if CLAUDE_SETTINGS.exists():
            return json.loads(CLAUDE_SETTINGS.read_text() or "{}")
        return {}

    def write_claude_settings(data: dict) -> None:
        if CLAUDE_SETTINGS.exists():
            shutil.copy(CLAUDE_SETTINGS, CLAUDE_SETTINGS.with_suffix(".json.bak"))
        CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        CLAUDE_SETTINGS.write_text(json.dumps(data, indent=2) + "\n")

    def is_ours(hook) -> bool:
        return any(m in json.dumps(hook) for m in (HOOK_MARKER, *LEGACY_HOOK_MARKERS))

    def hook_installed(cs: dict) -> bool:
        return any(is_ours(h) for h in cs.get("hooks", {}).get("Stop", []))

    @app.get("/api/setup/status")
    def setup_status():
        c = conn()
        settings = db.get_settings(c)
        projects = Path(settings["claude_projects_dir"]).expanduser()
        try:
            cs = read_claude_settings()
        except json.JSONDecodeError:
            cs = {}
        counts = c.execute(
            "SELECT (SELECT COUNT(*) FROM sessions) s, (SELECT COUNT(*) FROM prompts) p, "
            "(SELECT COUNT(*) FROM agents) a, (SELECT MAX(started_at) FROM prompts) last"
        ).fetchone()
        unpriced = set()
        for r in c.execute("SELECT unpriced_models FROM prompts WHERE unpriced_models != '[]'"):
            unpriced |= set(json.loads(r[0]))
        wanted, env = otel.telemetry_env(port), cs.get("env") or {}
        events = c.execute(
            "SELECT COUNT(*) n, SUM(prompt_id IN (SELECT id FROM prompts)) matched, MAX(timestamp) last FROM otel_requests"
        ).fetchone()
        return {
            "telemetry": {
                "configured": all(env.get(k) == v for k, v in wanted.items()),
                "env": wanted,
                # values the user already set to something else (another collector, another port)
                "conflicts": {k: env[k] for k in wanted if k in env and env[k] != wanted[k]},
                "events": events["n"],
                "matched_events": events["matched"] or 0,
                "last_event_at": events["last"],
                "cost_source": settings["cost_source"],
            },
            "projects_dir": str(projects),
            "projects_dir_exists": projects.is_dir(),
            "transcript_count": len(list(projects.glob("*/*.jsonl"))) if projects.is_dir() else 0,
            "claude_settings_path": str(CLAUDE_SETTINGS),
            "cleanup_period_days": cs.get("cleanupPeriodDays"),
            "hook_installed": hook_installed(cs),
            "autostart": {"enabled": bg.autostart_enabled(), "file": str(bg.autostart_file())},
            "hook_command": hook_command(),
            "db_path": str(db_path),
            "sessions": counts["s"],
            "prompts": counts["p"],
            "agents": counts["a"],
            "last_prompt_at": counts["last"],
            "unpriced_models": sorted(unpriced),
        }

    @app.post("/api/setup/hook")
    def install_hook(install: bool = True):
        cs = read_claude_settings()
        stop_hooks = [
            h for h in cs.setdefault("hooks", {}).get("Stop", []) if not is_ours(h)  # replaces a pre-rename hook too
        ]
        if install:
            stop_hooks.append({"hooks": [{"type": "command", "command": hook_command()}]})
        if stop_hooks:
            cs["hooks"]["Stop"] = stop_hooks
        else:
            cs["hooks"].pop("Stop", None)
            if not cs["hooks"]:
                cs.pop("hooks")
        write_claude_settings(cs)
        return setup_status()

    @app.post("/api/setup/telemetry")
    def setup_telemetry(enable: bool = Body(..., embed=True)):
        cs = read_claude_settings()
        env = cs.setdefault("env", {})
        for key, value in otel.telemetry_env(port).items():
            if enable:
                env[key] = value
            elif env.get(key) == value:
                env.pop(key)
        if not env:
            cs.pop("env")
        write_claude_settings(cs)
        return setup_status()

    @app.post("/api/setup/autostart")
    def setup_autostart(enable: bool = Body(..., embed=True)):
        bg.set_autostart(enable, db_path, port)
        return setup_status()

    @app.post("/api/setup/retention")
    def set_retention(days: int = Body(..., embed=True)):
        cs = read_claude_settings()
        cs["cleanupPeriodDays"] = days
        write_claude_settings(cs)
        return setup_status()

    # ---- prompts --------------------------------------------------------------

    def tags_for(c, ids: list[str]) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {i: [] for i in ids}
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            q = f"SELECT prompt_id, tag, auto FROM tags WHERE prompt_id IN ({','.join('?' * len(chunk))}) ORDER BY tag"
            for r in c.execute(q, chunk):
                out[r["prompt_id"]].append(_tag(r["tag"], r["auto"]))
        return out

    LIST_COLS = (
        "p.id, p.session_id, p.project, p.command, p.text, p.started_at, p.wall_ms, p.active_ms, "
        "p.total_tokens, p.total_cost_usd, p.cost_usd, p.agent_count, p.api_calls, p.tool_calls, "
        "p.estimated_cost_usd, p.official_cost_usd, p.cost_source, p.api_ms"
    )

    def telemetry_breakdown(c, where: str, args: tuple) -> list[dict]:
        """Claude Code's own cost per caller (main thread, subagent, auxiliary call) and model."""
        return [
            dict(r)
            for r in c.execute(
                "SELECT COALESCE(agent_name, query_source, 'unknown') source, model, COUNT(*) requests, "
                "SUM(cost_usd) cost_usd, SUM(duration_ms) api_ms, SUM(input_tokens) input_tokens, "
                "SUM(output_tokens) output_tokens, SUM(cache_read_tokens) cache_read_tokens, "
                f"SUM(cache_write_tokens) cache_write_tokens FROM otel_requests WHERE {where} "
                "GROUP BY source, model ORDER BY cost_usd DESC",
                args,
            )
        ]

    PROMPT_SORTS = {
        "recent": "p.started_at DESC",
        "cost": "p.total_cost_usd DESC",
        "duration": "COALESCE(p.active_ms, p.wall_ms) DESC",
        "tokens": "p.total_tokens DESC",
    }

    @app.get("/api/prompts")
    def list_prompts(
        q: str = "", tag: str = "", project: str = "", since: str = "", sort: str = "recent",
        limit: int = 100, offset: int = 0,
    ):
        """Prompts matching the filters, plus `summary`: totals over all of them (not just this page)."""
        if sort not in PROMPT_SORTS:
            raise HTTPException(400, f"sort must be one of {list(PROMPT_SORTS)}")
        c = conn()
        where, args = [], []
        if since:
            where.append("p.started_at >= ?")
            args.append(since)
        if q:
            where.append("p.text LIKE ?")
            args.append(f"%{q}%")
        if project:
            where.append("p.project = ?")
            args.append(project)
        if tag:
            where.append("p.id IN (SELECT prompt_id FROM tags WHERE tag = ?)")
            args.append(tag)
        sql = f"SELECT {LIST_COLS} FROM pv p"
        if where:
            sql += " WHERE " + " AND ".join(where)
        summary = dict(c.execute(
            "SELECT COUNT(*) count, SUM(total_cost_usd) cost_usd, SUM(total_tokens) tokens, "
            f"SUM(COALESCE(active_ms, wall_ms)) duration_ms, MAX(total_cost_usd) max_cost_usd FROM ({sql})",
            args,
        ).fetchone())
        rows = [_row(r) for r in c.execute(f"{sql} ORDER BY {PROMPT_SORTS[sort]} LIMIT ? OFFSET ?", [*args, limit, offset])]
        tags = tags_for(c, [r["id"] for r in rows])
        for r in rows:
            r["tags"] = tags[r["id"]]
        return {"total": summary["count"], "summary": summary, "items": rows}

    @app.get("/api/prompts/{prompt_id}")
    def get_prompt(prompt_id: str, compare: str = "similar", tag: str = ""):
        """`compare` picks the prompts shown next to this one:
        similar = similar text; tag = every prompt with `tag`; similar_tag = similar and with `tag`."""
        if compare not in COMPARE_SCOPES:
            raise HTTPException(400, f"compare must be one of {COMPARE_SCOPES}")
        if compare != "similar" and not tag:
            raise HTTPException(400, "tag is required for this comparison")
        c = conn()
        r = c.execute("SELECT * FROM pv WHERE id=?", (prompt_id,)).fetchone()
        if r is None:
            raise HTTPException(404)
        prompt = _row(r)
        prompt["tags"] = tags_for(c, [prompt_id])[prompt_id]
        prompt["agents"] = [_row(a) for a in c.execute("SELECT * FROM agents WHERE prompt_id=? ORDER BY started_at", (prompt_id,))]
        s = c.execute("SELECT title, reported_cost_usd FROM sessions WHERE id=?", (prompt["session_id"],)).fetchone()
        prompt["session"] = dict(s) if s else {}
        prompt["telemetry"] = telemetry_breakdown(c, "prompt_id = ?", (prompt_id,))

        threshold = db.get_settings(c)["similarity_threshold"]
        my_tags = {t["tag"] for t in prompt["tags"]}
        all_tags: dict[str, dict[str, int]] = {}
        for t in c.execute("SELECT prompt_id, tag, auto FROM tags ORDER BY tag"):
            all_tags.setdefault(t[0], {})[t[1]] = t[2]
        compared = []
        for o in c.execute(f"SELECT {LIST_COLS} FROM pv p WHERE p.id != ?", (prompt_id,)):
            other_tags = all_tags.get(o["id"], {})
            if compare != "similar" and tag not in other_tags:
                continue
            score = similarity(prompt["text"], my_tags, o["text"], set(other_tags))
            if compare != "tag" and score < threshold:
                continue
            compared.append({**_row(o), "score": round(score, 3), "tags": [_tag(t, a) for t, a in other_tags.items()]})
        compared.sort(key=lambda x: x["started_at"], reverse=True)
        prompt["compare"] = {"scope": compare, "tag": tag}
        prompt["similar"] = compared
        return prompt

    def add_rule(c, tag: str, keywords: list[str], match: str) -> None:
        """Keyword rule tagging every matching prompt, past and future, with the human tag `tag`."""
        keywords = sorted({k.strip() for k in keywords if k.strip()}, key=str.lower)
        if not keywords:
            return
        if match not in ("all", "any"):
            raise HTTPException(400, "match must be 'all' or 'any'")
        settings = db.get_settings(c)
        rule = {"tag": tag, "keywords": keywords, "match": match}
        _record_creation(c, tag)
        if rule not in settings["tag_rules"]:
            db.save_settings(c, {"tag_rules": [*settings["tag_rules"], rule]})
        apply_rules(c, db.get_settings(c)["tag_rules"])

    @app.post("/api/prompts/{prompt_id}/tags")
    def add_tag(
        prompt_id: str,
        tag: str = Body(..., embed=True),
        keywords: list[str] = Body([], embed=True),
        match: str = Body("all", embed=True),
    ):
        """Add a human tag; with `keywords`, also tag every prompt containing all (or any) of them."""
        tag = tag.strip()
        if not tag:
            raise HTTPException(400, "empty tag")
        c = conn()
        _record_creation(c, tag)
        with c:
            c.execute(
                "INSERT INTO tags(prompt_id, tag, auto) VALUES (?,?,0) ON CONFLICT DO UPDATE SET auto=0",
                (prompt_id, tag),
            )
        add_rule(c, tag, keywords, match)
        return tags_for(c, [prompt_id])[prompt_id]

    @app.get("/api/rules/preview")
    def preview_rule(keywords: str, match: str = "all"):
        """How many prompts a keyword rule would tag (`keywords` is comma-separated)."""
        c = conn()
        rule = {"keywords": keywords.split(","), "match": match}
        return {"matches": sum(rule_matches(rule, r[0]) for r in c.execute("SELECT text FROM prompts"))}

    @app.get("/api/tags/{tag:path}/rules")
    def tag_rules(tag: str):
        c = conn()
        return [{**r, "index": i} for i, r in enumerate(db.get_settings(c)["tag_rules"]) if r.get("tag") == tag]

    @app.post("/api/tags/{tag:path}/rules")
    def add_tag_rule(tag: str, keywords: list[str] = Body(..., embed=True), match: str = Body("all", embed=True)):
        c = conn()
        add_rule(c, tag, keywords, match)
        return tag_rules(tag)

    @app.delete("/api/tags/{tag:path}/rules/{index}")
    def delete_tag_rule(tag: str, index: int):
        c = conn()
        rules = db.get_settings(c)["tag_rules"]
        if not 0 <= index < len(rules) or rules[index].get("tag") != tag:
            raise HTTPException(404)
        rules.pop(index)
        db.save_settings(c, {"tag_rules": rules})
        apply_rules(c, rules)
        return tag_rules(tag)

    @app.delete("/api/prompts/{prompt_id}/tags/{tag:path}")
    def remove_tag(prompt_id: str, tag: str):
        c = conn()
        row = c.execute("SELECT auto FROM tags WHERE prompt_id=? AND tag=?", (prompt_id, tag)).fetchone()
        if row and row["auto"] == 1:
            raise HTTPException(400, "AI tags are derived from the transcript and cannot be removed")
        if row and row["auto"] == 2:
            raise HTTPException(400, "this tag comes from keywords or a rule: change them on the tag page")
        with c:
            c.execute("DELETE FROM tags WHERE prompt_id=? AND tag=?", (prompt_id, tag))
        return tags_for(c, [prompt_id])[prompt_id]

    # ---- tags & projects ------------------------------------------------------

    @app.get("/api/tags")
    def list_tags():
        """Aggregates per tag, plus `spark`: cost of its last prompts, oldest first."""
        c = conn()
        history: dict[str, list[float]] = {}
        for tag, cost in c.execute(
            "SELECT t.tag, p.total_cost_usd FROM tags t JOIN pv p ON p.id = t.prompt_id ORDER BY p.started_at"
        ):
            history.setdefault(tag, []).append(round(cost or 0, 4))
        return [
            {**dict(r), "spark": history.get(r["tag"], [])[-SPARK_POINTS:]}
            for r in c.execute(
                "SELECT t.tag, CASE WHEN MIN(t.auto = 1) = 0 THEN 'human' ELSE 'ai' END kind, COUNT(*) prompts, SUM(p.total_cost_usd) cost_usd, AVG(p.total_cost_usd) avg_cost_usd, "
                "AVG(p.total_tokens) avg_tokens, AVG(p.wall_ms) avg_wall_ms, MAX(p.started_at) last_used, "
                "MIN(p.started_at) first_used, "
                # human tags: when the user created them; AI tags (and older human tags): first prompt
                "COALESCE((SELECT created_at FROM tag_meta m WHERE m.tag = t.tag), MIN(p.started_at)) created_at "
                "FROM tags t JOIN pv p ON p.id = t.prompt_id GROUP BY t.tag ORDER BY kind DESC, last_used DESC"
            )
        ]

    @app.get("/api/tags/{tag:path}/prompts")
    def tag_prompts(tag: str):
        c = conn()
        return [
            _row(r)
            for r in c.execute(
                f"SELECT {LIST_COLS} FROM pv p JOIN tags t ON t.prompt_id = p.id WHERE t.tag = ? ORDER BY p.started_at",
                (tag,),
            )
        ]

    # ---- sessions ---------------------------------------------------------------

    SESSION_COLS = (
        "s.id, s.project, s.title, s.reported_cost_usd, COUNT(p.id) prompts, MIN(p.started_at) started_at, "
        "MAX(p.ended_at) ended_at, SUM(p.total_cost_usd) cost_usd, SUM(p.estimated_cost_usd) estimated_cost_usd, "
        "SUM(p.official_cost_usd) official_cost_usd, SUM(p.total_tokens) total_tokens, SUM(p.active_ms) active_ms, "
        "SUM(p.api_ms) api_ms, SUM(p.agent_count) agent_count, SUM(p.api_calls) api_calls, SUM(p.tool_calls) tool_calls"
    )

    @app.get("/api/sessions")
    def list_sessions(project: str = "", limit: int = 100, offset: int = 0):
        c = conn()
        where, args = ("WHERE s.project = ?", [project]) if project else ("", [])
        sql = f"SELECT {SESSION_COLS} FROM sessions s JOIN pv p ON p.session_id = s.id {where} GROUP BY s.id"
        total = c.execute(f"SELECT COUNT(*) FROM ({sql})", args).fetchone()[0]
        rows = c.execute(f"{sql} ORDER BY started_at DESC LIMIT ? OFFSET ?", [*args, limit, offset])
        return {"total": total, "items": [dict(r) for r in rows]}

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str):
        c = conn()
        r = c.execute(
            f"SELECT {SESSION_COLS}, s.path FROM sessions s JOIN pv p ON p.session_id = s.id WHERE s.id = ? GROUP BY s.id",
            (session_id,),
        ).fetchone()
        if r is None:
            raise HTTPException(404)
        session = dict(r)
        prompts = [_row(p) for p in c.execute(f"SELECT {LIST_COLS} FROM pv p WHERE p.session_id = ? ORDER BY p.started_at", (session_id,))]
        tags = tags_for(c, [p["id"] for p in prompts])
        for p in prompts:
            p["tags"] = tags[p["id"]]
        session["prompts"] = prompts
        # transcript usage of the main agent and of each subagent type and persona, summed over the session
        usage = (
            "SUM(input_tokens) input_tokens, SUM(output_tokens) output_tokens, SUM(cache_read_tokens) cache_read_tokens, "
            "SUM(cache_write_tokens) cache_write_tokens, SUM(cost_usd) cost_usd, SUM(api_calls) api_calls, "
            "SUM(tool_calls) tool_calls, COUNT(*) runs, json_group_array(models) models"
        )
        agents = [dict(c.execute(
            f"SELECT 'main' agent, NULL persona, NULL skills, {usage} FROM prompts WHERE session_id = ?", (session_id,)
        ).fetchone())]
        agents += [
            dict(r)
            for r in c.execute(
                f"SELECT agent_type agent, persona, GROUP_CONCAT(DISTINCT skill) skills, {usage} FROM agents "
                "WHERE session_id = ? GROUP BY agent_type, persona ORDER BY cost_usd DESC",
                (session_id,),
            )
        ]
        for a in agents:
            models: dict[str, int] = {}
            for m in json.loads(a["models"]):
                for name, n in json.loads(m or "{}").items():
                    models[name] = models.get(name, 0) + n
            a["models"] = models
        session["agents"] = agents
        session["telemetry"] = telemetry_breakdown(c, "session_id = ?", (session_id,))
        return session

    @app.get("/api/projects")
    def list_projects():
        c = conn()
        return [r[0] for r in c.execute("SELECT DISTINCT project FROM prompts WHERE project != '' ORDER BY project")]

    return app


def reload_app() -> FastAPI:
    """App factory for `--reload`: the reloader re-imports the code, so settings come from the environment."""
    return create_app(Path(os.environ["RERUNLY_DB"]), int(os.environ["RERUNLY_PORT"]))


def serve(db_path: Path, host: str, port: int, reload: bool = False) -> None:
    import uvicorn

    if not reload:
        uvicorn.run(create_app(db_path, port), host=host, port=port, log_level="warning")
        return
    # restart on Python changes; static files are sent with no-cache, so a page refresh shows UI changes
    os.environ.update(RERUNLY_DB=str(db_path), RERUNLY_PORT=str(port))
    uvicorn.run(
        "rerunly.server:reload_app", factory=True, host=host, port=port, log_level="warning",
        reload=True, reload_dirs=[str(Path(__file__).parent)],
    )
