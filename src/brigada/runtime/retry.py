"""Retry with exponential backoff and full jitter for Claude API calls.

The SDK client must be created with ``max_retries=0`` when this is used; otherwise every
attempt here turns into up to three requests (the SDK retries 2x by default).
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import anthropic

logger = logging.getLogger(__name__)

# 429 rate limit, 529 overloaded, 5xx server errors and network failures (timeouts included).
# Everything else (400, 401, 403, 404, 413, 422) is a bug or a config problem: retrying
# would only hide it and burn time.
RETRYABLE = (
    anthropic.RateLimitError,
    anthropic.OverloadedError,
    anthropic.InternalServerError,
    anthropic.ServiceUnavailableError,
    anthropic.APIConnectionError,
)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_seconds: float = 1.0
    cap_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")


DEFAULT_POLICY = RetryPolicy()


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, RETRYABLE):
        return True
    return isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500


def backoff_delay(attempt: int, policy: RetryPolicy, rand: Callable[[], float]) -> float:
    """Full jitter: uniform in [0, min(cap, base * 2**attempt)]. ``attempt`` starts at 0."""
    ceiling = min(policy.cap_seconds, policy.base_seconds * 2**attempt)
    return ceiling * rand()


def retry_after_seconds(exc: BaseException) -> float | None:
    """The server's own hint (``retry-after`` header, in seconds), when it sends one."""
    if not isinstance(exc, anthropic.APIStatusError):
        return None
    value = exc.response.headers.get("retry-after")
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None  # HTTP-date form: ignore and fall back to backoff


async def with_retry[T](
    call: Callable[[], Awaitable[T]],
    policy: RetryPolicy = DEFAULT_POLICY,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rand: Callable[[], float] = random.random,
) -> T:
    """Run ``call`` until it succeeds, a non-retryable error happens or attempts run out.

    ``sleep`` and ``rand`` are injectable so tests run instantly and deterministically.
    """
    for attempt in range(policy.max_attempts):
        try:
            return await call()
        except Exception as exc:
            last_attempt = attempt == policy.max_attempts - 1
            if not is_retryable(exc) or last_attempt:
                if last_attempt and is_retryable(exc):
                    exc.add_note(f"gave up after {policy.max_attempts} attempts")
                raise
            hint = retry_after_seconds(exc)
            delay = (
                min(hint, policy.cap_seconds)
                if hint is not None
                else backoff_delay(attempt, policy, rand)
            )
            logger.warning(
                "Claude API call failed (%s), retry %d/%d in %.2fs",
                type(exc).__name__,
                attempt + 1,
                policy.max_attempts - 1,
                delay,
            )
            await sleep(delay)
    raise AssertionError("unreachable: RetryPolicy guarantees max_attempts >= 1")
