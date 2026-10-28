import json
import time

from rerunly import live_limits


def test_claude_usage_from_its_login(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "t", "expiresAt": (time.time() + 3600) * 1000, "subscriptionType": "pro"}}))
    calls = []

    def get(url, headers):
        calls.append(headers["Authorization"])
        return {"five_hour": {"utilization": 12.0, "resets_at": "2096-10-28T11:59:59+00:00"},
                "seven_day": {"utilization": 37.0, "resets_at": None}, "seven_day_opus": None}
    monkeypatch.setattr(live_limits, "_get", get)
    got = live_limits._claude()
    assert got["plan"] == "pro" and calls == ["Bearer t"]
    assert [(w["used_percent"], w["window_minutes"]) for w in got["windows"]] == [(12.0, 300), (37.0, 10080)]
    assert got["windows"][0]["resets_at"] > 4_000_000_000 and got["windows"][1]["resets_at"] is None


def test_expired_claude_login_is_not_refreshed(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "t", "expiresAt": 1000}}))
    monkeypatch.setattr(live_limits, "_get", lambda *a: (_ for _ in ()).throw(AssertionError("no request")))
    assert live_limits._claude() is None


def test_codex_usage_from_its_login(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text(json.dumps({"tokens": {"access_token": "t", "account_id": "a"}}))
    monkeypatch.setattr(live_limits, "_get", lambda url, headers: {"plan_type": "plus", "rate_limit": {
        "primary_window": {"used_percent": 3, "limit_window_seconds": 18000, "reset_at": 1793189165},
        "secondary_window": {"used_percent": 0, "limit_window_seconds": 604800, "reset_at": 1793775965}}})
    got = live_limits._codex()
    assert got["plan"] == "plus"
    assert got["windows"] == [{"used_percent": 3, "window_minutes": 300, "resets_at": 1793189165},
                              {"used_percent": 0, "window_minutes": 10080, "resets_at": 1793775965}]


def test_no_login_and_failures_are_cached_as_none(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(live_limits, "_cache", {})
    assert live_limits._cached("codex", live_limits._codex) is None
    monkeypatch.setattr(live_limits, "_codex", lambda: (_ for _ in ()).throw(AssertionError("cached")))
    assert live_limits._cached("codex", live_limits._codex) is None
