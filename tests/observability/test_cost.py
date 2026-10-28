from decimal import Decimal

import pytest
from anthropic.types import CacheCreation, Usage

from brigada.observability.cost import (
    PRICES_CHECKED_ON,
    PRICING,
    UnknownModelError,
    UnsupportedPricingError,
    cost_from_usage,
    cost_usd,
)

ENV_EXAMPLE_MODELS = ["claude-sonnet-4-6", "claude-haiku-4-5"]
CURRENT_MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]


@pytest.mark.parametrize("model", ENV_EXAMPLE_MODELS + CURRENT_MODELS)
def test_required_models_are_priced(model: str) -> None:
    assert model in PRICING


def test_prices_checked_on_is_set() -> None:
    assert PRICES_CHECKED_ON.year >= 2026


def test_input_and_output_math() -> None:
    # sonnet-4-6: $3 in / $15 out per MTok
    assert cost_usd("claude-sonnet-4-6", 1_000_000, 1_000_000) == Decimal("18")
    assert cost_usd("claude-sonnet-4-6", 1000, 500) == Decimal("0.0105")


def test_cache_math() -> None:
    # opus-5-5: $4 in, $20 out, $5 5m write, $8 1h write, $0.20 read
    cost = cost_usd(
        "claude-opus-5-5",
        input_tokens=100_000,
        output_tokens=10_000,
        cache_creation_input_tokens=200_000,
        cache_read_input_tokens=500_000,
        cache_creation_1h_input_tokens=50_000,
    )
    expected = Decimal("0.4") + Decimal("0.2") + Decimal("1.0") + Decimal("0.4") + Decimal("0.1")
    assert cost == expected


def test_returns_exact_decimal() -> None:
    cost = cost_usd("claude-haiku-4-5", 3, 7)
    assert isinstance(cost, Decimal)
    assert cost == Decimal("0.000038")


def test_haiku_5_5_long_prompt_tier() -> None:
    at_threshold = cost_usd("claude-haiku-5-5", 100_000, 0)
    assert at_threshold == Decimal("0.01")  # $0.10/MTok
    # 90k input + 20k cache read = 110k prompt: everything billed at the long tier
    over = cost_usd("claude-haiku-5-5", 90_000, 1_000, cache_read_input_tokens=20_000)
    assert over == Decimal("0.045") + Decimal("0.0025") + Decimal("0.001")


def test_unknown_model_raises_clear_error() -> None:
    with pytest.raises(UnknownModelError) as info:
        cost_usd("claude-made-up-9", 10, 10)
    assert isinstance(info.value, KeyError)
    assert "claude-made-up-9" in str(info.value)
    assert "PRICING" in str(info.value)


def test_no_prefix_matching() -> None:
    with pytest.raises(UnknownModelError):
        cost_usd("claude-opus-5-5-20260101", 1, 1)


def test_negative_tokens_rejected() -> None:
    with pytest.raises(ValueError):
        cost_usd("claude-opus-5-5", -1, 0)


def test_cost_from_usage_plain() -> None:
    usage = Usage(input_tokens=1000, output_tokens=200)
    assert cost_from_usage("claude-sonnet-5-5", usage) == cost_usd("claude-sonnet-5-5", 1000, 200)


def test_cost_from_usage_with_cache_breakdown() -> None:
    usage = Usage(
        input_tokens=10,
        output_tokens=20,
        cache_creation_input_tokens=300,
        cache_read_input_tokens=400,
        cache_creation=CacheCreation(ephemeral_5m_input_tokens=100, ephemeral_1h_input_tokens=200),
    )
    expected = cost_usd(
        "claude-opus-5-5",
        10,
        20,
        cache_creation_input_tokens=100,
        cache_read_input_tokens=400,
        cache_creation_1h_input_tokens=200,
    )
    assert cost_from_usage("claude-opus-5-5", usage) == expected


def test_cost_from_usage_without_breakdown_treats_writes_as_5m() -> None:
    usage = Usage(input_tokens=0, output_tokens=0, cache_creation_input_tokens=1_000_000)
    assert cost_from_usage("claude-opus-5-5", usage) == Decimal("5")


def test_cost_from_usage_none_fields_are_zero() -> None:
    usage = Usage(
        input_tokens=5,
        output_tokens=5,
        cache_creation_input_tokens=None,
        cache_read_input_tokens=None,
    )
    assert cost_from_usage("claude-haiku-4-5", usage) == cost_usd("claude-haiku-4-5", 5, 5)


def test_cost_from_usage_inconsistent_breakdown_raises() -> None:
    usage = Usage(
        input_tokens=0,
        output_tokens=0,
        cache_creation_input_tokens=10,
        cache_creation=CacheCreation(ephemeral_5m_input_tokens=1, ephemeral_1h_input_tokens=1),
    )
    with pytest.raises(ValueError):
        cost_from_usage("claude-opus-5-5", usage)


@pytest.mark.parametrize(
    "extra",
    [{"speed": "fast"}, {"inference_geo": "us"}, {"service_tier": "batch"}],
)
def test_cost_from_usage_rejects_unmodelled_pricing(extra: dict[str, str]) -> None:
    usage = Usage(input_tokens=1, output_tokens=1, **extra)  # type: ignore[arg-type]
    with pytest.raises(UnsupportedPricingError):
        cost_from_usage("claude-opus-5-5", usage)


def test_cost_from_usage_unknown_model() -> None:
    with pytest.raises(UnknownModelError):
        cost_from_usage("gpt-nope", Usage(input_tokens=1, output_tokens=1))
