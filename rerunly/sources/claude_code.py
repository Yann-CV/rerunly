# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportOptionalMemberAccess=false, reportOperatorIssue=false
# Transcripts are untyped JSON written by other programs: a missing field is handled where it matters.
"""Read Claude Code transcripts from ~/.claude/projects.

Layout:
    <projects>/<project-slug>/<session-id>.jsonl                       main transcript
    <projects>/<project-slug>/<session-id>/subagents/agent-<id>.jsonl  one per subagent
    <projects>/<project-slug>/<session-id>/subagents/agent-<id>.meta.json

A prompt starts at a user line whose `origin.kind` is "human" (typed text or a
slash command). Everything until the next such line (assistant calls, tool
results, background task notifications) is attributed to it. Subagent
transcripts carry the `promptId` of the prompt that spawned them.
"""

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from ..pricing import Usage
from .base import AgentRecord, ApiCall, PromptRecord, SessionRecord, SkillRun

COMMAND_RE = re.compile(r"<command-name>/?([^<]+)</command-name>")
COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
NON_PROMPT_PREFIXES = ("<local-command", "<task-notification", "<system-reminder", "<command-message>", "Caveat:")
AGENT_TOOLS = {"Agent", "Task"}
# How skills brief a subagent with a persona, checked in this order on the start of its prompt.
PERSONA_RES = [
    re.compile(r"<persona>\s*(.+?)\s*</persona>", re.I | re.S),
    re.compile(r"^\s*(?:persona|role)\s*[:=]\s*(.+)$", re.I | re.M),
    re.compile(r"\b(?:you are|act as|acting as|you will act as|play the role of)\s+(?:an?\s+|the\s+)?([^.,;:!?\n]{3,80})", re.I),
]


def extract_persona(text: str) -> str | None:
    head = text[:1500]
    for pattern in PERSONA_RES:
        m = pattern.search(head)
        if m:
            persona = re.sub(r"\s+", " ", m.group(1)).strip().strip("*_`\"'").lower()
            return persona[:60] or None
    return None


def _read_jsonl(path: Path) -> list[dict]:
    lines = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                lines.append(json.loads(raw))
            except json.JSONDecodeError:
                continue  # file being written, or a truncated line
    return lines


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _parse_usage(u: dict) -> Usage:
    cc = u.get("cache_creation") or {}
    w5 = cc.get("ephemeral_5m_input_tokens")
    w1 = cc.get("ephemeral_1h_input_tokens")
    if w5 is None and w1 is None:
        w5, w1 = u.get("cache_creation_input_tokens", 0), 0
    return Usage(
        input_tokens=u.get("input_tokens") or 0,
        output_tokens=u.get("output_tokens") or 0,
        cache_read_tokens=u.get("cache_read_input_tokens") or 0,
        cache_write_5m_tokens=w5 or 0,
        cache_write_1h_tokens=w1 or 0,
        web_search_requests=(u.get("server_tool_use") or {}).get("web_search_requests") or 0,
    )


@lru_cache(maxsize=None)
def project_name(cwd: str) -> str:
    """Name of the git repository containing `cwd` (worktrees map to their main repo),
    so working from a sub-folder does not create a new project."""
    if not cwd:
        return ""
    path = Path(cwd.split("/.claude/worktrees/")[0])
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            return parent.name
    return path.name


def _prompt_text(text: str) -> tuple[str, str | None]:
    """Return (display text, slash command name or None)."""
    m = COMMAND_RE.search(text)
    if not m:
        return text.strip(), None
    name = m.group(1).strip()
    args = COMMAND_ARGS_RE.search(text)
    args_text = args.group(1).strip() if args else ""
    return f"/{name} {args_text}".strip(), name


class _CallCollector:
    """Assistant messages are streamed one content block per line, each line repeating
    the message usage: keep one usage per message id."""

    def __init__(self):
        self.by_id: dict[str, tuple[object, ApiCall]] = {}

    def add(self, owner, d: dict) -> None:
        msg = d.get("message") or {}
        model = msg.get("model") or ""
        usage = msg.get("usage")
        if not usage or model == "<synthetic>":
            return
        key = msg.get("id") or d.get("requestId") or d.get("uuid")
        call = ApiCall(model=model, usage=_parse_usage(usage), fast=usage.get("speed") == "fast")
        if key in self.by_id:
            first_owner, _ = self.by_id[key]
            self.by_id[key] = (first_owner, call)  # later lines hold the final usage
        else:
            self.by_id[key] = (owner, call)

    def flush(self) -> None:
        for owner, call in self.by_id.values():
            owner.calls.append(call)


SKILL_HEADER = "Base directory for this skill: "
# tools changing files: their paths tell what a run produced
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def _skill_run(name: str, text: str) -> SkillRun | None:
    """The skill's instructions Claude Code injects after a Skill call: its folder on the first line,
    then SKILL.md without its frontmatter, then the arguments it was called with."""
    if not text.startswith(SKILL_HEADER):
        return None
    base_dir, _, body = text[len(SKILL_HEADER):].partition("\n")
    body, sep, args = body.rpartition("\n\nARGUMENTS: ")
    if not sep:
        body, args = args, None
    return SkillRun(name=name, base_dir=base_dir.strip(), body=body.strip(), args=args)


def _count_tools(owner, msg: dict) -> list[dict]:
    """Count the message's tool calls on `owner` and return them."""
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
    for block in uses:
        owner.tools[block.get("name", "?")] += 1
    return uses


def _max_ts(a: str | None, b: str | None) -> str | None:
    if not a:
        return b
    if not b:
        return a
    return max(a, b)


class ClaudeCodeSource:
    name = "claude-code"

    def __init__(self, projects_dir: str | Path):
        self.root = Path(projects_dir).expanduser()

    def _session_files(self, main: Path) -> list[Path]:
        files = [main]
        sub = main.with_suffix("") / "subagents"
        if sub.is_dir():
            files += sorted(sub.glob("*.jsonl"))
        return files

    def sessions(self) -> Iterable[tuple[str, str]]:
        if not self.root.is_dir():
            return
        for main in self.root.glob("*/*.jsonl"):
            sig = []
            for f in self._session_files(main):
                st = f.stat()
                sig.append(f"{f.name}:{st.st_size}:{int(st.st_mtime)}")
            yield str(main), "|".join(sig)

    def load(self, session_key: str) -> SessionRecord | None:
        main = Path(session_key)
        if not main.exists():
            return None
        files = self._session_files(main)
        lines = _read_jsonl(main)
        session = SessionRecord(
            id=main.stem,
            source=self.name,
            project="",
            path=str(main),
            signature="",
        )
        has_origin = any("origin" in d for d in lines if d.get("type") == "user")
        prompts: list[PromptRecord] = []
        current: PromptRecord | None = None
        sidechain: dict[str, AgentRecord] = {}
        calls = _CallCollector()
        active_skill: str | None = None
        spawned_by_skill: dict[str, str | None] = {}  # Agent tool_use id -> skill active then
        skill_uses: dict[str, str] = {}  # Skill tool_use id -> skill name, until its instructions are loaded

        for d in lines:
            t = d.get("type")
            if t == "ai-title":
                session.title = d.get("aiTitle") or session.title
                continue
            if t == "cost-state":
                session.reported_cost_usd = d.get("totalCostUSD")
                continue
            if t == "user" and not d.get("isSidechain") and self._is_prompt(d, has_origin):
                text, command = _prompt_text(_content_text(d["message"].get("content")))
                cwd = d.get("cwd", "")
                current = PromptRecord(
                    id=d.get("promptId") or d.get("uuid"),
                    session_id=session.id,
                    text=text,
                    command=command,
                    started_at=d.get("timestamp"),
                    ended_at=d.get("timestamp"),
                    cwd=cwd,
                    project=project_name(cwd),
                    git_branch=d.get("gitBranch", ""),
                    harness="Claude Code",
                    harness_version=d.get("version", ""),
                    permissions={"permission_mode": d["permissionMode"]} if d.get("permissionMode") else {},
                )
                prompts.append(current)
                session.project = session.project or current.project
                active_skill = f"/{command}" if command else None
                continue
            if current is None:
                continue
            ts = d.get("timestamp")
            if d.get("isSidechain"):
                # Older Claude Code versions kept subagent messages in the main file.
                agent = sidechain.get(current.id)
                if agent is None:
                    agent = sidechain[current.id] = AgentRecord(
                        id=f"{session.id}:sidechain:{current.id}",
                        prompt_id=current.id,
                        agent_type="sidechain",
                        description="",
                        started_at=ts,
                    )
                    current.agents.append(agent)
                agent.ended_at = _max_ts(agent.ended_at, ts)
                if t == "assistant":
                    calls.add(agent, d)
                    _count_tools(agent, d.get("message") or {})
                continue
            current.ended_at = _max_ts(current.ended_at, ts)
            if t == "user" and d.get("isMeta") and d.get("sourceToolUseID") in skill_uses:
                run = _skill_run(skill_uses.pop(d["sourceToolUseID"]), _content_text(d["message"].get("content")))
                if run:
                    current.skill_runs.append(run)
            if t == "assistant":
                text = "\n".join(b.get("text", "") for b in (d.get("message") or {}).get("content") or []
                                 if isinstance(b, dict) and b.get("type") == "text").strip()
                if text:
                    current.answer = text  # the last one is the answer
                current.harness_version = current.harness_version or d.get("version", "")
                current.effort = current.effort or d.get("effort") or ""
                current.requested_model = current.requested_model or d.get("requestedModel") or ""
                calls.add(current, d)
                for use in _count_tools(current, d.get("message") or {}):
                    if use.get("name") == "Skill" and (use.get("input") or {}).get("skill"):
                        active_skill = use["input"]["skill"]
                        current.skills.add(active_skill)
                        skill_uses[use.get("id")] = active_skill
                    elif use.get("name") in AGENT_TOOLS:
                        spawned_by_skill[use.get("id")] = active_skill
                    elif use.get("name") in EDIT_TOOLS:
                        path = (use.get("input") or {}).get("file_path") or (use.get("input") or {}).get("notebook_path")
                        if path:
                            current.files.add(path)
            elif t == "system" and d.get("subtype") == "turn_duration":
                current.active_ms += d.get("durationMs") or 0

        by_id = {p.id: p for p in prompts}
        for f in files[1:]:
            agent = self._load_agent(f, session.id, calls, spawned_by_skill)
            if agent and agent.prompt_id in by_id:
                by_id[agent.prompt_id].agents.append(agent)

        calls.flush()
        # Local slash commands (/plugin, /clear...) never reach the model.
        session.prompts = [p for p in prompts if p.calls or p.agents]
        return session

    @staticmethod
    def _is_prompt(d: dict, has_origin: bool) -> bool:
        if has_origin:
            return (d.get("origin") or {}).get("kind") == "human"
        # Fallback for transcripts written before `origin` existed.
        if d.get("isMeta") or "toolUseResult" in d:
            return False
        content = (d.get("message") or {}).get("content")
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return False
        text = _content_text(content).lstrip()
        if not text:
            return False
        return "<command-name>" in text or not text.startswith(NON_PROMPT_PREFIXES)

    @staticmethod
    def _load_agent(
        path: Path, session_id: str, calls: _CallCollector, spawned_by_skill: dict[str, str | None]
    ) -> AgentRecord | None:
        lines = _read_jsonl(path)
        if not lines:
            return None
        meta_path = path.with_suffix(".meta.json")
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
            except json.JSONDecodeError:
                pass
        agent_id = next((d["agentId"] for d in lines if d.get("agentId")), path.stem.removeprefix("agent-"))
        prompt_id = next((d["promptId"] for d in lines if d.get("promptId")), None)
        first_user = next((d for d in lines if d.get("type") == "user"), None)
        brief = _content_text(first_user["message"].get("content")) if first_user else ""
        agent = AgentRecord(
            id=f"{session_id}:{agent_id}",
            prompt_id=prompt_id,
            agent_type=meta.get("agentType") or "general-purpose",
            description=meta.get("description") or brief[:200],
            persona=extract_persona(brief),
            skill=spawned_by_skill.get(meta.get("toolUseId")),
        )
        for d in lines:
            ts = d.get("timestamp")
            if ts:
                agent.started_at = agent.started_at or ts
                agent.ended_at = _max_ts(agent.ended_at, ts)
            if d.get("type") == "assistant":
                calls.add(agent, d)
                _count_tools(agent, d.get("message") or {})
        return agent
