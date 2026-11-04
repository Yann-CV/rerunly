from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rerunly.runner import build_skills_plugin
from rerunly.server import create_app


@pytest.fixture(autouse=True)
def test_socket_owner(monkeypatch):
    """TestClient's synthetic socket has this user's identity; real sockets still use /proc."""
    from rerunly import security
    real_peer_uid = security.peer_uid

    def peer_uid(client, server, proc_net=None):
        if proc_net is None and tuple(client) == ("127.0.0.1", 50000) and tuple(server) == ("127.0.0.1", 8765):
            return security.os.getuid()
        return real_peer_uid(client, server, proc_net)

    monkeypatch.setattr(security, "peer_uid", peer_uid)


def test_local_browser_request_boundaries(tmp_path):
    with TestClient(create_app(tmp_path / "security.db", background=False), base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/projects").status_code == 200
        response = client.get("/")
        assert "script-src 'self'" in response.headers["content-security-policy"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert client.get("/api/projects", headers={"Origin": "http://127.0.0.1:8765"}).status_code == 200
        assert client.post("/api/model-checks/cancel", headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.post("/api/model-checks/cancel", headers={"Origin": "https://127.0.0.1:8765"}).status_code == 403
        assert client.get("/api/projects", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert client.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200
        assert client.get("/api/projects", headers={"Host": "evil.example:8765", "Origin": "http://evil.example:8765"}).status_code == 403
        assert client.get("/api/projects", headers={"Host": "localhost:8765"}).status_code == 200
        assert client.post("/api/ingest", headers={"Origin": "null"}).status_code == 403


def test_skill_restoration_rejects_paths_and_collisions_before_writing(tmp_path):
    folder = tmp_path / "plugin"
    def version(name):
        return {"skill": name, "git_state": "uncommitted", "body": "test"}
    for name in ("../../outside", "/tmp/escape", "kit:..", "name\ninjected"):
        with pytest.raises(ValueError, match="unsafe name"):
            build_skills_plugin(folder, [version("safe"), version(name)])
        assert not folder.exists()
    with pytest.raises(ValueError, match="same name"):
        build_skills_plugin(folder, [version("first:review"), version("second:review")])
    assert not folder.exists()


def test_usage_redirect_does_not_forward_credentials_to_other_origins():
    from urllib.request import Request
    from rerunly.live_limits import _SameOriginRedirect

    handler = _SameOriginRedirect()
    request = Request("https://api.anthropic.com/api/oauth/usage", headers={"Authorization": "Bearer secret"})
    for target in ("https://attacker.example/usage", "http://api.anthropic.com/usage", "https://api.anthropic.com:444/usage"):
        with pytest.raises(ValueError, match="different origin"):
            handler.redirect_request(request, None, 302, "Found", {}, target)
    redirected = handler.redirect_request(request, None, 302, "Found", {}, "https://api.anthropic.com/usage")
    assert redirected.full_url == "https://api.anthropic.com/usage"


def test_dashboard_loads_chart_library_locally():
    html = (Path(__file__).parents[1] / "rerunly/static/index.html").read_text()
    assert '/static/chart-4.4.1.umd.min.js' in html
    assert 'cdnjs.cloudflare.com' not in html
    assert (Path(__file__).parents[1] / "rerunly/static/CHARTJS-LICENSE.txt").is_file()


def test_background_start_closes_log_when_launch_fails(tmp_path, monkeypatch):
    from rerunly import runner
    opened = []
    original_open = type(tmp_path).open
    def record_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        opened.append(stream)
        return stream
    monkeypatch.setattr(type(tmp_path), "open", record_open)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("missing")))
    with pytest.raises(OSError):
        runner.start(str(tmp_path), ["missing"], tmp_path / "stream.jsonl", lambda: None)
    assert opened[0].closed


def test_new_database_is_private_and_existing_permissions_are_preserved(tmp_path):
    import os
    import stat
    from rerunly import db

    path = tmp_path / "private.db"
    old_umask = os.umask(0)
    try:
        db.connect(path).close()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        path.chmod(0o640)
        db.connect(path).close()
        assert stat.S_IMODE(path.stat().st_mode) == 0o640
    finally:
        os.umask(old_umask)


# ---- who may reach the dashboard ----------------------------------------------------------------

def _proc_tables(tmp_path, *rows):
    header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    (tmp_path / "tcp").write_text(header + "".join(rows))
    (tmp_path / "tcp6").write_text(header)
    return tmp_path


def test_peer_uid_reads_the_owner_of_the_connecting_socket(tmp_path):
    from rerunly.security import peer_uid

    # 127.0.0.1:50000 -> 127.0.0.1:8765, owned by 1001; the server's own end is owned by 1000.
    proc = _proc_tables(tmp_path,
                        "   0: 0100007F:C350 0100007F:223D 01 00000000:00000000 00:00000000 00000000  1001        0 1\n",
                        "   1: 0100007F:223D 0100007F:C350 01 00000000:00000000 00:00000000 00000000  1000        0 2\n")
    assert peer_uid(("127.0.0.1", 50000), ("127.0.0.1", 8765), proc) == 1001
    assert peer_uid(("127.0.0.1", 50001), ("127.0.0.1", 8765), proc) is None


def test_peer_uid_reads_ipv6_addresses(tmp_path):
    from rerunly.security import peer_uid

    proc = _proc_tables(tmp_path)
    (proc / "tcp6").write_text("header\n   0: 00000000000000000000000001000000:C350 00000000000000000000000001000000:223D 01 "
                               "00000000:00000000 00:00000000 00000000  1001        0 1\n")
    assert peer_uid(("::1", 50000), ("::1", 8765), proc) == 1001


def test_real_loopback_connection_is_recognised_as_this_user():
    """Through the kernel's own table, not a sample: this breaks if the table's format is misread."""
    import os
    import socket
    from rerunly.security import peer_uid

    if not Path("/proc/net/tcp").exists():
        pytest.skip("no /proc/net/tcp")
    with socket.create_server(("127.0.0.1", 0)) as server, socket.create_connection(server.getsockname()) as client:
        assert peer_uid(client.getsockname(), server.getsockname()) == os.getuid()


def _scope_app(monkeypatch, tmp_path, server_host):
    """A request as uvicorn would describe it, arriving on `server_host`."""
    from rerunly import security
    app = create_app(tmp_path / "t.db", background=False)
    return security, TestClient(app, base_url=f"http://{server_host}:8765", client=("127.0.0.1", 50000))


def test_other_local_users_are_refused(tmp_path, monkeypatch):
    security, client = _scope_app(monkeypatch, tmp_path, "127.0.0.1")
    monkeypatch.setattr(security.sys, "platform", "linux")
    monkeypatch.setattr(security, "peer_uid", lambda *a: 999_999)
    assert client.get("/api/projects").status_code == 403
    assert client.post("/api/setup/agents/claude-code", json={"directory": "/tmp/attacker"}).status_code == 403
    monkeypatch.setattr(security, "peer_uid", lambda *a: None)  # closed before we could look: unknown owner
    assert client.get("/api/projects").status_code == 403
    monkeypatch.setattr(security, "peer_uid", lambda *a: security.os.getuid())
    assert client.get("/api/projects").status_code == 200


def test_operating_systems_without_the_table_keep_working(tmp_path, monkeypatch):
    security, client = _scope_app(monkeypatch, tmp_path, "127.0.0.1")
    monkeypatch.setattr(security.sys, "platform", "darwin")
    assert client.get("/api/projects").status_code == 200
    monkeypatch.setattr(security.sys, "platform", "linux")
    monkeypatch.setattr(security, "peer_uid", lambda *a: (_ for _ in ()).throw(OSError("no /proc")))
    assert client.get("/api/projects").status_code == 403


@pytest.mark.parametrize("forwarded", ["unknown", "localhost", "127.0.0.1", "192.0.2.1"])
def test_forwarded_headers_cannot_bypass_linux_socket_identity(tmp_path, monkeypatch, forwarded):
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    from rerunly import security

    monkeypatch.setattr(security.sys, "platform", "linux")
    monkeypatch.setattr(security, "peer_uid", lambda *a: security.os.getuid() + 1)
    # Even an externally supplied ASGI wrapper must not turn an invalid identity into approval.
    app = ProxyHeadersMiddleware(create_app(tmp_path / "t.db", background=False), trusted_hosts="127.0.0.1")
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/projects").status_code == 403
        headers = {"X-Forwarded-For": forwarded}
        assert client.get("/api/projects", headers=headers).status_code == 403
        assert client.put("/api/settings", json={"judge_effort": "low"}, headers=headers).status_code == 403


@pytest.mark.parametrize("client,server", [(None, ("127.0.0.1", 8765)), (("unknown", 0), ("127.0.0.1", 8765)),
                                          (("127.0.0.1", 50000), None)])
def test_unknown_linux_socket_identity_is_refused(monkeypatch, client, server):
    from rerunly import security
    monkeypatch.setattr(security.sys, "platform", "linux")
    assert not security.peer_is_this_user({"client": client, "server": server})


@pytest.mark.parametrize("reload", [False, True])
def test_serving_never_trusts_forwarded_headers(tmp_path, monkeypatch, reload):
    import uvicorn
    from rerunly import server
    calls = []
    monkeypatch.setattr(server, "create_app", lambda *a: object())
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append(kw))
    server.serve(tmp_path / "t.db", "127.0.0.1", 8765, reload=reload)
    assert calls[0]["proxy_headers"] is False


def test_tray_server_never_trusts_forwarded_headers(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from rerunly import server, tray
    monkeypatch.setattr(server, "create_app", lambda *a: object())
    monkeypatch.setattr(tray.threading, "Thread", Mock())
    assert tray._start_server(tmp_path / "t.db", "127.0.0.1", 8765).config.proxy_headers is False


@pytest.mark.require_login
def test_network_access_needs_password_or_machine_bearer(tmp_path, monkeypatch):
    import os
    import stat
    from rerunly import auth
    security, client = _scope_app(monkeypatch, tmp_path, "192.0.2.10")
    client.follow_redirects = False
    assert client.get("/").status_code == 303
    assert client.get("/login").status_code == 200
    assert client.get("/api/projects", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/projects", params={"token": "wrong"}).status_code == 401
    token = security.access_token(tmp_path / "t.db")
    assert stat.S_IMODE(os.stat(tmp_path / "machine-token").st_mode) == 0o600
    assert client.get("/api/projects", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.post("/api/ingest", params={"token": token}).status_code == 401  # the link only opens the page
    opened = client.get("/", params={"token": token})
    assert opened.status_code == 303 and opened.headers["location"] == "/login"
    assert "set-cookie" not in opened.headers
    auth.set_password(tmp_path / "t.db", "private local password")
    assert client.post("/auth/login", json={"password": "private local password"}).status_code == 200
    assert client.get("/").status_code == 200
    assert security.access_token(tmp_path / "t.db") == token


def test_only_the_dashboards_own_pages_reach_the_api(tmp_path):
    with TestClient(create_app(tmp_path / "t.db", background=False), base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/projects", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
        assert client.get("/api/projects", headers={"Sec-Fetch-Site": "none"}).status_code == 200
        assert client.get("/api/projects", headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
        assert client.post("/api/ingest", headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
        assert "form-action 'self'" in client.get("/").headers["content-security-policy"]


# ---- what a request may carry --------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    {"excluded_sessions": ["a\x00b"]}, {"excluded_projects": 5}, {"prices": "x"}, {"prices": {"m": {"input": 1}}},
    {"prices": {"m": {"input": -1, "output": 1, "cache_read": 1}}}, {"tag_rules": "x"}, {"tag_rules": [{"keywords": []}]},
    {"auto_ingest_interval": "soon"}, {"auto_ingest_interval": -1}, {"similarity_threshold": 2}, {"cost_source": "other"},
])
def test_invalid_settings_are_refused_and_pages_keep_working(tmp_path, bad):
    with TestClient(create_app(tmp_path / "t.db", background=False), base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)) as client:
        before = client.get("/api/settings").json()
        assert client.put("/api/settings", json=bad).status_code == 400
        assert client.get("/api/settings").json() == before
        assert client.get("/api/prompts").status_code == 200


def test_settings_the_dashboard_sends_are_still_accepted(tmp_path):
    with TestClient(create_app(tmp_path / "t.db", background=False), base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)) as client:
        settings = client.get("/api/settings").json()
        assert client.put("/api/settings", json=settings).status_code == 200
        assert client.put("/api/settings", json={"similarity_threshold": 0.5, "auto_ingest_interval": 0, "cost_source": "prices",
                                                 "judge_model": "codex:gpt-6-luna", "judge_effort": "high", "excluded_tags": ["it's"]}).status_code == 200
        assert client.get("/api/prompts").status_code == 200


def test_a_bad_exclusion_saved_earlier_cannot_break_every_page(tmp_path):
    from rerunly import db
    with TestClient(create_app(tmp_path / "t.db", background=False), base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)) as client:
        db.save_settings(db.connect(tmp_path / "t.db"), {"excluded_sessions": ["a\x00b", 5], "excluded_projects": 5})
        assert client.get("/api/prompts").status_code == 200


def test_oversized_bodies_are_refused_with_or_without_a_length(tmp_path):
    from rerunly.security import MAX_BODY_BYTES
    with TestClient(create_app(tmp_path / "t.db", background=False), base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)) as client:
        big = b" " * (MAX_BODY_BYTES + 1)
        headers = {"content-type": "application/json"}
        assert client.post("/v1/logs", content=big, headers=headers).status_code == 413
        assert client.post("/v1/logs", content=iter([big]), headers=headers).status_code == 413
        assert client.post("/v1/logs", json={"resourceLogs": []}).status_code == 200


@pytest.mark.parametrize("days", [0, -1, 36501])
def test_retention_outside_the_sensible_range_is_refused(tmp_path, monkeypatch, days):
    from rerunly import server
    settings = tmp_path / "claude.json"
    monkeypatch.setattr(server, "CLAUDE_SETTINGS", settings)
    with TestClient(create_app(tmp_path / "t.db", background=False), base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)) as client:
        assert client.post("/api/setup/retention", json={"days": days}).status_code == 400
    assert not settings.exists()


# ---- what reaches an agent's command line ---------------------------------------------------------

@pytest.mark.parametrize("build", ["background", "terminal"])
def test_a_prompt_cannot_become_a_claude_option(build):
    from rerunly import replay, runner
    prompt = '--settings={"hooks":{}}'
    argv = (runner.background_argv(prompt, "s", "", "", "", "ask", None, None, []) if build == "background"
            else replay.claude_argv(prompt, "s"))
    assert argv[-2:] == ["--", prompt]
    with_permissions = replay.with_options(argv, ["--permission-mode", "plan"])
    assert with_permissions[-4:] == ["--permission-mode", "plan", "--", prompt]


def test_codex_prompts_cannot_become_options_either():
    from rerunly import replay
    assert replay.codex_argv("--help", "s")[-1].startswith("<rerunly-replay")
    assert replay.with_options(["codex", "prompt"], ["-c", "x"]) == ["codex", "-c", "x", "prompt"]


def test_one_decision_on_which_agent_runs_a_model():
    from rerunly import judge, replay
    for model, codex in (("codex:gpt-6-luna", True), ("gpt-6-luna", True), ("o3-mini", True), ("o4-mini", True),
                         ("gpt-oss-20b", False), ("claude-haiku-5-5", False), ("opus", False)):
        assert replay.uses_codex(model, "Claude Code") is codex
        assert (judge.judge_agent(model)[0] == "codex") is codex


# ---- files and processes ---------------------------------------------------------------------------

def test_default_data_folder_is_closed_to_other_users_at_startup(tmp_path, monkeypatch):
    import stat
    from rerunly import db
    data = tmp_path / "rerunly"
    data.mkdir(mode=0o775)
    (data / "dashboard.db").write_text("")
    (data / "tray.log").write_text("")
    (data / "reruns").mkdir(mode=0o755)
    for path in (data, data / "dashboard.db", data / "tray.log", data / "reruns"):
        path.chmod(0o775 if path.is_dir() else 0o644)
    monkeypatch.setattr(db, "DATA_DIR", data)
    db.tighten_permissions(data / "dashboard.db")
    modes = {path.name: stat.S_IMODE(path.stat().st_mode) for path in (data, data / "dashboard.db", data / "tray.log", data / "reruns")}
    assert modes == {"rerunly": 0o700, "dashboard.db": 0o600, "tray.log": 0o600, "reruns": 0o700}


def test_first_foreground_start_creates_private_default_data_folder(tmp_path, monkeypatch):
    import os
    import stat
    from rerunly import db
    data = tmp_path / "fresh-data"
    monkeypatch.setattr(db, "DATA_DIR", data)
    previous_umask = os.umask(0)
    try:
        with TestClient(create_app(data / "dashboard.db", background=False)) as client:
            assert client.get("/api/projects").status_code == 200
        assert stat.S_IMODE(data.stat().st_mode) == 0o700
        assert stat.S_IMODE((data / "dashboard.db").stat().st_mode) == 0o600
    finally:
        os.umask(previous_umask)


def test_startup_cleans_judge_data_retained_by_older_prompt_deletions(tmp_path):
    from rerunly import db
    path = tmp_path / "t.db"
    c = db.connect(path)
    with c:
        c.execute("INSERT INTO deleted_prompts VALUES('deleted','2026-10-01')")
        for base, other in (("deleted", "kept"), ("kept", "deleted")):
            c.execute("INSERT INTO judge_rating_history(base_id,other_id,summary) VALUES(?,?,'private summary')", (base, other))
            c.execute("INSERT INTO judge_feedback(base_id,other_id,input_hash,model,judge_score,human_score,note,updated_at) "
                      "VALUES(?,?,'hash','model',90,80,'private note','2026-10-01')", (base, other))
            c.execute("INSERT INTO replay_queue(prompt_id,other_prompt_id,kind,added_at) VALUES(?,?,'judge','2026-10-01')", (base, other))
    with TestClient(create_app(path, background=False)) as client:
        assert client.get("/api/projects").status_code == 200
    for table in ("judge_rating_history", "judge_feedback", "replay_queue"):
        assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM deleted_prompts").fetchone()[0] == 1


def test_a_database_placed_elsewhere_keeps_its_permissions(tmp_path):
    import stat
    from rerunly import db
    shared = tmp_path / "shared.db"
    shared.write_text("")
    shared.chmod(0o640)
    db.tighten_permissions(shared)
    assert stat.S_IMODE(shared.stat().st_mode) == 0o640


def test_settings_writes_are_atomic_and_keep_links_and_modes(tmp_path):
    import stat
    from rerunly.processes import write_atomic
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text("old")
    real.chmod(0o640)
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    write_atomic(link, "new")
    assert link.is_symlink() and real.read_text() == "new" and stat.S_IMODE(real.stat().st_mode) == 0o640
    assert sorted(p.name for p in real.parent.iterdir()) == ["settings.json"]


def test_timed_out_agent_loses_its_children_and_never_reports_its_prompt(tmp_path):
    import subprocess
    import sys
    import time
    from rerunly import processes
    marker = tmp_path / "child.pid"
    script = ("import os, subprocess, sys, time; child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
              f" open({str(marker)!r}, 'w').write(str(child.pid)); time.sleep(60)")
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        processes.run([sys.executable, "-c", script, "SECRET PROMPT"], timeout=2)
    assert "SECRET PROMPT" not in str(raised.value)
    pid = int(marker.read_text())
    deadline = time.monotonic() + 5
    from rerunly.runner import alive
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not alive(pid)


# ---- interrupted work, and connections --------------------------------------------------------------

def test_a_judgment_interrupted_by_a_stop_can_be_judged_again(tmp_path):
    from rerunly import db
    create_app(tmp_path / "t.db", background=False)
    c = db.connect(tmp_path / "t.db")
    with c:
        c.execute("INSERT INTO run_ratings(base_id, other_id, status, input_hash) VALUES ('a', 'b', 'rating', 'h')")
    create_app(tmp_path / "t.db", background=False)
    assert tuple(c.execute("SELECT status, error FROM run_ratings").fetchone()) == ("failed", "The judgment was interrupted when Rerunly stopped.")
    c.close()


def test_requests_close_their_database_connections(tmp_path, monkeypatch):
    from rerunly import db
    opened = []
    real = db.sqlite3.connect
    monkeypatch.setattr(db.sqlite3, "connect", lambda *a, **k: opened.append(real(*a, **k)) or opened[-1])
    with TestClient(create_app(tmp_path / "t.db", background=False), base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/prompts").status_code == 200
        assert client.get("/api/tags").status_code == 200
    assert opened
    for conn in opened:
        with pytest.raises(db.sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_schema_steps_run_once_per_database_version(tmp_path):
    from rerunly import db
    first = db.connect(tmp_path / "t.db")
    assert first.execute("PRAGMA user_version").fetchone()[0] == db._schema_version()
    first.close()
    statements = []
    second = db.sqlite3.connect(tmp_path / "t.db")
    second.close()
    original = db._migrate
    try:
        db._migrate = lambda conn: statements.append("migrated")
        db.connect(tmp_path / "t.db").close()
    finally:
        db._migrate = original
    assert statements == []


def test_terminal_watcher_stops_searching_once_it_found_the_transcript(tmp_path, monkeypatch):
    from rerunly.terminal import CompletionWatcher
    (tmp_path / "ours.jsonl").write_text('{"type":"user"}\n')
    watcher = CompletionWatcher(str(tmp_path), "ours", False)
    watcher.started = 0
    assert not watcher.complete()
    monkeypatch.setattr(Path, "rglob", lambda *a: pytest.fail("searched the transcript tree again"))
    assert not watcher.complete()


# ---- the command line, and how background runs end ---------------------------------------------------

def test_cli_prints_login_instructions_without_access_tokens(tmp_path, monkeypatch, capsys):
    import sys
    from rerunly import cli, server
    monkeypatch.setattr(server, "serve", lambda *a, **k: None)
    monkeypatch.setattr(cli, "migrate_legacy_data", lambda: False)
    monkeypatch.setattr(sys, "argv", ["rerunly", "--db", str(tmp_path / "d.db"), "serve"])
    cli.main()
    assert "token=" not in capsys.readouterr().out and not (tmp_path / "machine-token").exists()
    monkeypatch.setattr(sys, "argv", ["rerunly", "--db", str(tmp_path / "d.db"), "serve", "--host", "0.0.0.0", "--port", "9000"])
    cli.main()
    out = capsys.readouterr().out
    assert ":9000/login" in out and "unencrypted" in out and "token=" not in out
    assert not (tmp_path / "machine-token").exists()


@pytest.mark.parametrize("line, status, blocked", [
    ({"type": "result", "subtype": "success", "result": "All good\nRERUN STATUS: done"}, "done", ""),
    ({"type": "result", "subtype": "success", "result": "RERUN STATUS: blocked: which release?"}, "needs_you", "which release?"),
    ({"type": "result", "subtype": "success", "result": "x", "permission_denials": [{"tool_name": "Bash", "tool_input": {}}]}, "needs_you", ""),
    ({"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "result": "Budget"}, "failed", ""),
    ({"type": "error", "message": "Rerun exceeded its time limit."}, "failed", ""),
    ({"type": "turn.failed", "error": {"message": "bad"}}, "failed", ""),
])
def test_how_a_background_run_ended(tmp_path, line, status, blocked):
    import json
    from rerunly import runner
    log = tmp_path / "stream.jsonl"
    log.write_text("not json\n" + json.dumps(line) + "\n")
    out = runner.outcome(log)
    assert out["status"] == status and out["blocked"] == blocked


def test_a_run_without_a_result_is_not_finished_yet(tmp_path):
    from rerunly import runner
    log = tmp_path / "stream.jsonl"
    log.write_text('{"type":"system"}\n')
    assert runner.outcome(log) is None
    assert runner.outcome(tmp_path / "missing.jsonl") is None


def test_timed_out_background_run_is_stopped_and_reported(tmp_path):
    import sys
    import threading
    from rerunly import runner
    finished = threading.Event()
    log = tmp_path / "stream.jsonl"
    runner.start(str(tmp_path), [sys.executable, "-c", "import time; time.sleep(60)"], log, finished.set, timeout=0.5)
    assert finished.wait(15)
    assert runner.outcome(log)["status"] == "failed" and "time limit" in log.read_text()


def _rerun_command(page):
    """Run the page's own rerunCommand under Node, on a transcript's values."""
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("needs Node.js")
    js = (Path(__file__).parents[1] / "rerunly/static/app.js").read_text()
    helpers = js[js.index("const shellQuote ="):js.index("let modelCatalog = [];")]
    script = helpers + f"\nprocess.stdout.write(rerunCommand({json.dumps(page)}));"
    return subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout


def _rerun_page(**rerun):
    return {"text": "fix it", "models": {"claude-haiku-5-5": 1},
            "rerun": {"cwd": "/w", "skills": [], "harness": "Claude Code", "requested_model": "", "harness_version": "2.1.0", "effort": "high",
                      "permission_args": [], **rerun}}


def test_ordinary_copied_command_is_unchanged():
    assert _rerun_command(_rerun_page()) == ("cd '/w' || exit 1\n"
                                             "npx @anthropic-ai/claude-code@2.1.0 --model 'claude-haiku-5-5' --effort high 'fix it'")


def test_transcript_values_cannot_inject_into_the_copied_command():
    command = _rerun_command(_rerun_page(harness_version="1; touch /tmp/x", effort="high; touch /tmp/y", skills=[
        {"skill": "s\ntouch /tmp/z", "origin": "folder", "base_dir": "/d\ntouch /tmp/q"}]))
    assert "@'1; touch /tmp/x'" in command and "--effort 'high; touch /tmp/y'" in command
    assert not any(line.startswith("touch") for line in command.splitlines())


def test_copied_command_protects_a_prompt_starting_with_a_dash():
    page = {**_rerun_page(), "text": "--settings={}"}
    assert _rerun_command(page).endswith(" -- '--settings={}'")
