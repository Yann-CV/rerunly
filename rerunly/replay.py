"""Replay a prompt with Claude Code or Codex, interactively, in a terminal window the user watches.

The replay gets a session id chosen here, so that once its transcript is imported it can be linked
back to the prompt it replays.
"""

import functools
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
MODEL_RE = re.compile(r"^[A-Za-z0-9][\w.\-\[\]:]*$")
VERSION_RE = re.compile(r"^\d+(\.\d+)*$")


FAMILIES = ("fable", "opus", "sonnet", "haiku")


@functools.lru_cache(maxsize=1)
def model_aliases() -> tuple[str, ...]:
    """The aliases Claude Code accepts for its latest models, as its own `--model` help names them."""
    try:
        text = subprocess.run(["claude", "--help"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ()
    help_text = re.search(r"--model <model>(.*?)(?:\n  -|\Z)", text, re.S)
    return tuple(dict.fromkeys(re.findall(r"'([a-z][\w.-]*)'", help_text.group(1)))) if help_text else ()


CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")


@functools.lru_cache(maxsize=1)
def claude_efforts() -> tuple[str, ...]:
    """The effort levels Claude Code accepts, as its own `--effort` help lists them."""
    try:
        text = subprocess.run(["claude", "--help"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return CLAUDE_EFFORTS
    levels = re.search(r"--effort <level>.*?\(([a-z, ]+)\)", text, re.S)
    found = tuple(e for e in (x.strip() for x in levels.group(1).split(",")) if e in EFFORTS) if levels else ()
    return found or CLAUDE_EFFORTS


def _model_order(model_id: str) -> tuple:
    family = next((i for i, f in enumerate(FAMILIES) if f in model_id), len(FAMILIES))
    version = [int(n) for n in re.findall(r"\d+", model_id)]
    # padded, so 5.1 sorts before 5 (newest first)
    return (family, [-n for n in (version + [0] * 4)[:4]], model_id)


def agent_models(priced: list[str], used: list[str]) -> list[dict]:
    """Models to pick: Claude Code's aliases for its latest models, every full Claude model id known (the price
    table's, and any that ran) newest first within each family, then Codex models. Each says which `agent` runs it,
    the `efforts` it accepts, and its `default_effort` when the agent tells it."""
    claude = {"agent": "claude", "efforts": list(claude_efforts()), "default_effort": None}
    aliases = [{"id": a, "label": f"{a} (the latest {a.capitalize()})", "kind": "alias", "used": False, **claude} for a in model_aliases()]
    ids = sorted({m for m in [*priced, *used] if MODEL_RE.match(m) and m.startswith("claude-")}, key=_model_order)
    levels = codex_efforts()
    codex = dict.fromkeys([*codex_models(), *(m for m in used if m.startswith(("gpt-", "o3", "o4")) and not m.startswith("gpt-oss"))])
    return (aliases + [{"id": m, "label": m, "kind": "model", "used": m in used, **claude} for m in ids]
            + [{"id": CODEX_PREFIX + m, "label": f"Codex · {m}", "kind": "codex", "used": m in used, "agent": "codex",
                "efforts": levels.get(m, ([], None))[0] or list(CODEX_EFFORTS), "default_effort": levels.get(m, ([], None))[1]} for m in codex])


def claude_argv(prompt: str, session_id: str, model: str = "", effort: str = "", version: str = "",
                plugin_dir: str = "", note: str = "") -> list[str]:
    """Claude Code started on `prompt`: the installed one, or release `version` through npx; with the skills
    restored in `plugin_dir` and `note` appended to its system prompt."""
    argv = ["npx", "-y", f"@anthropic-ai/claude-code@{version}"] if version else ["claude"]
    argv += ["--session-id", session_id]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    if plugin_dir:
        argv += ["--plugin-dir", plugin_dir]
    if note:
        argv += ["--append-system-prompt", note]
    return [*argv, prompt]


def terminal_argv(cwd: str, argv: list[str]) -> list[str] | None:
    """A command opening a terminal window that runs `argv` in `cwd`, or None without a known terminal."""
    if sys.platform == "darwin":
        script = f"cd {shlex.quote(cwd)} && {shlex.join(argv)}"
        return ["osascript", "-e", f'tell application "Terminal" to do script {_applescript(script)}', "-e",
                'tell application "Terminal" to activate']
    if shutil.which("gnome-terminal"):
        return ["gnome-terminal", f"--working-directory={cwd}", "--", *argv]
    if shutil.which("konsole"):
        return ["konsole", "--workdir", cwd, "-e", *argv]
    for name in ("x-terminal-emulator", "kitty", "alacritty", "xterm"):
        if shutil.which(name):
            return [name, "-e", *argv]
    return None


def _applescript(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def launch(cwd: str, argv: list[str]) -> str | None:
    """Open the terminal window; the name of the terminal used, or None when there is none."""
    term = terminal_argv(cwd, argv)
    if term is None or not Path(cwd).is_dir():
        return None
    subprocess.Popen(term, cwd=cwd, start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return Path(term[0]).name


CODEX_PREFIX = "codex:"
CODEX_MODELS = ("gpt-6.1-sol", "gpt-6-astra", "gpt-6.1-terra", "gpt-6-sol", "gpt-6-luna", "gpt-5.3-codex")


CODEX_EFFORTS = ("low", "medium", "high", "xhigh")


def _codex_cache() -> list[dict]:
    """Codex's own cache of the models the account can use: read locally, no authenticated network call."""
    import json
    import os
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        return json.loads((home / "models_cache.json").read_text()).get("models", [])
    except (OSError, ValueError, AttributeError):
        return []


def codex_models():
    """Include account-visible cached models, without making authenticated network calls."""
    models = _codex_cache()
    return tuple(dict.fromkeys([*CODEX_MODELS, *(m["slug"] for m in models if m.get("visibility") == "list" and MODEL_RE.fullmatch(m.get("slug", "")))]))


def codex_efforts() -> dict[str, tuple[list[str], str | None]]:
    """The effort levels each cached Codex model supports, and its default one."""
    found = {}
    for m in _codex_cache():
        levels = [lv.get("effort") for lv in m.get("supported_reasoning_levels") or [] if isinstance(lv, dict)]
        if m.get("slug"):
            found[m["slug"]] = ([e for e in levels if e in EFFORTS], m.get("default_reasoning_level"))
    return found


def uses_codex(model: str, harness: str) -> bool:
    return model.startswith(CODEX_PREFIX) or (model.startswith("gpt-") and not model.startswith("gpt-oss")) or (not model and harness == "Codex")


def original_permission_args(harness: str, permissions: dict) -> tuple[list[str], bool]:
    """Translate recorded permissions; never invent defaults for missing metadata."""
    import json
    if harness == "Claude Code":
        mode = permissions.get("permission_mode")
        if not isinstance(mode, str) or mode not in {"default", "acceptEdits", "plan", "dontAsk", "bypassPermissions", "auto"}:
            raise ValueError("Original Claude Code permissions were not recorded or are unsupported. Re-import history or choose explicit permissions.")
        return ["--permission-mode", mode], mode not in {"dontAsk", "bypassPermissions"}
    approval = permissions.get("approval_policy")
    sandbox = permissions.get("sandbox_policy")
    if not isinstance(approval, str) or approval not in {"untrusted", "on-failure", "on-request", "never"} or not isinstance(sandbox, dict):
        raise ValueError("Original Codex approval and sandbox permissions were not recorded. Re-import history or choose explicit permissions.")
    kind = sandbox.get("type")
    if kind not in {"read-only", "workspace-write", "danger-full-access"}:
        raise ValueError("The original Codex sandbox cannot be restored by this launcher. Choose explicit permissions.")
    supported = {"type", "writable_roots", "network_access", "exclude_tmpdir_env_var", "exclude_slash_tmp"} if kind == "workspace-write" else {"type"}
    if set(sandbox) - supported:
        raise ValueError("The original Codex sandbox has settings this launcher cannot restore exactly. Choose explicit permissions.")
    if kind == "workspace-write":
        roots = sandbox.get("writable_roots", [])
        if not isinstance(roots, list) or not all(isinstance(root, str) for root in roots):
            raise ValueError("The original Codex writable directories cannot be restored exactly.")
        if any(key in sandbox and not isinstance(sandbox[key], bool) for key in ("network_access", "exclude_tmpdir_env_var", "exclude_slash_tmp")):
            raise ValueError("The original Codex sandbox flags cannot be restored exactly.")
    args = ["-c", "approval_policy=" + json.dumps(approval), "--sandbox", kind]
    if kind == "workspace-write":
        for key, default in (("writable_roots", []), ("network_access", False), ("exclude_tmpdir_env_var", False), ("exclude_slash_tmp", False)):
            args += ["-c", f"sandbox_workspace_write.{key}=" + json.dumps(sandbox.get(key, default))]
    return args, approval != "never"


def codex_argv(prompt: str, session_id: str, model: str = "", effort: str = "", version: str = "",
               background: bool = False, permission: str = "ask", note: str = "", original_permissions: dict | None = None) -> list[str]:
    import json
    argv = ["npx", "-y", f"@openai/codex@{version}"] if version else ["codex"]
    if background:
        argv += ["exec", "--json"]
    argv += original_permission_args("Codex", original_permissions)[0] if original_permissions is not None else [
        "-c", 'approval_policy="never"' if background else 'approval_policy="on-request"',
        "--sandbox", "workspace-write" if permission == "edits" else "read-only"]
    if model:
        argv += ["--model", model.removeprefix(CODEX_PREFIX)]
    if effort:
        argv += ["-c", "model_reasoning_effort=" + json.dumps(effort)]
    if note:
        argv += ["-c", "developer_instructions=" + json.dumps(note)]
    # Codex chooses its own thread id. A persisted marker links the imported rerun to the queue row.
    return [*argv, f'<rerunly-replay id="{session_id}"/>\n{prompt}']
