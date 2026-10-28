import pytest

from rerunly.replay import codex_argv, original_permission_args


@pytest.mark.parametrize("mode,interactive", [("default", True), ("acceptEdits", True), ("plan", True), ("dontAsk", False), ("bypassPermissions", False)])
def test_claude_recorded_permission_mode_is_preserved(mode, interactive):
    assert original_permission_args("Claude Code", {"permission_mode": mode}) == (["--permission-mode", mode], interactive)


def test_codex_original_sandbox_and_approval_are_preserved():
    permissions = {"approval_policy": "on-request", "sandbox_policy": {
        "type": "workspace-write", "writable_roots": ["/work/additional root"],
        "network_access": True, "exclude_tmpdir_env_var": True, "exclude_slash_tmp": True}}
    argv = codex_argv("Fix", "session", original_permissions=permissions)
    assert 'approval_policy="on-request"' in argv
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert 'sandbox_workspace_write.writable_roots=["/work/additional root"]' in argv
    for key in ("network_access", "exclude_tmpdir_env_var", "exclude_slash_tmp"):
        assert f"sandbox_workspace_write.{key}=true" in argv
    assert "read-only" not in argv


@pytest.mark.parametrize("permissions", [{}, {"approval_policy": {}}, {"approval_policy": "never", "sandbox_policy": {"type": "external-sandbox"}}])
def test_unknown_codex_permissions_require_explicit_choice(permissions):
    with pytest.raises(ValueError):
        original_permission_args("Codex", permissions)
