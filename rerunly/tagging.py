"""Automatic tags and prompt similarity."""

import re

from .sources.base import PromptRecord

WORD_RE = re.compile(r"[a-z0-9_\-./]+")
STOPWORDS = set(
    "a an the and or of to in on for with by is are be it this that as at from into all any can "
    "do does i you we me my your our please make sure should would could will".split()
)


def model_family(model: str) -> str:
    return model.removeprefix("claude-")


def ai_tags(prompt: PromptRecord) -> set[str]:
    """Tags derived from what the agent did."""
    tags = set()
    if prompt.project:
        tags.add(f"project:{prompt.project}")
    if prompt.command:
        tags.add(f"cmd:{prompt.command}")
    for skill in prompt.skills:
        tags.add(f"skill:{skill}")
    for agent in prompt.agents:
        tags.add(f"agent:{agent.agent_type}")
        if agent.persona:
            tags.add(f"persona:{agent.persona}")
    for model in {c.model for c in prompt.calls}:
        tags.add(f"model:{model_family(model)}")
    return tags


def rule_matches(rule: dict, text: str) -> bool:
    """Keyword rules match case-insensitively on all (default) or any of their keywords;
    pattern rules are regular expressions."""
    if rule.get("keywords"):
        lowered = text.lower()
        hits = [k.lower() in lowered for k in rule["keywords"] if k.strip()]
        return bool(hits) and (any(hits) if rule.get("match") == "any" else all(hits))
    if rule.get("pattern"):
        try:
            return re.search(rule["pattern"], text, re.I) is not None
        except re.error:
            return False
    return False


def rule_scope(rule: dict) -> str:
    """"prompt" rules tag each matching prompt; "session" rules tag every prompt of a session whose
    title or prompts match."""
    return "session" if rule.get("scope") == "session" else "prompt"


def split_any(rule: dict) -> list[dict]:
    """A keyword rule matching "any" of several keywords, as one rule per keyword: they tag the same
    prompts, and each keyword can then be shown, changed or removed on its own."""
    keywords = rule.get("keywords") or []
    if rule.get("match") != "any" or len(keywords) < 2:
        return [rule]
    return [{**rule, "keywords": [k], "match": "all"} for k in keywords]


def rule_tags(text: str, rules: list[dict], scope: str = "prompt") -> set[str]:
    """Human tags given by the user's auto-tag rules of `scope` to a prompt's (or a session's) text."""
    return {r["tag"] for r in rules if r.get("tag") and rule_scope(r) == scope and rule_matches(r, text)}


def words(text: str) -> set[str]:
    return {w for w in WORD_RE.findall(text.lower()) if w not in STOPWORDS and len(w) > 1}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def similarity(text_a: str, tags_a: set[str], text_b: str, tags_b: set[str]) -> float:
    """Word overlap; running the same slash command counts as at least 0.5."""
    score = jaccard(words(text_a), words(text_b))
    if {t for t in tags_a if t.startswith("cmd:")} & tags_b:
        score = max(score, 0.5 + score / 2)
    return score
