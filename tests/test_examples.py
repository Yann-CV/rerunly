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
    c.commit()
    assert remove_examples(c) == {"removed": 8, "prompts": 0}
    for table in ("prompts", "sessions", "tags", "agents", "skill_versions", "prompt_skill_runs", "tag_reference", "run_ratings", "replays"):
        assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert c.execute("SELECT tag FROM tag_meta").fetchone()[0] == "keep-me"


def test_example_dashboard_flow_and_exclusion(tmp_path, monkeypatch):
    from rerunly import server
    monkeypatch.setattr(server, "CLAUDE_SETTINGS", tmp_path / "claude.json")
    with TestClient(create_app(tmp_path / "dashboard.db", background=False)) as client:
        assert client.post("/api/examples").json()["added"] == 8
        data = client.get("/api/prompts", params={"tag": SOURCE}).json()
        assert data["total"] == 8
        assert client.get("/api/setup/status").json()["example_prompts"] == 8
        for p in data["items"]:
            detail = client.get(f"/api/prompts/{p['id']}")
            assert detail.status_code == 200, detail.text
        # Rerun details expose the seeded illustrative rating on the base prompt.
        original = client.get("/api/prompts/rerunly-example:broad-prompt").json()
        assert original["replays"][0]["rating"]["score"] == 95
        assert client.post("/api/prompts/rerunly-example:broad-prompt/replay", json={}).status_code == 400
        # no real judge for illustrative runs
        rate = client.post("/api/ratings", json={"base": "rerunly-example:broad-prompt", "other": "rerunly-example:focused-prompt", "force": True})
        assert rate.status_code == 400
        assert len(client.get("/api/reruns").json()["launched"]) == 4
        assert max(p["started_at"] for p in data["items"]) < datetime.now(timezone.utc).isoformat()
        client.put("/api/settings", json={"excluded_tags": [SOURCE]})
        assert client.get("/api/prompts").json()["total"] == 0
        assert client.get("/api/reruns").json()["launched"] == []
        assert client.delete("/api/examples").json()["removed"] == 8
