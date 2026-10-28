"""Rerun planning estimates from recorded usage and the configured model prices."""

import json

from .pricing import Usage, cost_usd


def estimate_rerun(prompt, agents, model, prices):
    """Reuse original token volume; assume a cold cache and unchanged subagent models.

    This is an API-price estimate, not a subscription charge or a quota forecast.
    Effort, tools, cache behavior and the work itself may change on the rerun.
    """
    if prompt is None:
        return None
    models = json.loads(prompt.get("models") or "{}")
    target = (model or prompt.get("requested_model") or (next(iter(models)) if len(models) == 1 else "")).removeprefix("codex:")
    inputs, outputs, searches = 0, 0, 0
    costs = []
    tokens_by_model = {}
    for record, selected in [(prompt, target), *[(a, None) for a in agents]]:
        input_count = sum(record.get(k) or 0 for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens"))
        output_count = record.get("output_tokens") or 0
        search_count = record.get("web_search_requests") or 0
        if selected is None:
            observed = json.loads(record.get("models") or "{}")
            selected = next(iter(observed)) if len(observed) == 1 else ""
        costs.append(cost_usd(selected, Usage(input_tokens=input_count, output_tokens=output_count,
                                              web_search_requests=search_count), prices))
        if input_count + output_count:
            tokens_by_model[selected] = tokens_by_model.get(selected, 0) + input_count + output_count
        inputs += input_count
        outputs += output_count
        searches += search_count
    return {"model": target or None, "input_tokens": inputs, "output_tokens": outputs,
            "total_tokens": inputs + outputs, "web_search_requests": searches,
            "cost_usd": sum(costs) if all(c is not None for c in costs) else None,
            "duration_ms": prompt.get("active_ms") or prompt.get("wall_ms"),
            "tokens_by_model": tokens_by_model, "basis": "original_usage_cold_cache"}
