import json
import os
from datetime import datetime, timezone
import shlex
import shutil
import uuid
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import background as bg, db, judge, otel, replay, runner, statusline
from .config import DEFAULT_DB_PATH, DEFAULT_PORT
from .estimates import estimate_rerun
from .ingest import apply_curation, apply_rules, ingest, session_texts
from .pricing import DEFAULT_PRICES
from .sources.codex import CodexSource
from .tagging import jaccard, rule_matches, rule_scope, similarity, split_any, words

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


def _record_creation(c, tag: str, description: str | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    with c:
        c.execute("INSERT OR IGNORE INTO tag_meta(tag, created_at, description) VALUES (?, ?, ?)", (tag, now, description))


def _row(r) -> dict:
    d = dict(r)
    for key in ("tools", "models", "unpriced_models", "files"):
        if key in d and d[key]:
            d[key] = json.loads(d[key])
    return d


class LazyApp:
    """An ASGI app built on its first request: the example app costs nothing until someone opens it."""

    def __init__(self, factory):
        self.factory, self.app, self.lock = factory, None, threading.Lock()

    async def __call__(self, scope, receive, send):
        if self.app is None:
            with self.lock:
                if self.app is None:
                    self.app = self.factory()
        await self.app(scope, receive, send)


def create_app(db_path: Path = DEFAULT_DB_PATH, port: int = DEFAULT_PORT, background: bool = True, examples: bool = False) -> FastAPI:
    """The dashboard over `db_path`; with `examples`, the example app: a fresh database holding only the illustrative
    records, which never imports transcripts nor touches the agents' settings."""
    from .examples import add_examples, remove_examples

    if examples:
        for path in (db_path, db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm")):
            path.unlink(missing_ok=True)  # rebuilt at each start: the illustrative dates stay recent
        add_examples(db.connect(db_path))
    ingest_lock = threading.Lock()
    stop = threading.Event()

    def conn():
        c = db.connect(db_path)
        settings = db.get_settings(c)
        db.create_prompt_view(c, settings["cost_source"] == "claude-code", settings)
        return c

    def split_any_rules() -> None:
        """Rules saved before "any" rules were split: split them once (they tag the same prompts)."""
        c = conn()
        rules = db.get_settings(c)["tag_rules"]
        split = [part for r in rules for part in split_any(r)]
        if split != rules:
            db.save_settings(c, {"tag_rules": split})
            apply_rules(c, split)

    split_any_rules()
    if not examples:
        remove_examples(conn())  # added to the history by an earlier version: they live in the example app now

    def run_ingest(force: bool = False) -> dict:
        with ingest_lock:
            c = conn()
            try:
                out = ingest(c, force=force)
                rate_pending(c)  # reruns just imported get their rating
                return out
            finally:
                c.close()

    # ---- ratings: Claude says how much a run's output differs from another's ------------------

    rating_lock = threading.Lock()

    def rating_of(c, base_id: str, other_id: str) -> dict | None:
        r = c.execute("SELECT * FROM run_ratings WHERE base_id = ? AND other_id = ?", (base_id, other_id)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["differences"] = json.loads(d["differences"] or "[]")
        d["look"] = bool(d["look"]) if d["look"] is not None else None
        return d

    def start_rating(base_id: str, other_id: str, force: bool = False, model: str = "") -> dict | None:
        """Have an LLM judge rate `other_id`'s output against `base_id`'s, in the background, with `model` (else the
        default judge of the Setup page); None when one has no answer."""
        c = conn()
        runs = {r["id"]: dict(r) for r in c.execute("SELECT id, text, answer, files FROM prompts WHERE id IN (?, ?)", (base_id, other_id))}
        if base_id == other_id or len(runs) != 2 or not runs[base_id]["answer"] or not runs[other_id]["answer"]:
            return None
        with rating_lock:
            current = rating_of(c, base_id, other_id)
            if current and (current["status"] == "rating" or (current["status"] == "done" and not force)):
                return current
            # the default judge runs at its effort; a model picked for this rating, at its own default
            settings = db.get_settings(c)
            effort = "" if model else settings["judge_effort"]
            model = model or settings["judge_model"]
            with c:
                label = f"{model or 'default model'} · {effort} effort" if effort else model  # "Judged by …" on the page
                c.execute("INSERT OR REPLACE INTO run_ratings(base_id, other_id, status, model, rated_at) VALUES (?,?,?,?,?)",
                          (base_id, other_id, "rating", label, runner.now()))

        def work():
            files = lambda r: json.loads(r["files"] or "[]")
            try:
                out = judge.rate(runs[base_id]["text"], {"answer": runs[base_id]["answer"], "files": files(runs[base_id])},
                                 {"answer": runs[other_id]["answer"], "files": files(runs[other_id])}, model, db_path.parent / "judge", effort=effort)
                values = ("done", out["score"], out["verdict"], out["quality"], int(out["look"]), out["summary"],
                          json.dumps(out["differences"]), out["cost_usd"], None)
            except Exception as e:  # noqa: BLE001 - any failure is reported on the page
                values = ("failed", None, None, None, None, None, None, None, str(e)[:300])
            w = conn()
            with w:
                w.execute("UPDATE run_ratings SET status=?, score=?, verdict=?, quality=?, look=?, summary=?, differences=?, cost_usd=?, "
                          "error=?, rated_at=? WHERE base_id=? AND other_id=?", (*values, runner.now(), base_id, other_id))

        threading.Thread(target=work, daemon=True).start()
        return rating_of(c, base_id, other_id)

    def rate_pending(c) -> None:
        """Reruns imported with an answer, to rate against their original, and not rated yet."""
        for x in c.execute(
            "SELECT x.prompt_id base, x.judge_model, (SELECT id FROM prompts WHERE session_id = x.session_id ORDER BY started_at LIMIT 1) other "
            "FROM replays x WHERE COALESCE(x.auto_rate, 1) = 1").fetchall():
            if x["other"] and not rating_of(c, x["base"], x["other"]):
                start_rating(x["base"], x["other"], model=x["judge_model"] or "")

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

    app = FastAPI(title="Rerunly examples" if examples else "Rerunly", lifespan=lifespan)
    app.mount("/static", FreshStaticFiles(directory=STATIC), name="static")
    if examples:
        @app.middleware("http")
        async def examples_stay_apart(request: Request, call_next):
            """The example app reads and curates its own records only: no import, no agent setup, no telemetry."""
            path, root = request.scope["path"], request.scope.get("root_path", "")
            path = path[len(root):] if root and path.startswith(root) else path
            if path.startswith(("/api/ingest", "/v1/")) or (path.startswith("/api/setup") and request.method != "GET"):
                return JSONResponse({"detail": "The example app only shows illustrative records: open your own Rerunly for this."}, 403)
            return await call_next(request)
    else:
        app.mount("/examples", LazyApp(lambda: create_app(db_path.with_name(db_path.stem + "-examples.db"), port, background=False, examples=True)))

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
        if isinstance(values.get("prices"), dict):
            # Setup sends the whole table back: keep only what the user set, so found prices stay automatic.
            found = {**DEFAULT_PRICES, **db.get_settings(c)["auto_prices"]}
            values = {**values, "prices": {k: v for k, v in values["prices"].items() if found.get(k) != v}}
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

    # ---- setup: connecting local coding agents ---------------------------

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

    @app.get("/api/models")
    def available_models():
        c = conn()
        used = set()
        for row in c.execute("SELECT models FROM prompts"):
            used.update(json.loads(row[0]))
        return replay.agent_models(list(db.get_settings(c)["prices"]), sorted(used))

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
        connections = []
        for agent_id, name, source, path_key, enabled_key, binary, pattern in (
            ("claude-code", "Claude Code", "claude-code", "claude_projects_dir", "claude_enabled", "claude", "*/*.jsonl"),
            ("codex", "OpenAI Codex", "codex", "codex_sessions_dir", "codex_enabled", "codex", "**/rollout-*.jsonl"),
        ):
            directory = Path(settings[path_key]).expanduser()
            imported = c.execute(
                "SELECT COUNT(*) prompts, COUNT(DISTINCT session_id) sessions, MAX(started_at) last_prompt_at "
                "FROM prompts WHERE source=?", (source,)
            ).fetchone()
            connections.append({
                "id": agent_id, "name": name, "enabled": settings[enabled_key],
                "directory": str(directory), "directory_exists": directory.is_dir(),
                "installed": shutil.which(binary) is not None,
                "transcript_count": sum(1 for _ in directory.glob(pattern)) if directory.is_dir() else 0,
                **dict(imported),
                "updates": "hook-and-polling" if agent_id == "claude-code" else "polling",
                "reported_cost": agent_id == "claude-code",
            })
        wanted, env = otel.telemetry_env(port), cs.get("env") or {}
        events = c.execute(
            "SELECT COUNT(*) n, SUM(prompt_id IN (SELECT id FROM prompts)) matched, MAX(timestamp) last FROM otel_requests"
        ).fetchone()
        return {
            "connections": connections,
            "auto_ingest_interval": settings["auto_ingest_interval"],
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
            "codex": {"sessions_dir": settings["codex_sessions_dir"],
                      "exists": Path(settings["codex_sessions_dir"]).expanduser().is_dir(),
                      "transcript_count": len(list(Path(settings["codex_sessions_dir"]).expanduser().rglob("rollout-*.jsonl"))),
                      "installed": shutil.which("codex") is not None},
            "projects_dir": str(projects),
            "projects_dir_exists": projects.is_dir(),
            "transcript_count": len(list(projects.glob("*/*.jsonl"))) if projects.is_dir() else 0,
            # the connected agent: the page names it here only, the rest of the UI stays agent-neutral
            "agent": "Claude Code",
            "claude_settings_path": str(CLAUDE_SETTINGS),
            "cleanup_period_days": cs.get("cleanupPeriodDays"),
            "hook_installed": hook_installed(cs),
            "statusline_installed": statusline.installed(cs),
            "statusline_command": statusline.command(),
            "autostart": {"enabled": bg.autostart_enabled(), "file": str(bg.autostart_file())},
            "hook_command": hook_command(),
            "db_path": str(db_path),
            "sessions": counts["s"],
            "prompts": counts["p"],
            "agents": counts["a"],
            "last_prompt_at": counts["last"],
            "unpriced_models": sorted(unpriced),
        }

    @app.post("/api/setup/agents/{agent_id}")
    def connect_agent(agent_id: str, body: dict = Body(...)):
        keys = {"claude-code": ("claude_enabled", "claude_projects_dir"),
                "codex": ("codex_enabled", "codex_sessions_dir")}
        if agent_id not in keys:
            raise HTTPException(404, "unknown agent")
        enabled_key, path_key = keys[agent_id]
        values = {}
        if "enabled" in body:
            if not isinstance(body["enabled"], bool):
                raise HTTPException(400, "enabled must be true or false")
            values[enabled_key] = body["enabled"]
        if "directory" in body:
            if not isinstance(body["directory"], str) or not body["directory"].strip():
                raise HTTPException(400, "a transcript directory is required")
            values[path_key] = body["directory"].strip()
        db.save_settings(conn(), values)
        return setup_status()

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

    @app.post("/api/setup/statusline")
    def install_statusline(install: bool = True):
        """Capture Claude Code's usage limits from its statusline, keeping the user's own statusline; or put it back."""
        write_claude_settings(statusline.install(read_claude_settings(), install))
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

    @app.get("/api/setup/history")
    def history_scope():
        """Every project and session, excluded ones included, for the Setup page's exclusion lists."""
        c = conn()
        s = db.get_settings(c)
        projects = [
            {"project": r[0], "prompts": r[1], "excluded": r[0] in s["excluded_projects"]}
            for r in c.execute("SELECT project, COUNT(*) FROM prompts WHERE project != '' GROUP BY project ORDER BY project")
        ]
        sessions = [
            {**dict(r), "excluded": r["id"] in s["excluded_sessions"]}
            for r in c.execute(
                "SELECT s.id, s.title, s.project, MIN(p.started_at) started_at, COUNT(p.id) prompts, "
                "(SELECT text FROM prompts f WHERE f.session_id = s.id ORDER BY f.started_at LIMIT 1) first_prompt "
                "FROM sessions s JOIN prompts p ON p.session_id = s.id GROUP BY s.id ORDER BY started_at DESC"
            )
        ]
        # skills and subagent types the agent used, from their AI tags
        used = [
            {"tag": r[0], "prompts": r[1], "excluded": r[0] in s["excluded_tags"]}
            for r in c.execute(
                "SELECT tag, COUNT(*) FROM tags WHERE auto = 1 AND (tag LIKE 'skill:%' OR tag LIKE 'agent:%') GROUP BY tag ORDER BY tag"
            )
        ]
        reruns = c.execute("SELECT COUNT(*) FROM tags WHERE tag = 'rerun' AND auto = 1").fetchone()[0]
        return {
            "reruns": [{"tag": "rerun", "prompts": reruns, "excluded": "rerun" in s["excluded_tags"]}] if reruns else [],
            # prompts left out by any exclusion, each counted once
            "hidden": c.execute("SELECT (SELECT COUNT(*) FROM prompts) - (SELECT COUNT(*) FROM pv)").fetchone()[0],
            "projects": projects, "sessions": sessions,
            "skills": [u for u in used if u["tag"].startswith("skill:")],
            "agents": [u for u in used if u["tag"].startswith("agent:")],
        }

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

    # each skill version a prompt ran, labelled "skill @ commit" (or its plugin or bundling harness version),
    # separated by \x1f: lets the charts split runs by skill version
    SKILL_VERSIONS_SQL = (
        "SELECT GROUP_CONCAT(k.skill || ' @ ' || COALESCE(substr(v.git_commit, 1, 7), 'Claude Code ' || v.bundled_version, "
        "'v' || v.plugin_version, substr(k.hash, 1, 7)), char(31)) FROM prompt_skill_runs k "
        "JOIN skill_versions v ON v.hash = k.hash WHERE k.prompt_id = p.id"
    )

    LIST_COLS = (
        "p.id, p.session_id, p.project, p.command, p.text, p.started_at, p.wall_ms, p.active_ms, "
        "p.total_tokens, p.total_cost_usd, p.cost_usd, p.agent_count, p.api_calls, p.tool_calls, "
        "p.estimated_cost_usd, p.official_cost_usd, p.cost_source, p.api_ms, p.harness, p.harness_version, p.effort, p.requested_model, "
        f"({SKILL_VERSIONS_SQL}) skill_versions"
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

    def prompt_filters(q: str, tag: str, project: str, since: str) -> tuple[str, list]:
        """WHERE clause (or "") over the prompts view `p` shared by the prompt list and the overview."""
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
        return ("WHERE " + " AND ".join(where) if where else ""), args

    @app.get("/api/prompts")
    def list_prompts(
        q: str = "", tag: str = "", project: str = "", since: str = "", sort: str = "recent",
        limit: int = 100, offset: int = 0,
    ):
        """Prompts matching the filters, plus `summary`: totals over all of them (not just this page)."""
        if sort not in PROMPT_SORTS:
            raise HTTPException(400, f"sort must be one of {list(PROMPT_SORTS)}")
        c = conn()
        where, args = prompt_filters(q, tag, project, since)
        sql = f"SELECT {LIST_COLS} FROM pv p {where}"
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
        prompt["skill_versions"] = c.execute(f"SELECT ({SKILL_VERSIONS_SQL}) FROM prompts p WHERE p.id = ?", (prompt_id,)).fetchone()[0]
        prompt["agents"] = [_row(a) for a in c.execute("SELECT * FROM agents WHERE prompt_id=? ORDER BY started_at", (prompt_id,))]
        s = c.execute("SELECT title, reported_cost_usd FROM sessions WHERE id=?", (prompt["session_id"],)).fetchone()
        prompt["session"] = dict(s) if s else {}
        prompt["telemetry"] = telemetry_breakdown(c, "prompt_id = ?", (prompt_id,))
        # reference runs of this prompt's tags: their settings can be reused to rerun it
        prompt["references"] = [
            dict(x) for x in c.execute(
                "SELECT r.tag, p.id, p.started_at, p.total_cost_usd, p.requested_model model, p.effort, p.harness_version "
                "FROM tag_reference r JOIN tags t ON t.tag = r.tag AND t.prompt_id = ? "
                "JOIN pv p ON p.id = r.prompt_id JOIN tags rt ON rt.tag = r.tag AND rt.prompt_id = p.id "
                "WHERE p.id != ? ORDER BY r.tag", (prompt_id, prompt_id))
        ]
        # replays launched from here, and the prompt this one replays
        refresh_reruns(c)
        prompt["replays"] = []
        for x in c.execute("SELECT * FROM replays WHERE prompt_id = ? ORDER BY launched_at DESC", (prompt_id,)).fetchall():
            done = c.execute("SELECT id, started_at, total_cost_usd, total_tokens, active_ms, wall_ms FROM pv WHERE session_id = ? "
                             "ORDER BY started_at LIMIT 1", (x["session_id"],)).fetchone()
            prompt["replays"].append({**rerun_row(x), "prompt": dict(done) if done else None,
                                      "rating": rating_of(c, prompt_id, done["id"]) if done else None})
        # a prompt of a rerun session: the prompt it reruns, and the configuration chosen to launch it
        # with what the comparison needs: the original's answer, files and figures
        of = c.execute(
            "SELECT p.id, p.started_at, p.total_cost_usd, p.text, p.answer, p.files, p.total_tokens, p.active_ms, p.wall_ms, "
            "p.api_calls, p.tool_calls, p.tools, x.model, x.effort, x.harness_version, x.launched_at, x.mode, x.status, x.result, "
            "x.denials, x.skills, x.permission, x.budget_usd, x.cost_usd run_cost_usd, "
            # how the initial run itself ran: what "same" means in the rerun's settings
            "p.harness original_harness, p.harness_version original_harness_version, p.requested_model original_model, "
            "p.models original_models, p.effort original_effort, p.project original_project, "
            "? = (SELECT id FROM prompts WHERE session_id = x.session_id ORDER BY started_at LIMIT 1) first "
            "FROM replays x LEFT JOIN pv p ON p.id = x.prompt_id WHERE x.session_id = ?",
            (prompt_id, prompt["session_id"]),
        ).fetchone()
        if of:
            o = _row(of)
            o.update(first=bool(o["first"]), denials=json.loads(o["denials"] or "null"), skills=json.loads(o["skills"] or "[]"),
                     original_models=json.loads(o["original_models"] or "[]"),
                     original_skills=[dict(k) for k in c.execute("SELECT skill, hash FROM prompt_skill_runs WHERE prompt_id = ? ORDER BY skill", (o["id"],))],
                     rating=rating_of(c, o["id"], prompt_id) if o["id"] and o["first"] else None)
            prompt["replay_of"] = o
        else:
            prompt["replay_of"] = None
        # what ran it, to run it again: harness, effort, model and the exact skill versions
        prompt["rerun"] = {
            "harness": r["harness"] or "", "harness_version": r["harness_version"] or "", "effort": r["effort"] or "",
            "requested_model": r["requested_model"] or "", "cwd": r["cwd"] or "",
            "permissions": json.loads(r["permissions"] or "{}"),
            "skills": [
                dict(x) for x in c.execute(
                    "SELECT r.skill, r.hash, r.args, v.base_dir, v.origin, v.git_repo, v.git_path, v.git_commit, v.git_state, "
                    "v.plugin, v.plugin_version, v.bundled_version FROM prompt_skill_runs r JOIN skill_versions v ON v.hash = r.hash "
                    "WHERE r.prompt_id = ? ORDER BY r.skill", (prompt_id,))
            ],
        }
        try:
            prompt["rerun"]["permission_args"] = replay.original_permission_args(r["harness"], prompt["rerun"]["permissions"])[0]
        except ValueError:
            prompt["rerun"]["permission_args"] = None

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
        prompt["compare"] = {"scope": compare, "tag": tag, "reference": reference_of(c, tag) if tag else None}
        prompt["similar"] = compared
        return prompt

    def make_rules(
        tag: str, keywords: list[str], match: str = "all", scope: str = "prompt", skill: str = "", preferred_tag: str = "", include_tag: str = "", exclude: bool = False,
    ) -> list[dict]:
        """Rule tagging, past and future, with the human tag `tag` every prompt (or every prompt of a session)
        containing the keywords, or where the agent used `skill`: one rule, one per keyword with "any", none
        without keywords or skill."""
        keywords = sorted({k.strip() for k in keywords if k.strip()}, key=str.lower)
        skill = skill.strip().removeprefix("skill:")
        preferred_tag = preferred_tag.strip()
        include_tag = include_tag.strip()
        if exclude and (not keywords or skill or preferred_tag or include_tag):
            raise HTTPException(400, "Keyword exclusion needs keywords and cannot be combined with a tag or skill rule")
        if include_tag:
            if preferred_tag or scope not in ("prompt", "session"):
                raise HTTPException(400, "Choose include or exclude and a valid scope")
            c = conn()
            if include_tag == tag or not c.execute("SELECT 1 FROM tags WHERE tag=? UNION SELECT 1 FROM tag_meta WHERE tag=?", (include_tag, include_tag)).fetchone():
                raise HTTPException(400, "Choose another existing tag to include")
            return [{"tag": tag, "include_tag": include_tag, **({"scope": "session"} if scope == "session" else {})}]
        if preferred_tag:
            c = conn()
            if preferred_tag == tag or not c.execute("SELECT 1 FROM tags WHERE tag=? UNION SELECT 1 FROM tag_meta WHERE tag=?", (preferred_tag, preferred_tag)).fetchone():
                raise HTTPException(400, "Choose another existing tag to take priority")
            return [{"tag": tag, "preferred_tag": preferred_tag}]
        if match not in ("all", "any"):
            raise HTTPException(400, "match must be 'all' or 'any'")
        if scope not in ("prompt", "session"):
            raise HTTPException(400, "scope must be 'prompt' or 'session'")
        if skill:
            rule = {"tag": tag, "skill": skill}
            if scope == "session":
                rule["scope"] = "session"
            return [rule]
        if not keywords:
            return []
        rule = {"tag": tag, "keywords": keywords, "match": match}
        if exclude:
            rule["exclude"] = True
        if scope == "session":  # prompt rules keep their original shape
            rule["scope"] = "session"
        return split_any(rule)

    def validate_dependencies(c, rules, priority_override=None):
        edges = {}
        for lower, higher in c.execute("SELECT tag, preferred_tag FROM tag_priorities"):
            if priority_override is None or lower != priority_override[0]:
                edges.setdefault(lower, set()).add(higher)
        if priority_override is not None:
            edges.setdefault(priority_override[0], set()).update(priority_override[1])
        for rule in rules:
            if rule.get("include_tag"):
                edges.setdefault(rule["tag"], set()).add(rule["include_tag"])
        visited, visiting = set(), set()
        def visit(node):
            if node in visiting:
                raise HTTPException(400, "Tag rules cannot contain circular dependencies")
            if node in visited:
                return
            visiting.add(node)
            for dependency in edges.get(node, ()):
                visit(dependency)
            visiting.remove(node)
            visited.add(node)
        for node in edges:
            visit(node)

    def add_rules(c, tag: str, rules: list[dict]) -> None:
        preferred = {r[0] for r in c.execute("SELECT preferred_tag FROM tag_priorities WHERE tag=?", (tag,))}
        added_priorities = {r["preferred_tag"] for r in rules if r.get("preferred_tag")}
        rules = [r for r in rules if not r.get("preferred_tag")]
        settings = db.get_settings(c)
        new = [r for r in rules if r not in settings["tag_rules"]]
        validate_dependencies(c, [*settings["tag_rules"], *new], (tag, preferred | added_priorities))
        _record_creation(c, tag)
        if added_priorities:
            set_tag_priorities(tag, sorted(preferred | added_priorities))
        if new:
            db.save_settings(c, {"tag_rules": [*settings["tag_rules"], *new]})
            apply_rules(c, db.get_settings(c)["tag_rules"])

    def add_rule(c, tag: str, keywords: list[str], match: str, scope: str = "prompt") -> None:
        add_rules(c, tag, make_rules(tag, keywords, match, scope))

    @app.post("/api/prompts/{prompt_id}/tags")
    def add_tag(
        prompt_id: str,
        tag: str = Body(..., embed=True),
        keywords: list[str] = Body([], embed=True),
        match: str = Body("all", embed=True),
        scope: str = Body("prompt", embed=True),
    ):
        """Add a human tag; with `keywords`, also tag every prompt (or session) containing all (or any) of them."""
        tag = tag.strip()
        if not tag:
            raise HTTPException(400, "empty tag")
        c = conn()
        _record_creation(c, tag)
        with c:
            c.execute("DELETE FROM tag_exclusions WHERE prompt_id=? AND tag=?", (prompt_id, tag))
            c.execute(
                "INSERT INTO tags(prompt_id, tag, auto) VALUES (?,?,0) ON CONFLICT DO UPDATE SET auto=0",
                (prompt_id, tag),
            )
        add_rule(c, tag, keywords, match, scope)
        apply_curation(c)
        return tags_for(c, [prompt_id])[prompt_id]

    @app.get("/api/rules/preview")
    def preview_rule(keywords: str = "", match: str = "all", scope: str = "prompt", skill: str = ""):
        """How many prompts (or sessions, with scope=session) a keyword rule (`keywords` comma-separated)
        or a skill rule would tag."""
        c = conn()
        if skill:
            used = "SELECT prompt_id FROM tags WHERE tag = ? AND auto = 1"
            what = "COUNT(DISTINCT session_id)" if scope == "session" else "COUNT(*)"
            n = c.execute(f"SELECT {what} FROM pv WHERE id IN ({used})", (f"skill:{skill.removeprefix('skill:')}",)).fetchone()[0]
            return {"matches": n}
        rule = {"keywords": keywords.split(","), "match": match}
        # counted in the history only: excluded projects and sessions are left out
        if scope == "session":
            shown = {r[0] for r in c.execute("SELECT DISTINCT session_id FROM pv")}
            return {"matches": sum(rule_matches(rule, text) for sid, text in session_texts(c) if sid in shown)}
        return {"matches": sum(rule_matches(rule, r[0]) for r in c.execute("SELECT text FROM pv"))}

    @app.post("/api/tags")
    def create_tag(
        tag: str = Body(..., embed=True),
        description: str = Body("", embed=True),
        keywords: list[str] = Body([], embed=True),
        match: str = Body("all", embed=True),
        scope: str = Body("prompt", embed=True),
        rules: list[dict] = Body([], embed=True),
    ):
        """A new human tag, before any prompt carries it. Each of `rules` ({keywords, match, scope}, or the
        single `keywords`/`match`/`scope`) tags the prompts or sessions it matches."""
        tag = tag.strip()
        if not tag:
            raise HTTPException(400, "empty tag")
        c = conn()
        if c.execute("SELECT 1 FROM tag_meta WHERE tag = ? UNION SELECT 1 FROM tags WHERE tag = ?", (tag, tag)).fetchone():
            raise HTTPException(409, f"the tag {tag} already exists")
        # validate every rule before saving anything
        made = make_rules(tag, keywords, match, scope) + [
            part for r in rules
            for part in make_rules(tag, r.get("keywords") or [], r.get("match", "all"), r.get("scope", "prompt"), r.get("skill") or "", r.get("preferred_tag") or "", r.get("include_tag") or "", r.get("exclude", False))
        ]
        _record_creation(c, tag, description.strip() or None)
        add_rules(c, tag, made)
        return {"tag": tag, "prompts": c.execute("SELECT COUNT(*) FROM tags WHERE tag = ?", (tag,)).fetchone()[0]}

    @app.put("/api/tags/{tag:path}/description")
    def set_tag_description(tag: str, description: str = Body(..., embed=True)):
        c = conn()
        _record_creation(c, tag)
        with c:
            c.execute("UPDATE tag_meta SET description = ? WHERE tag = ?", (description.strip() or None, tag))
        return {"tag": tag, "description": description.strip() or None}

    @app.get("/api/tags/{tag:path}/rules")
    def tag_rules(tag: str):
        c = conn()
        return ([{**r, "index": i} for i, r in enumerate(db.get_settings(c)["tag_rules"]) if r.get("tag") == tag]
                + [{"tag": tag, "preferred_tag": r[0], "index": -i - 1} for i, r in enumerate(c.execute("SELECT preferred_tag FROM tag_priorities WHERE tag=? ORDER BY preferred_tag", (tag,)))])

    @app.post("/api/tags/{tag:path}/rules")
    def add_tag_rule(
        tag: str, keywords: list[str] = Body([], embed=True), match: str = Body("all", embed=True),
        scope: str = Body("prompt", embed=True), skill: str = Body("", embed=True), preferred_tag: str = Body("", embed=True), include_tag: str = Body("", embed=True), exclude: bool = Body(False, embed=True),
    ):
        c = conn()
        add_rules(c, tag, make_rules(tag, keywords, match, scope, skill, preferred_tag, include_tag, exclude))
        return tag_rules(tag)

    @app.put("/api/tags/{tag:path}/rules/{index}")
    def update_tag_rule(
        tag: str, index: int, keywords: list[str] = Body([], embed=True), match: str = Body("all", embed=True),
        scope: str = Body("prompt", embed=True), skill: str = Body("", embed=True), preferred_tag: str = Body("", embed=True), include_tag: str = Body("", embed=True), exclude: bool = Body(False, embed=True),
    ):
        c = conn()
        made = make_rules(tag, keywords, match, scope, skill, preferred_tag, include_tag, exclude)
        if not made:
            raise HTTPException(400, "Choose keywords or a tag to include or exclude")
        if index < 0:
            old = next((r for r in tag_rules(tag) if r["index"] == index), None)
            if not old:
                raise HTTPException(404)
            preferred = [r[0] for r in c.execute("SELECT preferred_tag FROM tag_priorities WHERE tag=? AND preferred_tag!=?", (tag, old["preferred_tag"]))]
            validate_dependencies(c, [*db.get_settings(c)["tag_rules"], *[r for r in made if r.get("include_tag")]], (tag, preferred + ([preferred_tag.strip()] if preferred_tag.strip() else [])))
            set_tag_priorities(tag, preferred + ([preferred_tag.strip()] if preferred_tag.strip() else []))
            if not preferred_tag.strip():
                add_rules(c, tag, made)
            return tag_rules(tag)
        rules = db.get_settings(c)["tag_rules"]
        if not 0 <= index < len(rules) or rules[index].get("tag") != tag:
            raise HTTPException(404)
        if preferred_tag.strip():
            add_rules(c, tag, made)
            rules.pop(index)
            db.save_settings(c, {"tag_rules": rules})
            apply_rules(c, rules)
            return tag_rules(tag)
        others = rules[:index] + rules[index + 1:]
        # "any" of several keywords becomes one rule each, in its place, skipping rules the tag already has
        rules[index:index + 1] = [r for r in made if r not in others]
        validate_dependencies(c, rules)
        db.save_settings(c, {"tag_rules": rules})
        apply_rules(c, rules)
        return tag_rules(tag)

    @app.delete("/api/tags/{tag:path}/rules/{index}")
    def delete_tag_rule(tag: str, index: int):
        c = conn()
        if index < 0:
            old = next((r for r in tag_rules(tag) if r["index"] == index), None)
            if not old:
                raise HTTPException(404)
            preferred = [r[0] for r in c.execute("SELECT preferred_tag FROM tag_priorities WHERE tag=? AND preferred_tag!=?", (tag, old["preferred_tag"]))]
            set_tag_priorities(tag, preferred)
            return tag_rules(tag)
        rules = db.get_settings(c)["tag_rules"]
        if not 0 <= index < len(rules) or rules[index].get("tag") != tag:
            raise HTTPException(404)
        rules.pop(index)
        db.save_settings(c, {"tag_rules": rules})
        apply_rules(c, rules)
        return tag_rules(tag)

    def exclude(c, tag: str, prompt_ids) -> None:
        """Take `tag` off these prompts and keep it off, remembering how they had it so a restore puts it back the same way."""
        for pid in prompt_ids:
            had = c.execute("SELECT auto FROM tags WHERE prompt_id=? AND tag=? UNION ALL SELECT auto FROM tag_suppressed WHERE prompt_id=? AND tag=?",
                            (pid, tag, pid, tag)).fetchone()
            c.execute("INSERT OR IGNORE INTO tag_exclusions(prompt_id, tag, auto) VALUES (?,?,?)", (pid, tag, had[0] if had else -1))
            c.execute("DELETE FROM tag_suppressed WHERE prompt_id=? AND tag=?", (pid, tag))
            c.execute("DELETE FROM tags WHERE prompt_id=? AND tag=? AND auto!=1", (pid, tag))

    @app.delete("/api/prompts/{prompt_id}/tags/{tag:path}")
    def remove_tag(prompt_id: str, tag: str):
        c = conn()
        row = c.execute("SELECT auto FROM tags WHERE prompt_id=? AND tag=?", (prompt_id, tag)).fetchone()
        if row and row["auto"] == 1:
            raise HTTPException(400, "AI tags are derived from the transcript and cannot be removed")
        if c.execute("SELECT 1 FROM tag_reference WHERE tag=? AND prompt_id=?", (tag, prompt_id)).fetchone():
            raise HTTPException(400, "Clear the reference before excluding this prompt from the tag")
        with c:
            exclude(c, tag, [prompt_id])
        apply_curation(c)
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
        described = {r[0]: (r[1], r[2]) for r in c.execute("SELECT tag, created_at, description FROM tag_meta")}
        out = [
            {**dict(r), "description": described.get(r["tag"], (None, None))[1], "spark": history.get(r["tag"], [])[-SPARK_POINTS:]}
            for r in c.execute(
                "SELECT t.tag, CASE WHEN MIN(t.auto = 1) = 0 THEN 'human' ELSE 'ai' END kind, COUNT(*) prompts, SUM(p.total_cost_usd) cost_usd, AVG(p.total_cost_usd) avg_cost_usd, "
                "AVG(p.total_tokens) avg_tokens, AVG(p.wall_ms) avg_wall_ms, MAX(p.started_at) last_used, "
                "MIN(p.started_at) first_used, "
                # human tags: when the user created them; AI tags (and older human tags): first prompt
                "COALESCE((SELECT created_at FROM tag_meta m WHERE m.tag = t.tag), MIN(p.started_at)) created_at "
                "FROM tags t JOIN pv p ON p.id = t.prompt_id GROUP BY t.tag ORDER BY kind DESC, last_used DESC"
            )
        ]
        # tags created on the Tags page that no prompt carries yet
        used = {t["tag"] for t in out}
        out += [
            {"tag": tag, "kind": "human", "prompts": 0, "cost_usd": 0, "avg_cost_usd": None, "avg_tokens": None,
             "avg_wall_ms": None, "last_used": None, "first_used": None, "created_at": created, "description": desc, "spark": []}
            for tag, (created, desc) in described.items() if tag not in used
        ]
        return out

    def reference_of(c, tag: str) -> dict | None:
        """The tag's reference run, while it still carries the tag and is in the history."""
        r = c.execute(
            f"SELECT {LIST_COLS} FROM pv p JOIN tag_reference r ON r.prompt_id = p.id "
            "JOIN tags t ON t.prompt_id = p.id AND t.tag = r.tag WHERE r.tag = ?",
            (tag,),
        ).fetchone()
        return _row(r) if r else None

    @app.get("/api/tags/{tag:path}/reference")
    def get_tag_reference(tag: str):
        return {"tag": tag, "reference": reference_of(conn(), tag)}

    @app.put("/api/tags/{tag:path}/reference")
    def set_tag_reference(tag: str, prompt_id: str | None = Body(None, embed=True)):
        """Make one of the tag's runs its reference; null clears it."""
        c = conn()
        with c:
            if prompt_id is None:
                c.execute("DELETE FROM tag_reference WHERE tag = ?", (tag,))
            elif not c.execute("SELECT 1 FROM tags WHERE tag = ? AND prompt_id = ?", (tag, prompt_id)).fetchone():
                raise HTTPException(400, "the reference must be one of the tag's runs")
            else:
                c.execute("INSERT INTO tag_reference(tag, prompt_id) VALUES (?, ?) ON CONFLICT(tag) DO UPDATE SET prompt_id = excluded.prompt_id", (tag, prompt_id))
        return {"tag": tag, "reference": reference_of(c, tag)}

    @app.post("/api/tags/{tag:path}/prompts")
    def tag_some_prompts(tag: str, prompt_ids: list[str] = Body(..., embed=True)):
        """Give the human tag `tag` by hand to every prompt of `prompt_ids` (a selection on a tag page)."""
        tag = tag.strip()
        if not tag:
            raise HTTPException(400, "empty tag")
        c = conn()
        known = {r[0] for r in c.execute(f"SELECT id FROM prompts WHERE id IN ({','.join('?' * len(prompt_ids))})", prompt_ids)} if prompt_ids else set()
        _record_creation(c, tag)
        with c:
            c.executemany("DELETE FROM tag_exclusions WHERE prompt_id=? AND tag=?", [(i, tag) for i in known])
            c.executemany(
                "INSERT INTO tags(prompt_id, tag, auto) VALUES (?,?,0) ON CONFLICT DO UPDATE SET auto=0",
                [(i, tag) for i in prompt_ids if i in known],
            )
        apply_curation(c)
        return {"tag": tag, "tagged": c.execute(
            f"SELECT COUNT(*) FROM tags WHERE tag=? AND prompt_id IN ({','.join('?' * len(known))})", (tag, *known)).fetchone()[0] if known else 0}

    def require_human_tag(c, tag):
        if c.execute("SELECT 1 FROM tags WHERE tag=? AND auto=1", (tag,)).fetchone():
            raise HTTPException(400, "AI tags are derived from transcripts and cannot be curated")
        if not c.execute("SELECT 1 FROM tag_meta WHERE tag=? UNION SELECT 1 FROM tags WHERE tag=? AND auto!=1", (tag, tag)).fetchone():
            raise HTTPException(404, "unknown human tag")

    @app.delete("/api/tags/{tag:path}/prompts")
    def untag_prompts(tag: str, prompt_ids: list[str] = Body(..., embed=True)):
        c = conn()
        require_human_tag(c, tag)
        known = {r[0] for r in c.execute(f"SELECT id FROM prompts WHERE id IN ({','.join('?' * len(prompt_ids))})", prompt_ids)} if prompt_ids else set()
        # the reference stays: it is skipped, not an error, so a selection including it still goes through
        reference = c.execute("SELECT prompt_id FROM tag_reference WHERE tag=?", (tag,)).fetchone()
        skipped = reference[0] if reference and reference[0] in known else None
        with c:
            exclude(c, tag, known - {skipped})
        apply_curation(c)
        return {"tag": tag, "removed": len(known - {skipped}), "skipped_reference": skipped}

    def exclusions(c, tag: str, prompt_ids: list[str]) -> dict[str, int]:
        """The prompts of `prompt_ids` excluded from `tag`, with how they had it."""
        if not prompt_ids:
            return {}
        return dict(c.execute(f"SELECT prompt_id, auto FROM tag_exclusions WHERE tag=? AND prompt_id IN ({','.join('?' * len(prompt_ids))})",
                              (tag, *prompt_ids)).fetchall())

    @app.post("/api/tags/{tag:path}/exclusions/restore")
    def restore_excluded(tag: str, prompt_ids: list[str] = Body(..., embed=True)):
        """Restore the original assignment; unknown legacy assignments fall back to manual membership."""
        c = conn()
        require_human_tag(c, tag)
        excluded = exclusions(c, tag, prompt_ids)
        with c:
            c.executemany("DELETE FROM tag_exclusions WHERE prompt_id=? AND tag=?", [(pid, tag) for pid in excluded])
            c.executemany("INSERT OR IGNORE INTO tags(prompt_id, tag, auto) VALUES (?,?,0)", [(pid, tag) for pid, auto in excluded.items() if auto in (-1, 0)])
        if any(auto == 2 for auto in excluded.values()):
            apply_rules(c, db.get_settings(c)["tag_rules"])
        else:
            apply_curation(c)
        return {"tag": tag, "restored": len(excluded)}

    @app.put("/api/tags/{tag:path}/exclusions")
    def set_exclusion_examples(tag: str, prompt_ids: list[str] = Body(..., embed=True), example: bool = Body(..., embed=True)):
        """Use excluded prompts as examples of what does not belong to the tag, or not: they stay excluded either way."""
        c = conn()
        require_human_tag(c, tag)
        excluded = exclusions(c, tag, prompt_ids)
        with c:
            c.executemany("UPDATE tag_exclusions SET example=? WHERE prompt_id=? AND tag=?", [(int(example), pid, tag) for pid in excluded])
        return {"tag": tag, "updated": len(excluded)}

    @app.get("/api/tags/{tag:path}/curation")
    def tag_curation(tag: str):
        c = conn()
        rows = [dict(r) for r in c.execute("SELECT p.id, p.text, p.started_at FROM pv p JOIN tags t ON t.prompt_id=p.id WHERE t.tag=? ORDER BY p.started_at, p.id", (tag,))]
        reference = c.execute("SELECT prompt_id FROM tag_reference WHERE tag=?", (tag,)).fetchone()
        excluded = [dict(r) for r in c.execute(
            "SELECT p.id, p.text, p.started_at, e.example FROM pv p JOIN tag_exclusions e ON e.prompt_id=p.id WHERE e.tag=? ORDER BY p.started_at, p.id", (tag,))
            if not reference or r["id"] != reference[0]]
        threshold = max(0.8, db.get_settings(c)["similarity_threshold"])
        # each text is split into words and normalized once: every prompt is compared with every example
        def key(row):
            return words(row["text"]), row["text"].strip().casefold()

        def match(row, candidates):
            row_words, row_text = key(row)
            score, other = max(((1.0 if row_text == text else jaccard(row_words, w), o) for w, text, o in candidates),
                               key=lambda pair: pair[0], default=(0, None))
            return {**row, "similar_to": other, "similarity": round(score, 3)} if other and score >= threshold else None

        examples = [(*key(row), row) for row in excluded if row["example"]]
        flagged = [found for row in rows if not (reference and row["id"] == reference[0]) and (found := match(row, examples))]
        kept, redundant = [], []
        for row in excluded:
            if not row["example"]:
                continue
            found = match(row, kept)
            if found:
                redundant.append(found)
            else:
                kept.append((*key(row), row))
        return {"flagged": flagged, "excluded": excluded, "redundant_exclusions": redundant, "threshold": threshold,
                "preferred_tags": [r[0] for r in c.execute("SELECT preferred_tag FROM tag_priorities WHERE tag=? ORDER BY preferred_tag", (tag,))]}

    @app.put("/api/tags/{tag:path}/priorities")
    def set_tag_priorities(tag: str, preferred_tags: list[str] = Body(..., embed=True)):
        c = conn()
        require_human_tag(c, tag)
        preferred_tags = sorted(set(t.strip() for t in preferred_tags if t.strip()))
        known = {r[0] for r in c.execute("SELECT tag FROM tags UNION SELECT tag FROM tag_meta")}
        if any(t not in known for t in preferred_tags):
            raise HTTPException(400, "choose existing tags")
        edges = {}
        for lower, higher in c.execute("SELECT tag, preferred_tag FROM tag_priorities WHERE tag!=?", (tag,)):
            edges.setdefault(lower, set()).add(higher)
        edges[tag] = set(preferred_tags)
        def cyclic(node, path):
            return node in path or any(cyclic(n, path | {node}) for n in edges.get(node, ()))
        if cyclic(tag, set()):
            raise HTTPException(400, "tag priorities cannot contain a cycle or prioritize themselves")
        validate_dependencies(c, db.get_settings(c)["tag_rules"], (tag, preferred_tags))
        with c:
            c.execute("DELETE FROM tag_priorities WHERE tag=?", (tag,))
            c.executemany("INSERT INTO tag_priorities VALUES (?,?)", [(tag, t) for t in preferred_tags])
        apply_curation(c)
        return tag_curation(tag)

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

    SESSION_SORTS = {
        "recent": "started_at DESC",
        "cost": "cost_usd DESC",
        "prompts": "prompts DESC, cost_usd DESC",
        "duration": "active_ms DESC",
        "tokens": "total_tokens DESC",
    }

    @app.get("/api/sessions")
    def list_sessions(
        q: str = "", project: str = "", since: str = "", sort: str = "recent", limit: int = 100, offset: int = 0,
    ):
        """Sessions matching the filters (`q` searches titles and prompts; `since` keeps sessions started after it),
        plus `summary` over all of them and, on the first page, `highlights`: the priciest, busiest and longest."""
        if sort not in SESSION_SORTS:
            raise HTTPException(400, f"sort must be one of {list(SESSION_SORTS)}")
        c = conn()
        where, args = [], []
        if project:
            where.append("s.project = ?")
            args.append(project)
        if q:
            where.append("(s.title LIKE ? OR s.id IN (SELECT session_id FROM prompts WHERE text LIKE ?))")
            args += [f"%{q}%", f"%{q}%"]
        sql = (
            f"SELECT {SESSION_COLS}, (SELECT text FROM prompts f WHERE f.session_id = s.id ORDER BY f.started_at LIMIT 1) first_prompt "
            f"FROM sessions s JOIN pv p ON p.session_id = s.id {'WHERE ' + ' AND '.join(where) if where else ''} GROUP BY s.id"
        )
        if since:
            sql += " HAVING MIN(p.started_at) >= ?"
            args.append(since)
        summary = dict(c.execute(
            "SELECT COUNT(*) count, SUM(prompts) prompts, SUM(cost_usd) cost_usd, SUM(total_tokens) tokens, "
            f"SUM(active_ms) active_ms, MAX(cost_usd) max_cost_usd FROM ({sql})",
            args,
        ).fetchone())
        page = lambda order, n, skip=0: [dict(r) for r in c.execute(f"{sql} ORDER BY {order} LIMIT ? OFFSET ?", [*args, n, skip])]
        out = {"total": summary["count"], "summary": summary, "items": page(SESSION_SORTS[sort], limit, offset)}
        if offset == 0 and summary["count"]:
            out["highlights"] = {key: page(SESSION_SORTS[key], 1)[0] for key in ("cost", "prompts", "duration")}
        return out

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

    # ---- overview (home page) ------------------------------------------------------

    @app.get("/api/limits")
    def usage_limits():
        """Subscription usage of every connected agent, as last seen: Claude Code through its statusline (`capture`:
        whether that is set up), Codex in its rollouts. No `windows` when nothing was reported yet; a window whose
        reset time has passed since is back to 0%."""
        if examples:
            return []  # the illustrative records have no subscription
        settings = db.get_settings(conn())
        empty = {"observed_at": None, "plan": None, "windows": []}
        found = []
        if settings.get("claude_enabled", True):
            found.append({"agent": "Claude Code", "capture": statusline.installed(read_claude_settings()),
                          **(statusline.rate_limits() or empty)})
        if settings.get("codex_enabled", True):
            found.append({"agent": "Codex", "capture": True, **(CodexSource(settings["codex_sessions_dir"]).rate_limits() or empty)})
        now = datetime.now(timezone.utc).timestamp()
        for limits in found:
            for w in limits["windows"]:
                w["reset"] = bool(w["resets_at"] and w["resets_at"] <= now)
                if w["reset"]:
                    w["used_percent"] = 0
                if w["resets_at"]:
                    w["resets_at"] = datetime.fromtimestamp(w["resets_at"], timezone.utc).isoformat()
        return found

    @app.get("/api/overview")
    def overview(q: str = "", tag: str = "", project: str = "", since: str = ""):
        """Totals, the priciest prompt, the most repeated prompt text, the repeated text costing the most in total
        and usage per tag, over the prompts matching the same filters as /api/prompts."""
        c = conn()
        where, args = prompt_filters(q, tag, project, since)
        totals = dict(c.execute(
            "SELECT COUNT(*) prompts, COUNT(DISTINCT p.session_id) sessions, SUM(p.total_cost_usd) cost_usd, "
            "SUM(p.total_tokens) tokens, SUM(COALESCE(p.active_ms, p.wall_ms)) duration_ms, "
            f"MIN(p.started_at) first_at, MAX(p.started_at) last_at FROM pv p {where}",
            args,
        ).fetchone())
        r = c.execute(f"SELECT {LIST_COLS} FROM pv p {where} ORDER BY p.total_cost_usd DESC LIMIT 1", args).fetchone()
        priciest = _row(r) if r else None
        if priciest:
            priciest["tags"] = tags_for(c, [priciest["id"]])[priciest["id"]]
        def top_repeated(order: str) -> dict | None:
            """Same text typed again (whitespace and case aside); the latest run opens the comparison with the others."""
            r = c.execute(
                "SELECT COUNT(*) runs, SUM(p.total_cost_usd) cost_usd, AVG(p.total_cost_usd) avg_cost_usd, "
                "MAX(p.started_at) last_at, MAX(p.started_at || '|' || p.id) latest, MIN(p.text) text "
                f"FROM pv p {where} GROUP BY LOWER(TRIM(p.text)) HAVING runs > 1 ORDER BY {order} LIMIT 1",
                args,
            ).fetchone()
            if r is None:
                return None
            return {**{k: r[k] for k in ("runs", "cost_usd", "avg_cost_usd", "last_at", "text")}, "id": r["latest"].split("|", 1)[1]}
        tags = [
            dict(t)
            for t in c.execute(
                "SELECT t.tag, CASE WHEN MIN(t.auto = 1) = 0 THEN 'human' ELSE 'ai' END kind, COUNT(*) prompts, "
                "SUM(p.total_cost_usd) cost_usd, AVG(p.total_cost_usd) avg_cost_usd, MAX(p.started_at) last_used "
                f"FROM tags t JOIN pv p ON p.id = t.prompt_id {where} GROUP BY t.tag ORDER BY prompts DESC, cost_usd DESC",
                args,
            )
        ]
        return {
            "totals": totals, "priciest": priciest, "tags": tags,
            "repeated": top_repeated("runs DESC, cost_usd DESC"),
            "priciest_repeated": top_repeated("cost_usd DESC, runs DESC"),
        }

    # declared after the /rules/{index} routes: the greedy {tag:path} would otherwise swallow them
    @app.delete("/api/tags/{tag:path}")
    def delete_tag(tag: str):
        """Delete one of your tags: off every prompt, with its rules and description. AI tags come from the
        transcripts and cannot be deleted."""
        c = conn()
        mine = c.execute("SELECT 1 FROM tag_meta WHERE tag = ? UNION SELECT 1 FROM tags WHERE tag = ? AND auto != 1", (tag, tag)).fetchone()
        if not mine:
            if c.execute("SELECT 1 FROM tags WHERE tag = ?", (tag,)).fetchone():
                raise HTTPException(400, "AI tags are derived from the transcripts and cannot be deleted")
            raise HTTPException(404)
        rules = db.get_settings(c)["tag_rules"]
        kept = [r for r in rules if r.get("tag") != tag]
        with c:
            prompts = c.execute("DELETE FROM tags WHERE tag = ? AND auto != 1", (tag,)).rowcount
            c.execute("DELETE FROM tag_meta WHERE tag = ?", (tag,))
            c.execute("DELETE FROM tag_reference WHERE tag = ?", (tag,))
            c.execute("DELETE FROM tag_exclusions WHERE tag=?", (tag,))
            c.execute("DELETE FROM tag_suppressed WHERE tag=?", (tag,))
            c.execute("DELETE FROM tag_priorities WHERE tag=? OR preferred_tag=?", (tag, tag))
        if kept != rules:
            db.save_settings(c, {"tag_rules": kept})
            apply_rules(c, kept)
        else:
            apply_curation(c)
        return {"tag": tag, "prompts": prompts, "rules": len(rules) - len(kept)}

    async def dashboard_json(request: Request) -> dict:
        """The body of a request that starts (or queues) agents: JSON sent by the dashboard itself, never by
        another page posting here."""
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise HTTPException(415, "send JSON")
        origin = request.headers.get("origin")
        if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
            raise HTTPException(403, "replays start from the dashboard only")
        return await request.json()

    def rerun_options(body: dict) -> dict:
        """How to rerun: model, effort, Claude Code release ("" installed, "same" the prompt's own, or a
        version such as a reference run's), mode ("background": unattended, or "terminal": interactive),
        what it may do without asking (background), a budget cap in USD, and whether to restore the skills."""
        model, effort = str(body.get("model") or ""), str(body.get("effort") or "")
        if model and not replay.MODEL_RE.match(model):
            raise HTTPException(400, "unknown model")
        if effort and effort not in replay.EFFORTS:
            raise HTTPException(400, f"effort must be one of {replay.EFFORTS}")
        release = str(body.get("version") or "") or ("same" if body.get("same_version") else "")
        if release not in ("", "same") and not replay.VERSION_RE.match(release):
            raise HTTPException(400, "unknown Claude Code version")
        permission = str(body.get("permission") or "same")
        mode = str(body.get("mode") or ("terminal" if permission == "same" else "background"))
        if mode not in ("background", "terminal"):
            raise HTTPException(400, "mode must be background or terminal")
        if permission not in {*runner.PERMISSIONS, "same"}:
            raise HTTPException(400, "permission must be same, ask, or edits")
        try:
            budget = float(body["budget_usd"]) if body.get("budget_usd") not in (None, "") else None
        except (TypeError, ValueError):
            raise HTTPException(400, "budget must be a number") from None
        if budget is not None and not 0 < budget <= 1000:
            raise HTTPException(400, "budget must be between 0 and 1000 USD")
        judge_model = str(body.get("judge_model") or "")
        if judge_model and not replay.MODEL_RE.match(judge_model):
            raise HTTPException(400, "unknown judge model")
        return {"model": model, "effort": effort, "release": release, "mode": mode, "permission": permission,
                "budget": budget, "restore_skills": body.get("restore_skills", True) is not False,
                "rate": body.get("rate", True) is not False, "judge_model": judge_model}

    def replayable(c, prompt_id: str):
        r = c.execute("SELECT text, cwd, harness, harness_version, requested_model, effort, source, permissions FROM pv WHERE id = ?", (prompt_id,)).fetchone()
        if r is None:
            raise HTTPException(404, f"no prompt {prompt_id}")
        if r["source"] == "rerunly-examples":
            raise HTTPException(400, "Illustrative examples have no executable workspace; use a real prompt to launch a rerun.")
        if r["harness"] and r["harness"] not in ("Claude Code", "Codex"):
            raise HTTPException(400, "this harness does not support replay")
        return r

    reruns_dir = db_path.parent / "reruns"

    @app.get("/api/prompts/{prompt_id}/rerun-estimate")
    def prompt_rerun_estimate(prompt_id: str, model: str = ""):
        if model and not replay.MODEL_RE.match(model):
            raise HTTPException(400, "unknown model")
        c = conn()
        original = replayable(c, prompt_id)
        row = dict(c.execute("SELECT * FROM pv WHERE id=?", (prompt_id,)).fetchone())
        agents = [dict(a) for a in c.execute("SELECT * FROM agents WHERE prompt_id=?", (prompt_id,))]
        agent = "Codex" if replay.uses_codex(model, original["harness"]) else "Claude Code"
        limits = next((entry for entry in usage_limits() if entry["agent"] == agent), None)
        return {"agent": agent, "limits": limits,
                "estimate": estimate_rerun(row, agents, model, db.get_settings(c)["prices"])}

    def finish_rerun(session_id: str) -> None:
        """Record how a background rerun ended, from its log, and drop its restored skills."""
        c = conn()
        x = c.execute("SELECT log_path, status FROM replays WHERE session_id = ?", (session_id,)).fetchone()
        if x is None or x["status"] != "running":
            return
        log = Path(x["log_path"])
        out = runner.outcome(log)
        if out is None:  # no result: it crashed, or Claude Code could not start
            try:
                tail = log.read_text()[-600:].strip()
            except OSError:
                tail = ""
            out = {"status": "failed", "result": tail or "It stopped without a result.", "denials": [], "cost_usd": None, "blocked": ""}
        with c:
            c.execute("UPDATE replays SET status = ?, result = ?, denials = ?, cost_usd = ?, ended_at = ? WHERE session_id = ?",
                      (out["status"], out["result"], json.dumps({"denied": out["denials"], "blocked": out["blocked"]}),
                       out["cost_usd"], runner.now(), session_id))
        runner.remove_plugin(reruns_dir / session_id / "plugin")

    def refresh_reruns(c) -> None:
        """Background reruns whose process ended while the dashboard was not watching it."""
        for x in c.execute("SELECT session_id, pid FROM replays WHERE status = 'running'").fetchall():
            if not runner.alive(x["pid"]):
                finish_rerun(x["session_id"])

    def start_replay(c, prompt_id: str, opts: dict) -> dict:
        """Rerun a prompt with Claude Code: the same folder and prompt, the skills as they ran, the model,
        effort and release asked for; in the background (unattended) or in a terminal window."""
        r = replayable(c, prompt_id)
        opts = dict(opts)
        codex = replay.uses_codex(opts["model"], r["harness"])
        if codex and opts["mode"] == "background" and opts["budget"] is not None:
            raise HTTPException(400, "Codex cannot enforce a USD budget cap; clear the budget to continue.")
        same_harness = codex == (r["harness"] == "Codex")
        recorded_permissions = None
        permission_args = []
        if opts["permission"] == "same":
            if not same_harness:
                raise HTTPException(400, "Original permissions belong to another agent. Choose explicit permissions when changing agents.")
            recorded_permissions = json.loads(r["permissions"] or "{}")
            try:
                permission_args, interactive = replay.original_permission_args(r["harness"], recorded_permissions)
            except ValueError as error:
                raise HTTPException(400, str(error)) from None
            if interactive and opts["mode"] == "background":
                raise HTTPException(400, "Original permissions can ask for approval. Choose a terminal to preserve them, or choose explicit unattended permissions.")
        opts["model"] = opts["model"] or r["requested_model"] or ""
        opts["effort"] = opts["effort"] or r["effort"] or ""
        version = (r["harness_version"] or "") if opts["release"] == "same" else opts["release"]
        version = version if replay.VERSION_RE.match(version) else ""
        if opts["release"] == "same" and not same_harness:
            version = ""
        session_id = str(uuid.uuid4())
        cwd = r["cwd"]
        if not cwd:
            raise HTTPException(400, "The original working directory was not recorded; this prompt cannot be rerun in the same directory.")
        # the skills as they ran, in a plugin for this session only: the user's copies stay untouched
        versions = [dict(v) for v in c.execute(
            "SELECT k.skill, v.* FROM prompt_skill_runs k JOIN skill_versions v ON v.hash = k.hash WHERE k.prompt_id = ? "
            "AND v.origin != 'bundled' ORDER BY k.skill", (prompt_id,))] if opts["restore_skills"] else []
        plugin = reruns_dir / session_id / "plugin" if versions else None
        skills = runner.build_skills_plugin(plugin, versions) if plugin else []
        if codex:
            for skill in skills:
                skill["pinned"] = str(plugin / "skills" / runner._skill_name(skill["skill"]) / "SKILL.md")
        row = {"session_id": session_id, "prompt_id": prompt_id, "model": opts["model"], "effort": opts["effort"],
               "harness_version": version, "launched_at": runner.now(), "mode": opts["mode"], "skills": json.dumps(skills),
               "permission": opts["permission"], "budget_usd": opts["budget"], "auto_rate": int(opts.get("rate", True)),
               "judge_model": opts.get("judge_model", "")}
        if opts["mode"] == "terminal":
            note = "\n".join(f"To use the skill `{k['skill']}`, use `{k['pinned']}` instead: it is the version this task ran with."
                              for k in skills)
            argv = replay.claude_argv(r["text"], session_id, opts["model"], opts["effort"], version, str(plugin or ""), note)
            if codex:
                argv = replay.codex_argv(r["text"], session_id, opts["model"], opts["effort"], version, note=note, original_permissions=recorded_permissions)
            elif recorded_permissions is not None:
                argv = [*argv[:-1], *permission_args, argv[-1]]
            launched = replay.launch(cwd, argv)
            row.update(status="interactive")
        else:
            argv = runner.background_argv(r["text"], session_id, opts["model"], opts["effort"], version, "ask" if recorded_permissions is not None else opts["permission"],
                                          opts["budget"], plugin, skills)
            if codex:
                argv = replay.codex_argv(r["text"], session_id, opts["model"], opts["effort"], version,
                                         background=True, permission=opts["permission"], note=runner.unattended_note(skills), original_permissions=recorded_permissions)
            elif recorded_permissions is not None:
                argv = [*argv[:-1], *permission_args, argv[-1]]
            log = reruns_dir / session_id / "stream.jsonl"
            row.update(status="running", log_path=str(log))
            # recorded before it starts: a quick end must find its row
            with c:
                c.execute(f"INSERT INTO replays({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
            try:
                pid = runner.start(cwd, argv, log, lambda: finish_rerun(session_id), **({"timeout": 3600} if codex else {}))
            except OSError as e:
                with c:
                    c.execute("UPDATE replays SET status = 'failed', result = ?, ended_at = ? WHERE session_id = ?",
                              (f"{'Codex' if codex else 'Claude Code'} could not start: {e}", runner.now(), session_id))
                raise HTTPException(500, f"{'Codex' if codex else 'Claude Code'} could not start: {e}") from None
            with c:
                c.execute("UPDATE replays SET pid = ? WHERE session_id = ?", (pid, session_id))
            return {"prompt_id": prompt_id, "session_id": session_id, "mode": "background", "terminal": None, "cwd": cwd,
                    "skills": skills, "command": f"cd {shlex.quote(cwd)} && {shlex.join(argv)}"}
        # recorded even without a terminal: the command carries the session id, so running it by hand links it too
        with c:
            c.execute(f"INSERT INTO replays({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
        return {"prompt_id": prompt_id, "session_id": session_id, "mode": "terminal", "terminal": launched, "cwd": cwd,
                "skills": skills, "command": f"cd {shlex.quote(cwd)} && {shlex.join(argv)}"}

    @app.post("/api/prompts/{prompt_id}/replay")
    async def replay_prompt(prompt_id: str, request: Request):
        return start_replay(conn(), prompt_id, rerun_options(await dashboard_json(request)))

    # ---- reruns: a queue, launched together from the Reruns page -----------------------------

    QUEUE_KEYS = ("model", "effort", "release", "mode", "permission", "budget_usd", "restore_skills")

    @app.post("/api/reruns/queue")
    async def queue_reruns(request: Request):
        """Queue prompts for a rerun with these options; a prompt already queued with them is not added twice."""
        body = await dashboard_json(request)
        o = rerun_options(body)
        values = {"model": o["model"], "effort": o["effort"], "release": o["release"], "mode": o["mode"], "permission": o["permission"],
                  "budget_usd": o["budget"], "restore_skills": int(o["restore_skills"]), "auto_rate": int(o["rate"]),
                  "judge_model": o["judge_model"]}
        c = conn()
        ids = [str(i) for i in body.get("prompt_ids") or []]
        for i in ids:
            replayable(c, i)
        same = " AND ".join(f"COALESCE({k}, '') = COALESCE(?, '')" for k in values)
        with c:
            added = sum(
                c.execute(
                    f"INSERT INTO replay_queue(prompt_id, same_version, added_at, {', '.join(values)}) "
                    f"SELECT ?, ?, ?, {', '.join('?' * len(values))} WHERE NOT EXISTS (SELECT 1 FROM replay_queue WHERE prompt_id = ? AND {same})",
                    (i, int(o["release"] == "same"), runner.now(), *values.values(), i, *values.values()),
                ).rowcount
                for i in ids
            )
        return {"added": added, "queued": c.execute("SELECT COUNT(*) FROM replay_queue").fetchone()[0]}

    @app.get("/api/reruns")
    def list_reruns():
        """The queue, and the reruns launched so far with how they ended, the prompt they replay and, once
        imported, the rerun itself."""
        c = conn()
        refresh_reruns(c)
        prompt_cols = "id, text, project, started_at, total_cost_usd, total_tokens, active_ms, wall_ms, harness_version, cwd"
        def prompt(pid):
            r = c.execute(f"SELECT {prompt_cols} FROM pv WHERE id = ?", (pid,)).fetchone()
            return dict(r) if r else None
        prices = db.get_settings(c)["prices"]
        queue = []
        for q in c.execute("SELECT * FROM replay_queue ORDER BY id"):
            original = c.execute("SELECT * FROM pv WHERE id=?", (q["prompt_id"],)).fetchone()
            agents = [dict(a) for a in c.execute("SELECT * FROM agents WHERE prompt_id=?", (q["prompt_id"],))]
            queue.append({**dict(q), "prompt": prompt(q["prompt_id"]),
                          "estimate": estimate_rerun(dict(original) if original else None, agents, q["model"], prices)})
        launched = []
        for x in c.execute("SELECT * FROM replays ORDER BY launched_at DESC LIMIT 200"):
            original = prompt(x["prompt_id"])
            done = c.execute(f"SELECT {prompt_cols} FROM pv WHERE session_id = ? ORDER BY started_at LIMIT 1", (x["session_id"],)).fetchone()
            launched.append({**rerun_row(x), "original": original, "replay": dict(done) if done else None,
                             "rating": rating_of(c, x["prompt_id"], done["id"]) if done else None})
        return {"queue": queue, "launched": launched}

    def rerun_row(x) -> dict:
        d = dict(x)
        d["skills"] = json.loads(d.get("skills") or "[]")
        d["denials"] = json.loads(d.get("denials") or "null")
        d.pop("pid", None), d.pop("log_path", None)
        return d

    @app.delete("/api/reruns/queue/{item_id}")
    def unqueue(item_id: int):
        c = conn()
        with c:
            c.execute("DELETE FROM replay_queue WHERE id = ?", (item_id,))
        return {"queued": c.execute("SELECT COUNT(*) FROM replay_queue").fetchone()[0]}

    @app.post("/api/reruns/launch")
    async def launch_reruns(request: Request):
        """Launch queued reruns together and take them out of the queue."""
        body = await dashboard_json(request)
        c = conn()
        results = []
        for item_id in dict.fromkeys(body.get("ids") or []):
            q = c.execute("SELECT * FROM replay_queue WHERE id = ?", (int(item_id),)).fetchone()
            if q is None:
                continue
            release = q["release"] if q["release"] is not None else ("same" if q["same_version"] else "")
            opts = {"model": q["model"] or "", "effort": q["effort"] or "", "release": release, "mode": q["mode"] or "background",
                    "permission": q["permission"] or "same", "budget": q["budget_usd"],
                    "restore_skills": q["restore_skills"] is None or bool(q["restore_skills"]),
                    "rate": q["auto_rate"] is None or bool(q["auto_rate"]), "judge_model": q["judge_model"] or ""}
            try:
                results.append({"id": q["id"], **start_replay(c, q["prompt_id"], opts)})
            except HTTPException as e:
                results.append({"id": q["id"], "prompt_id": q["prompt_id"], "error": e.detail})
                continue  # keep failed items queued so a batch launch can be retried
            with c:
                c.execute("DELETE FROM replay_queue WHERE id = ?", (q["id"],))
        return {"results": results}

    @app.post("/api/ratings")
    async def rate_pair(request: Request):
        """Have an LLM judge rate how much run `other`'s output differs from run `base`'s: with `model` (else the
        default judge), again with `force`."""
        body = await dashboard_json(request)
        model = str(body.get("model") or "")
        if model and not replay.MODEL_RE.match(model):
            raise HTTPException(400, "unknown judge model")
        c = conn()
        for pid in (body.get("base"), body.get("other")):
            r = c.execute("SELECT source FROM pv WHERE id = ?", (str(pid),)).fetchone()
            if not r:
                raise HTTPException(404, f"no prompt {pid}")
            if r["source"] == "rerunly-examples":
                raise HTTPException(400, "Illustrative examples keep their illustrative rating; rate real runs instead.")
        rating = start_rating(str(body["base"]), str(body["other"]), bool(body.get("force")), model)
        if rating is None:
            raise HTTPException(400, "both runs need a final answer to be rated (and two different runs)")
        return rating

    @app.get("/api/compare")
    def compare_runs(a: str, b: str):
        """Two runs side by side: their figures, files and answers, their rating, and the runs related to the
        first (its original, every rerun of it, and its tags' reference runs) to pick from."""
        c = conn()
        refresh_reruns(c)
        cols = (f"{LIST_COLS}, p.answer, p.files, p.session_id")
        def run(pid):
            r = c.execute(f"SELECT {cols} FROM pv p WHERE p.id = ?", (pid,)).fetchone()
            if r is None:
                raise HTTPException(404, f"no prompt {pid}")
            d = _row(r)
            x = c.execute("SELECT prompt_id, model, effort, harness_version, launched_at FROM replays WHERE session_id = ?", (d["session_id"],)).fetchone()
            d["rerun_of"] = dict(x) if x else None
            return d
        first, second = run(a), run(b)
        original = (first["rerun_of"] or {}).get("prompt_id") or a
        related = [original] + [r[0] for r in c.execute(
            "SELECT (SELECT id FROM pv WHERE session_id = x.session_id ORDER BY started_at LIMIT 1) FROM replays x "
            "WHERE x.prompt_id = ? ORDER BY x.launched_at", (original,)) if r[0]]
        related += [r[0] for r in c.execute(
            "SELECT r.prompt_id FROM tag_reference r JOIN tags t ON t.tag = r.tag AND t.prompt_id = ? JOIN pv p ON p.id = r.prompt_id",
            (original,))]
        candidates = []
        for pid in dict.fromkeys([*related, a, b]):
            r = c.execute("SELECT id, started_at, total_cost_usd, session_id FROM pv WHERE id = ?", (pid,)).fetchone()
            if r:
                is_rerun = c.execute("SELECT 1 FROM replays WHERE session_id = ?", (r["session_id"],)).fetchone()
                candidates.append({"id": r["id"], "started_at": r["started_at"], "total_cost_usd": r["total_cost_usd"],
                                   "kind": "original" if pid == original else "rerun" if is_rerun else "reference"})
        return {"a": first, "b": second, "rating": rating_of(c, a, b), "candidates": candidates}

    @app.get("/api/skill-versions/{hash}")
    def skill_version(hash: str):
        """A skill exactly as a run loaded it (SKILL.md without its frontmatter), and where it comes from."""
        r = conn().execute("SELECT * FROM skill_versions WHERE hash = ?", (hash,)).fetchone()
        if r is None:
            raise HTTPException(404)
        return dict(r)

    @app.get("/api/projects")
    def list_projects():
        c = conn()
        return [r[0] for r in c.execute("SELECT DISTINCT project FROM pv WHERE project != '' ORDER BY project")]

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
