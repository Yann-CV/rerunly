import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rerunly import db
from rerunly.ingest import ingest
from rerunly.pricing import DEFAULT_PRICES, Usage, cost_usd
from rerunly.server import create_app
from rerunly import server
from rerunly.sources.claude_code import ClaudeCodeSource, extract_persona
from rerunly.tagging import rule_matches, similarity

SESSION = "11111111-1111-1111-1111-111111111111"
CWD = "/work/repo"


def user(uuid, ts, content, prompt_id, human=True, **extra):
    d = {"type": "user", "uuid": uuid, "timestamp": ts, "sessionId": SESSION, "cwd": CWD, "gitBranch": "main",
         "promptId": prompt_id, "message": {"role": "user", "content": content}, **extra}
    if human:
        d["origin"] = {"kind": "human"}
    return d


def assistant(msg_id, ts, blocks, usage, model="claude-opus-5-5"):
    return {"type": "assistant", "uuid": f"u-{msg_id}-{ts}", "timestamp": ts, "sessionId": SESSION, "cwd": CWD,
            "message": {"id": msg_id, "model": model, "content": blocks, "usage": usage}}


USAGE = {"input_tokens": 10, "output_tokens": 100, "cache_read_input_tokens": 1000,
         "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 2000}}


@pytest.fixture
def projects(tmp_path: Path) -> Path:
    proj = tmp_path / "projects" / "-work-repo"
    proj.mkdir(parents=True)
    lines = [
        user("p1", "2026-10-01T10:00:00Z", "fix the flaky test in tests/test_api.py", "prompt-1"),
        # one message streamed as two lines: usage must be counted once
        assistant("m1", "2026-10-01T10:00:02Z", [{"type": "thinking", "thinking": ""}], USAGE),
        assistant("m1", "2026-10-01T10:00:03Z", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}], USAGE),
        user("r1", "2026-10-01T10:00:05Z", [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
             "prompt-1", human=False, toolUseResult={"stdout": "ok"}),
        assistant("m2", "2026-10-01T10:00:09Z", [{"type": "text", "text": "done"}], USAGE),
        {"type": "system", "subtype": "turn_duration", "durationMs": 9000, "timestamp": "2026-10-01T10:00:09Z"},
        # local command: no origin, never reaches the model
        user("c1", "2026-10-01T10:01:00Z", "<command-name>/plugin</command-name><command-args></command-args>",
             "prompt-2", human=False),
        user("p3", "2026-10-02T10:00:00Z",
             "<command-message>review</command-message><command-name>/review</command-name><command-args>fix the flaky test in tests/test_api.py</command-args>",
             "prompt-3"),
        assistant("m3", "2026-10-02T10:00:04Z", [
            {"type": "tool_use", "id": "s0", "name": "Skill", "input": {"skill": "review-skill"}},
            {"type": "tool_use", "id": "t2", "name": "Agent", "input": {}},
        ], USAGE),
        user("p4", "2026-10-03T10:00:00Z", "write the release notes", "prompt-4"),
        assistant("m4", "2026-10-03T10:00:04Z", [{"type": "text", "text": "done"}], USAGE),
        {"type": "cost-state", "sessionId": SESSION, "totalCostUSD": 0.5},
        {"type": "ai-title", "sessionId": SESSION, "aiTitle": "Flaky test"},
    ]
    (proj / f"{SESSION}.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    sub = proj / SESSION / "subagents"
    sub.mkdir(parents=True)
    agent_lines = [
        {"type": "user", "agentId": "a1", "promptId": "prompt-3", "isSidechain": True, "timestamp": "2026-10-02T10:00:05Z",
         "message": {"role": "user", "content": "You are a senior security reviewer. Explore the tests."}},
        {**assistant("s1", "2026-10-02T10:00:30Z", [], USAGE, model="claude-haiku-5-5"), "agentId": "a1", "isSidechain": True},
    ]
    (sub / "agent-a1.jsonl").write_text("\n".join(json.dumps(l) for l in agent_lines) + "\n")
    (sub / "agent-a1.meta.json").write_text(json.dumps({"agentType": "Explore", "description": "find flaky tests", "toolUseId": "t2"}))
    return tmp_path / "projects"


def test_parse_session(projects):
    src = ClaudeCodeSource(projects)
    [(key, _)] = list(src.sessions())
    s = src.load(key)
    assert s.title == "Flaky test" and s.reported_cost_usd == 0.5
    assert [p.id for p in s.prompts] == ["prompt-1", "prompt-3", "prompt-4"]  # /plugin dropped
    p1, p3, _ = s.prompts
    assert len(p1.calls) == 2  # m1 deduplicated
    assert p1.active_ms == 9000 and p1.tools["Bash"] == 1
    assert p3.text == "/review fix the flaky test in tests/test_api.py" and p3.command == "review"
    [agent] = p3.agents
    assert agent.agent_type == "Explore" and agent.calls[0].model == "claude-haiku-5-5"
    assert agent.persona == "senior security reviewer" and agent.skill == "review-skill"


@pytest.mark.parametrize("brief, persona", [
    ("<persona>Grumpy QA</persona> test it", "grumpy qa"),
    ("Persona: **Data engineer**\nDo the thing", "data engineer"),
    ("Act as an expert in Python, then review", "expert in python"),
    ("Review the diff", None),
])
def test_extract_persona(brief, persona):
    assert extract_persona(brief) == persona


def test_cost():
    u = Usage(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=1_000_000,
              cache_write_5m_tokens=1_000_000, cache_write_1h_tokens=1_000_000)
    assert cost_usd("claude-opus-5-5", u, DEFAULT_PRICES) == pytest.approx(4 + 20 + 0.2 + 5 + 8)
    assert cost_usd("claude-opus-5-20260101", Usage(input_tokens=1_000_000), DEFAULT_PRICES) == pytest.approx(5)
    assert cost_usd("gpt-x", u, DEFAULT_PRICES) is None


def test_similarity():
    a = "fix the flaky test in tests/test_api.py"
    assert similarity(a, set(), a + " please", set()) == 1.0
    assert similarity(a, set(), "write the release notes", set()) < 0.2
    # same slash command with some overlap
    assert similarity("/review fix api tests", {"cmd:review"}, "/review fix ui tests", {"cmd:review"}) >= 0.6


def test_ingest_and_api(projects, tmp_path):
    conn = db.connect(tmp_path / "d.db")
    db.save_settings(conn, {"claude_projects_dir": str(projects)})
    assert ingest(conn) == {"scanned": 1, "updated": 1}
    assert ingest(conn) == {"scanned": 1, "updated": 0}

    client = TestClient(create_app(tmp_path / "d.db", background=False))
    items = client.get("/api/prompts").json()["items"]
    assert [i["id"] for i in items] == ["prompt-4", "prompt-3", "prompt-1"]  # newest first
    kinds = {t["tag"]: t["kind"] for t in items[1]["tags"]}
    assert kinds["project:repo"] == kinds["cmd:review"] == kinds["agent:Explore"] == "ai"

    detail = client.get("/api/prompts/prompt-3").json()
    assert detail["agent_count"] == 1 and detail["agents"][0]["agent_type"] == "Explore"
    assert detail["total_cost_usd"] == pytest.approx(detail["cost_usd"] + detail["agent_cost_usd"])
    assert [s["id"] for s in detail["similar"]] == ["prompt-1"]

    # human tags survive re-ingestion, AI tags are recomputed and cannot be removed
    for pid in ("prompt-1", "prompt-3", "prompt-4"):
        client.post(f"/api/prompts/{pid}/tags", json={"tag": "task:flaky"})
    ingest(conn, force=True)
    tags = {t["tag"]: t["kind"] for t in client.get("/api/prompts/prompt-1").json()["tags"]}
    assert tags["task:flaky"] == "human" and tags["project:repo"] == "ai"
    assert client.delete("/api/prompts/prompt-1/tags/project:repo").status_code == 400
    kinds = {t["tag"]: t["kind"] for t in client.get("/api/tags").json()}
    assert kinds["task:flaky"] == "human" and kinds["project:repo"] == "ai"

    # compare with all prompts sharing the tag, or only the similar ones
    def compared(**params):
        return [s["id"] for s in client.get("/api/prompts/prompt-3", params=params).json()["similar"]]

    assert compared(compare="tag", tag="task:flaky") == ["prompt-4", "prompt-1"]
    assert compared(compare="similar_tag", tag="task:flaky") == ["prompt-1"]
    client.delete("/api/prompts/prompt-1/tags/task:flaky")
    assert compared(compare="similar_tag", tag="task:flaky") == []
    assert client.get("/api/prompts/prompt-3", params={"compare": "tag"}).status_code == 400

    # rule tags are human tags, recomputed on ingestion
    db.save_settings(conn, {"tag_rules": [{"pattern": "release", "tag": "task:release"}]})
    ingest(conn, force=True)
    [rule_tag] = [t for t in client.get("/api/prompts/prompt-4").json()["tags"] if t["tag"] == "task:release"]
    assert rule_tag["kind"] == "human" and rule_tag["from_rule"]


def test_rule_matches():
    rule = {"tag": "t", "keywords": ["Review", "branch"], "match": "all"}
    assert rule_matches(rule, "review the current branch")
    assert not rule_matches(rule, "review the diff")
    assert rule_matches({**rule, "match": "any"}, "review the diff")
    assert rule_matches({"tag": "t", "pattern": "rev.*diff"}, "review the diff")


def otlp(prompt_id: str, request_id: str, cost: float, agent: str | None = None) -> dict:
    attrs = {"event.name": "api_request", "prompt.id": prompt_id, "session.id": SESSION, "request_id": request_id,
             "model": "claude-opus-5-5", "cost_usd": cost, "duration_ms": 1500, "input_tokens": 10,
             "event.timestamp": "2026-10-01T10:00:02Z", "query_source": "repl_main_thread"}
    if agent:
        attrs["agent.name"] = agent

    def value(v):
        if isinstance(v, float):
            return {"doubleValue": v}
        if isinstance(v, int):
            return {"intValue": str(v)}
        return {"stringValue": v}

    return {"resourceLogs": [{"resource": {"attributes": []}, "scopeLogs": [{"logRecords": [
        {"body": {"stringValue": "claude_code.api_request"},
         "attributes": [{"key": k, "value": value(v)} for k, v in attrs.items()]}]}]}]}


@pytest.fixture
def client(projects, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CLAUDE_SETTINGS", tmp_path / "claude-settings.json")
    conn = db.connect(tmp_path / "d.db")
    db.save_settings(conn, {"claude_projects_dir": str(projects)})
    ingest(conn)
    return TestClient(create_app(tmp_path / "d.db", background=False))


def test_keyword_tags(client):
    assert client.get("/api/rules/preview", params={"keywords": "flaky,test"}).json() == {"matches": 2}
    client.post("/api/prompts/prompt-1/tags", json={"tag": "task:flaky", "keywords": ["flaky", "test"], "match": "all"})
    tagged = {p["id"] for p in client.get("/api/tags/task:flaky/prompts").json()}
    assert tagged == {"prompt-1", "prompt-3"}  # prompt-3 got it from the keywords
    [tag] = [t for t in client.get("/api/prompts/prompt-3").json()["tags"] if t["tag"] == "task:flaky"]
    assert tag["kind"] == "human" and tag["from_rule"]
    assert client.delete("/api/prompts/prompt-3/tags/task:flaky").status_code == 400  # managed by the rule

    created = {t["tag"]: t["created_at"] for t in client.get("/api/tags").json()}
    assert created["task:flaky"] > "2026-10-03"  # human tag: when it was created, not its first prompt
    assert created["project:repo"] == "2026-10-01T10:00:00Z"  # AI tag: its first prompt

    [rule] = client.get("/api/tags/task:flaky/rules").json()
    assert rule["keywords"] == ["flaky", "test"]
    client.delete(f"/api/tags/task:flaky/rules/{rule['index']}")
    # the hand-added tag stays, the keyword-assigned one goes
    assert {p["id"] for p in client.get("/api/tags/task:flaky/prompts").json()} == {"prompt-1"}


def test_create_tag(client):
    r = client.post("/api/tags", json={"tag": "task:release", "description": "Ship a version", "keywords": ["release"]})
    assert r.json() == {"tag": "task:release", "prompts": 1}
    assert client.post("/api/tags", json={"tag": "task:release"}).status_code == 409
    # no keywords: listed with no prompt yet
    client.post("/api/tags", json={"tag": "task:later", "description": "Not started"})
    tags = {t["tag"]: t for t in client.get("/api/tags").json()}
    assert tags["task:release"]["description"] == "Ship a version" and tags["task:release"]["prompts"] == 1
    assert tags["task:later"]["prompts"] == 0 and tags["task:later"]["kind"] == "human"
    client.put("/api/tags/task:later/description", json={"description": "Soon"})
    assert {t["tag"]: t for t in client.get("/api/tags").json()}["task:later"]["description"] == "Soon"


def test_session_rule(client):
    # "release" is only in the last prompt, "Flaky" only in the title: a session rule sees both
    assert client.get("/api/rules/preview", params={"keywords": "flaky,release", "scope": "session"}).json() == {"matches": 1}
    assert client.get("/api/rules/preview", params={"keywords": "flaky,release"}).json() == {"matches": 0}
    client.post("/api/tags", json={"tag": "task:session", "keywords": ["release notes"], "scope": "session"})
    assert {p["id"] for p in client.get("/api/tags/task:session/prompts").json()} == {"prompt-1", "prompt-3", "prompt-4"}
    [rule] = client.get("/api/tags/task:session/rules").json()
    assert rule["scope"] == "session"
    assert client.post("/api/tags", json={"tag": "x", "keywords": ["a"], "scope": "nope"}).status_code == 400
    assert "x" not in {t["tag"] for t in client.get("/api/tags").json()}


def test_official_cost(client):
    estimate = client.get("/api/prompts/prompt-1").json()["total_cost_usd"]
    # prompt-1 made 2 API calls: one telemetry event does not cover it, the estimate stays
    client.post("/v1/logs", json=otlp("prompt-1", "r1", 0.05))
    p = client.get("/api/prompts/prompt-1").json()
    assert p["cost_source"] == "prices" and p["total_cost_usd"] == pytest.approx(estimate)
    client.post("/v1/logs", json=otlp("prompt-1", "r2", 0.07))
    p = client.get("/api/prompts/prompt-1").json()
    assert p["cost_source"] == "claude-code" and p["total_cost_usd"] == pytest.approx(0.12)
    assert p["estimated_cost_usd"] == pytest.approx(estimate) and p["api_ms"] == 3000
    assert p["telemetry"][0]["requests"] == 2

    client.put("/api/settings", json={"cost_source": "prices"})
    assert client.get("/api/prompts/prompt-1").json()["total_cost_usd"] == pytest.approx(estimate)

    status = client.get("/api/setup/status").json()["telemetry"]
    assert status["events"] == 2 and status["matched_events"] == 2 and not status["configured"]
    assert client.post("/api/setup/telemetry", json={"enable": True}).json()["telemetry"]["configured"]
    env = json.loads(server.CLAUDE_SETTINGS.read_text())["env"]
    assert env["OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"].endswith("/v1/logs")
    client.post("/api/setup/telemetry", json={"enable": False})
    assert "env" not in json.loads(server.CLAUDE_SETTINGS.read_text())


def test_session(client):
    [s] = client.get("/api/sessions").json()["items"]
    assert s["prompts"] == 3 and s["reported_cost_usd"] == 0.5 and s["title"] == "Flaky test"
    detail = client.get(f"/api/sessions/{SESSION}").json()
    assert [p["id"] for p in detail["prompts"]] == ["prompt-1", "prompt-3", "prompt-4"]
    main, explore = detail["agents"]
    assert main["agent"] == "main" and main["runs"] == 3
    assert explore["agent"] == "Explore" and explore["persona"] == "senior security reviewer"
    assert explore["skills"] == "review-skill" and explore["models"] == {"claude-haiku-5-5": 1}
    tags = {t["tag"] for t in client.get("/api/prompts/prompt-3").json()["tags"]}
    assert "persona:senior security reviewer" in tags


def test_session_filters(client):
    data = client.get("/api/sessions").json()
    assert data["summary"]["count"] == 1 and data["summary"]["prompts"] == 3
    assert data["items"][0]["first_prompt"].startswith("fix the flaky test")
    assert data["highlights"]["cost"]["id"] == SESSION
    assert client.get("/api/sessions", params={"q": "release notes"}).json()["total"] == 1
    assert client.get("/api/sessions", params={"q": "Flaky"}).json()["total"] == 1  # the title
    assert client.get("/api/sessions", params={"q": "nothing like it"}).json()["total"] == 0
    assert client.get("/api/sessions", params={"since": "2026-10-02T00:00:00Z"}).json()["total"] == 0
    assert client.get("/api/sessions", params={"sort": "nope"}).status_code == 400


def test_prompt_filters(client):
    data = client.get("/api/prompts", params={"since": "2026-10-02T00:00:00Z"}).json()
    assert [p["id"] for p in data["items"]] == ["prompt-4", "prompt-3"]
    assert data["summary"]["count"] == 2 and data["summary"]["cost_usd"] == pytest.approx(
        sum(p["total_cost_usd"] for p in data["items"]))
    by_cost = client.get("/api/prompts", params={"sort": "cost"}).json()["items"]
    costs = [p["total_cost_usd"] for p in by_cost]
    assert costs == sorted(costs, reverse=True) and len(costs) == 3
    assert client.get("/api/prompts", params={"sort": "nope"}).status_code == 400


def test_overview(client):
    client.post("/api/prompts/prompt-4/tags", json={"tag": "task:release"})
    o = client.get("/api/overview").json()
    prompts = client.get("/api/prompts", params={"sort": "cost"}).json()
    assert o["totals"]["prompts"] == 3 and o["totals"]["sessions"] == 1
    assert o["totals"]["cost_usd"] == pytest.approx(prompts["summary"]["cost_usd"])
    assert o["priciest"]["id"] == prompts["items"][0]["id"] and o["priciest"]["tags"]
    assert o["repeated"] is None and o["priciest_repeated"] is None  # no prompt text was typed twice
    kinds = {t["tag"]: t["kind"] for t in o["tags"]}
    assert kinds["task:release"] == "human" and kinds["project:repo"] == "ai"
    later = client.get("/api/overview", params={"since": "2026-10-03T00:00:00Z"}).json()
    assert later["totals"]["prompts"] == 1 and later["priciest"]["id"] == "prompt-4"
    assert {t["tag"] for t in later["tags"]} >= {"task:release"}
    tagged = client.get("/api/overview", params={"tag": "task:release", "q": "release"}).json()
    assert tagged["totals"]["prompts"] == 1 and tagged["priciest"]["id"] == "prompt-4"
    assert client.get("/api/overview", params={"project": "nope"}).json()["priciest"] is None


def test_overview_repeated_prompt(projects, tmp_path, monkeypatch):
    path = next(projects.glob("*/*.jsonl"))
    lines = path.read_text().splitlines()
    lines.append(json.dumps(user("p5", "2026-10-04T10:00:00Z", "Write the release notes ", "prompt-5")))
    lines.append(json.dumps(assistant("m5", "2026-10-04T10:00:04Z", [{"type": "text", "text": "done"}], USAGE)))
    path.write_text("\n".join(lines) + "\n")
    conn = db.connect(tmp_path / "r.db")
    db.save_settings(conn, {"claude_projects_dir": str(projects)})
    ingest(conn)
    o = TestClient(create_app(tmp_path / "r.db", background=False)).get("/api/overview").json()
    assert o["repeated"]["runs"] == 2 and o["repeated"]["id"] == "prompt-5"
    assert o["priciest_repeated"] == o["repeated"]  # the only repeated text


def test_static_files_are_revalidated(tmp_path):
    client = TestClient(create_app(tmp_path / "db.sqlite", background=False))
    for path in ("/", "/static/app.js"):
        assert client.get(path).headers["cache-control"] == "no-cache"


def test_autostart_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    client = TestClient(create_app(tmp_path / "db.sqlite", port=8765, background=False))
    assert client.get("/api/setup/status").json()["autostart"]["enabled"] is False
    st = client.post("/api/setup/autostart", json={"enable": True}).json()
    entry = (tmp_path / "config/autostart/rerunly.desktop").read_text()
    assert st["autostart"]["enabled"] and "-m rerunly" in entry and "tray --port 8765" in entry
    assert str(tmp_path / "db.sqlite") in entry
    assert client.post("/api/setup/autostart", json={"enable": False}).json()["autostart"]["enabled"] is False


def test_hook_from_before_the_rename_is_replaced(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    old = {"type": "command", "command": "curl -s -X POST http://127.0.0.1:8765/api/ingest || true # agent-dashboard"}
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [old]}]}}))
    monkeypatch.setattr(server, "CLAUDE_SETTINGS", settings)
    client = TestClient(create_app(tmp_path / "db.sqlite", background=False))
    assert client.get("/api/setup/status").json()["hook_installed"]
    client.post("/api/setup/hook?install=true")
    stop = json.loads(settings.read_text())["hooks"]["Stop"]
    assert len(stop) == 1 and "# rerunly" in stop[0]["hooks"][0]["command"]


def test_tag_with_several_rules(client):
    rules = [
        {"keywords": ["release"]},  # one keyword alone
        {"keywords": ["flaky", "test"], "match": "all"},  # several together
        {"keywords": ["Flaky"], "scope": "session"},
        {"keywords": [" "]},  # empty: ignored
    ]
    client.post("/api/tags", json={"tag": "task:multi", "rules": rules})
    got = client.get("/api/tags/task:multi/rules").json()
    assert [(r["keywords"], r.get("scope", "prompt")) for r in got] == [
        (["release"], "prompt"), (["flaky", "test"], "prompt"), (["Flaky"], "session")]
    assert {p["id"] for p in client.get("/api/tags/task:multi/prompts").json()} == {"prompt-1", "prompt-3", "prompt-4"}
    # update the session rule into a prompt rule matching nothing
    session_rule = got[2]["index"]
    updated = client.put(f"/api/tags/task:multi/rules/{session_rule}", json={"keywords": ["nowhere"]}).json()
    assert [r["keywords"] for r in updated] == [["release"], ["flaky", "test"], ["nowhere"]]
    assert {p["id"] for p in client.get("/api/tags/task:multi/prompts").json()} == {"prompt-1", "prompt-3", "prompt-4"}
    client.put(f"/api/tags/task:multi/rules/{got[0]['index']}", json={"keywords": ["nope"]})
    assert {p["id"] for p in client.get("/api/tags/task:multi/prompts").json()} == {"prompt-1", "prompt-3"}
    assert client.put(f"/api/tags/task:multi/rules/{session_rule}", json={"keywords": []}).status_code == 400
    assert client.put("/api/tags/task:other/rules/0", json={"keywords": ["x"]}).status_code == 404
    bad = client.post("/api/tags", json={"tag": "task:bad", "rules": [{"keywords": ["a"]}, {"keywords": ["b"], "match": "x"}]})
    assert bad.status_code == 400 and "task:bad" not in {t["tag"] for t in client.get("/api/tags").json()}


def test_any_rules_are_split(client, tmp_path):
    client.post("/api/tags", json={"tag": "task:any", "rules": [{"keywords": ["release", "flaky"], "match": "any", "scope": "session"}]})
    rules = client.get("/api/tags/task:any/rules").json()
    assert [(r["keywords"], r["match"], r["scope"]) for r in rules] == [(["flaky"], "all", "session"), (["release"], "all", "session")]
    # updating one rule into "any" of two keywords: two rules in its place
    client.put(f"/api/tags/task:any/rules/{rules[0]['index']}", json={"keywords": ["notes", "test"], "match": "any"})
    assert [r["keywords"] for r in client.get("/api/tags/task:any/rules").json()] == [["notes"], ["test"], ["release"]]
    # rules saved before the split are split when the app starts, tagging the same prompts
    conn = db.connect(tmp_path / "d.db")
    db.save_settings(conn, {"tag_rules": [{"tag": "task:old", "keywords": ["release", "flaky"], "match": "any"}]})
    restarted = TestClient(create_app(tmp_path / "d.db", background=False))
    assert [r["keywords"] for r in restarted.get("/api/tags/task:old/rules").json()] == [["release"], ["flaky"]]  # saved order
    assert {p["id"] for p in restarted.get("/api/tags/task:old/prompts").json()} == {"prompt-1", "prompt-3", "prompt-4"}
    # a part the tag already has is not added twice
    rules = restarted.get("/api/tags/task:old/rules").json()
    restarted.put(f"/api/tags/task:old/rules/{rules[1]['index']}", json={"keywords": ["release", "notes"], "match": "any"})
    assert [r["keywords"] for r in restarted.get("/api/tags/task:old/rules").json()] == [["release"], ["notes"]]


def test_skill_rules(client):
    # prompt-3 is the only prompt where the agent used review-skill
    assert client.get("/api/rules/preview", params={"skill": "review-skill"}).json() == {"matches": 1}
    assert client.get("/api/rules/preview", params={"skill": "review-skill", "scope": "session"}).json() == {"matches": 1}
    client.post("/api/tags", json={"tag": "task:review", "rules": [{"skill": "skill:review-skill"}]})
    [rule] = client.get("/api/tags/task:review/rules").json()
    assert rule["skill"] == "review-skill" and "keywords" not in rule
    assert {p["id"] for p in client.get("/api/tags/task:review/prompts").json()} == {"prompt-3"}
    # session scope: every prompt of the session where it was used
    client.put(f"/api/tags/task:review/rules/{rule['index']}", json={"skill": "review-skill", "scope": "session"})
    assert {p["id"] for p in client.get("/api/tags/task:review/prompts").json()} == {"prompt-1", "prompt-3", "prompt-4"}
    # a skill and keywords on one tag: separate rules
    client.post("/api/tags/task:review/rules", json={"keywords": ["release"]})
    assert len(client.get("/api/tags/task:review/rules").json()) == 2
    assert client.post("/api/tags/task:review/rules", json={}).status_code == 200  # nothing to add
    assert client.put(f"/api/tags/task:review/rules/{rule['index']}", json={}).status_code == 400


def test_tag_selected_prompts(client):
    r = client.post("/api/tags/task:picked/prompts", json={"prompt_ids": ["prompt-1", "prompt-4", "nope"]})
    assert r.json() == {"tag": "task:picked", "tagged": 2}
    assert {p["id"] for p in client.get("/api/tags/task:picked/prompts").json()} == {"prompt-1", "prompt-4"}
    [tag] = [t for t in client.get("/api/prompts/prompt-4").json()["tags"] if t["tag"] == "task:picked"]
    assert tag["kind"] == "human" and not tag["from_rule"]  # by hand: removable from the prompt
    assert {t["tag"]: t["kind"] for t in client.get("/api/tags").json()}["task:picked"] == "human"


def test_delete_tag(client):
    client.post("/api/tags", json={"tag": "task:gone", "description": "x", "rules": [{"keywords": ["release"]}]})
    client.post("/api/tags/task:gone/prompts", json={"prompt_ids": ["prompt-1"]})
    before = client.get("/api/prompts").json()["total"]
    assert client.delete("/api/tags/task:gone").json() == {"tag": "task:gone", "prompts": 2, "rules": 1}
    # only the tag goes: every prompt it was on is still there, with its other tags
    assert client.get("/api/prompts").json()["total"] == before
    assert {t["tag"] for t in client.get("/api/prompts/prompt-1").json()["tags"]} >= {"project:repo"}
    assert "task:gone" not in {t["tag"] for t in client.get("/api/tags").json()}
    assert client.get("/api/tags/task:gone/rules").json() == []
    # the rule is gone too: a new import does not bring the tag back
    client.post("/api/ingest", params={"force": True})
    assert "task:gone" not in {t["tag"] for t in client.get("/api/tags").json()}
    # rule routes still reach their handler, AI tags stay
    client.post("/api/tags", json={"tag": "task:kept", "rules": [{"keywords": ["release"]}]})
    assert client.delete("/api/tags/task:kept/rules/0").status_code == 200
    assert client.delete("/api/tags/project:repo").status_code == 400
    assert client.delete("/api/tags/task:never").status_code == 404
