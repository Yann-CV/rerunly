"""Receive Claude Code's OpenTelemetry log events (OTLP over HTTP, JSON encoding).

Only `claude_code.api_request` events are kept: each carries Claude Code's own cost for one
API call, plus the `prompt.id` linking it to the prompt being answered."""

import hashlib
import json
import sqlite3


def _value(v: dict):
    for key in ("stringValue", "boolValue", "doubleValue"):
        if key in v:
            return v[key]
    if "intValue" in v:
        return int(v["intValue"])  # int64 values are encoded as strings in OTLP JSON
    return None


def _attributes(items: list[dict] | None) -> dict:
    return {a["key"]: _value(a.get("value") or {}) for a in items or []}


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def api_requests(payload: dict) -> list[tuple]:
    """Rows for the `otel_requests` table, from an OTLP ExportLogsServiceRequest."""
    rows = []
    for resource_logs in payload.get("resourceLogs") or []:
        resource = _attributes((resource_logs.get("resource") or {}).get("attributes"))
        for scope_logs in resource_logs.get("scopeLogs") or []:
            for record in scope_logs.get("logRecords") or []:
                attrs = {**resource, **_attributes(record.get("attributes"))}
                body = _value(record.get("body") or {})
                if attrs.get("event.name") not in ("api_request", "claude_code.api_request") and body != "claude_code.api_request":
                    continue
                cost = _float(attrs.get("cost_usd"))
                if cost is None and attrs.get("cost_usd_micros") is not None:
                    cost = _int(attrs["cost_usd_micros"]) / 1_000_000
                request_id = attrs.get("request_id") or attrs.get("client_request_id") or hashlib.sha1(
                    json.dumps(record, sort_keys=True).encode(), usedforsecurity=False
                ).hexdigest()
                rows.append((
                    request_id,
                    attrs.get("prompt.id"),
                    attrs.get("session.id"),
                    attrs.get("event.timestamp"),
                    attrs.get("model"),
                    attrs.get("query_source"),
                    attrs.get("agent.name"),
                    cost,
                    _int(attrs.get("duration_ms")),
                    _int(attrs.get("input_tokens")),
                    _int(attrs.get("output_tokens")),
                    _int(attrs.get("cache_read_tokens")),
                    _int(attrs.get("cache_creation_tokens")),
                ))
    return rows


def store(conn: sqlite3.Connection, payload: dict) -> int:
    rows = api_requests(payload)
    with conn:
        conn.executemany(f"INSERT OR REPLACE INTO otel_requests VALUES ({','.join('?' * 13)})", rows)
    return len(rows)


def telemetry_env(port: int) -> dict[str, str]:
    """Claude Code settings `env` entries sending log events to this dashboard."""
    return {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL": "http/json",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": f"http://127.0.0.1:{port}/v1/logs",
    }
