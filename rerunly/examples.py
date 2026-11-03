"""Small, explicitly illustrative records for exploring the dashboard without running an agent."""

import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import judge
from .db import get_settings
from .ingest import store_session
from .pricing import Usage
from .sources.base import AgentRecord, ApiCall, PromptRecord, SessionRecord, SkillRun

SOURCE = "rerunly-examples"
PREFIX = "rerunly-example:"
TAGS = {
    SOURCE: "Illustrative examples, not real agent runs. Filter or exclude this tag to hide the entire set.",
    "example:prompt-tuning": "Compare a broad request with a focused prompt using the same Codex model and effort.",
    "example:cache": "Same task, fewer uncached input tokens on the second run.",
    "example:skills": "Compare two saved skill instruction snapshots.",
    "example:subagents": "A reviewer delegates work; inspect the subagent's persona, tokens and cost.",
    "example:quality": "A cheaper rerun can miss an important case; inspect the illustrative judge verdict.",
    "example:rerun-comparison": "One CSV validation task, three completed reruns, and cost, token and time trends.",
    "example:judges": "Three illustrative judge assessments of the same pair, with human feedback and usage metrics.",
    "example:blocked": "An unattended rerun stops because it needs a decision.",
}


def example_count(conn):
    return conn.execute("SELECT COUNT(*) FROM prompts WHERE source=?", (SOURCE,)).fetchone()[0]


def add_examples(conn):
    existing = example_count(conn)
    if existing:
        return {"added": 0, "prompts": existing}
    settings = get_settings(conn)
    # Keep all examples in the past, including the last run in the comparison series.
    today = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(hours=2)
    created = today.isoformat()
    prompts = {}

    def add(key, text, answer, tag, *, codex=False, day=0, duration=20, inputs=2000, cached=0, outputs=300, skill=None, agent=False, model=None):
        start = today - timedelta(days=day) + timedelta(minutes=len(prompts) * 10)
        end = start + timedelta(seconds=duration)
        model = model or ("gpt-6.1-sol" if codex else "claude-opus-5-5")
        pid = PREFIX + key
        sid = pid + ":session"
        p = PromptRecord(id=pid, session_id=sid, text="[Illustrative example] " + text,
            started_at=start.isoformat(), ended_at=end.isoformat(), active_ms=duration * 1000,
            cwd="", project=SOURCE, git_branch="example", harness="Codex" if codex else "Claude Code",
            effort="medium", requested_model=model,
            answer="Illustrative output — no agent was run.\n\n" + answer,
            calls=[ApiCall(model, Usage(input_tokens=inputs, output_tokens=outputs, cache_read_tokens=cached))],
            tools=Counter({"read_file": 2, "apply_patch": 1}), files={"example/calculator.py", "example/test_calculator.py"})
        if skill:
            p.skills.add("example-review")
            p.skill_runs.append(SkillRun("example-review", "/__rerunly_examples__/skills/review", skill))
        if agent:
            p.agents.append(AgentRecord(id=pid + ":reviewer", prompt_id=pid, agent_type="reviewer",
                description="Check that divide(1, None) raises TypeError and divide(1, 0) raises ZeroDivisionError.", persona="test reviewer", skill="example-review",
                started_at=(start + timedelta(seconds=2)).isoformat(), ended_at=(end - timedelta(seconds=2)).isoformat(),
                calls=[ApiCall("gpt-6-luna", Usage(input_tokens=1500, output_tokens=180))], tools=Counter({"read_file": 2})))
        session = SessionRecord(sid, SOURCE, SOURCE, "example://" + key, "example-v1", title="Example · " + key.replace("-", " "), prompts=[p])
        store_session(conn, session, settings)
        for label in (SOURCE, tag, *( ["example:subagents"] if agent else [])):
            conn.execute("INSERT OR IGNORE INTO tags(prompt_id,tag,auto) VALUES(?,?,?)", (pid, label, int(label == SOURCE)))
        prompts[key] = p
        return p

    def rate(base, other, *, score=95, quality="same"):
        """An illustrative judge verdict on `other` against `base`: any two runs can be compared."""
        conn.execute("INSERT INTO run_ratings(base_id,other_id,status,score,verdict,quality,look,summary,differences,model,rated_at,origin,duration_ms,total_tokens,cost_usd) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (base.id, other.id, "done", score, "equivalent" if score >= 85 else "major", quality, int(score < 85),
                 "Illustrative verdict: equivalent coverage of the required cases with less work." if score >= 85 else "Illustrative verdict: cheaper, but omits division-by-zero verification.",
                 json.dumps([] if score >= 85 else ["The rerun omitted the division-by-zero check."]),
                 "Illustrative rating (no LLM call)", created, "explicit", 4200, 2380, 0.012))

    def rerun(base, other=None, *, blocked="", score=95, quality="same"):
        """`other` ran `base`'s own prompt again, as reruns do: never another text."""
        assert other is None or other.text == base.text, "a rerun sends the same prompt"
        sid = other.session_id if other else PREFIX + "blocked-rerun:session"
        conn.execute("INSERT INTO replays(session_id,prompt_id,model,effort,launched_at,mode,status,result,denials,ended_at,skills,permission,auto_rate) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, base.id, ("codex:" if (other or base).harness == "Codex" else "") + (other or base).requested_model, "medium", (other or base).started_at, "background",
             "needs_you" if blocked else "done", blocked or other.answer,  # pyright: ignore[reportOptionalMemberAccess]
             json.dumps({"denied": [], "blocked": blocked}), (other or base).ended_at, "[]", "ask", 0))
        if other:
            conn.execute("INSERT OR IGNORE INTO tags(prompt_id,tag,auto) VALUES(?,?,1)", (other.id, "rerun"))
            rate(base, other, score=score, quality=quality)

    with conn:
        broad = add("broad-prompt", "Fix the calculator bug. Explore the project and improve the tests.",
            "Changed divide(a, b) to return a / b and added tests for positive and negative numbers and division by zero.",
            "example:prompt-tuning", codex=True, day=3, duration=80, inputs=16000, outputs=1800)
        focused = add("focused-prompt", "Fix divide(a, b) in example/calculator.py. Return a / b; test positive and negative numbers and division by zero. Limit changes to the calculator and its tests.",
            "Fixed divide(a, b). Tests cover positive numbers, negative numbers, and division by zero.",
            "example:prompt-tuning", codex=True, day=2, duration=18, inputs=3000, outputs=500)
        # two different prompts: compared through their tag's reference run, not as a rerun
        rate(broad, focused)
        add("cache-cold", "Summarize the calculator module and list its public functions.", "Public functions: add, subtract, multiply, divide. No files changed.", "example:cache", day=2, inputs=12000, duration=25)
        add("cache-warm", "Summarize the calculator module and list its public functions.", "Public functions: add, subtract, multiply, divide. No files changed.", "example:cache", day=1, inputs=1000, cached=11000, duration=9)
        old = add("review-skill-v1", "Review divide(a, b) using the example-review skill.", "Checked normal arithmetic and negative inputs; division by zero is covered.",
            "example:skills", codex=True, day=2, inputs=7000, duration=45, skill="# Example review v1\nRead the module and check normal arithmetic.")
        new = add("review-skill-v2", "Review divide(a, b) using the example-review skill.", "Checked normal arithmetic and negative inputs; delegated edge-case review confirmed divide(1, None) raises TypeError and divide(1, 0) raises ZeroDivisionError.",
            "example:skills", codex=True, day=1, inputs=3500, duration=24, skill="# Example review v2\nReview only changed functions. Test normal arithmetic and negative inputs. Ask a test reviewer to verify that divide(1, None) raises TypeError and divide(1, 0) raises ZeroDivisionError.", agent=True)
        rerun(old, new)
        weak = add("cheap-but-incomplete", "Review divide(a, b) using the example-review skill.", "Normal arithmetic works. Division by zero was not checked.",
            "example:quality", codex=True, day=0, inputs=700, outputs=100, duration=5)
        conn.execute("INSERT OR IGNORE INTO tags(prompt_id,tag,auto) VALUES(?,?,0)", (old.id, "example:quality"))
        rerun(old, weak, score=55, quality="worse")
        release = add("blocked-release", "Write release notes for the calculator. Ask which version to release if it is not specified.",
            "Which release version should the notes describe? No files changed.", "example:blocked", day=0, inputs=800, outputs=100, duration=5)
        rerun(release, blocked="Please choose the release version before I write the notes.")
        request = "Add CSV validation to example/import_sales.py. Reject missing headers, invalid amounts and duplicate invoice IDs; test all three cases and valid rows."
        baseline = add("csv-original", request,
            "Implemented CSV validation with line-numbered errors for missing headers, invalid amounts and duplicate invoice IDs. All 12 tests pass, including valid rows.",
            "example:rerun-comparison", codex=True, day=4, duration=110, inputs=24000, outputs=2200)
        variants = [
            ("csv-rerun-balanced", "gpt-6.1-sol", True, 3, 68, 13000, 1400, 97),
            ("csv-rerun-compact", "gpt-6-luna", True, 2, 32, 6500, 900, 94),
            ("csv-rerun-alternative", "claude-sonnet-5-5", False, 1, 46, 8500, 1100, 92),
        ]
        for key, model, codex, day, seconds, inputs, outputs, score in variants:
            variant = add(key, request,
                "Validated headers, amounts and duplicate invoice IDs, preserving line-numbered errors. All 12 tests pass, including valid rows. Factored validation into a small helper.",
                "example:rerun-comparison", model=model, codex=codex, day=day, duration=seconds, inputs=inputs, outputs=outputs)
            rerun(baseline, variant, score=score)
            conn.execute("UPDATE run_ratings SET summary=?, differences=? WHERE base_id=? AND other_id=?",
                ("Illustrative verdict: all required validation cases remain covered.", json.dumps(["The rerun extracts validation into a helper; the reference keeps it inline."]), baseline.id, variant.id))
        compared = prompts["csv-rerun-compact"]
        for run in (baseline, *[prompts[v[0]] for v in variants]):
            conn.execute("UPDATE prompts SET files=? WHERE id=?", (json.dumps(["example/import_sales.py", "example/test_import_sales.py"]), run.id))
        for run in (baseline, compared):
            conn.execute("INSERT INTO tags(prompt_id,tag,auto) VALUES(?,?,0)", (run.id, "example:judges"))
        pair = (baseline.id, compared.id)
        rows = {r["id"]: dict(r) for r in conn.execute("SELECT id,text,answer,files FROM prompts WHERE id IN (?,?)", pair)}
        fingerprint = judge.input_hash(rows)
        # These are saved illustrative results, never live model calls.
        conn.execute("UPDATE run_ratings SET model=?,input_hash=? WHERE base_id=? AND other_id=?",
            ("Example judge · Haiku", fingerprint, *pair))
        for model, score, seconds, tokens, cost, summary, observation in [
            ("Example judge · Sonnet", 96, 8.1, 3100, 0.028, "All requested cases are covered with equivalent behavior.", "The helper preserves the reference's line-numbered errors."),
            ("Example judge · Opus", 91, 12.6, 4200, 0.065, "Coverage is equivalent; the internal organization differs.", "The extracted helper changes structure, while the listed tests cover the same requirements."),
        ]:
            conn.execute("INSERT INTO judge_rating_history(base_id,other_id,score,verdict,quality,look,summary,differences,model,rated_at,input_hash,origin,duration_ms,total_tokens,cost_usd) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*pair, score, "equivalent", "same", 0, "Illustrative verdict: " + summary, json.dumps([observation]), model, created, fingerprint, "explicit", round(seconds * 1000), tokens, cost))
        conn.execute("INSERT INTO judge_feedback(base_id,other_id,input_hash,model,judge_score,human_score,note,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (*pair, fingerprint, "Example judge · Haiku", 94, 95, "Illustrative human assessment: all required cases are covered; helper extraction is acceptable.", created))
        # Summaries, reviews and the blocked release only read files.
        for key in ("cache-cold", "cache-warm", "review-skill-v1", "review-skill-v2", "cheap-but-incomplete", "blocked-release"):
            conn.execute("UPDATE prompts SET files='[]', tools=?, tool_calls=2 WHERE id=?", (json.dumps({"read_file": 2}), prompts[key].id))
        for tag, description in TAGS.items():
            conn.execute("INSERT OR IGNORE INTO tag_meta(tag,created_at,description) VALUES(?,?,?)", (tag, created, description))
        for tag, reference in (("example:prompt-tuning", broad), ("example:cache", prompts["cache-cold"]), ("example:skills", old), ("example:quality", old), ("example:rerun-comparison", baseline), ("example:judges", baseline)):
            conn.execute("INSERT OR IGNORE INTO tag_reference(tag,prompt_id) VALUES(?,?)", (tag, reference.id))
    return {"added": len(prompts), "prompts": example_count(conn)}


def remove_examples(conn):
    ids = [row[0] for row in conn.execute("SELECT id FROM prompts WHERE source=?", (SOURCE,))]
    with conn:
        for pid in ids:
            for table in ("tags", "tag_exclusions", "tag_suppressed", "prompt_skill_runs", "agents", "replay_queue"):
                conn.execute(f"DELETE FROM {table} WHERE prompt_id=?", (pid,))
            conn.execute("DELETE FROM replays WHERE prompt_id=?", (pid,))
            conn.execute("DELETE FROM run_ratings WHERE base_id=? OR other_id=?", (pid, pid))
            conn.execute("DELETE FROM judge_rating_history WHERE base_id=? OR other_id=?", (pid, pid))
            conn.execute("DELETE FROM judge_feedback WHERE base_id=? OR other_id=?", (pid, pid))
            conn.execute("DELETE FROM tag_reference WHERE prompt_id=?", (pid,))
        conn.execute("DELETE FROM prompts WHERE source=?", (SOURCE,))
        conn.execute("DELETE FROM sessions WHERE source=?", (SOURCE,))
        conn.execute("DELETE FROM skill_versions WHERE skill='example-review' AND hash NOT IN (SELECT hash FROM prompt_skill_runs)")
        for tag in TAGS:
            conn.execute("DELETE FROM tag_meta WHERE tag=? AND NOT EXISTS(SELECT 1 FROM tags WHERE tag=?)", (tag, tag))
            conn.execute("DELETE FROM tag_priorities WHERE (tag=? OR preferred_tag=?) AND NOT EXISTS(SELECT 1 FROM tags WHERE tag=?)", (tag, tag, tag))
    return {"removed": len(ids), "prompts": 0}
