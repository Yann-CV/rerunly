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
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import background as bg, db, judge, otel, replay, runner
from .config import DEFAULT_DB_PATH, DEFAULT_PORT
from .ingest import apply_rules, ingest, link_replay, session_texts
from .tagging import rule_matches, rule_scope, similarity, split_any

STATIC = Path(__file__).parent / "static"
CLAUDE_SETTINGS = Path.home() / ".claude/settings.json"
ANTIGRAVITY_CONFIG_DIR = Path.home() / ".gemini/config"
ANTIGRAVITY_HOOKS = ANTIGRAVITY_CONFIG_DIR / "hooks.json"
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


def create_app(db_path: Path = DEFAULT_DB_PATH, port: int = DEFAULT_PORT, background: bool = True) -> FastAPI:
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
    # judgements this process runs: a "rating" row missing here was cut off (the dashboard stopped) and can restart
    judging: set[tuple[str, str]] = set()

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
            if current and ((base_id, other_id) in judging or (current["status"] == "done" and not force)):
                return current
            judging.add((base_id, other_id))
            model = model or db.get_settings(c)["judge_model"]
            with c:
                c.execute("INSERT OR REPLACE INTO run_ratings(base_id, other_id, status, model, rated_at) VALUES (?,?,?,?,?)",
                          (base_id, other_id, "rating", model, runner.now()))

        def work():
            files = lambda r: json.loads(r["files"] or "[]")
            try:
                out = judge.rate(runs[base_id]["text"], {"answer": runs[base_id]["answer"], "files": files(runs[base_id])},
                                 {"answer": runs[other_id]["answer"], "files": files(runs[other_id])}, model, db_path.parent / "judge")
                values = ("done", out["score"], out["verdict"], out["quality"], int(out["look"]), out["summary"],
                          json.dumps(out["differences"]), out["cost_usd"], None)
            except Exception as e:  # noqa: BLE001 - any failure is reported on the page
                values = ("failed", None, None, None, None, None, None, None, str(e)[:300])
            w = conn()
            with w:
                w.execute("UPDATE run_ratings SET status=?, score=?, verdict=?, quality=?, look=?, summary=?, differences=?, cost_usd=?, "
                          "error=?, rated_at=? WHERE base_id=? AND other_id=?", (*values, runner.now(), base_id, other_id))
            judging.discard((base_id, other_id))

        threading.Thread(target=work, daemon=True).start()
        return rating_of(c, base_id, other_id)

    def rate_pending(c) -> None:
        """Reruns imported with an answer, to rate against their original, and not rated yet."""
        for x in c.execute(
            "SELECT x.prompt_id base, x.judge_model, (SELECT id FROM prompts WHERE session_id = x.session_id ORDER BY started_at LIMIT 1) other "
            "FROM replays x WHERE COALESCE(x.auto_rate, 1) = 1").fetchall():
            current = rating_of(c, x["base"], x["other"]) if x["other"] else None
            if x["other"] and (not current or (current["status"] == "rating" and (x["base"], x["other"]) not in judging)):
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

    def read_antigravity_hooks() -> dict:
        if ANTIGRAVITY_HOOKS.exists():
            return json.loads(ANTIGRAVITY_HOOKS.read_text() or "{}")
        return {}

    def write_antigravity_hooks(data: dict) -> None:
        if ANTIGRAVITY_HOOKS.exists():
            shutil.copy(ANTIGRAVITY_HOOKS, ANTIGRAVITY_HOOKS.with_suffix(".json.bak"))
        ANTIGRAVITY_HOOKS.parent.mkdir(parents=True, exist_ok=True)
        ANTIGRAVITY_HOOKS.write_text(json.dumps(data, indent=2) + "\n")

    def is_ours(hook) -> bool:
        return any(m in json.dumps(hook) for m in (HOOK_MARKER, *LEGACY_HOOK_MARKERS))

    def hook_installed(cs: dict) -> bool:
        return any(is_ours(h) for h in cs.get("hooks", {}).get("Stop", []))

    def antigravity_hook_installed(gh: dict) -> bool:
        return HOOK_MARKER in gh or any(is_ours(h) for h in gh.values())

    def antigravity_hook_command() -> str:
        # an Antigravity Stop hook answers with JSON on stdout: "{}" lets the agent stop
        return f"curl -s -m 2 -X POST http://127.0.0.1:{port}/api/ingest >/dev/null 2>&1; echo '{{}}' # {HOOK_MARKER}"

    def antigravity_installed(settings: dict) -> bool:
        return Path(settings.get("antigravity_dir") or "").expanduser().is_dir() if settings.get("antigravity_dir") else False

    @app.get("/api/setup/status")
    def setup_status():
        c = conn()
        settings = db.get_settings(c)
        projects = Path(settings["claude_projects_dir"]).expanduser()
        ag_dir = Path(settings.get("antigravity_dir") or Path.home() / ".gemini/antigravity-cli").expanduser()
        try:
            cs = read_claude_settings()
        except json.JSONDecodeError:
            cs = {}
        try:
            gh = read_antigravity_hooks()
        except json.JSONDecodeError:
            gh = {}
        c_hook = hook_installed(cs)
        a_hook = antigravity_hook_installed(gh)

        ag_exists = ag_dir.is_dir()
        ag_transcripts = len(list((ag_dir / "brain").glob("*/.system_generated/logs/transcript.jsonl"))) if (ag_dir / "brain").is_dir() else 0
        claude_transcripts = len(list(projects.glob("*/*.jsonl"))) if projects.is_dir() else 0

        has_claude = projects.is_dir()
        has_ag = ag_exists
        if has_claude and has_ag:
            agent = "Claude Code & Antigravity"
        elif has_ag:
            agent = "Antigravity"
        else:
            agent = "Claude Code"

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
            "projects_dir_exists": projects.is_dir() or ag_exists,
            "transcript_count": claude_transcripts + ag_transcripts,
            # the connected agent: the page names it here only, the rest of the UI stays agent-neutral
            "agent": agent,
            "claude_settings_path": str(CLAUDE_SETTINGS),
            "antigravity_hooks_path": str(ANTIGRAVITY_HOOKS),
            "antigravity_dir": str(ag_dir),
            "antigravity_dir_exists": ag_exists,
            "antigravity_transcript_count": ag_transcripts,
            "antigravity_hook_installed": a_hook,
            "cleanup_period_days": cs.get("cleanupPeriodDays"),
            # installed for each agent there is
            "hook_installed": c_hook and (a_hook or not ag_exists),
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
        # Claude Code hook
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

        # Antigravity's hooks.json: one named hook per tool, ours named after the marker
        gh = {k: h for k, h in read_antigravity_hooks().items() if k != HOOK_MARKER and not is_ours(h)}
        if install and antigravity_installed(db.get_settings(conn())):
            gh[HOOK_MARKER] = {"Stop": [{"type": "command", "command": antigravity_hook_command(), "timeout": 10}]}
        if antigravity_installed(db.get_settings(conn())) or ANTIGRAVITY_HOOKS.exists():
            write_antigravity_hooks(gh)

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
            "? = (SELECT id FROM prompts WHERE session_id = x.session_id ORDER BY started_at LIMIT 1) first "
            "FROM replays x LEFT JOIN pv p ON p.id = x.prompt_id WHERE x.session_id = ?",
            (prompt_id, prompt["session_id"]),
        ).fetchone()
        if of:
            o = _row(of)
            o.update(first=bool(o["first"]), denials=json.loads(o["denials"] or "null"), skills=json.loads(o["skills"] or "[]"),
                     rating=rating_of(c, o["id"], prompt_id) if o["id"] and o["first"] else None)
            prompt["replay_of"] = o
        else:
            prompt["replay_of"] = None
        # what ran it, to run it again: harness, effort, model and the exact skill versions
        prompt["rerun"] = {
            "harness": r["harness"] or "", "harness_version": r["harness_version"] or "", "effort": r["effort"] or "",
            "requested_model": r["requested_model"] or "", "cwd": r["cwd"] or "",
            "skills": [
                dict(x) for x in c.execute(
                    "SELECT r.skill, r.hash, r.args, v.base_dir, v.origin, v.git_repo, v.git_path, v.git_commit, v.git_state, "
                    "v.plugin, v.plugin_version, v.bundled_version FROM prompt_skill_runs r JOIN skill_versions v ON v.hash = r.hash "
                    "WHERE r.prompt_id = ? ORDER BY r.skill", (prompt_id,))
            ],
        }

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
        tag: str, keywords: list[str], match: str = "all", scope: str = "prompt", skill: str = "",
    ) -> list[dict]:
        """Rule tagging, past and future, with the human tag `tag` every prompt (or every prompt of a session)
        containing the keywords, or where the agent used `skill`: one rule, one per keyword with "any", none
        without keywords or skill."""
        keywords = sorted({k.strip() for k in keywords if k.strip()}, key=str.lower)
        skill = skill.strip().removeprefix("skill:")
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
        if scope == "session":  # prompt rules keep their original shape
            rule["scope"] = "session"
        return split_any(rule)

    def add_rules(c, tag: str, rules: list[dict]) -> None:
        settings = db.get_settings(c)
        new = [r for r in rules if r not in settings["tag_rules"]]
        _record_creation(c, tag)
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
            c.execute(
                "INSERT INTO tags(prompt_id, tag, auto) VALUES (?,?,0) ON CONFLICT DO UPDATE SET auto=0",
                (prompt_id, tag),
            )
        add_rule(c, tag, keywords, match, scope)
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
            for part in make_rules(tag, r.get("keywords") or [], r.get("match", "all"), r.get("scope", "prompt"), r.get("skill") or "")
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
        return [{**r, "index": i} for i, r in enumerate(db.get_settings(c)["tag_rules"]) if r.get("tag") == tag]

    @app.post("/api/tags/{tag:path}/rules")
    def add_tag_rule(
        tag: str, keywords: list[str] = Body([], embed=True), match: str = Body("all", embed=True),
        scope: str = Body("prompt", embed=True), skill: str = Body("", embed=True),
    ):
        c = conn()
        add_rules(c, tag, make_rules(tag, keywords, match, scope, skill))
        return tag_rules(tag)

    @app.put("/api/tags/{tag:path}/rules/{index}")
    def update_tag_rule(
        tag: str, index: int, keywords: list[str] = Body([], embed=True), match: str = Body("all", embed=True),
        scope: str = Body("prompt", embed=True), skill: str = Body("", embed=True),
    ):
        c = conn()
        rules = db.get_settings(c)["tag_rules"]
        if not 0 <= index < len(rules) or rules[index].get("tag") != tag:
            raise HTTPException(404)
        made = make_rules(tag, keywords, match, scope, skill)
        if not made:
            raise HTTPException(400, "a rule needs at least one keyword, or a skill")
        others = rules[:index] + rules[index + 1:]
        # "any" of several keywords becomes one rule each, in its place, skipping rules the tag already has
        rules[index:index + 1] = [r for r in made if r not in others]
        db.save_settings(c, {"tag_rules": rules})
        apply_rules(c, rules)
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
            c.executemany(
                "INSERT INTO tags(prompt_id, tag, auto) VALUES (?,?,0) ON CONFLICT DO UPDATE SET auto=0",
                [(i, tag) for i in prompt_ids if i in known],
            )
        return {"tag": tag, "tagged": len(known)}

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
        if kept != rules:
            db.save_settings(c, {"tag_rules": kept})
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
        mode = str(body.get("mode") or "background")
        if mode not in ("background", "terminal"):
            raise HTTPException(400, "mode must be background or terminal")
        permission = str(body.get("permission") or "ask")
        if permission not in runner.PERMISSIONS:
            raise HTTPException(400, f"permission must be one of {list(runner.PERMISSIONS)}")
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
        r = c.execute("SELECT text, cwd, harness, harness_version, requested_model, effort FROM pv WHERE id = ?", (prompt_id,)).fetchone()
        if r is None:
            raise HTTPException(404, f"no prompt {prompt_id}")
        if r["harness"] and r["harness"] not in ("Claude Code", "Antigravity"):
            raise HTTPException(400, "only Claude Code and Antigravity prompts can be replayed")
        return r

    reruns_dir = db_path.parent / "reruns"
    adopted: dict[str, str] = {}  # the id an Antigravity rerun started with -> its conversation's

    def finish_rerun(session_id: str) -> None:
        """Record how a background rerun ended, from its log, and drop its restored skills."""
        c = conn()
        session_id = adopted.get(session_id, session_id)
        x = c.execute("SELECT log_path, status FROM replays WHERE session_id = ?", (session_id,)).fetchone()
        if x is None or x["status"] != "running":
            return
        log = Path(x["log_path"])
        out = runner.outcome(log)
        if out is None:  # no result: it crashed, or the agent could not start
            try:
                tail = log.read_text()[-600:].strip()
            except OSError:
                tail = ""
            out = {"status": "failed", "result": tail or "It stopped without a result.", "denials": [], "cost_usd": None, "blocked": ""}
        session_id = adopt_conversation(c, session_id, out.get("conversation_id") or runner.conversation_id(log))
        with c:
            c.execute("UPDATE replays SET status = ?, result = ?, denials = ?, cost_usd = ?, ended_at = ? WHERE session_id = ?",
                      (out["status"], out["result"], json.dumps({"denied": out["denials"], "blocked": out["blocked"]}),
                       out["cost_usd"], runner.now(), session_id))
        runner.remove_plugin(reruns_dir / session_id / "plugin")

    def adopt_conversation(c, session_id: str, conversation_id: str | None) -> str:
        """An Antigravity rerun takes the id of the conversation it runs in (its transcript's), and its prompts,
        if already imported, the rerun tag."""
        if not conversation_id or conversation_id == session_id:
            return session_id
        with c:
            c.execute("UPDATE replays SET session_id = ? WHERE session_id = ?", (conversation_id, session_id))
            link_replay(c, conversation_id)
        adopted[session_id] = conversation_id
        return conversation_id

    def refresh_reruns(c) -> None:
        """Background reruns whose process ended while the dashboard was not watching it; Antigravity's running
        ones linked to their conversation."""
        for x in c.execute("SELECT session_id, pid, log_path FROM replays WHERE status = 'running'").fetchall():
            if not runner.alive(x["pid"]):
                finish_rerun(x["session_id"])
            elif x["log_path"]:
                adopt_conversation(c, x["session_id"], runner.conversation_id(Path(x["log_path"])))

    def start_replay(c, prompt_id: str, opts: dict) -> dict:
        """Rerun a prompt with Claude Code or Antigravity: the same folder and prompt, the skills as they ran,
        the model, effort and release asked for; in the background (unattended) or in a terminal window."""
        r = replayable(c, prompt_id)
        # "same" model and effort: the run's own, not whatever the agent defaults to now
        opts = {**opts, "model": opts["model"] or (r["requested_model"] if replay.MODEL_RE.match(r["requested_model"] or "") else ""),
                "effort": opts["effort"] or (r["effort"] if r["effort"] in replay.EFFORTS else "")}
        version = (r["harness_version"] or "") if opts["release"] == "same" else opts["release"]
        version = version if replay.VERSION_RE.match(version) else ""
        session_id = str(uuid.uuid4())
        cwd = r["cwd"] or str(Path.home())
        # the skills as they ran, in a plugin for this session only: the user's copies stay untouched
        versions = [dict(v) for v in c.execute(
            "SELECT k.skill, v.* FROM prompt_skill_runs k JOIN skill_versions v ON v.hash = k.hash WHERE k.prompt_id = ? "
            "AND v.origin != 'bundled' ORDER BY k.skill", (prompt_id,))] if opts["restore_skills"] and r["harness"] != "Antigravity" else []
        plugin = reruns_dir / session_id / "plugin" if versions else None
        skills = runner.build_skills_plugin(plugin, versions) if plugin else []
        row = {"session_id": session_id, "prompt_id": prompt_id, "model": opts["model"], "effort": opts["effort"],
               "harness_version": version, "launched_at": runner.now(), "mode": opts["mode"], "skills": json.dumps(skills),
               "permission": opts["permission"], "budget_usd": opts["budget"], "auto_rate": int(opts.get("rate", True)),
               "judge_model": opts.get("judge_model", "")}

        if r["harness"] == "Antigravity":
            # Antigravity picks its own conversation id: the rerun is linked once its stream (or, in a terminal,
            # its transcript) names it; it runs the installed release, with no budget cap
            row.update(harness_version="", budget_usd=None)
            if opts["mode"] == "terminal":
                argv = replay.antigravity_argv(r["text"], opts["model"], opts["effort"])
                launched = replay.launch(cwd, argv)
                row.update(status="interactive")
                with c:
                    c.execute(f"INSERT INTO replays({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
                return {"prompt_id": prompt_id, "session_id": session_id, "mode": "terminal", "terminal": launched, "cwd": cwd,
                        "skills": [], "command": f"cd {shlex.quote(cwd)} && {shlex.join(argv)}"}
            else:
                argv = runner.antigravity_background_argv(r["text"], opts["model"], opts["effort"], opts["permission"])
                log = reruns_dir / session_id / "stream.jsonl"
                row.update(status="running", log_path=str(log))
                with c:
                    c.execute(f"INSERT INTO replays({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
                try:
                    pid = runner.start(cwd, argv, log, lambda: finish_rerun(session_id))
                except OSError as e:
                    with c:
                        c.execute("UPDATE replays SET status = 'failed', result = ?, ended_at = ? WHERE session_id = ?",
                                  (f"Antigravity could not start: {e}", runner.now(), session_id))
                    raise HTTPException(500, f"Antigravity could not start: {e}") from None
                with c:
                    c.execute("UPDATE replays SET pid = ? WHERE session_id = ?", (pid, session_id))
                return {"prompt_id": prompt_id, "session_id": session_id, "mode": "background", "terminal": None, "cwd": cwd,
                        "skills": [], "command": f"cd {shlex.quote(cwd)} && {shlex.join(argv)}"}

        if opts["mode"] == "terminal":
            note = "\n".join(f"To use the skill `{k['skill']}`, use `{k['pinned']}` instead: it is the version this task ran with."
                              for k in skills)
            argv = replay.claude_argv(r["text"], session_id, opts["model"], opts["effort"], version, str(plugin or ""), note)
            launched = replay.launch(cwd, argv)
            row.update(status="interactive")
        else:
            argv = runner.background_argv(r["text"], session_id, opts["model"], opts["effort"], version, opts["permission"],
                                          opts["budget"], plugin, skills)
            log = reruns_dir / session_id / "stream.jsonl"
            row.update(status="running", log_path=str(log))
            # recorded before it starts: a quick end must find its row
            with c:
                c.execute(f"INSERT INTO replays({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
            try:
                pid = runner.start(cwd, argv, log, lambda: finish_rerun(session_id))
            except OSError as e:
                with c:
                    c.execute("UPDATE replays SET status = 'failed', result = ?, ended_at = ? WHERE session_id = ?",
                              (f"Claude Code could not start: {e}", runner.now(), session_id))
                raise HTTPException(500, f"Claude Code could not start: {e}") from None
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
        prompt_cols = "id, text, project, started_at, total_cost_usd, total_tokens, active_ms, wall_ms, harness_version"
        def prompt(pid):
            r = c.execute(f"SELECT {prompt_cols} FROM pv WHERE id = ?", (pid,)).fetchone()
            return dict(r) if r else None
        queue = [{**dict(q), "prompt": prompt(q["prompt_id"])} for q in c.execute("SELECT * FROM replay_queue ORDER BY id")]
        launched = []
        for x in c.execute("SELECT * FROM replays ORDER BY launched_at DESC LIMIT 200"):
            done = c.execute(f"SELECT {prompt_cols} FROM pv WHERE session_id = ? ORDER BY started_at LIMIT 1", (x["session_id"],)).fetchone()
            launched.append({**rerun_row(x), "original": prompt(x["prompt_id"]), "replay": dict(done) if done else None,
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
        for item_id in body.get("ids") or []:
            q = c.execute("SELECT * FROM replay_queue WHERE id = ?", (int(item_id),)).fetchone()
            if q is None:
                continue
            release = q["release"] if q["release"] is not None else ("same" if q["same_version"] else "")
            opts = {"model": q["model"] or "", "effort": q["effort"] or "", "release": release, "mode": q["mode"] or "background",
                    "permission": q["permission"] or "ask", "budget": q["budget_usd"],
                    "restore_skills": q["restore_skills"] is None or bool(q["restore_skills"]),
                    "rate": q["auto_rate"] is None or bool(q["auto_rate"]), "judge_model": q["judge_model"] or ""}
            try:
                results.append({"id": q["id"], **start_replay(c, q["prompt_id"], opts)})
            except HTTPException as e:
                results.append({"id": q["id"], "prompt_id": q["prompt_id"], "error": e.detail})
            with c:
                c.execute("DELETE FROM replay_queue WHERE id = ?", (q["id"],))
        return {"results": results}

    @app.get("/api/agent/models")
    def agent_models():
        """The models the agents can run here (for the judge and reruns): Claude Code's aliases, the price table's
        Claude models and the ones that ran, and Antigravity's models."""
        c = conn()
        used = set()
        # anywhere: the main agent's models, and the subagents'
        for (models,) in c.execute("SELECT DISTINCT models FROM prompts UNION SELECT DISTINCT models FROM agents"):
            used |= set(json.loads(models or "{}"))
        return {"models": replay.agent_models(list(db.get_settings(c)["prices"]), sorted(used)),
                "judge_default": db.get_settings(c)["judge_model"]}

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
            if not c.execute("SELECT 1 FROM pv WHERE id = ?", (str(pid),)).fetchone():
                raise HTTPException(404, f"no prompt {pid}")
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
