"""Keep integration tests independent of locally installed agent histories."""
import pytest
from rerunly.config import DEFAULT_SETTINGS


@pytest.fixture(autouse=True)
def isolate_codex_history(monkeypatch, tmp_path):
    monkeypatch.setitem(DEFAULT_SETTINGS, "codex_sessions_dir", str(tmp_path / "no-local-codex"))
    monkeypatch.setitem(DEFAULT_SETTINGS, "auto_price_lookup", False)
