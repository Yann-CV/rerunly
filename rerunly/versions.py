"""Where a skill version comes from, so a run can be repeated with the same skill.

The transcript holds the exact instructions the agent loaded (the snapshot). This module ties that
snapshot to something restorable: the git commit whose SKILL.md has those instructions, the version
of the plugin shipping the skill, or the harness release bundling it.
"""

import hashlib
import json
import re
import subprocess
from pathlib import Path

# Claude Code unpacks its built-in skills under a folder named after its version
BUNDLED_RE = re.compile(r"/bundled-skills/(\d[\w.\-]*)/")
FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.S)
GIT_HISTORY_DEPTH = 200


def body_hash(body: str) -> str:
    return hashlib.sha256(body.strip().encode()).hexdigest()


def skill_body(markdown: str) -> str:
    """SKILL.md as the agent loads it: without its frontmatter."""
    return FRONTMATTER_RE.sub("", markdown, count=1).strip()


def _git(repo: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def _plugin(base: Path) -> tuple[str | None, str | None]:
    """Name and version of the plugin shipping the skill: the nearest `.claude-plugin/plugin.json` above it."""
    for folder in [base, *base.parents][:6]:
        manifest = folder / ".claude-plugin" / "plugin.json"
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text())
            except (OSError, ValueError):
                return None, None
            return data.get("name"), data.get("version")
    return None, None


def resolve(base_dir: str, body: str, at: str | None = None) -> dict:
    """Origin of a skill version used at time `at`. `git_state`: "committed" (SKILL.md had these
    instructions at `git_commit`: the newest such commit made before `at`, else the newest one),
    "uncommitted" (only the working copy has them), "changed" (neither has them any more), or None
    outside git."""
    out = {"origin": "folder", "git_repo": None, "git_path": None, "git_commit": None, "git_state": None,
           "plugin": None, "plugin_version": None, "bundled_version": None}
    bundled = BUNDLED_RE.search(base_dir + "/")
    if bundled:
        return {**out, "origin": "bundled", "bundled_version": bundled.group(1)}
    base = Path(base_dir)
    out["plugin"], out["plugin_version"] = _plugin(base)
    if not base.is_dir():
        return {**out, "origin": "missing"}
    top = _git(base, "rev-parse", "--show-toplevel")
    if not top:
        return out
    repo = Path(top.strip()).resolve()
    try:  # git reports the real path: compare real paths (the folder may sit behind a symlink)
        path = base.resolve().relative_to(repo).as_posix()
    except ValueError:
        return out
    rel = f"{path}/SKILL.md" if path != "." else "SKILL.md"
    out.update(origin="git", git_repo=str(repo), git_path=path)
    wanted = body.strip()
    for before in ([f"--before={at}"] if at else []) + [None]:
        args = ["log", f"-n{GIT_HISTORY_DEPTH}", "--format=%H", *([before] if before else []), "--", rel]
        for commit in (_git(repo, *args) or "").split():
            shown = _git(repo, "show", f"{commit}:{rel}")
            if shown is not None and skill_body(shown) == wanted:
                return {**out, "git_commit": commit, "git_state": "committed"}
    try:
        current = skill_body((base / "SKILL.md").read_text())
    except OSError:
        current = None
    return {**out, "git_state": "uncommitted" if current == wanted else "changed"}
