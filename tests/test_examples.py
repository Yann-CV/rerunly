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
        estimate = client.get("/examples/api/prompts/rerunly-example:broad-prompt/rerun-estimate")
        assert estimate.status_code == 200
        assert estimate.json()["estimate"]["total_tokens"] > 0
        assert estimate.json()["limits"] is None
        assert client.get("/examples/api/prompts/missing/rerun-estimate").status_code == 404
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


def test_rerun_history_and_original_include_more_than_200_attempts(tmp_path):
    path = tmp_path / "examples.db"
    with TestClient(create_app(path, background=False, examples=True)) as client:
        c = db.connect(path)
        with c:
            c.executemany(
                "INSERT INTO replays(session_id, prompt_id, launched_at, status) VALUES (?, ?, ?, ?)",
                [(f"history-test-{i}", "rerunly-example:review-skill-v1", "2020-01-01T00:00:00Z", "failed")
                 for i in range(205)],
            )
        c.close()
        history = client.get("/api/reruns").json()["launched"]
        assert len([x for x in history if x["session_id"].startswith("history-test-")]) == 205
        original = client.get("/api/prompts/rerunly-example:review-skill-v1").json()
        assert len([x for x in original["replays"] if x["session_id"].startswith("history-test-")]) == 205


def test_rerun_reference_is_persistent_scoped_and_reprices_ratings(tmp_path):
    with TestClient(create_app(tmp_path / "examples.db", background=False, examples=True)) as client:
        original = "rerunly-example:review-skill-v1"
        better = "rerunly-example:review-skill-v2"
        weak = "rerunly-example:cheap-but-incomplete"
        url = f"/api/reruns/reference/{original}"
        data = client.get(url).json()
        assert data["reference_id"] == original
        assert data["ratings"][better]["score"] == 95
        assert data["ratings"][weak]["score"] == 55
        assert client.put(url, json={"reference_id": "rerunly-example:broad-prompt"}).status_code == 400
        assert client.put(url, json={"reference_id": better}).status_code == 200
        data = client.get(url).json()
        assert data["reference_id"] == better
        comparison = client.get("/api/compare", params={"a": weak, "b": better}).json()
        choices = {item["id"]: item for item in comparison["candidates"]}
        assert choices[better]["is_reference"]
        assert not choices[original]["is_reference"]
        assert choices[better]["text"]
        # Swapping the runs keeps the whole group available in both selectors.
        swapped = client.get("/api/compare", params={"a": better, "b": weak}).json()
        assert {item["id"] for item in swapped["candidates"]} == set(choices)
        assert data["ratings"][weak] is None
        assert data["ratings"][original] is None
        assert client.put(url, json={"reference_id": original}).status_code == 200
        assert client.get(url).json()["ratings"][weak]["score"] == 55
        assert client.get("/api/reruns/reference/nonexistent").status_code == 404


def test_judge_compares_against_selected_rerun_reference(tmp_path, monkeypatch):
    import time
    from rerunly import judge

    calls = []
    def fake_rate(request, reference, other, model, workdir, **kwargs):
        calls.append((reference["answer"], other["answer"], model))
        return {"score": 80, "verdict": "minor", "quality": "worse", "look": False,
                "summary": "Missing a check.", "differences": [], "cost_usd": 0.01}
    monkeypatch.setattr(judge, "rate", fake_rate)
    path = tmp_path / "dashboard.db"
    with TestClient(create_app(path, background=False)) as client:
        c = db.connect(path)
        add_examples(c)
        with c:
            c.execute("UPDATE prompts SET source='test-data'")
        c.close()
        original = "rerunly-example:review-skill-v1"
        reference = "rerunly-example:review-skill-v2"
        other = "rerunly-example:cheap-but-incomplete"
        url = f"/api/reruns/reference/{original}"
        assert client.put(url, json={"reference_id": reference}).status_code == 200
        response = client.post("/api/ratings", json={"base": reference, "other": other, "model": "codex:gpt-6.1-sol", "force": True})
        assert response.status_code == 200
        for _ in range(50):
            rating = client.get(url).json()["ratings"][other]
            if rating and rating["status"] == "done":
                break
            time.sleep(0.05)
        assert rating["score"] == 80 and rating["quality"] == "worse"
        assert "delegated edge-case" in calls[0][0]
        assert "Division by zero was not checked" in calls[0][1]
        assert calls[0][2] == "codex:gpt-6.1-sol"


def test_human_judge_feedback_and_removal(tmp_path):
    from rerunly import judge

    path = tmp_path / "calibration.db"
    with TestClient(create_app(path, background=False)) as client:
        c = db.connect(path)
        add_examples(c)
        ids = ["rerunly-example:review-skill-v1", "rerunly-example:review-skill-v2",
               "rerunly-example:cheap-but-incomplete", "rerunly-example:broad-prompt"]
        pairs = [(ids[0], ids[1]), (ids[0], ids[2]), (ids[1], ids[2])]
        with c:
            for base, other in pairs:
                rows = {r["id"]: dict(r) for r in c.execute(
                    "SELECT id, text, answer, files FROM prompts WHERE id IN (?, ?)", (base, other))}
                c.execute("INSERT OR REPLACE INTO run_ratings(base_id,other_id,status,score,model,input_hash) VALUES (?,?,?,?,?,?)",
                          (base, other, "done", 70, "test-model", judge.input_hash(rows)))
        c.close()
        endpoint = "/api/ratings/feedback"
        assert client.put(endpoint, json={"base": ids[0], "other": ids[3], "score": 90}).status_code == 400
        assert client.put(endpoint, json={"base": ids[0], "other": ids[1], "score": 101}).status_code == 400
        for base, other in pairs:
            response = client.put(endpoint, json={"base": base, "other": other, "score": 90, "note": "More alike"})
            assert response.status_code == 200
            rating = response.json()
            assert rating["score"] == 70 and "calibrated_score" not in rating
        rating = client.get("/api/compare", params={"a": ids[0], "b": ids[1]}).json()["rating"]
        assert rating["human_feedback"] == {"human_score": 90, "note": "More alike"}
        c = db.connect(path)
        with c:
            c.execute("UPDATE prompts SET answer = answer || ' changed' WHERE id=?", (ids[2],))
        c.close()
        rating = client.get("/api/compare", params={"a": ids[0], "b": ids[1]}).json()["rating"]
        assert rating["human_feedback"] == {"human_score": 90, "note": "More alike"}
        assert client.request("DELETE", endpoint, json={"base": ids[0], "other": ids[1]}).status_code == 200
        rating = client.get("/api/compare", params={"a": ids[0], "b": ids[1]}).json()["rating"]
        assert rating["human_feedback"] is None and rating["score"] == 70
