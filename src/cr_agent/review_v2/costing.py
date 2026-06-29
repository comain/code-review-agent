"""Token cost helpers adapted from comain/unit-test-agent task accounting."""

from __future__ import annotations

from typing import Optional


def estimate_cost_from_tokens(
    *,
    model: Optional[str],
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    reasoning_tokens: int = 0,
) -> float:
    model_lower = (model or "").lower()
    input_rate = 2.50
    cache_rate = 0.25
    output_rate = 15.0
    if "kimi-k2.6" in model_lower or "kimi/k2.6" in model_lower:
        input_rate = 0.7448
        cache_rate = input_rate * 0.25
        output_rate = 4.655
    elif "gpt-5.5" in model_lower:
        input_rate = 5.00
        cache_rate = 0.50
        output_rate = 30.0
    elif "gpt-5.4-mini" in model_lower:
        input_rate = 0.75
        cache_rate = 0.075
        output_rate = 4.50
    elif "gpt-5.4-nano" in model_lower:
        input_rate = 0.20
        cache_rate = 0.02
        output_rate = 1.25
    elif "gpt-5.4" in model_lower:
        input_rate = 2.50
        cache_rate = 0.25
        output_rate = 15.0
    elif "gpt-5.3-codex" in model_lower or "gpt-5.3-chat" in model_lower or "gpt-5.3" in model_lower:
        input_rate = 1.75
        cache_rate = 0.175
        output_rate = 14.0
    return (
        ((input_tokens * input_rate) / 1_000_000)
        + ((cache_read_tokens * cache_rate) / 1_000_000)
        + (((output_tokens + reasoning_tokens) * output_rate) / 1_000_000)
    )


def cost_from_provider_or_tokens(
    *,
    provider_cost_usd: Optional[float],
    model: Optional[str],
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    reasoning_tokens: int = 0,
) -> float:
    if provider_cost_usd is not None and provider_cost_usd > 0:
        return float(provider_cost_usd)
    if not (input_tokens or output_tokens or cache_read_tokens or reasoning_tokens):
        return 0.0
    return estimate_cost_from_tokens(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        reasoning_tokens=reasoning_tokens,
    )
