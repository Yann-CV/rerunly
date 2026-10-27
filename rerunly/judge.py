"""An LLM as judge: it rates how much a rerun's output differs from the run it is compared with.

The rating tells the user whether to read the differences or trust the rerun: a score (100 = same
substance, wording aside), a verdict, a quality call, and a few points. It runs `claude -p` in an empty
scratch folder (nothing of the user's to act on), with no session saved, nobody to approve anything, and a
budget cap; Codex judges use a read-only sandbox and a time limit.
"""

import json
import re
import subprocess
from pathlib import Path

from .replay import EFFORTS

MAX_ANSWER = 30_000
VERDICTS = ("equivalent", "minor", "major")
QUALITIES = ("better", "same", "worse", "unclear")

INSTRUCTIONS = """You compare two outputs an AI coding agent produced for the same request: the REFERENCE run and
the RERUN. Judge whether the rerun can be trusted without a human reading it, not whether the wording matches.

Reply with JSON only, no other text:
{"score": <0-100: how close the rerun is to the reference in substance (conclusions, findings, changes made,
  answers given); 100 = the same substance in other words, 0 = unrelated or contradicting>,
 "verdict": "equivalent" | "minor" | "major",
 "quality": "better" | "same" | "worse" | "unclear",
 "look": <true if a human should read the differences before relying on the rerun, else false>,
 "summary": "<one sentence a busy reader can act on>",
 "differences": ["<up to 5 short points, most important first; empty if equivalent>"]}"""


def _clip(text: str) -> str:
    text = text or ""
    return text if len(text) <= MAX_ANSWER else text[:MAX_ANSWER] + "\n[… cut]"


def rating_prompt(request: str, reference: dict, rerun: dict) -> str:
    def run(label: str, r: dict) -> str:
        files = ", ".join(r.get("files") or []) or "none"
        return f"## {label}\nFiles it edited: {files}\nFinal answer:\n{_clip(r.get('answer') or '(no answer)')}\n"
    return f"{INSTRUCTIONS}\n\n## REQUEST\n{_clip(request)}\n\n{run('REFERENCE', reference)}\n{run('RERUN', rerun)}"


def parse_rating(text: str) -> dict:
    """The rating in the judge's reply; ValueError when there is none."""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        raise ValueError("no rating in the reply")
    data = json.loads(match.group(0))
    score = max(0, min(100, int(round(float(data.get("score"))))))
    verdict = data.get("verdict") if data.get("verdict") in VERDICTS else ("equivalent" if score >= 85 else "minor" if score >= 60 else "major")
    quality = data.get("quality") if data.get("quality") in QUALITIES else "unclear"
    look = bool(data.get("look")) if "look" in data else verdict == "major"
    differences = [str(d) for d in (data.get("differences") or [])][:5]
    return {"score": score, "verdict": verdict, "quality": quality, "look": look,
            "summary": str(data.get("summary") or "").strip(), "differences": differences}


def judge_agent(model: str) -> tuple[str, str]:
    """Which CLI judges with `model` ("claude" or "codex"), and the model id to give it."""
    if model.startswith("codex:"):
        return "codex", model.removeprefix("codex:")
    if model.startswith(("gpt-", "o3", "o4")) and not model.startswith("gpt-oss"):
        return "codex", model
    return "claude", model


def rate(request: str, reference: dict, rerun: dict, model: str, workdir: Path, budget: float = 0.5, effort: str = "") -> dict:
    """Ask an LLM for the rating, at `effort` ("" = the model's default): the parsed rating, plus its cost when
    reported by the harness."""
    effort = effort if effort in EFFORTS else ""
    workdir.mkdir(parents=True, exist_ok=True)
    prompt = rating_prompt(request, reference, rerun)
    agent, model = judge_agent(model)
    if agent == "codex":
        argv = ["codex", "exec", "--json", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
                "-c", 'approval_policy="never"']
        if model:
            argv += ["--model", model]
        if effort:
            argv += ["-c", "model_reasoning_effort=" + json.dumps(effort)]
        out = subprocess.run([*argv, prompt], cwd=workdir, capture_output=True, text=True, timeout=600)
        answer = ""
        completed = False
        for line in out.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            item = event.get("item") or {}
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                answer = item.get("text", "")
            completed |= event.get("type") == "turn.completed"
        if out.returncode or not completed:
            raise ValueError((out.stderr or out.stdout or "Codex gave no answer")[-300:])
        return {**parse_rating(answer), "cost_usd": None}
    argv = ["claude", "-p", "--output-format", "json", "--no-session-persistence", "--permission-prompts", "none",
            "--disable-slash-commands", "--max-budget-usd", f"{budget:.2f}"]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    out = subprocess.run(argv, input=prompt, cwd=workdir, capture_output=True,
                         text=True, timeout=600)
    try:
        result = json.loads(out.stdout)
    except ValueError:
        raise ValueError((out.stderr or out.stdout or "Claude Code gave no answer").strip()[-300:]) from None
    if result.get("is_error"):
        raise ValueError(str(result.get("result") or result.get("subtype") or "the rating failed")[:300])
    return {**parse_rating(result.get("result") or ""), "cost_usd": result.get("total_cost_usd")}
