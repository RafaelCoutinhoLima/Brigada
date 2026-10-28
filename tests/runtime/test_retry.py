import anthropic
import httpx2
import pytest

from brigada.runtime.retry import RetryPolicy, backoff_delay, with_retry

REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def api_error(
    cls: type[anthropic.APIStatusError], status: int, headers: dict[str, str] | None = None
) -> anthropic.APIStatusError:
    response = httpx2.Response(status, headers=headers or {}, request=REQUEST)
    return cls("boom", response=response, body=None)


class Flaky:
    """Fails with the given errors in order, then returns "ok"."""

    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


POLICY = RetryPolicy(max_attempts=4, base_seconds=1.0, cap_seconds=10.0)


async def run(call: Flaky, sleeps: Sleeps, policy: RetryPolicy = POLICY) -> str:
    return await with_retry(call, policy, sleep=sleeps, rand=lambda: 1.0)


async def test_success_first_try_does_not_sleep() -> None:
    call, sleeps = Flaky(), Sleeps()
    assert await run(call, sleeps) == "ok"
    assert call.calls == 1
    assert sleeps.delays == []


@pytest.mark.parametrize(
    "error",
    [
        api_error(anthropic.RateLimitError, 429),
        api_error(anthropic.OverloadedError, 529),
        api_error(anthropic.InternalServerError, 500),
        api_error(anthropic.ServiceUnavailableError, 503),
        api_error(anthropic.APIStatusError, 502),
        anthropic.APIConnectionError(request=REQUEST),
        anthropic.APITimeoutError(request=REQUEST),
    ],
)
async def test_retryable_errors_are_retried(error: Exception) -> None:
    call, sleeps = Flaky(error), Sleeps()
    assert await run(call, sleeps) == "ok"
    assert call.calls == 2


@pytest.mark.parametrize(
    "error",
    [
        api_error(anthropic.BadRequestError, 400),
        api_error(anthropic.AuthenticationError, 401),
        api_error(anthropic.PermissionDeniedError, 403),
        api_error(anthropic.NotFoundError, 404),
        ValueError("bug in our code"),
    ],
)
async def test_non_retryable_errors_raise_immediately(error: Exception) -> None:
    call, sleeps = Flaky(error), Sleeps()
    with pytest.raises(type(error)):
        await run(call, sleeps)
    assert call.calls == 1
    assert sleeps.delays == []


async def test_gives_up_after_max_attempts_and_says_so() -> None:
    errors = [api_error(anthropic.OverloadedError, 529) for _ in range(10)]
    call, sleeps = Flaky(*errors), Sleeps()
    with pytest.raises(anthropic.OverloadedError) as info:
        await run(call, sleeps)
    assert call.calls == POLICY.max_attempts
    assert len(sleeps.delays) == POLICY.max_attempts - 1  # no sleep after the last failure
    assert "gave up after 4 attempts" in "".join(info.value.__notes__)


async def test_backoff_doubles_and_is_capped() -> None:
    errors = [api_error(anthropic.OverloadedError, 529) for _ in range(5)]
    call, sleeps = Flaky(*errors), Sleeps()
    policy = RetryPolicy(max_attempts=6, base_seconds=1.0, cap_seconds=5.0)
    await run(call, sleeps, policy)
    assert sleeps.delays == [1.0, 2.0, 4.0, 5.0, 5.0]


def test_full_jitter_stays_within_ceiling() -> None:
    assert backoff_delay(3, POLICY, rand=lambda: 0.0) == 0.0
    assert backoff_delay(3, POLICY, rand=lambda: 0.5) == 4.0
    assert backoff_delay(3, POLICY, rand=lambda: 1.0) == 8.0


async def test_retry_after_header_wins_over_backoff_but_is_capped() -> None:
    call = Flaky(
        api_error(anthropic.RateLimitError, 429, {"retry-after": "3"}),
        api_error(anthropic.RateLimitError, 429, {"retry-after": "999"}),
        api_error(anthropic.RateLimitError, 429, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}),
    )
    sleeps = Sleeps()
    await run(call, sleeps)
    assert sleeps.delays == [3.0, 10.0, 4.0]  # hint, capped hint, backoff (attempt 2 -> 4s)


def test_policy_needs_at_least_one_attempt() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)


def test_negative_retry_after_is_clamped_to_zero() -> None:
    from brigada.runtime.retry import retry_after_seconds

    assert retry_after_seconds(api_error(anthropic.RateLimitError, 429, {"retry-after": "-5"})) == 0
