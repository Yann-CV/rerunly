import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rerunly import db, replay, runner, judge
from rerunly.server import create_app
from rerunly.sources.codex import CodexSource
from rerunly.ingest import ingest


def rollout(root, sid="s1", records=(), parent=None):
    root.mkdir(parents=True, exist_ok=True)
    p = root / f"rollout-{sid}.jsonl"
    meta = {"id": sid, "cwd": str(root), "cli_version": "0.164.0", "git": {"branch": "main"}}
    if parent:
        meta["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
    data = [("session_meta", meta), *records]
    p.write_text("\n".join(json.dumps({"timestamp": f"2026-10-26T10:00:{i:02d}Z", "type": kind, "payload": payload}) for i, (kind, payload) in enumerate(data)) + "\n{broken")
    return p


def context(turn="t1", model="gpt-6.1-sol"):
    return ("turn_context", {"turn_id": turn, "model": model, "effort": "high",
                             "approval_policy": "never", "sandbox_policy": {"type": "workspace-write"}})


def user(text):
    return ("event_msg", {"type": "user_message", "message": text})


def tokens(n, cached=0, output=10):
    return {"input_tokens": n, "cached_input_tokens": cached, "output_tokens": output, "reasoning_output_tokens": 5, "total_tokens": n + output}


def count(u):
    return ("event_msg", {"type": "token_count", "info": {"total_token_usage": u, "last_token_usage": u}})


def test_import_usage_tools_skills_and_turns(tmp_path):
    p = rollout(tmp_path, records=[context(), user("Fix bug"),
        ("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Fix bug"}]}),
        ("response_item", {"type": "custom_tool_call", "name": "apply_patch", "call_id": "c1", "input": "*** Update File: app.py"}),
        ("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<skill><name>review</name><path>/old/SKILL.md</path>Exact body</skill>"}]}),
        count(tokens(100, 80)), count(tokens(100, 80)),
        ("event_msg", {"type": "task_complete", "last_agent_message": "Fixed"}),
        context("t2", "gpt-6-luna"), user("Next"), count(tokens(150, 90, 20))])
    session = CodexSource(tmp_path).load(str(p))
    a, b = session.prompts
    assert a.text == "Fix bug" and a.answer == "Fixed"
    assert a.requested_model == "gpt-6.1-sol" and b.requested_model == "gpt-6-luna"
    assert a.files == {"app.py"} and a.tools == {"apply_patch": 1}
    assert a.skills == {"review"} and a.skill_runs[0].body == "Exact body"
    assert a.calls[0].usage.total_tokens == 110 and a.calls[0].usage.input_tokens == 20
    assert len(a.calls) == 1 and b.calls[0].usage.total_tokens == 60
    assert a.active_ms > 0


def test_detailed_usage_deduplicates_and_links_replay(tmp_path):
    p = rollout(tmp_path, records=[context(), user('<rerunly-replay id="rerun-123"/>\nFix'),
        ("token_usage_record", {"response_id": "r1", "usage": tokens(100, 80)}),
        ("token_usage_record", {"response_id": "r1", "usage": tokens(100, 80)}), count(tokens(100, 80))])
    session = CodexSource(tmp_path).load(str(p))
    assert session.id == "rerun-123" and session.prompts[0].text == "Fix"
    assert len(session.prompts[0].calls) == 1


def test_subagent_import_and_signature(tmp_path):
    main = rollout(tmp_path, records=[context(), user("Review"),
        ("response_item", {"type": "function_call", "name": "spawn_agent", "call_id": "c"}),
        ("response_item", {"type": "function_call_output", "call_id": "c", "output": json.dumps({"agent_id": "child"})})])
    source = CodexSource(tmp_path)
    before = list(source.sessions())[0][1]
    rollout(tmp_path, "child", [context(), user("You are a security reviewer."), count(tokens(50))], parent="s1")
    assert len(list(source.sessions())) == 1 and list(source.sessions())[0][1] != before
    [agent] = source.load(str(main)).prompts[0].agents
    assert agent.persona == "security reviewer" and agent.calls[0].usage.total_tokens == 60


def test_cli_and_outcome(tmp_path):
    argv = replay.codex_argv("prompt", "id", "codex:gpt-6.1-sol", "high", "0.164.0", True, "edits")
    assert argv[:3] == ["npx", "-y", "@openai/codex@0.164.0"]
    assert "workspace-write" in argv and 'approval_policy="never"' in argv
    assert argv[argv.index("--model") + 1] == "gpt-6.1-sol"
    assert judge.judge_agent("codex:gpt-6.1-sol") == ("codex", "gpt-6.1-sol")
    log = tmp_path / "log"
    log.write_text('\n'.join(json.dumps(e) for e in [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Need access\nRERUN STATUS: blocked: access"}},
        {"type": "turn.completed", "usage": {}}]))
    assert runner.outcome(log)["status"] == "needs_you"
    assert runner.outcome(log)["cost_usd"] is None


def test_dashboard_codex_flow(tmp_path, monkeypatch):
    from rerunly import server
    monkeypatch.setattr(server, "CLAUDE_SETTINGS", tmp_path / "settings.json")
    root = tmp_path / "sessions"
    rollout(root, records=[context(), user("Fix bug"), count(tokens(100, 80)),
        ("event_msg", {"type": "task_complete", "last_agent_message": "Fixed"})])
    path = tmp_path / "dashboard.db"
    c = db.connect(path)
    db.save_settings(c, {"codex_sessions_dir": str(root), "claude_projects_dir": str(tmp_path / "none")})
    assert ingest(c) == {"scanned": 1, "updated": 1}
    assert ingest(c)["updated"] == 0
    c.close()
    monkeypatch.setattr(replay, "launch", lambda *args: None)
    monkeypatch.setattr(replay, "model_aliases", lambda: ())
    with TestClient(create_app(path)) as client:
        items = client.get("/api/prompts").json()["items"]
        assert len(items) == 1
        pid = items[0]["id"]
        assert any(m["id"] == "codex:gpt-6.1-sol" for m in client.get("/api/models").json())
        response = client.post(f"/api/prompts/{pid}/replay", json={"mode": "terminal", "same_version": True})
        assert response.status_code == 200, response.text
        assert "@openai/codex@0.164.0" in response.json()["command"]
        response = client.post(f"/api/prompts/{pid}/replay", json={"budget_usd": 1, "mode": "background"})
        assert response.status_code == 400 and "budget" in response.json()["detail"]
        response = client.post("/api/reruns/queue", json={"prompt_ids": [pid], "model": "codex:gpt-6-luna"})
        assert response.status_code == 200, response.text


def test_background_rerun_import_link_and_timeout(tmp_path, monkeypatch):
    from rerunly import server
    monkeypatch.setattr(server, "CLAUDE_SETTINGS", tmp_path / "claude.json")
    root = tmp_path / "sessions"
    rollout(root, records=[context(), user("Fix"), count(tokens(100))])
    path = tmp_path / "dashboard.db"
    c = db.connect(path)
    db.save_settings(c, {"codex_sessions_dir": str(root), "claude_projects_dir": str(tmp_path / "none")})
    ingest(c)
    c.close()
    calls = []

    def start(cwd, argv, log, on_exit, **kwargs):
        calls.append((argv, kwargs))
        return 12345

    monkeypatch.setattr(runner, "start", start)
    monkeypatch.setattr(runner, "alive", lambda pid: True)
    with TestClient(create_app(path, background=False)) as client:
        pid = client.get("/api/prompts").json()["items"][0]["id"]
        response = client.post(f"/api/prompts/{pid}/replay", json={"model": "codex:gpt-6-luna", "rate": False, "mode": "background"})
        assert response.status_code == 200, response.text
        sid = response.json()["session_id"]
        assert calls[0][1] == {"timeout": 3600}
        assert "exec" in calls[0][0] and "--json" in calls[0][0]
        rollout(root, "rerun-native", [context("new", "gpt-6-luna"), user(f'<rerunly-replay id="{sid}"/>\nFix'), count(tokens(30))])
        c = db.connect(path)
        ingest(c)
        assert c.execute("SELECT session_id FROM prompts WHERE session_id=?", (sid,)).fetchone()
        assert c.execute("SELECT COUNT(*) FROM tags WHERE tag='rerun'").fetchone()[0] == 1
        c.close()


def test_codex_judge_stream(tmp_path, monkeypatch):
    calls = []
    rating = {"score": 95, "verdict": "equivalent", "quality": "same", "summary": "Same result", "differences": []}

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        events = [{"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(rating)}}, {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 20}}]
        return type("Result", (), {"returncode": 0, "stdout": "\n".join(json.dumps(e) for e in events), "stderr": ""})()

    monkeypatch.setattr(judge.subprocess, "run", run)
    result = judge.rate("Fix", {"answer": "Fixed"}, {"answer": "Fixed"}, "codex:gpt-6-luna", tmp_path)
    assert result["score"] == 95 and result["cost_usd"] is None and result["total_tokens"] == 120
    argv, opts = calls[0]
    assert "--ephemeral" in argv and "read-only" in argv and opts["timeout"] == 600
    assert 'approval_policy="never"' in argv


def test_setup_links_agents_independently(tmp_path, monkeypatch):
    from rerunly import server
    monkeypatch.setattr(server, "CLAUDE_SETTINGS", tmp_path / "claude-settings.json")
    monkeypatch.setattr(server.shutil, "which", lambda name: "/bin/codex" if name == "codex" else None)
    root = tmp_path / "codex-sessions"
    rollout(root, records=[context(), user("Fix"), count(tokens(100))])
    claude = tmp_path / "claude-projects" / "project"
    claude.mkdir(parents=True)
    (claude / "session.jsonl").write_text("\n".join(json.dumps(event) for event in [
        {"type": "user", "uuid": "claude-prompt", "promptId": "claude-prompt", "timestamp": "2026-10-26T10:00:00Z",
         "cwd": str(tmp_path), "origin": {"kind": "human"}, "message": {"content": "Claude task"}},
        {"type": "assistant", "timestamp": "2026-10-26T10:00:01Z", "message": {"id": "m1", "model": "claude-haiku-5-5",
         "content": [{"type": "text", "text": "Done"}], "usage": {"input_tokens": 10, "output_tokens": 10}}},
    ]))
    path = tmp_path / "dashboard.db"
    with TestClient(create_app(path, background=False)) as client:
        for agent, directory in [("claude-code", claude.parent), ("codex", root)]:
            response = client.post(f"/api/setup/agents/{agent}", json={"directory": str(directory), "enabled": True})
            assert response.status_code == 200
        assert client.post("/api/ingest").json()["updated"] == 2
        status = client.get("/api/setup/status").json()
        connections = {a["id"]: a for a in status["connections"]}
        assert set(connections) == {"claude-code", "codex"}
        assert all(a["enabled"] and a["directory_exists"] and a["prompts"] == 1 for a in connections.values())
        assert connections["codex"]["installed"] and not connections["claude-code"]["installed"]
        assert connections["codex"]["updates"] == "polling"
        assert not connections["codex"]["reported_cost"]
        client.post("/api/setup/agents/codex", json={"enabled": False})
        rollout(root, "another", [context("another"), user("Another"), count(tokens(20))])
        result = client.post("/api/ingest").json()
        assert result == {"scanned": 1, "updated": 0}
        assert client.get("/api/prompts").json()["total"] == 2  # unlink preserves history
        client.post("/api/setup/agents/codex", json={"enabled": True})
        assert client.post("/api/ingest").json()["updated"] == 1
        assert client.get("/api/prompts").json()["total"] == 3
        assert not (tmp_path / "claude-settings.json").exists()  # linking never rewrites harness config
        assert client.post("/api/setup/agents/codex", json={"enabled": "false"}).status_code == 400
        assert client.post("/api/setup/agents/codex", json={"directory": " "}).status_code == 400
        assert client.post("/api/setup/agents/unknown", json={"enabled": True}).status_code == 404


def test_rate_limits_come_from_the_newest_rollout(tmp_path):
    import json, os
    from rerunly.sources.codex import CodexSource

    def rollout(name, used, mtime):
        path = tmp_path / name
        event = {"timestamp": "2026-10-27T10:00:00Z", "type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
            "plan_type": "plus", "primary": {"used_percent": used, "window_minutes": 300, "resets_at": 1793144251},
            "secondary": {"used_percent": 10.0, "window_minutes": 10080, "resets_at": 1793652602}}}}
        path.write_text("\n".join(json.dumps(e) for e in ({"type": "session_meta", "payload": {}}, event,
                                                        {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": None}})) + "\n")
        os.utime(path, (mtime, mtime))

    assert CodexSource(tmp_path).rate_limits() is None
    rollout("rollout-old.jsonl", 90.0, 1_000)
    rollout("rollout-new.jsonl", 14.0, 2_000)
    limits = CodexSource(tmp_path).rate_limits()
    assert limits["plan"] == "plus" and limits["observed_at"] == "2026-10-27T10:00:00Z"
    assert limits["windows"] == [{"used_percent": 14.0, "window_minutes": 300, "resets_at": 1793144251},
                                 {"used_percent": 10.0, "window_minutes": 10080, "resets_at": 1793652602}]


def test_judge_runs_at_the_chosen_effort(tmp_path, monkeypatch):
    calls = []
    rating = {"score": 90, "verdict": "equivalent", "quality": "same", "summary": "Same", "differences": []}

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "codex":
            events = [{"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(rating)}}, {"type": "turn.completed"}]
            return type("Result", (), {"returncode": 0, "stdout": "\n".join(json.dumps(e) for e in events), "stderr": ""})()
        return type("Result", (), {"returncode": 0, "stdout": json.dumps({"result": json.dumps(rating), "total_cost_usd": 0.01}), "stderr": ""})()

    monkeypatch.setattr(judge.subprocess, "run", run)
    judge.rate("Fix", {"answer": "A"}, {"answer": "A"}, "codex:gpt-6-luna", tmp_path, effort="high")
    judge.rate("Fix", {"answer": "A"}, {"answer": "A"}, "claude-haiku-5-5", tmp_path, effort="max")
    judge.rate("Fix", {"answer": "A"}, {"answer": "A"}, "claude-haiku-5-5", tmp_path, effort="bogus; rm -rf /")
    assert 'model_reasoning_effort="high"' in calls[0]
    assert calls[1][calls[1].index("--effort") + 1] == "max"
    assert "--effort" not in calls[2]  # an unknown level is dropped, not passed on


def test_models_list_the_efforts_each_accepts(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "gpt-6-luna", "visibility": "list", "default_reasoning_level": "medium",
         "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}, {"effort": "high"}]}]}))
    monkeypatch.setattr(replay, "model_aliases", lambda: ("opus",))
    monkeypatch.setattr(replay, "claude_efforts", lambda: ("low", "medium", "high", "xhigh", "max"))
    models = {m["id"]: m for m in replay.agent_models(["claude-haiku-5-5"], [])}
    assert models["opus"]["agent"] == "claude" and models["claude-haiku-5-5"]["efforts"] == ["low", "medium", "high", "xhigh", "max"]
    luna = models["codex:gpt-6-luna"]
    assert (luna["agent"], luna["efforts"], luna["default_effort"]) == ("codex", ["low", "medium", "high"], "medium")
    assert models["codex:gpt-6-astra"]["efforts"] == list(replay.CODEX_EFFORTS)  # not in the cache: a safe default


def test_claude_efforts_come_from_its_help(monkeypatch):
    replay.claude_efforts.cache_clear()
    help_text = "  --effort <level>    Effort level for the current session\n                      (low, medium, high, xhigh, max)\n"
    monkeypatch.setattr(replay.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": help_text})())
    assert replay.claude_efforts() == ("low", "medium", "high", "xhigh", "max")
    replay.claude_efforts.cache_clear()


@pytest.mark.parametrize('text', [
    'Review this transcript:\n<rerunly-replay id="rerun-123"/>\nFix',
    'A quoted example: <rerunly-replay id="rerun-123"/>\nFix',
])
def test_quoted_replay_marker_does_not_change_session_identity(tmp_path, text):
    path = rollout(tmp_path, records=[context(), user(text), count(tokens(10))])
    session = CodexSource(tmp_path).load(str(path))
    assert session.id == 'codex:s1'
    assert session.prompts[0].session_id == session.id
    assert session.prompts[0].text == text


def test_only_first_prompt_can_identify_replay_session(tmp_path):
    first = '<rerunly-replay id="rerun-123"/>\nFix'
    followup = '<rerunly-replay id="quoted-id"/>\nExplain this marker'
    path = rollout(tmp_path, records=[context(), user(first), count(tokens(10)),
                                     context('t2'), user(followup)])
    session = CodexSource(tmp_path).load(str(path))
    assert session.id == 'rerun-123'
    assert [p.session_id for p in session.prompts] == ['rerun-123', 'rerun-123']
    assert session.prompts[0].text == 'Fix'
    assert session.prompts[1].text == followup


@pytest.mark.parametrize('reviewer_id', ['a-reviewer', 'z-reviewer'])
def test_reimport_repairs_marker_collision_and_queues_draft(tmp_path, reviewer_id, monkeypatch):
    from rerunly.ingest import store_session
    from rerunly import server

    monkeypatch.setattr(server, 'CLAUDE_SETTINGS', tmp_path / 'claude-settings.json')
    root = tmp_path / 'sessions'
    marker = '<rerunly-replay id="rerun-123"/>\n'
    pid = 'codex:msg-reported-prompt'
    main = rollout(root, 'm-main', [context(), ('response_item', {
        'type': 'message', 'role': 'user', 'id': 'msg-reported-prompt',
        'content': [{'type': 'input_text', 'text': marker + 'Fix the issue'}]}), count(tokens(30))])
    review = rollout(root, reviewer_id, [context('review-turn'), user('Review the following:\n' + marker + 'Fix the issue'), count(tokens(10))])
    source = CodexSource(root)
    path = tmp_path / 'dashboard.db'
    c = db.connect(path)
    db.save_settings(c, {'codex_sessions_dir': str(root), 'claude_projects_dir': str(tmp_path / 'none')})
    settings = db.get_settings(c)
    # Reproduce the old parser replacing the real rerun with a transcript quoting its marker.
    original = source.load(str(main))
    incorrect = source.load(str(review))
    incorrect.id = 'rerun-123'
    for prompt in incorrect.prompts:
        prompt.session_id = incorrect.id
    with c:
        store_session(c, original, settings)
        c.execute("INSERT INTO tags(prompt_id,tag,auto) VALUES(?, 'keep-this-tag', 0)", (pid,))
        store_session(c, incorrect, settings)
    assert c.execute('SELECT 1 FROM prompts WHERE id=?', (pid,)).fetchone() is None
    assert ingest(c)['updated'] == 2
    assert c.execute('SELECT session_id FROM prompts WHERE id=?', (pid,)).fetchone()[0] == 'rerun-123'
    assert c.execute('SELECT 1 FROM tags WHERE prompt_id=? AND tag=?', (pid, 'keep-this-tag')).fetchone()
    assert c.execute('SELECT id FROM sessions WHERE path=?', (str(review),)).fetchone()[0] == 'codex:' + reviewer_id
    assert ingest(c)['updated'] == 0
    # Subsequent refreshes must keep both sessions and the prompt queued from the open page.
    with TestClient(create_app(path, background=False)) as client:
        for _ in range(2):
            response = client.post('/api/reruns/queue', json={'prompt_ids': [pid], 'permission': '', 'mode': 'terminal'})
            assert response.status_code == 200, response.text
            q = client.get('/api/reruns').json()['queue'][0]
            assert q['prompt_id'] == pid and q['permission'] == '' and q['needs_input']
            assert q['issues'][0]['field'] == 'permission'
            assert ingest(c, force=True)['updated'] == 2
            assert client.get('/api/prompts/' + pid).status_code == 200
    assert c.execute('SELECT COUNT(*) FROM replay_queue').fetchone()[0] == 1


def test_judge_token_counts_include_separate_claude_cache():
    assert judge.usage_tokens({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 7}, cached_separate=True) == 42
    assert judge.usage_tokens({}, cached_separate=True) is None
    assert judge.usage_tokens(None, cached_separate=False) is None
