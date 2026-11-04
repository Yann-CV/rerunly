"""Unattended reruns: Claude Code in the background, with the skills as they ran, stopping to report
whatever would need the user.

A rerun runs `claude -p` with `--permission-prompts none`: anything that would ask is denied, and the
agent is told to stop and say what it needed. Its stream (`--output-format stream-json`) is written to a
log; its final `result` line gives the answer, the cost and the denied actions, which decide the status.
"""

import json
import re
import os
import shutil
import subprocess
import tarfile
import io
import threading
from datetime import datetime, timezone
from pathlib import Path

from .replay import claude_permission_args

# the plugin carrying the skills as they ran: skill `x` is loaded as `rerunly-pinned:x`
PINNED_PLUGIN = "rerunly-pinned"
STATUS_LINE = "RERUN STATUS:"
PERMISSIONS = {
    "ask": claude_permission_args("ask", background=True),
    "edits": claude_permission_args("edits", background=True),
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def unattended_note(skills: list[dict]) -> str:
    """Appended to the system prompt: nobody is there to answer, and which skill versions to use."""
    lines = [
        "This is an unattended rerun of an earlier task: nobody can answer questions or approve actions.",
        "If you need a decision, information, or an action that gets denied, stop right away and say exactly what you needed.",
        f"End your final message with one line: `{STATUS_LINE} done` if the task is complete, "
        f"or `{STATUS_LINE} blocked: <what you needed>` otherwise.",
    ]
    for k in skills:
        if Path(k["pinned"]).is_absolute():
            lines.append(f"For skill `{k['skill']}`, read the instructions at `{k['pinned']}` with a file-reading tool: it is the version this task ran with.")
        else:
            lines.append(f"To use the skill `{k['skill']}`, use `{k['pinned']}` instead: it is the version this task ran with.")
    return "\n".join(lines)


def _skill_name(skill: str) -> str:
    name = skill.split(":")[-1]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) or name in (".", ".."):
        raise ValueError(f"Cannot restore skill with an unsafe name: {skill!r}")
    return name


def build_skills_plugin(folder: Path, versions: list[dict]) -> list[dict]:
    """A plugin in `folder` with each skill version: all its files from its git commit, or its SKILL.md
    snapshot otherwise. Returns, per skill, its pinned name and where it was restored from."""
    names = [_skill_name(v["skill"]) for v in versions]
    if len(names) != len(set(names)):
        raise ValueError("Cannot restore multiple skills with the same name")
    restored = []
    skills_dir = folder / "skills"
    for v, name in zip(versions, names, strict=True):
        target = skills_dir / name
        target.mkdir(parents=True, exist_ok=True)
        source = "snapshot"
        if v.get("git_state") == "committed" and v.get("git_repo"):
            archive = subprocess.run(["git", "-C", v["git_repo"], "archive", "--format=tar", v["git_commit"], v["git_path"]],
                                     capture_output=True, timeout=30)
            if archive.returncode == 0:
                depth = len(Path(v["git_path"]).parts)
                with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
                    for member in tar.getmembers():
                        parts = Path(member.name).parts[depth:]
                        if not parts or member.issym() or member.islnk() or ".." in parts:
                            continue
                        member.name = str(Path(*parts))
                        tar.extract(member, target, filter="data")
                source = f"commit {v['git_commit'][:7]}"
        if source == "snapshot":
            # the snapshot is SKILL.md without its frontmatter: give it back a name and a description
            description = f"The {name} skill, as an earlier run used it."
            (target / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n{v['body']}\n")
        restored.append({"skill": v["skill"], "pinned": f"{PINNED_PLUGIN}:{name}", "source": source})
    (folder / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (folder / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": PINNED_PLUGIN, "version": "1.0.0", "description": "Skills restored by Rerunly as an earlier run used them.",
    }))
    return restored


def background_argv(prompt: str, session_id: str, model: str, effort: str, version: str, permission: str,
                    budget: float | None, plugin_dir: Path | None, skills: list[dict]) -> list[str]:
    argv = ["npx", "-y", f"@anthropic-ai/claude-code@{version}"] if version else ["claude"]
    argv += ["-p", "--output-format", "stream-json", "--verbose", "--session-id", session_id,
             "--permission-prompts", "none", *(PERMISSIONS[permission] if permission != "same" else []),
             "--append-system-prompt", unattended_note(skills)]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    if budget:
        argv += ["--max-budget-usd", f"{budget:.2f}"]
    if plugin_dir:
        # Restricted runs read snapshots directly, without invoking skill preprocessing or scripts.
        argv += ["--plugin-dir" if permission == "same" else "--add-dir", str(plugin_dir)]
    return [*argv, "--", prompt]


def outcome(log: Path) -> dict | None:
    """How a rerun ended, from its log: None while it has no result yet."""
    result = None
    codex_answer = ""
    try:
        for line in log.read_text().splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if data.get("type") == "item.completed" and (data.get("item") or {}).get("type") == "agent_message":
                codex_answer = data["item"].get("text", "")
            elif data.get("type") in ("turn.completed", "turn.failed"):
                result = {"result": codex_answer or str(data.get("error") or ""),
                          "is_error": data["type"] == "turn.failed", "subtype": "success" if data["type"] == "turn.completed" else "error"}
            elif data.get("type") == "error":
                result = {"result": data.get("message", "Codex failed"), "is_error": True, "subtype": "error"}
            elif data.get("type") == "result":
                result = data
    except (OSError, ValueError):
        return None
    if result is None:
        return None
    text = (result.get("result") or "").strip()
    reported, _, rest = text.rpartition(STATUS_LINE)
    said = rest.strip().lower() if reported or text.startswith(STATUS_LINE) else ""
    answer = reported.strip() if said else text
    denials = [{"tool": d.get("tool_name"), "input": d.get("tool_input")} for d in result.get("permission_denials") or []]
    if result.get("is_error") or result.get("subtype") != "success":
        status = "failed"
    elif denials or said.startswith("blocked"):
        status = "needs_you"
    else:
        status = "done"
    return {"status": status, "result": answer, "denials": denials, "cost_usd": result.get("total_cost_usd"),
            "blocked": rest.strip()[len("blocked"):].lstrip(": ").strip() if said.startswith("blocked") else ""}


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # a finished child not reaped yet is a zombie: not running any more
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return True


def terminal_process(session_id: str) -> bool | None:
    """Whether this user's terminal wrapper of `session_id` is running; None when processes cannot be inspected."""
    proc = Path('/proc')
    if not proc.is_dir():
        return None
    uncertain = False
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            args = (entry / 'cmdline').read_bytes().split(b'\0')
            if session_id.encode() in args and any(arg.endswith(b'/rerunly/terminal.py') for arg in args):
                if alive(int(entry.name)):
                    return True
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            uncertain = True
    return None if uncertain else False


def start(cwd: str, argv: list[str], log: Path, on_exit, timeout: float | None = None) -> int:
    """Run `argv` in the background, its output to `log`; `on_exit()` once it ends. Returns its pid."""
    log.parent.mkdir(parents=True, exist_ok=True)
    out = log.open("w")
    try:
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    except BaseException:
        out.close()
        raise

    def wait():
        try:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # A process can exit between wait() timing out and signalling its group.
                import signal
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()
                out.write(json.dumps({"type": "error", "message": "Rerun exceeded its time limit."}) + "\n")
        finally:
            out.close()
            on_exit()

    threading.Thread(target=wait, daemon=True).start()
    return proc.pid


def remove_plugin(folder: Path | None) -> None:
    if folder and folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)
