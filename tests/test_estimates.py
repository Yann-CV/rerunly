import json

import pytest

from rerunly.estimates import estimate_rerun


PRICES = {"original": {"input": 10, "output": 20, "cache_read": 1},
          "cheaper": {"input": 1, "output": 2, "cache_read": 0.1}}


def test_estimate_reprices_recorded_cache_and_includes_agents():
    prompt = {"requested_model": "original", "models": json.dumps({"original": 2}),
              "input_tokens": 100, "cache_read_tokens": 200, "cache_write_tokens": 50,
              "output_tokens": 20, "active_ms": 3000, "wall_ms": 9000}
    agent = {"models": json.dumps({"original": 1}), "input_tokens": 100,
             "output_tokens": 10, "web_search_requests": 1}
    result = estimate_rerun(prompt, [agent], "codex:cheaper", PRICES)
    assert result["model"] == "cheaper"
    assert result["input_tokens"] == 450 and result["output_tokens"] == 30
    assert result["total_tokens"] == 480
    assert result["tokens_by_model"] == {"cheaper": 370, "original": 110}
    assert result["duration_ms"] == 3000
    assert result["cost_usd"] == pytest.approx((100 + 200 * 0.1 + 50 * 1.25 + 20 * 2 + 100 * 10 + 10 * 20) / 1_000_000 + 0.01)


def test_unknown_prices_or_missing_prompt_are_not_zero_estimates():
    prompt = {"models": json.dumps({"original": 1}), "input_tokens": 100, "wall_ms": 4000}
    result = estimate_rerun(prompt, [], "unknown", PRICES)
    assert result["cost_usd"] is None and result["total_tokens"] == 100
    assert result["duration_ms"] == 4000
    agent = {"models": json.dumps({"original": 1, "cheaper": 1}), "input_tokens": 30}
    assert estimate_rerun(prompt, [agent], "", PRICES)["cost_usd"] is None
    assert estimate_rerun(None, [], "cheaper", PRICES) is None
    assert estimate_rerun(prompt, [], "", PRICES)["model"] == "original"


def test_same_model_preserves_cache_cost_instead_of_charging_every_hit_as_input():
    prompt = {"models": json.dumps({"original": 10}), "input_tokens": 100_000,
              "cache_read_tokens": 900_000, "output_tokens": 10_000}
    result = estimate_rerun(prompt, [], "", PRICES)
    assert result["cost_usd"] == pytest.approx(2.1)
    assert result["total_tokens"] == 1_010_000
    assert result["basis"] == "original_usage_recorded_cache"


@pytest.mark.parametrize("model", ["", "original"])
def test_unchanged_model_reuses_precise_recorded_cost_including_agents(model):
    # Aggregates lose the one-hour cache TTL; repricing as five-minute writes
    # would understate this original cost even with exactly the same settings.
    prompt = {"models": json.dumps({"original": 2}), "requested_model": "original",
              "cache_write_tokens": 100_000, "cost_usd": 2.0}
    agent = {"models": json.dumps({"original": 1}), "cost_usd": 0.4}
    assert estimate_rerun(prompt, [agent], model, PRICES)["cost_usd"] == pytest.approx(2.4)
    assert estimate_rerun(prompt, [agent], "cheaper", PRICES)["cost_usd"] == pytest.approx(0.525)


def test_unchanged_model_preserves_reported_total_without_adding_agents_twice():
    prompt = {"models": json.dumps({"original": 1}), "cost_usd": 2.0,
              "cost_source": "claude-code", "total_cost_usd": 2.7}
    agent = {"models": json.dumps({"original": 1}), "cost_usd": 0.4}
    assert estimate_rerun(prompt, [agent], "original", PRICES)["cost_usd"] == 2.7


def test_unpriced_recorded_cost_is_not_treated_as_a_complete_estimate():
    prompt = {"models": json.dumps({"unknown": 1}), "unpriced_models": '["unknown"]',
              "cost_usd": 0, "input_tokens": 100}
    assert estimate_rerun(prompt, [], "", PRICES)["cost_usd"] is None
