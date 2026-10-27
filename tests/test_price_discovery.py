import json

import pytest

from rerunly import db, price_discovery as discovery
from rerunly.ingest import ingest


PRICE = {"input": 8.0, "output": 40.0, "cache_read": 0.8}


def database(tmp_path):
    c = db.connect(tmp_path / "prices.db")
    db.save_settings(c, {"auto_price_lookup": True, "codex_enabled": False})
    return c


def test_parse_standard_prices_and_claude_names():
    openai = '''<table><tr><th>Model</th><th>Input</th><th>Output</th><th>Cached input</th></tr>
    <tr><td>gpt-99</td><td>$8.00</td><td>$40</td><td>$0.8</td></tr></table>
    <table><tr><th>Model</th><th>Input</th><th>Output</th><th>Cached input</th></tr>
    <tr><td>gpt-99</td><td>$4</td><td>$20</td><td>$0.4</td></tr></table>'''
    assert discovery.parse_catalog(openai, "openai") == {"gpt-99": PRICE}
    claude = '''<table><tr><th>Model</th><th>Base tokens</th><th>Prompt caching</th></tr>
    <tr><th>Name</th><th>Input</th><th>Output</th><th>5m writes</th><th>1h writes</th><th>Hits and refreshes</th></tr>
    <tr><td><a>Claude Opus 5.9</a>Some description</td><td>$8 / MTok</td><td>$40 / MTok</td><td>$10 / MTok</td><td>$16 / MTok</td><td>$0.8 / MTok</td></tr></table>'''
    assert discovery.parse_catalog(claude, "anthropic") == {"claude-opus-5-9": PRICE}
    specialized = '''<table><tr><th>Category</th><th>Model</th><th>Input</th><th>Output</th><th>Cached input</th></tr>
    <tr><td>Codex</td><td>gpt-99-codex</td><td>$8</td><td>$40</td><td>$0.8</td></tr></table>
    <table><tr><th>Model</th><th>Training</th><th>Input</th><th>Output</th><th>Cached input</th></tr>
    <tr><td>gpt-98</td><td>$100.00 / hour</td><td>$4</td><td>$16</td><td>$1</td></tr></table>'''
    assert discovery.parse_catalog(specialized, "openai") == {"gpt-99-codex": PRICE}


def test_bad_or_incomplete_table_is_not_guessed():
    for html in ["Login required", '<table><tr><th>Model</th><th>Input</th><th>Output</th></tr><tr><td>gpt-99</td><td>$8</td><td>$40</td></tr></table>']:
        with pytest.raises(ValueError):
            discovery.parse_catalog(html, "openai")


def test_lookup_cache_snapshot_and_manual_prices(tmp_path, monkeypatch):
    c = database(tmp_path)
    calls = []
    def fetch(provider):
        calls.append(provider)
        return {"claude-opus-5-9": PRICE, "claude-opus-5-10": PRICE}
    monkeypatch.setattr(discovery, "fetch_catalog", fetch)
    assert discovery.discover_prices(c, ["claude-opus-5-9-20261026"], db.get_settings(c), now=100)
    settings = db.get_settings(c)
    assert settings["prices"]["claude-opus-5-9"] == PRICE
    assert settings["price_provenance"]["claude-opus-5-9"]["source"] == discovery.SOURCES["anthropic"]
    assert discovery.discover_prices(c, ["claude-opus-5-10"], settings, now=101)
    assert calls == ["anthropic"]  # another new model uses the cached catalog
    custom = {"input": 1, "output": 2, "cache_read": 0.1}
    db.save_settings(c, {"prices": {"claude-opus-5-9": custom}})
    assert not discovery.discover_prices(c, ["claude-opus-5-9"], db.get_settings(c), now=102)
    assert db.get_settings(c)["prices"]["claude-opus-5-9"] == custom


def test_unknown_model_retry_and_offline_import(tmp_path, monkeypatch):
    c = database(tmp_path)
    calls = []
    def offline(provider):
        calls.append(provider)
        raise OSError("offline")
    monkeypatch.setattr(discovery, "fetch_catalog", offline)
    assert not discovery.discover_prices(c, ["gpt-99"], db.get_settings(c), now=100)
    settings = db.get_settings(c)
    assert "gpt-99" not in settings["prices"]
    assert settings["price_lookup_missing"] == ["gpt-99"]
    assert "unavailable" in settings["price_catalogs"]["openai"]["error"]
    discovery.discover_prices(c, ["gpt-99"], settings, now=101)
    assert calls == ["openai"]
    monkeypatch.setattr(discovery, "fetch_catalog", lambda provider: {"gpt-99": PRICE})
    assert discovery.discover_prices(c, ["gpt-99"], db.get_settings(c), now=3701)
    assert db.get_settings(c)["price_lookup_missing"] == []


def test_disabled_lookup_and_custom_family_override(tmp_path, monkeypatch):
    c = database(tmp_path)
    def unexpected(provider):
        raise AssertionError("must not fetch")
    monkeypatch.setattr(discovery, "fetch_catalog", unexpected)
    db.save_settings(c, {"auto_price_lookup": False})
    assert not discovery.discover_prices(c, ["gpt-99"], db.get_settings(c))
    db.save_settings(c, {"auto_price_lookup": True, "prices": {"claude-opus-5": PRICE}})
    assert not discovery.discover_prices(c, ["claude-opus-5-9"], db.get_settings(c))


def test_discovered_price_recalculates_unchanged_history(tmp_path, monkeypatch):
    c = database(tmp_path)
    root = tmp_path / "claude" / "project"
    root.mkdir(parents=True)
    (root / "session.jsonl").write_text("\n".join(json.dumps(event) for event in [
        {"type": "user", "uuid": "p1", "promptId": "p1", "timestamp": "2026-10-26T10:00:00Z", "cwd": str(root), "origin": {"kind": "human"}, "message": {"content": "Fix"}},
        {"type": "assistant", "timestamp": "2026-10-26T10:00:01Z", "message": {"id": "m1", "model": "claude-opus-5-9", "content": [], "usage": {"input_tokens": 1000000}}},
    ]))
    db.save_settings(c, {"claude_projects_dir": str(root.parent), "auto_price_lookup": False})
    assert ingest(c)["updated"] == 1
    assert c.execute("SELECT cost_usd FROM prompts").fetchone()[0] == 5
    db.save_settings(c, {"auto_price_lookup": True})
    monkeypatch.setattr(discovery, "fetch_catalog", lambda provider: {"claude-opus-5-9": PRICE})
    assert ingest(c)["updated"] == 1  # file has not changed, but its estimate has
    assert c.execute("SELECT cost_usd FROM prompts").fetchone()[0] == 8
    assert ingest(c)["updated"] == 0


def test_included_codex_model_is_discovered_without_sessions(tmp_path, monkeypatch):
    from rerunly import replay
    c = database(tmp_path)
    db.save_settings(c, {"codex_enabled": True, "judge_model": "", "claude_projects_dir": str(tmp_path / "none")})
    monkeypatch.setattr(replay, "codex_models", lambda: ("gpt-99",))
    monkeypatch.setattr(discovery, "fetch_catalog", lambda provider: {"gpt-99": PRICE})
    assert ingest(c) == {"scanned": 0, "updated": 0}
    assert db.get_settings(c)["prices"]["gpt-99"] == PRICE


def test_setup_save_keeps_found_prices_automatic(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from rerunly.server import create_app
    c = database(tmp_path)
    db.save_settings(c, {"auto_prices": {"claude-sonnet-6": PRICE}})
    custom = {"input": 1, "output": 2, "cache_read": 0.1}
    client = TestClient(create_app(tmp_path / "prices.db", background=False))
    table = {**client.get("/api/settings").json()["prices"], "gpt-99": custom}
    client.put("/api/settings", json={"prices": table})
    assert json.loads(c.execute("SELECT value FROM settings WHERE key='prices'").fetchone()[0]) == {"gpt-99": custom}
    # a newer version is still looked up: the found family price is not a manual override
    monkeypatch.setattr(discovery, "fetch_catalog", lambda provider: {"claude-sonnet-6-1": custom})
    assert discovery.discover_prices(c, ["claude-sonnet-6-1"], db.get_settings(c))
