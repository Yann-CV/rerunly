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


def _model_order(model_id: str) -> tuple:
    family = next((i for i, f in enumerate(FAMILIES) if f in model_id), len(FAMILIES))
    version = [int(n) for n in re.findall(r"\d+", model_id)]
    # padded, so 5.1 sorts before 5 (newest first)
    return (family, [-n for n in (version + [0] * 4)[:4]], model_id)


def agent_models(priced: list[str], used: list[str]) -> list[dict]:
    """Models to pick: Claude Code's aliases for its latest models, every full Claude model id known (the price
    table's, and any that ran) newest first within each family, then Codex models."""
    aliases = [{"id": a, "label": f"{a} (the latest {a.capitalize()})", "kind": "alias", "used": False} for a in model_aliases()]
    ids = sorted({m for m in [*priced, *used] if MODEL_RE.match(m) and m.startswith("claude-")}, key=_model_order)
    return aliases + [{"id": m, "label": m, "kind": "model", "used": m in used} for m in ids] + [{"id": CODEX_PREFIX + m, "label": f"Codex · {m}", "kind": "codex", "used": m in used} for m in dict.fromkeys([*codex_models(), *(m for m in used if m.startswith(("gpt-", "o3", "o4")) and not m.startswith("gpt-oss"))])]


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


def codex_models():
    """Include account-visible cached models, without making authenticated network calls."""
    import json
    import os
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        models = json.loads((home / "models_cache.json").read_text()).get("models", [])
    except (OSError, ValueError):
        models = []
    return tuple(dict.fromkeys([*CODEX_MODELS, *(m["slug"] for m in models if m.get("visibility") == "list" and MODEL_RE.fullmatch(m.get("slug", "")))]))


def codex_argv(prompt: str, session_id: str, model: str = "", effort: str = "", version: str = "",
               background: bool = False, permission: str = "ask", note: str = "") -> list[str]:
    import json
    argv = ["npx", "-y", f"@openai/codex@{version}"] if version else ["codex"]
    if background:
        argv += ["exec", "--json"]
    argv += ["-c", 'approval_policy="never"' if background else 'approval_policy="on-request"',
             "--sandbox", "workspace-write" if permission == "edits" else "read-only"]
    if model:
        argv += ["--model", model.removeprefix(CODEX_PREFIX)]
    if effort:
        argv += ["-c", "model_reasoning_effort=" + json.dumps(effort)]
    if note:
        argv += ["-c", "developer_instructions=" + json.dumps(note)]
    # Codex chooses its own thread id. A persisted marker links the imported rerun to the queue row.
    return [*argv, f'<rerunly-replay id="{session_id}"/>\n{prompt}']
