"""Keep integration tests independent of locally installed agent histories."""
import pytest
from rerunly.config import DEFAULT_SETTINGS


@pytest.fixture(autouse=True)
def isolate_codex_history(monkeypatch, tmp_path):
    monkeypatch.setitem(DEFAULT_SETTINGS, "codex_sessions_dir", str(tmp_path / "no-local-codex"))
    monkeypatch.setitem(DEFAULT_SETTINGS, "auto_price_lookup", False)
    from rerunly import statusline
    monkeypatch.setattr(statusline, "DIR", tmp_path / "rerunly-data")
    from rerunly import live_limits
    monkeypatch.setattr(live_limits, "claude_code", lambda: None)
    monkeypatch.setattr(live_limits, "codex", lambda: None)
    from rerunly import server
    monkeypatch.setattr(server, "CLAUDE_SETTINGS", tmp_path / "claude-settings.json")


@pytest.fixture(autouse=True)
def authenticated_dashboard_for_feature_tests(request, monkeypatch):
    """Feature tests exercise an already signed-in dashboard; authentication tests use the real gate."""
    if not request.node.get_closest_marker("require_login"):
        from rerunly.auth import LocalLogin
        monkeypatch.setattr(LocalLogin, "reject", lambda self, request: None)


@pytest.fixture(autouse=True)
def close_test_connections():
    """Connections a test opens itself; the app closes its own, and a leak now fails the run."""
    from rerunly import db
    with db.connection_scope():
        yield
