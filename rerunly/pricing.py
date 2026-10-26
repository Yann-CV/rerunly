"""Token pricing, in USD per million tokens.

Cache writes are priced as a multiple of the input price (1.25x for the 5-minute
TTL, 2x for the 1-hour TTL). The table is only a default: it is stored in the
settings and can be edited from the Setup page.
"""

from dataclasses import dataclass

CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.0
FAST_MODE_MULT = 2.0
WEB_SEARCH_USD = 0.01  # $10 per 1,000 searches

# model id prefix -> {input, output, cache_read}
DEFAULT_PRICES: dict[str, dict[str, float]] = {
    # Standard, short-context API estimates; subscription credits are not USD charges.
    "gpt-6-astra": {"input": 10.0, "output": 50.0, "cache_read": 1.0},
    "gpt-6.1-sol": {"input": 2.0, "output": 10.0, "cache_read": 0.10},
    "gpt-6.1-terra": {"input": 1.0, "output": 5.0, "cache_read": 0.05},
    "gpt-6-luna": {"input": 0.10, "output": 0.50, "cache_read": 0.01},
    "gpt-5.3-codex": {"input": 1.75, "output": 14.0, "cache_read": 0.175},
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25},
    "claude-fable-5": {"input": 10.0, "output": 50.0, "cache_read": 1.0},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20},
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cache_read": 0.50},
    "claude-opus-4": {"input": 5.0, "output": 25.0, "cache_read": 0.50},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.10},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_read": 0.20},
    "claude-sonnet-4": {"input": 3.0, "output": 15.0, "cache_read": 0.30},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01},
    "claude-haiku-4": {"input": 1.0, "output": 5.0, "cache_read": 0.10},
}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    web_search_requests: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_5m_tokens += other.cache_write_5m_tokens
        self.cache_write_1h_tokens += other.cache_write_1h_tokens
        self.web_search_requests += other.web_search_requests

    @property
    def cache_write_tokens(self) -> int:
        return self.cache_write_5m_tokens + self.cache_write_1h_tokens

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens


def find_price(model: str, prices: dict[str, dict[str, float]]) -> dict[str, float] | None:
    """Longest-prefix match, so `claude-opus-5-5` wins over `claude-opus-5`."""
    best = None
    for prefix in prices:
        if model.startswith(prefix) and (best is None or len(prefix) > len(best)):
            best = prefix
    return prices[best] if best else None


def cost_usd(model: str, usage: Usage, prices: dict[str, dict[str, float]], fast: bool = False) -> float | None:
    """Cost of one API call, or None when the model has no known price."""
    price = find_price(model, prices)
    if price is None:
        return None
    cost = (
        usage.input_tokens * price["input"]
        + usage.output_tokens * price["output"]
        + usage.cache_read_tokens * price["cache_read"]
        + usage.cache_write_5m_tokens * price["input"] * CACHE_WRITE_5M_MULT
        + usage.cache_write_1h_tokens * price["input"] * CACHE_WRITE_1H_MULT
    ) / 1_000_000
    return cost * (FAST_MODE_MULT if fast else 1.0) + usage.web_search_requests * WEB_SEARCH_USD
