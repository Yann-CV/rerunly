"""Rerun planning estimates from recorded usage and the configured model prices."""

import json

from .pricing import Usage, cost_usd, find_price


def estimate_rerun(prompt, agents, model, prices):
    """Tokens, cost and duration of rerunning `prompt` with `model`, from the original's usage: its token
    volume, its recorded cache usage and its subagents' models.

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
    main_unchanged = not model or set(models) == {target}
    for record, selected in [(prompt, target), *[(a, None) for a in agents]]:
        input_count = sum(record.get(k) or 0 for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens"))
        output_count = record.get("output_tokens") or 0
        search_count = record.get("web_search_requests") or 0
        if selected is None:
            observed = json.loads(record.get("models") or "{}")
            selected = next(iter(observed)) if len(observed) == 1 else ""
        # A fresh run can still reuse its cache between API calls. Charging all
        # historical cache hits as uncached input greatly overstates long runs.
        # Only aggregate cache writes are stored; use the standard 5-minute rate.
        usage = Usage(input_tokens=record.get("input_tokens") or 0,
                      output_tokens=output_count,
                      cache_read_tokens=record.get("cache_read_tokens") or 0,
                      cache_write_5m_tokens=record.get("cache_write_tokens") or 0,
                      web_search_requests=search_count)
        unchanged = record is not prompt or main_unchanged
        # Imported costs retain per-call details lost in aggregate token columns,
        # notably one-hour cache writes and fast mode. Reuse that baseline when
        # the model is unchanged rather than approximating those details again.
        recorded_cost = record.get("cost_usd")
        unpriced = json.loads(record.get("unpriced_models") or "[]")
        if (unchanged and recorded_cost is not None and not unpriced
                and all(find_price(m, prices) is not None
                        for m in json.loads(record.get("models") or "{}"))):
            costs.append(recorded_cost)
        else:
            costs.append(cost_usd(selected, usage, prices))
        if input_count + output_count:
            tokens_by_model[selected] = tokens_by_model.get(selected, 0) + input_count + output_count
        inputs += input_count
        outputs += output_count
        searches += search_count
    total_cost = sum(costs) if all(c is not None for c in costs) else None
    if main_unchanged and prompt.get("cost_source") == "claude-code":
        total_cost = prompt.get("total_cost_usd", total_cost)
    return {"model": target or None, "input_tokens": inputs, "output_tokens": outputs,
            "total_tokens": inputs + outputs, "web_search_requests": searches,
            "cost_usd": total_cost,
            "duration_ms": prompt.get("active_ms") or prompt.get("wall_ms"),
            "tokens_by_model": tokens_by_model, "basis": "original_usage_recorded_cache"}
