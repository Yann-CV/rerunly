"""The dashboard has exactly one local account and never treats a browser token link as a login."""

import json
import os
import stat
import time

import pytest
from fastapi.testclient import TestClient

from rerunly import auth, db, security, server

pytestmark = pytest.mark.require_login
PASSWORD = "a private local passphrase"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(security, "peer_uid", lambda *args: os.getuid())
    with TestClient(server.create_app(tmp_path / "d.db", background=False), base_url="http://127.0.0.1:8765",
                    client=("127.0.0.1", 50000), follow_redirects=False) as client:
        yield client


def setup(client):
    response = client.post("/auth/setup", json={"password": PASSWORD, "confirm_password": PASSWORD})
    assert response.status_code == 200, response.text
    return response


def test_password_required_even_for_this_local_user_and_mounted_examples(client, tmp_path):
    assert client.get("/").status_code == 303
    assert client.get("/").headers["location"] == "/login"
    for path in ("/api/projects", "/api/settings", "/api/setup/status", "/examples/api/prompts"):
        response = client.get(path)
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"
    for path in ("/api/ingest", "/v1/logs", "/api/prompts/some-prompt/replay"):
        assert client.post(path, json={}).status_code == 401
    assert client.get("/examples/").status_code == 303
    assert client.get("/login").status_code == 200
    assert client.get("/static/login.js").status_code == 200
    assert client.get("/auth/status").json() == {"configured": False, "signed_in": False, "can_setup": True}
    token = security.access_token(tmp_path / "d.db")
    assert client.get("/api/projects", params={"token": token}).status_code == 401
    client.cookies.set("rerunly_token", token)
    assert client.get("/api/projects").status_code == 401


def test_first_local_setup_creates_only_one_hashed_account_and_signs_in(client, tmp_path):
    response = setup(client)
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert "max-age" not in cookie and "expires=" not in cookie
    assert "; secure" not in cookie
    assert client.get("/api/projects").status_code == 200
    assert client.get("/examples/api/prompts").json()["total"] == 12
    assert client.get("/auth/status").json()["signed_in"] is True
    c = db.connect(tmp_path / "d.db")
    account = c.execute("SELECT * FROM login_account").fetchone()
    assert account["password_hash"] != PASSWORD and len(account["salt"]) == 32
    stored = c.execute("SELECT token_hash FROM login_sessions").fetchone()[0]
    assert stored != client.cookies.get(auth.SESSION_COOKIE)
    with pytest.raises(db.sqlite3.IntegrityError):
        c.execute("INSERT INTO login_account(id,salt,password_hash) VALUES(2,'salt','hash')")
    c.rollback()
    assert client.post("/auth/setup", json={"password": PASSWORD, "confirm_password": PASSWORD}).status_code == 409
    assert c.execute("SELECT COUNT(*) FROM login_account").fetchone()[0] == 1


@pytest.mark.parametrize("body", [{"password": "short", "confirm_password": "short"},
                                  {"password": PASSWORD, "confirm_password": "different"},
                                  {"password": 15}, {"password": "x" * 1025},
                                  {"password": "\ud800", "confirm_password": "\ud800"}])
def test_bad_setup_input_does_not_create_an_account(client, body):
    # Send escaped JSON so invalid Unicode reaches server validation, rather than the HTTP client encoder.
    response = client.post("/auth/setup", content=json.dumps(body), headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert client.get("/auth/status").json()["configured"] is False


def test_remote_user_cannot_claim_the_first_account(client, tmp_path):
    with TestClient(server.create_app(tmp_path / "d.db", background=False), base_url="http://192.0.2.10:8765",
                    client=("192.0.2.20", 50000), follow_redirects=False) as remote:
        assert remote.get("/auth/status").json()["can_setup"] is False
        assert remote.post("/auth/setup", json={"password": PASSWORD, "confirm_password": PASSWORD}).status_code == 403
        auth.set_password(tmp_path / "d.db", PASSWORD)
        assert remote.get("/api/projects").status_code == 401
        assert remote.post("/auth/login", json={"password": PASSWORD}).status_code == 200
        assert remote.get("/api/projects").status_code == 200


def test_sign_out_revokes_the_cookie_and_password_can_sign_in_again(client):
    setup(client)
    old_cookie = client.cookies.get(auth.SESSION_COOKIE)
    assert client.post("/auth/logout").status_code == 200
    assert client.get("/api/projects").status_code == 401
    client.cookies.set(auth.SESSION_COOKIE, old_cookie, domain="127.0.0.1", path="/")
    assert client.get("/api/projects").status_code == 401
    assert client.post("/auth/login", json={"password": "wrong password"}).status_code == 401
    assert client.get("/api/projects").status_code == 401
    assert client.post("/auth/login", json={"password": PASSWORD}).status_code == 200
    assert client.get("/api/projects").status_code == 200


def test_restart_revokes_sessions_but_preserves_the_password(client, tmp_path):
    setup(client)
    cookie = client.cookies.get(auth.SESSION_COOKIE)
    with TestClient(server.create_app(tmp_path / "d.db", background=False), base_url="http://127.0.0.1:8765",
                    client=("127.0.0.1", 50000)) as restarted:
        restarted.cookies.set(auth.SESSION_COOKIE, cookie, domain="127.0.0.1", path="/")
        assert restarted.get("/api/projects").status_code == 401
        assert restarted.get("/auth/status").json()["configured"] is True
        assert restarted.post("/auth/login", json={"password": PASSWORD}).status_code == 200
        assert restarted.get("/api/projects").status_code == 200
        auth.set_password(tmp_path / "d.db", "a different private passphrase", current_password=PASSWORD)
        assert restarted.get("/api/projects").status_code == 401
        assert restarted.post("/auth/login", json={"password": PASSWORD}).status_code == 401
        assert restarted.post("/auth/login", json={"password": "a different private passphrase"}).status_code == 200
        c = db.connect(tmp_path / "d.db")
        with c:
            c.execute("UPDATE login_sessions SET expires_at=?", (time.time() - 1,))
        assert restarted.get("/api/projects").status_code == 401


def test_shutdown_clears_all_browser_sessions_but_keeps_machine_clients(tmp_path, monkeypatch):
    monkeypatch.setattr(security, "peer_uid", lambda *args: os.getuid())
    path = tmp_path / "d.db"
    app = server.create_app(path, background=False)
    token = security.access_token(path)
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)) as browser:
        setup(browser)
        assert browser.post("/auth/login", json={"password": PASSWORD}).status_code == 200
        c = db.connect(path)
        assert c.execute("SELECT COUNT(*) FROM login_sessions").fetchone()[0] == 2
    assert c.execute("SELECT COUNT(*) FROM login_sessions").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM login_account").fetchone()[0] == 1
    assert security.access_token(path) == token


def test_repeated_bad_passwords_are_throttled_and_lockout_expires(client, tmp_path):
    setup(client)
    client.post("/auth/logout")
    for _ in range(auth.MAX_FAILURES):
        assert client.post("/auth/login", json={"password": "bad passphrase"}).status_code == 401
    blocked = client.post("/auth/login", json={"password": PASSWORD})
    assert blocked.status_code == 429 and blocked.headers["retry-after"] == str(auth.LOCKOUT_SECONDS)
    c = db.connect(tmp_path / "d.db")
    with c:
        c.execute("UPDATE login_account SET blocked_until=?", (time.time() - 1,))
    assert client.post("/auth/login", json={"password": PASSWORD}).status_code == 200


def test_login_setup_and_logout_keep_cross_origin_protection(client):
    for path in ("/auth/login", "/auth/setup", "/auth/logout"):
        assert client.post(path, json={"password": PASSWORD}, headers={"Origin": "https://attacker.example"}).status_code == 403
        assert client.post(path, json={}, headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
    setup(client)
    assert client.post("/auth/logout", headers={"Origin": "http://localhost:8766"}).status_code == 403
    assert client.get("/api/projects").status_code == 200


def test_machine_clients_authenticate_with_a_private_bearer_not_a_browser_cookie(client, tmp_path):
    old_token = "retired-browser-access-link"
    (tmp_path / "access-token").write_text(old_token)
    assert client.get("/api/projects", headers={"Authorization": f"Bearer {old_token}"}).status_code == 401
    token = security.access_token(tmp_path / "d.db")
    assert token != old_token
    assert stat.S_IMODE((tmp_path / "machine-token").stat().st_mode) == 0o600
    assert client.get("/api/projects", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.get("/api/projects", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/auth/status").json()["signed_in"] is False


def test_https_sessions_use_secure_cookies(client, tmp_path):
    auth.set_password(tmp_path / "d.db", PASSWORD)
    with TestClient(server.create_app(tmp_path / "d.db", background=False), base_url="https://127.0.0.1:8765",
                    client=("127.0.0.1", 50000)) as secure:
        response = secure.post("/auth/login", json={"password": PASSWORD})
        assert response.status_code == 200 and "; Secure" in response.headers["set-cookie"]
        assert secure.get("/api/projects").status_code == 200


def test_password_command_requires_current_password_without_echoing_passwords(client, tmp_path, monkeypatch, capsys):
    import getpass
    import sys
    from rerunly import cli
    setup(client)
    new_password = "a new private passphrase"
    monkeypatch.setattr(sys, "argv", ["rerunly", "--db", str(tmp_path / "d.db"), "password"])
    answers = iter([PASSWORD, new_password, new_password])
    monkeypatch.setattr(getpass, "getpass", lambda *args: next(answers))
    cli.main()
    output = capsys.readouterr().out
    assert new_password not in output and "signed out" in output
    assert client.get("/api/projects").status_code == 401
    assert client.post("/auth/login", json={"password": new_password}).status_code == 200


def test_password_change_without_proof_cannot_overwrite_the_account(client, tmp_path):
    setup(client)
    path = tmp_path / "d.db"
    c = db.connect(path)
    previous = c.execute("SELECT password_hash,recovery_hash FROM login_account").fetchone()
    with pytest.raises(auth.OwnershipError, match="current password or a saved recovery code"):
        auth.set_password(path, "an unauthorized new password")
    assert tuple(c.execute("SELECT password_hash,recovery_hash FROM login_account").fetchone()) == tuple(previous)
    assert client.get("/api/projects").status_code == 200


def test_wrong_current_password_cannot_reset_or_issue_recovery(client, tmp_path):
    setup(client)
    path = tmp_path / "d.db"
    for operation in (lambda: auth.set_password(path, "an unauthorized new password", current_password="wrong password"),
                      lambda: auth.issue_recovery_code(path, "wrong password")):
        with pytest.raises(auth.OwnershipError, match="Incorrect password"):
            operation()
    assert client.get("/api/projects").status_code == 200
    c = db.connect(path)
    assert c.execute("SELECT failures FROM login_account").fetchone()[0] == 2


def test_recovery_code_is_hashed_shown_once_and_replaced_after_use(client, tmp_path):
    import hashlib
    response = setup(client)
    code = response.json()["recovery_code"]
    path = tmp_path / "d.db"
    c = db.connect(path)
    account = c.execute("SELECT recovery_hash FROM login_account").fetchone()
    assert account[0] == hashlib.sha256(code.encode()).hexdigest()
    assert code not in client.get("/auth/status").text
    assert code not in client.get("/api/settings").text
    assert code not in client.post("/auth/login", json={"password": PASSWORD}).text
    changed = auth.set_password(path, "a recovered private password", recovery_code=code)
    assert changed.recovery_code != code
    assert client.get("/api/projects").status_code == 401
    with pytest.raises(auth.OwnershipError, match="Incorrect recovery code"):
        auth.set_password(path, "another private password", recovery_code=code)
    assert client.post("/auth/login", json={"password": PASSWORD}).status_code == 401
    assert client.post("/auth/login", json={"password": "a recovered private password"}).status_code == 200


def test_recovery_and_login_share_persistent_attempt_limit(client, tmp_path):
    code = setup(client).json()["recovery_code"]
    path = tmp_path / "d.db"
    for _ in range(auth.MAX_FAILURES):
        with pytest.raises(auth.OwnershipError, match="Incorrect recovery code"):
            auth.set_password(path, "a recovered private password", recovery_code="wrong code")
    with pytest.raises(auth.AuthenticationThrottled):
        auth.set_password(path, "a recovered private password", recovery_code=code)
    assert client.post("/auth/login", json={"password": PASSWORD}).status_code == 429
    c = db.connect(path)
    with c:
        c.execute("UPDATE login_account SET blocked_until=?", (time.time() - 1,))
    assert auth.set_password(path, "a recovered private password", recovery_code=code).recovery_code != code


def test_replacing_a_recovery_code_requires_password_and_revokes_old_code(client, tmp_path):
    old_code = setup(client).json()["recovery_code"]
    path = tmp_path / "d.db"
    new_code = auth.issue_recovery_code(path, PASSWORD)
    assert new_code != old_code
    assert client.get("/api/projects").status_code == 401
    with pytest.raises(auth.OwnershipError, match="Incorrect recovery code"):
        auth.set_password(path, "a recovered private password", recovery_code=old_code)
    assert auth.set_password(path, "a recovered private password", recovery_code=new_code).recovery_code != new_code


def test_cli_reset_with_wrong_password_is_refused(client, tmp_path, monkeypatch, capsys):
    import getpass
    import sys
    from rerunly import cli
    setup(client)
    answers = iter(["wrong password", "an unauthorized new password", "an unauthorized new password"])
    monkeypatch.setattr(getpass, "getpass", lambda *args: next(answers))
    monkeypatch.setattr(sys, "argv", ["rerunly", "--db", str(tmp_path / "d.db"), "password"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "Incorrect password" in capsys.readouterr().err
    assert client.get("/api/projects").status_code == 200


def test_cli_recovery_requires_saved_code_and_never_echoes_input_secrets(client, tmp_path, monkeypatch, capsys):
    import getpass
    import sys
    from rerunly import cli
    code = setup(client).json()["recovery_code"]
    new_password = "a recovered private password"
    answers = iter([code, new_password, new_password])
    monkeypatch.setattr(getpass, "getpass", lambda *args: next(answers))
    monkeypatch.setattr(sys, "argv", ["rerunly", "--db", str(tmp_path / "d.db"), "password", "--recover"])
    cli.main()
    output = capsys.readouterr().out
    assert new_password not in output and code not in output
    assert "RR-" in output and "shown only once" in output
    assert client.get("/api/projects").status_code == 401
    assert client.post("/auth/login", json={"password": new_password}).status_code == 200


def test_existing_accounts_enroll_recovery_only_with_their_password(client, tmp_path):
    setup(client)
    path = tmp_path / "d.db"
    c = db.connect(path)
    with c:
        c.execute("UPDATE login_account SET recovery_hash=NULL")
    assert client.post("/auth/login", json={"password": PASSWORD}).status_code == 200
    with pytest.raises(auth.OwnershipError):
        auth.set_password(path, "a recovered private password", recovery_code="")
    code = auth.issue_recovery_code(path, PASSWORD)
    assert auth.set_password(path, "a recovered private password", recovery_code=code).recovery_code != code


def test_legacy_account_schema_migrates_without_resetting_its_password(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    salt = "12" * 16
    digest = auth._hash_password(PASSWORD, salt)
    with sqlite3.connect(path) as old:
        old.execute("CREATE TABLE login_account(id INTEGER PRIMARY KEY CHECK(id=1),salt TEXT NOT NULL,password_hash TEXT NOT NULL,"
                    "failures INTEGER NOT NULL DEFAULT 0,blocked_until REAL NOT NULL DEFAULT 0)")
        old.execute("INSERT INTO login_account(id,salt,password_hash) VALUES(1,?,?)", (salt, digest))
    c = db.connect(path)
    row = c.execute("SELECT password_hash,recovery_hash FROM login_account").fetchone()
    assert row["password_hash"] == digest and row["recovery_hash"] is None
    with pytest.raises(auth.OwnershipError):
        auth.set_password(path, "an unauthorized new password")
    code = auth.issue_recovery_code(path, PASSWORD)
    assert c.execute("SELECT password_hash FROM login_account").fetchone()[0] == digest
    assert auth.set_password(path, "a recovered private password", recovery_code=code).recovery_code != code


def test_concurrent_recovery_cannot_spend_the_same_code_twice(client, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    code = setup(client).json()["recovery_code"]
    path = tmp_path / "d.db"
    barrier = threading.Barrier(2)
    real_hash = auth._hash_password

    def derive(password, salt):
        barrier.wait(timeout=10)
        return real_hash(password, salt)

    monkeypatch.setattr(auth, "_hash_password", derive)

    def recover(password):
        try:
            return auth.set_password(path, password, recovery_code=code)
        except auth.OwnershipError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(recover, ["first recovered password", "second recovered password"]))
    assert sum(result is not None for result in results) == 1
    monkeypatch.setattr(auth, "_hash_password", real_hash)
    with pytest.raises(auth.OwnershipError, match="Incorrect recovery code"):
        auth.set_password(path, "another recovered password", recovery_code=code)


def test_recovery_code_command_requires_password_and_only_outputs_a_new_code(client, tmp_path, monkeypatch, capsys):
    import getpass
    import sys
    from rerunly import cli
    original = setup(client).json()["recovery_code"]
    path = tmp_path / "d.db"
    monkeypatch.setattr(getpass, "getpass", lambda *args: PASSWORD)
    monkeypatch.setattr(sys, "argv", ["rerunly", "--db", str(path), "recovery-code"])
    cli.main()
    output = capsys.readouterr().out
    assert PASSWORD not in output and original not in output
    code = next(line for line in output.splitlines() if line.startswith("RR-"))
    assert client.get("/api/projects").status_code == 401
    with pytest.raises(auth.OwnershipError):
        auth.set_password(path, "a recovered private password", recovery_code=original)
    assert auth.set_password(path, "a recovered private password", recovery_code=code).recovery_code != code


def test_machine_bearer_cannot_reset_an_existing_password(client, tmp_path):
    setup(client)
    client.post("/auth/logout")
    headers = {"Authorization": "Bearer " + security.access_token(tmp_path / "d.db")}
    assert client.post("/auth/setup", json={"password": "an unauthorized password", "confirm_password": "an unauthorized password"},
                       headers=headers).status_code == 409
    assert client.post("/auth/login", json={"password": PASSWORD}).status_code == 200


def test_owned_hook_and_telemetry_are_upgraded_with_private_credentials(tmp_path, monkeypatch):
    settings = tmp_path / "claude-settings.json"
    old_hook = "curl -s -X POST http://127.0.0.1:8765/api/ingest # rerunly"
    custom_hook = {"type": "command", "command": "echo keep-me"}
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": old_hook}, custom_hook]}]},
                                    "env": {"CUSTOM_ENV": "keep", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://127.0.0.1:8765/v1/logs",
                                            "OTEL_EXPORTER_OTLP_LOGS_HEADERS": "X-Example=keep"}}))
    settings.chmod(0o644)
    monkeypatch.setattr(server, "CLAUDE_SETTINGS", settings)
    server.create_app(tmp_path / "d.db", background=False)
    values = json.loads(settings.read_text())
    token = security.access_token(tmp_path / "d.db")
    command = values["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert "--header" in command and "machine-token-header" in command and token not in command
    assert values["hooks"]["Stop"][0]["hooks"][1] == custom_hook
    assert values["env"]["CUSTOM_ENV"] == "keep"
    assert values["env"]["OTEL_EXPORTER_OTLP_LOGS_HEADERS"] == "Authorization=Bearer%20" + token + ",X-Example=keep"
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600
    header = tmp_path / "machine-token-header"
    assert header.read_text() == f"Authorization: Bearer {token}\n"
    assert stat.S_IMODE(header.stat().st_mode) == 0o600


def test_malformed_agent_settings_do_not_block_the_login_page(tmp_path, monkeypatch):
    settings = tmp_path / "claude-settings.json"
    settings.write_text("{invalid JSON")
    monkeypatch.setattr(server, "CLAUDE_SETTINGS", settings)
    with TestClient(server.create_app(tmp_path / "d.db", background=False), follow_redirects=False) as client:
        assert client.get("/login").status_code == 200
        assert client.get("/api/projects").status_code == 401
    assert settings.read_text() == "{invalid JSON"


def test_setup_status_never_shows_the_machine_token(client, tmp_path):
    settings = server.CLAUDE_SETTINGS
    settings.write_text(json.dumps({"env": {"OTEL_EXPORTER_OTLP_LOGS_HEADERS": "Authorization=Bearer%20stale,X-Collector-Key=secret"}}))
    setup(client)
    token = security.access_token(tmp_path / "d.db")
    body = client.get("/api/setup/status").text
    telemetry = json.loads(body)["telemetry"]
    assert token not in body and "stale" not in body and "secret" not in body
    assert telemetry["env"]["OTEL_EXPORTER_OTLP_LOGS_HEADERS"] == "Authorization=<hidden>,X-Collector-Key=<hidden>"
    assert telemetry["conflicts"]["OTEL_EXPORTER_OTLP_LOGS_HEADERS"] == "Authorization=<hidden>,X-Collector-Key=<hidden>"
    # enabling still writes the real token, and the page then reports it as configured
    assert client.post("/api/setup/telemetry", json={"enable": True}).json()["telemetry"]["configured"]
    assert json.loads(settings.read_text())["env"]["OTEL_EXPORTER_OTLP_LOGS_HEADERS"] == f"Authorization=Bearer%20{token},X-Collector-Key=secret"
