from datetime import datetime, timezone

from fastapi.testclient import TestClient
from rerunly import db
from rerunly.examples import add_examples, remove_examples, SOURCE
from rerunly.server import create_app


def test_examples_are_tagged_idempotent_and_removable(tmp_path):
    c = db.connect(tmp_path / "examples.db")
    assert add_examples(c) == {"added": 8, "prompts": 8}
    assert add_examples(c) == {"added": 0, "prompts": 8}
    assert c.execute("SELECT COUNT(*) FROM tags WHERE tag=?", (SOURCE,)).fetchone()[0] == 8
    assert c.execute("SELECT COUNT(DISTINCT harness) FROM prompts").fetchone()[0] == 2
    assert c.execute("SELECT COUNT(*) FROM skill_versions").fetchone()[0] == 2
    assert c.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM run_ratings").fetchone()[0] == 3
    assert c.execute("SELECT COUNT(*) FROM replays WHERE status='needs_you'").fetchone()[0] == 1
    c.execute("INSERT INTO tag_meta VALUES('keep-me','2026-10-26','User data')")
    c.execute("INSERT INTO tag_exclusions(prompt_id, tag) VALUES('rerunly-example:cache-warm','example:cache')")
    c.execute("INSERT INTO tag_suppressed VALUES('rerunly-example:cache-cold','example:cache',0)")
    c.execute("INSERT INTO tag_priorities VALUES('example:quality','example:skills')")
    c.commit()
    assert remove_examples(c) == {"removed": 8, "prompts": 0}
    for table in ("prompts", "sessions", "tags", "agents", "skill_versions", "prompt_skill_runs", "tag_reference", "run_ratings", "replays",
                  "tag_exclusions", "tag_suppressed", "tag_priorities"):
        assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert c.execute("SELECT tag FROM tag_meta").fetchone()[0] == "keep-me"


def test_example_app_is_apart_from_the_history(tmp_path, monkeypatch):
    from rerunly import server
    monkeypatch.setattr(server, "CLAUDE_SETTINGS", tmp_path / "claude.json")
    # examples an earlier version added to the history leave it
    add_examples(db.connect(tmp_path / "dashboard.db"))
    with TestClient(create_app(tmp_path / "dashboard.db", background=False)) as client:
        assert client.get("/api/prompts").json()["total"] == 0
        assert client.post("/api/examples").status_code in (404, 405)
        data = client.get("/examples/api/prompts", params={"tag": SOURCE}).json()
        assert data["total"] == 8
        assert client.get("/examples/").status_code == 200
        for p in data["items"]:
            detail = client.get(f"/examples/api/prompts/{p['id']}")
            assert detail.status_code == 200, detail.text
        # a rerun sends its original's own prompt: prompt tuning is a comparison within the tag, not a rerun
        assert client.get("/examples/api/prompts/rerunly-example:broad-prompt").json()["replays"] == []
        compared = client.get("/examples/api/compare", params={"a": "rerunly-example:broad-prompt", "b": "rerunly-example:focused-prompt"}).json()
        assert compared["rating"]["score"] == 95
        # Rerun details expose the seeded illustrative rating on the base prompt.
        original = client.get("/examples/api/prompts/rerunly-example:review-skill-v1").json()
        assert sorted(r["rating"]["score"] for r in original["replays"]) == [55, 95]
        assert client.post("/examples/api/prompts/rerunly-example:broad-prompt/replay", json={}).status_code == 400
        # no real judge for illustrative runs
        rate = client.post("/examples/api/ratings", json={"base": "rerunly-example:broad-prompt", "other": "rerunly-example:focused-prompt", "force": True})
        assert rate.status_code == 400
        # every rerun leads to its original, which is itself among the examples, with the settings it ran with
        ids = {p["id"] for p in data["items"]}
        reruns = [d for d in (client.get(f"/examples/api/prompts/{i}").json() for i in ids) if d["replay_of"]]
        assert {d["id"] for d in reruns} == {"rerunly-example:review-skill-v2", "rerunly-example:cheap-but-incomplete"}
        for d in reruns:
            o = d["replay_of"]
            assert o["id"] in ids and o["text"] == d["text"]
            assert (o["original_harness"], o["original_model"], o["original_effort"]) == ("Codex", "gpt-6.1-sol", "medium")
            assert [k["skill"] for k in o["original_skills"]] == ["example-review"]
        launched = client.get("/examples/api/reruns").json()["launched"]
        assert len(launched) == 3
        assert all(x["replay"] is None or x["replay"]["text"] == x["original"]["text"] for x in launched)
        assert max(p["started_at"] for p in data["items"]) < datetime.now(timezone.utc).isoformat()
        # nothing imported, no agent set up, no subscription
        assert client.post("/examples/api/ingest").status_code == 403
        assert client.post("/examples/api/setup/hook?install=true").status_code == 403
        assert client.post("/examples/v1/logs", json={}).status_code == 403
        assert not (tmp_path / "claude.json").exists()
        assert client.get("/examples/api/limits").json() == []
        # curating the examples changes the example app only
        client.put("/examples/api/settings", json={"excluded_tags": [SOURCE]})
        assert client.get("/examples/api/prompts").json()["total"] == 0
        assert client.get("/api/settings").json()["excluded_tags"] == []
    # the example app starts afresh
    with TestClient(create_app(tmp_path / "dashboard.db", background=False)) as client:
        assert client.get("/examples/api/prompts").json()["total"] == 8
