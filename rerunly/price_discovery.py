"""Discover standard token estimates from public provider pricing tables, without sending user data."""

import json
import math
import re
import time
import urllib.request
from html.parser import HTMLParser

from . import db
from .pricing import DEFAULT_PRICES, find_price

SOURCES = {
    "openai": "https://developers.openai.com/api/docs/pricing",
    "anthropic": "https://platform.claude.com/docs/en/about-claude/pricing",
}
SUCCESS_TTL = 86400
FAILURE_TTL = 3600


class PricingTables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = []
        self.rows = None
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.rows = []
        elif tag == "tr" and self.rows is not None:
            self.row = []
        elif tag in ("th", "td") and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("th", "td") and self.cell is not None:
            self.row.append(" ".join(" ".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        elif tag == "table" and self.rows is not None:
            self.tables.append(self.rows)
            self.rows = None


def canonical_model(model):
    # A dated snapshot may use its base model price; a different version must be looked up.
    return re.sub(r"-\d{4}-\d{2}-\d{2}$|-\d{8}$", "", model.removeprefix("codex:"))


def provider_for(model):
    if model.startswith("claude-"):
        return "anthropic"
    if model.startswith(("gpt-", "o1", "o3", "o4")) and not model.startswith("gpt-oss"):
        return "openai"
    return None


def parse_catalog(html, provider):
    parser = PricingTables()
    parser.feed(html)
    models = {}
    for rows in parser.tables:
        columns = None
        for row in rows:
            headers = [c.lower() for c in row]
            if "input" in headers and "output" in headers:
                cache = next((i for i, c in enumerate(headers) if c in ("cached input", "cache hits and refreshes", "hits and refreshes", "cache read")), None)
                # Fine-tuning tables price trained models, not the base model.
                if cache is None or "training" in headers:
                    columns = None
                    continue
                name = next((i for i, c in enumerate(headers) if c in ("model", "name")), 0)
                columns = (headers.index("input"), headers.index("output"), cache, len(headers), name)
                continue
            if not columns or len(row) != columns[3]:
                continue
            if provider == "openai":
                model = row[columns[4]].strip()
                if not re.fullmatch(r"(?:gpt-|o[134])[-\w.]+", model):
                    continue
            else:
                match = re.search(r"\bClaude\s+([A-Za-z]+)\s+(\d+(?:\.\d+)*)", row[columns[4]])
                if not match:
                    continue
                model = f"claude-{match[1].lower()}-{match[2].replace('.', '-')}"
            values = []
            for i in columns[:3]:
                match = re.search(r"\$\s*(\d+(?:\.\d+)?)", row[i])
                if not match:
                    break
                values.append(float(match[1]))
            if len(values) == 3 and all(math.isfinite(v) and v >= 0 for v in values):
                # The first full token table is standard/short-context. Later tables show other tiers.
                models.setdefault(model, dict(zip(("input", "output", "cache_read"), values)))
    if not models:
        raise ValueError("No supported token-price table found; existing estimates kept")
    return models


def fetch_catalog(provider):
    request = urllib.request.Request(SOURCES[provider], headers={"User-Agent": "Rerunly/0.1 pricing lookup"})
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read(4_000_001)
    if len(raw) > 4_000_000:
        raise ValueError("Pricing page exceeded size limit")
    return parse_catalog(raw.decode("utf-8", errors="replace"), provider)


def discover_prices(conn, models, settings, now=None):
    """Add exact model estimates; never replace existing manual entries. Return whether prices changed."""
    if not settings.get("auto_price_lookup", True):
        return False
    now = time.time() if now is None else now
    prices = settings["prices"]
    pending = {}
    # Explicit saved prices take precedence even when they are intentional family prefixes.
    manual_row = conn.execute("SELECT value FROM settings WHERE key='prices'").fetchone()
    manual = json.loads(manual_row[0]) if manual_row else {}
    for raw in models:
        model = canonical_model(raw)
        if model in prices:
            continue
        matched_manual = find_price(model, manual)
        if matched_manual is not None and any(model.startswith(k) and v == matched_manual and (k not in DEFAULT_PRICES or v != DEFAULT_PRICES[k]) for k, v in manual.items()):
            continue
        provider = provider_for(model)
        if provider:
            pending.setdefault(provider, set()).add(model)
    if not pending:
        if settings.get("price_lookup_missing"):
            db.save_settings(conn, {"price_lookup_missing": []})
        return False
    catalogs = dict(settings.get("price_catalogs", {}))
    automatic = dict(settings.get("auto_prices", {}))
    provenance = dict(settings.get("price_provenance", {}))
    changed = False
    missing = []
    for provider, wanted in pending.items():
        state = dict(catalogs.get(provider, {}))
        ttl = FAILURE_TTL if state.get("error") else SUCCESS_TTL
        if not state or now - state.get("checked_at", 0) >= ttl:
            try:
                catalog = fetch_catalog(provider)
                state = {"checked_at": now, "fetched_at": now, "source": SOURCES[provider], "models": catalog, "error": ""}
            except Exception as error:
                # Failure must not block transcript imports or discard a last-known-good catalog.
                state.update(checked_at=now, source=SOURCES[provider], error=f"Price lookup unavailable ({type(error).__name__}); retrying later")
            catalogs[provider] = state
        catalog = state.get("models", {})
        for model in wanted:
            price = catalog.get(model)
            if price is None:
                missing.append(model)
                continue
            automatic[model] = price
            provenance[model] = {"source": SOURCES[provider], "checked_at": state.get("fetched_at", state["checked_at"]), "basis": "standard short-context API token estimate"}
            changed = True
    db.save_settings(conn, {"auto_prices": automatic, "price_catalogs": catalogs, "price_provenance": provenance, "price_lookup_missing": sorted(missing)})
    return changed
