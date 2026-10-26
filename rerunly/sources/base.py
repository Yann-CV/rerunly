"""Harness-independent records produced by every source (Claude Code, Codex and other adapters)."""

from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Protocol

from ..pricing import Usage


@dataclass
class ApiCall:
    model: str
    usage: Usage
    fast: bool = False


@dataclass
class AgentRecord:
    """A subagent run spawned while handling a prompt."""

    id: str
    prompt_id: str | None
    agent_type: str
    description: str
    # role the subagent was briefed to play ("security reviewer"), from the start of its prompt
    persona: str | None = None
    # skill (or slash command, as "/name") active in the parent when the subagent was spawned
    skill: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    calls: list[ApiCall] = field(default_factory=list)
    tools: Counter = field(default_factory=Counter)


@dataclass
class SkillRun:
    """A skill as the agent loaded it during a prompt: enough to know, and restore, the exact version."""

    name: str
    base_dir: str = ""
    # the skill's instructions as loaded (SKILL.md without its frontmatter): an exact snapshot
    body: str = ""
    args: str | None = None


@dataclass
class PromptRecord:
    """One user prompt and everything the main agent did to answer it."""

    id: str
    session_id: str
    text: str
    started_at: str
    cwd: str = ""
    project: str = ""
    git_branch: str = ""
    command: str | None = None
    ended_at: str | None = None
    active_ms: int = 0
    calls: list[ApiCall] = field(default_factory=list)
    tools: Counter = field(default_factory=Counter)
    skills: set[str] = field(default_factory=set)
    agents: list[AgentRecord] = field(default_factory=list)
    # what ran it, to run it again: the harness and its version, the reasoning effort, the model asked for
    harness: str = ""
    harness_version: str = ""
    effort: str = ""
    requested_model: str = ""
    skill_runs: list[SkillRun] = field(default_factory=list)
    # what it produced, to compare runs: the main agent's last message, and the files it edited
    answer: str = ""
    files: set[str] = field(default_factory=set)


@dataclass
class SessionRecord:
    id: str
    source: str
    project: str
    path: str
    signature: str
    title: str = ""
    reported_cost_usd: float | None = None
    prompts: list[PromptRecord] = field(default_factory=list)


class Source(Protocol):
    name: str

    def sessions(self) -> Iterable[tuple[str, str]]:
        """Yield (session_key, change_signature) for every session available."""

    def load(self, session_key: str) -> SessionRecord | None:
        """Parse one session."""
