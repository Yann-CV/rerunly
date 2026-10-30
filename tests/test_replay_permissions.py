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


def test_terminal_keep_open_preserves_arguments_and_exit_status(tmp_path):
    import subprocess
    import sys
    from rerunly.replay import terminal_run_argv

    marker = tmp_path / 'must-not-exist'
    prompt = f"literal ' quote; $(touch {marker}) `touch {marker}`\nnext line"
    argv = [sys.executable, '-c', 'import sys; print(sys.argv[1]); sys.exit(7)', prompt]
    assert terminal_run_argv(argv, False) == argv
    result = subprocess.run(terminal_run_argv(argv, True), input='\n', text=True, capture_output=True, timeout=5)
    assert result.returncode == 7
    assert prompt in result.stdout
    assert 'Press Enter to close this terminal' in result.stdout
    assert not marker.exists()
