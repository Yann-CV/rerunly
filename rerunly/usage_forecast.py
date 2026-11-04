"""Empirical, model-specific quota forecasts from local token/usage observations.

Account activity outside imported history cannot be separated from local activity.
Ranges describe observed variation, not a provider quota formula or confidence interval.
"""

import json
import math
from datetime import datetime, timedelta, timezone

MIN_SAMPLES = 3
MAX_INTERVAL = 3600
MAX_AGE_DAYS = 30


def _time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _scope(limits):
    return json.dumps([limits.get("plan"), bool(limits.get("live"))])


def _records(c, agent, observed):
    """Token totals of `agent`'s prompts and subagents (counted apart) that ended in the day before `observed`,
    excluded runs included."""
    records = {}
    cutoff = observed - timedelta(days=1)
    for table, key, join in (("prompts", "p.id", ""),
                             ("agents", "r.id", "JOIN prompts p ON p.id=r.prompt_id")):
        alias = "p" if table == "prompts" else "r"
        for row in c.execute(f"SELECT {key} id, {alias}.started_at, {alias}.ended_at, {alias}.models, "
                             f"{alias}.input_tokens, {alias}.output_tokens, {alias}.cache_read_tokens, "
                             f"{alias}.cache_write_tokens FROM {table} {alias} {join} WHERE p.harness=?", (agent,)):
            if not row["ended_at"] or not row["started_at"]:
                continue
            ended = _time(row["ended_at"])
            if not cutoff <= ended <= observed:
                continue
            models = json.loads(row["models"] or "{}")
            model = next(iter(models)) if len(models) == 1 else None
            tokens = sum(row[k] or 0 for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))
            records[f"{table}:{row['id']}"] = {"model": model, "tokens": tokens,
                                                   "started": row["started_at"], "ended": row["ended_at"]}
    return records


def observe(c, limits, now=None):
    """Record each new usage reading once; when a single model ran since the previous one, also record how many
    tokens moved each window by how many points."""
    now = now or datetime.now(timezone.utc)
    if not limits.get("observed_at") or not limits.get("windows"):
        return
    try:
        observed = _time(limits["observed_at"])
    except (TypeError, ValueError):
        return
    if not 0 <= (now - observed).total_seconds() <= MAX_INTERVAL:
        return
    agent, scope = limits["agent"], _scope(limits)
    previous = c.execute("SELECT * FROM usage_observations WHERE agent=?", (agent,)).fetchone()
    if previous and _time(previous["observed_at"]) >= observed:
        return
    records = _records(c, agent, observed)
    deltas = {}
    valid = previous is not None and previous["scope"] == scope
    if previous is not None and valid:
        start = _time(previous["observed_at"])
        valid = 0 < (observed - start).total_seconds() <= MAX_INTERVAL
        old = json.loads(previous["records"])
        # Reimports/deletions and late imports must not look like new consumption.
        if any(k not in records or records[k]["tokens"] < v["tokens"] for k, v in old.items()):
            valid = False
        for key, record in records.items():
            delta = record["tokens"] - old.get(key, {}).get("tokens", 0)
            if not delta:
                continue
            if not record["model"] or delta < 0 or (key not in old and _time(record["started"]) < start):
                valid = False
            deltas[record["model"]] = deltas.get(record["model"], 0) + delta
    with c:
        if previous is not None and valid and len(deltas) == 1:
            model, tokens = next(iter(deltas.items()))
            old_windows = {w["window_minutes"]: w for w in json.loads(previous["windows"])}
            for w in limits["windows"]:
                if not w.get("window_minutes"):
                    continue
                before = old_windows.get(w.get("window_minutes"))
                reset = w.get("resets_at")
                if not before or not reset or before.get("resets_at") != reset or reset <= observed.timestamp():
                    continue
                change = w["used_percent"] - before["used_percent"]
                if tokens > 0 and math.isfinite(change) and 0 <= change <= 100 and w["used_percent"] < 100:
                    c.execute("INSERT INTO usage_samples(agent,scope,model,window_minutes,observed_at,tokens,percentage) "
                              "VALUES(?,?,?,?,?,?,?)", (agent, scope, model, w["window_minutes"], observed.isoformat(), tokens, change))
        c.execute("INSERT OR REPLACE INTO usage_observations VALUES(?,?,?,?,?)",
                  (agent, observed.isoformat(), scope, json.dumps(limits["windows"]), json.dumps(records)))
        c.execute("DELETE FROM usage_samples WHERE observed_at < ?", ((now - timedelta(days=MAX_AGE_DAYS)).isoformat(),))


def forecast(c, limits, estimate, now=None):
    """How many points of each usage window `estimate` would take: "estimated" only once every model it uses
    has enough samples, "learning" otherwise."""
    now = now or datetime.now(timezone.utc)
    if not estimate or not limits:
        return {"status": "unavailable", "windows": []}
    models = estimate.get("tokens_by_model") or {}
    windows = []
    fresh = bool(limits.get("observed_at")) and 0 <= (now - _time(limits["observed_at"])).total_seconds() <= MAX_INTERVAL
    for w in limits["windows"]:
        item = {"window_minutes": w["window_minutes"], "status": "learning", "models": []}
        low, high, midpoint = 0, 0, 0
        ready = bool(models) and all(models)
        for model, tokens in models.items():
            samples = c.execute("SELECT tokens, percentage FROM usage_samples WHERE agent=? AND scope=? AND model=? "
                                "AND window_minutes=? AND observed_at>=?", (limits["agent"], _scope(limits), model,
                                w["window_minutes"], (now - timedelta(days=MAX_AGE_DAYS)).isoformat())).fetchall()
            calibration = {"model": model or "Unknown model", "samples": len(samples),
                           "observed_tokens": sum(s["tokens"] for s in samples),
                           "observed_percentage_points": sum(s["percentage"] for s in samples)}
            item["models"].append(calibration)
            if len(samples) < MIN_SAMPLES or sum(s["percentage"] for s in samples) < 3:
                ready = False
                continue
            calibration["tokens_per_percentage_point"] = calibration["observed_tokens"] / calibration["observed_percentage_points"]
            # One percentage point accommodates rounded reports; preserve observed spread.
            low += tokens * min(max(0, s["percentage"] - 1) / s["tokens"] for s in samples)
            high += tokens * max((s["percentage"] + 1) / s["tokens"] for s in samples)
            midpoint += tokens * sum(s["percentage"] for s in samples) / sum(s["tokens"] for s in samples)
        if ready:
            item.update(status="estimated", additional_percent=midpoint, additional_percent_low=low,
                        additional_percent_high=high, confidence="low", basis="observed_single_model_intervals")
            reset = w.get("resets_at")
            reset = _time(reset).timestamp() if isinstance(reset, str) else reset
            # Project from a recent report only within its still-active window.
            if fresh and reset and reset > now.timestamp() and not w.get("reset"):
                item.update(projected_percent=w["used_percent"] + midpoint,
                            projected_percent_low=w["used_percent"] + low,
                            projected_percent_high=w["used_percent"] + high)
                seconds = (reset - now.timestamp())
                item["may_cross_reset"] = bool(estimate.get("duration_ms") and estimate["duration_ms"] / 1000 >= seconds)
        windows.append(item)
    return {"status": "estimated" if windows and all(w["status"] == "estimated" for w in windows) else "learning", "windows": windows}
