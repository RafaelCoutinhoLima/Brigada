"""Cost accounting for Claude API calls.

Prices are USD per million tokens (MTok). They MUST be checked against the official pricing page
before trusting any cost number:

    https://docs.anthropic.com/en/docs/about-claude/pricing
    (currently served at https://platform.claude.com/docs/en/about-claude/pricing)

Every entry below was verified against that page on ``PRICES_CHECKED_ON``. When you add a model,
either verify it there or mark the line ``# unverified``. Unknown models raise instead of
silently costing zero.

Not modelled (and therefore rejected, never silently under-reported): fast mode (``speed="fast"``)
US-only inference (``inference_geo="us"``, a 1.1x multiplier) and non-standard service tiers
(``service_tier="batch"`` is 50% off, ``"priority"`` is priced separately).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

PRICES_CHECKED_ON = date(2026, 10, 28)

_MTOK = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD per million tokens for one pricing tier of a model."""

    input: Decimal
    output: Decimal
    cache_write_5m: Decimal
    cache_write_1h: Decimal
    cache_read: Decimal


@dataclass(frozen=True, slots=True)
class PricingEntry:
    """Pricing for a model, optionally with a higher tier for long prompts.

    When ``long_prompt_threshold`` is set, a request whose prompt (input + cache writes + cache
    reads) is strictly greater than the threshold is billed entirely at ``long_prompt``.
    """

    standard: ModelPrice
    long_prompt_threshold: int | None = None
    long_prompt: ModelPrice | None = None

    def price_for(self, prompt_tokens: int) -> ModelPrice:
        if (
            self.long_prompt_threshold is not None
            and self.long_prompt is not None
            and prompt_tokens > self.long_prompt_threshold
        ):
            return self.long_prompt
        return self.standard


def _p(inp: str, out: str, w5m: str, w1h: str, read: str) -> ModelPrice:
    return ModelPrice(
        input=Decimal(inp),
        output=Decimal(out),
        cache_write_5m=Decimal(w5m),
        cache_write_1h=Decimal(w1h),
        cache_read=Decimal(read),
    )


# Columns: input, output, 5m cache write, 1h cache write, cache read (hits and refreshes).
PRICING: dict[str, PricingEntry] = {
    "claude-opus-5-5": PricingEntry(_p("4", "20", "5", "8", "0.20")),
    "claude-sonnet-5-5": PricingEntry(_p("2", "10", "2.50", "4", "0.10")),
    "claude-haiku-5-5": PricingEntry(
        standard=_p("0.10", "0.50", "0.125", "0.20", "0.01"),
        long_prompt_threshold=100_000,
        long_prompt=_p("0.50", "2.50", "0.625", "1", "0.05"),
    ),
    "claude-sonnet-4-6": PricingEntry(_p("3", "15", "3.75", "6", "0.30")),
    "claude-haiku-4-5": PricingEntry(_p("1", "5", "1.25", "2", "0.10")),
}


class UnknownModelError(KeyError):
    """Raised when a model id has no entry in ``PRICING``."""

    def __init__(self, model: str) -> None:
        super().__init__(model)
        self.model = model

    def __str__(self) -> str:
        known = ", ".join(sorted(PRICING))
        return (
            f"No price for model {self.model!r}. Add it to brigada.observability.cost.PRICING "
            f"(check https://docs.anthropic.com/en/docs/about-claude/pricing). Known: {known}"
        )


class UnsupportedPricingError(ValueError):
    """Raised when usage needs a pricing modifier this module does not model."""


def get_pricing(model: str) -> PricingEntry:
    try:
        return PRICING[model]
    except KeyError:
        raise UnknownModelError(model) from None


def cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    *,
    cache_creation_1h_input_tokens: int = 0,
) -> Decimal:
    """Cost in USD of one request.

    ``cache_creation_input_tokens`` is billed at the 5-minute write rate and
    ``cache_creation_1h_input_tokens`` at the 1-hour rate; they are disjoint (do not include 1h
    tokens in the first argument too).
    """
    counts = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        "cache_creation_1h_input_tokens": cache_creation_1h_input_tokens,
    }
    for name, value in counts.items():
        if value < 0:
            raise ValueError(f"{name} must be >= 0, got {value}")

    entry = get_pricing(model)
    prompt_tokens = (
        input_tokens
        + cache_creation_input_tokens
        + cache_creation_1h_input_tokens
        + cache_read_input_tokens
    )
    price = entry.price_for(prompt_tokens)
    total = (
        Decimal(input_tokens) * price.input
        + Decimal(output_tokens) * price.output
        + Decimal(cache_creation_input_tokens) * price.cache_write_5m
        + Decimal(cache_creation_1h_input_tokens) * price.cache_write_1h
        + Decimal(cache_read_input_tokens) * price.cache_read
    )
    return total / _MTOK


class _CacheCreationLike(Protocol):
    ephemeral_5m_input_tokens: int
    ephemeral_1h_input_tokens: int


class UsageLike(Protocol):
    """Structural subset of ``anthropic.types.Usage`` used for costing."""

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None


def cost_from_usage(model: str, usage: UsageLike) -> Decimal:
    """Cost of a response given its ``anthropic`` ``Usage`` object (``response.usage``).

    OpenRouter's ``:free`` models cost nothing by definition; any other unknown model still
    raises, so a paid model never shows up as free by accident.
    """
    if model.endswith(":free"):
        return Decimal(0)
    speed = getattr(usage, "speed", None)
    if speed not in (None, "standard"):
        raise UnsupportedPricingError(f"speed={speed!r} pricing is not modelled in cost.py")
    tier = getattr(usage, "service_tier", None)
    if tier not in (None, "standard"):
        raise UnsupportedPricingError(f"service_tier={tier!r} pricing is not modelled in cost.py")
    geo = getattr(usage, "inference_geo", None)
    if geo not in (None, "global", "not_available"):
        raise UnsupportedPricingError(
            f"inference_geo={geo!r} carries a price multiplier not modelled in cost.py"
        )

    cache_total = usage.cache_creation_input_tokens or 0
    cache_5m = cache_total
    cache_1h = 0
    breakdown: _CacheCreationLike | None = getattr(usage, "cache_creation", None)
    if breakdown is not None:
        cache_5m = breakdown.ephemeral_5m_input_tokens or 0
        cache_1h = breakdown.ephemeral_1h_input_tokens or 0
        if cache_5m + cache_1h != cache_total:
            raise ValueError(
                f"cache_creation breakdown ({cache_5m}+{cache_1h}) does not match "
                f"cache_creation_input_tokens ({cache_total})"
            )

    return cost_usd(
        model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_input_tokens=cache_5m,
        cache_read_input_tokens=usage.cache_read_input_tokens or 0,
        cache_creation_1h_input_tokens=cache_1h,
    )
