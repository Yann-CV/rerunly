# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportOptionalMemberAccess=false, reportOperatorIssue=false
# Transcripts are untyped JSON written by other programs: a missing field is handled where it matters.
"""Import Codex CLI/IDE rollout files; never read authentication or current skill files."""

import json
import re
from pathlib import Path

from ..pricing import Usage
from .base import AgentRecord, ApiCall, PromptRecord, SessionRecord, SkillRun
from .claude_code import _read_jsonl, extract_persona, project_name

REPLAY_RE = re.compile(r'<rerunly-replay id="([\w-]+)"/>\n?')
SKILL_RE = re.compile(r"<skill>\s*<name>(.*?)</name>\s*<path>(.*?)</path>\s*(.*?)</skill>", re.S)


def text_content(content):
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") in ("input_text", "output_text", "text"))


def usage_record(u):
    # Codex includes cached tokens in input, and reasoning tokens in output.
    cached = u.get("cached_input_tokens", 0) or 0
    return Usage(input_tokens=max(0, (u.get("input_tokens", 0) or 0) - cached),
                 cache_read_tokens=cached, output_tokens=u.get("output_tokens", 0) or 0)


class CodexSource:
    name = "codex"

    def __init__(self, sessions_dir):
        self.root = Path(sessions_dir).expanduser()

    def _files(self):
        return sorted(self.root.rglob("rollout-*.jsonl")) if self.root.is_dir() else []

    def rate_limits(self, newest: int = 5, tail: int = 256 * 1024):
        """The usage limits Codex reported last (each `token_count` event carries them), read from the end of the
        newest rollouts only: {"observed_at", "plan", "windows": [{"used_percent", "window_minutes", "resets_at"}]}."""
        def stamp(path):
            try:
                return path.stat().st_mtime
            except OSError:
                return 0
        for path in sorted(self._files(), key=stamp, reverse=True)[:newest]:
            try:
                with path.open("rb") as f:
                    f.seek(max(0, f.seek(0, 2) - tail))
                    lines = f.read().decode("utf-8", "replace").splitlines()
            except OSError:
                continue
            for raw in reversed(lines):
                if '"rate_limits"' not in raw:
                    continue
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue  # the first line of the tail can be cut
                limits = (d.get("payload") or {}).get("rate_limits") if d.get("type") == "event_msg" else None
                windows = [w for w in ((limits or {}).get(k) for k in ("primary", "secondary"))
                           if isinstance(w, dict) and w.get("used_percent") is not None]
                if windows:
                    return {"observed_at": d.get("timestamp"), "plan": limits.get("plan_type"),
                            "windows": [{"used_percent": w["used_percent"], "window_minutes": w.get("window_minutes"),
                                         "resets_at": w.get("resets_at")} for w in windows]}
        return None

    @staticmethod
    def _meta(path):
        # Metadata is the first rollout record; avoid reading whole child transcripts for discovery.
        try:
            with path.open(encoding="utf-8") as f:
                for _, raw in zip(range(10), f, strict=False):
                    try:
                        d = json.loads(raw)
                    except ValueError:
                        continue
                    if d.get("type") == "session_meta":
                        return d.get("payload") or {}
        except OSError:
            pass
        return {}

    @staticmethod
    def _parent(meta):
        source = meta.get("source")
        sub = source.get("subagent", {}) if isinstance(source, dict) else {}
        spawn = sub.get("thread_spawn", {}) if isinstance(sub, dict) else {}
        return spawn.get("parent_thread_id")

    def sessions(self):
        files = self._files()
        meta = {p: self._meta(p) for p in files}
        for p in files:
            if self._parent(meta[p]):
                continue
            sid = meta[p].get("id") or meta[p].get("session_id")
            related = [p] + [f for f in files if self._parent(meta[f]) == sid]
            signature = "codex-v2:" + "|".join(f"{f}:{f.stat().st_size}:{f.stat().st_mtime_ns}" for f in related)
            yield str(p), signature

    def load(self, session_key):
        path = Path(session_key)
        if not path.is_file():
            return None
        lines = _read_jsonl(path)
        meta = next((d.get("payload") or {} for d in lines if d.get("type") == "session_meta"), {})
        native_id = meta.get("id") or meta.get("session_id") or path.stem
        session = SessionRecord(id="codex:" + native_id, source=self.name, project="", path=str(path), signature="")
        current = None
        context = {}
        turn_id = None
        current_turn = None
        previous_total = {}
        seen_responses = set()
        seen_tools = set()
        # New rollouts write both token_usage_record and token_count. Prefer the response-id records.
        detailed_usage = any(d.get("type") == "token_usage_record" for d in lines)
        has_user_events = any(d.get("type") == "event_msg" and (d.get("payload") or {}).get("type") == "user_message" for d in lines)
        child_owners = {}
        spawn_calls = {}

        def start(text, ts, ident=None):
            nonlocal current, current_turn
            # Only the launcher prefix on the first user prompt identifies a rerun.
            # A quoted marker in a review/debugging conversation must not hijack its session.
            marker = REPLAY_RE.match(text) if not session.prompts else None
            if marker:
                session.id = marker.group(1)
                text = text[marker.end():]
            current_turn = turn_id
            cwd = context.get("cwd") or meta.get("cwd", "")
            current = PromptRecord(id="codex:" + str(ident or turn_id or f"{native_id}:{len(session.prompts)}"),
                session_id=session.id, text=text, started_at=ts, ended_at=ts, cwd=cwd,
                project=project_name(cwd), git_branch=(meta.get("git") or {}).get("branch", ""),
                harness="Codex", harness_version=meta.get("cli_version", ""),
                requested_model=context.get("model", ""), effort=context.get("effort") or context.get("reasoning_effort") or "",
                permissions={k: context[k] for k in ("approval_policy", "sandbox_policy") if k in context})
            session.prompts.append(current)
            session.project = session.project or current.project

        for d in lines:
            kind, q, ts = d.get("type"), d.get("payload") or {}, d.get("timestamp")
            t = q.get("type")
            if kind == "turn_context":
                context = q
                turn_id = q.get("turn_id") or turn_id
                if current and (not q.get("turn_id") or q.get("turn_id") == current_turn):
                    current.requested_model = q.get("model") or current.requested_model
                    current.effort = q.get("effort") or q.get("reasoning_effort") or current.effort
                    current.permissions.update({k: q[k] for k in ("approval_policy", "sandbox_policy") if k in q})
                    if q.get("cwd"):
                        current.cwd = q["cwd"]
                        current.project = project_name(current.cwd)
                continue
            if kind == "event_msg" and t in ("task_started", "turn_started"):
                turn_id = q.get("turn_id") or turn_id
            user_text = None
            if kind == "event_msg" and t == "user_message":
                user_text = q.get("message", "")
            elif kind == "response_item" and t == "message" and q.get("role") == "user":
                text = text_content(q.get("content"))
                # Injected context and skill bodies are not human prompts.
                runs = list(SKILL_RE.finditer(text))
                if runs and current:
                    for m in runs:
                        current.skills.add(m[1])
                        current.skill_runs.append(SkillRun(m[1], str(Path(m[2]).parent), m[3].strip()))
                elif not has_user_events and not text.lstrip().startswith(("# AGENTS.md", "<environment_context>", "<permissions instructions>", "<turn_aborted>")):
                    user_text = text
            if user_text is not None:
                start(user_text, ts, q.get("id"))
                continue
            if not current:
                continue
            if ts:
                current.ended_at = max(current.ended_at or ts, ts)
            if kind == "token_usage_record":
                rid = q.get("response_id")
                if rid and rid in seen_responses:
                    continue
                if rid:
                    seen_responses.add(rid)
                current.calls.append(ApiCall(current.requested_model, usage_record(q.get("usage") or {})))
            elif kind == "event_msg" and t == "token_count" and not detailed_usage:
                info = q.get("info") or {}
                total = info.get("total_token_usage")
                if total is not None:
                    delta = {k: max(0, v - previous_total.get(k, 0)) for k, v in total.items() if isinstance(v, int)}
                    previous_total = total
                    if any(delta.values()):
                        current.calls.append(ApiCall(current.requested_model, usage_record(delta)))
            elif kind == "event_msg" and t in ("task_complete", "turn_complete", "task_completed"):
                current.answer = q.get("last_agent_message") or current.answer
                from ..ingest import _ms_between
                current.active_ms = _ms_between(current.started_at, ts) or 0
            elif kind == "event_msg" and t == "agent_message":
                current.answer = q.get("message") or current.answer
            elif kind == "response_item":
                if t == "message" and q.get("role") == "assistant":
                    current.answer = text_content(q.get("content")) or current.answer
                elif t in ("function_call", "custom_tool_call", "web_search_call"):
                    ident = q.get("call_id") or q.get("id")
                    if ident and ident in seen_tools:
                        continue
                    if ident:
                        seen_tools.add(ident)
                    name = q.get("name") or t
                    current.tools[name] += 1
                    raw = q.get("arguments") or q.get("input") or ""
                    if name.endswith("spawn_agent"):
                        spawn_calls[ident] = current
                    if name.endswith("apply_patch"):
                        for file in re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", str(raw), re.M):
                            current.files.add(file)
                elif t in ("function_call_output", "custom_tool_call_output") and q.get("call_id") in spawn_calls:
                    try:
                        out = json.loads(q.get("output", ""))
                        child = out.get("agent_id")
                        if child:
                            child_owners[child] = spawn_calls[q["call_id"]]
                    except (ValueError, TypeError):
                        pass
        # Child sessions are attributed only when the parent's spawn output identifies them.
        for child in self._files():
            cm = self._meta(child)
            cid = cm.get("id") or cm.get("session_id")
            if self._parent(cm) != native_id or cid not in child_owners:
                continue
            loaded = self.load(str(child))
            owner = child_owners[cid]
            brief = loaded.prompts[0].text if loaded and loaded.prompts else ""
            agent = AgentRecord(id="codex:" + cid, prompt_id=owner.id, agent_type="subagent", description=brief[:200], persona=extract_persona(brief))
            for prompt in loaded.prompts if loaded else []:
                agent.calls.extend(prompt.calls)
                agent.tools.update(prompt.tools)
                agent.started_at = agent.started_at or prompt.started_at
                agent.ended_at = prompt.ended_at
            owner.agents.append(agent)
        return session
