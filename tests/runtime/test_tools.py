import asyncio
import time

import pytest

from brigada.runtime.tools import ToolExecutionError, run_tools
from brigada.runtime.types import ToolCall


def call(name: str, i: int = 0, **input: object) -> ToolCall:
    return ToolCall(id=f"toolu_{name}_{i}", name=name, input=dict(input))


async def executor(c: ToolCall) -> str:
    match c.name:
        case "echo":
            return f"echo:{c.input['value']}"
        case "slow":
            await asyncio.sleep(float(str(c.input["seconds"])))
            return "slow done"
        case "reports_error":
            raise ToolExecutionError("restaurant 999 does not exist")
        case "crashes":
            raise KeyError("postgresql://user:secret@db/brigada")
    raise AssertionError(c.name)


async def test_results_keep_call_order_and_ids() -> None:
    calls = [call("echo", 1, value="a"), call("slow", 2, seconds=0.05), call("echo", 3, value="c")]
    out = await run_tools(calls, executor, timeout_seconds=1)
    assert [o.tool_use_id for o in out] == [c.id for c in calls]
    assert [o.content for o in out] == ["echo:a", "slow done", "echo:c"]
    assert not any(o.is_error for o in out)


async def test_tools_run_concurrently() -> None:
    calls = [call("slow", i, seconds=0.2) for i in range(5)]
    started = time.perf_counter()
    await run_tools(calls, executor, timeout_seconds=1)
    assert time.perf_counter() - started < 0.5  # sequential would take ~1s


async def test_timeout_becomes_error_result_and_does_not_block_others() -> None:
    calls = [call("slow", 1, seconds=5), call("echo", 2, value="ok")]
    started = time.perf_counter()
    out = await run_tools(calls, executor, timeout_seconds=0.1)
    assert time.perf_counter() - started < 1
    assert out[0].is_error and "timed out after 0.1s" in out[0].content
    assert out[1] == type(out[1])("toolu_echo_2", "echo:ok", False)


async def test_tool_reported_error_reaches_model_verbatim() -> None:
    out = await run_tools([call("reports_error")], executor, timeout_seconds=1)
    assert out[0].is_error
    assert out[0].content == "restaurant 999 does not exist"


async def test_crash_is_reported_without_internals(caplog: pytest.LogCaptureFixture) -> None:
    out = await run_tools([call("crashes")], executor, timeout_seconds=1)
    assert out[0].is_error
    assert out[0].content == "tool 'crashes' failed: KeyError"
    assert "secret" not in out[0].content
    assert "Traceback" in caplog.text  # but the full error is in our logs


async def test_one_failure_does_not_affect_the_others() -> None:
    calls = [call("crashes"), call("echo", value="x"), call("reports_error")]
    out = await run_tools(calls, executor, timeout_seconds=1)
    assert [o.is_error for o in out] == [True, False, True]


async def test_no_calls_returns_empty_list() -> None:
    assert await run_tools([], executor, timeout_seconds=1) == []


async def test_cancelling_the_run_cancels_the_tools() -> None:
    task = asyncio.create_task(run_tools([call("slow", seconds=5)], executor, timeout_seconds=10))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_long_tool_errors_are_truncated_for_the_model() -> None:
    async def noisy(c: ToolCall) -> str:
        raise ToolExecutionError("x" * 5000)

    [out] = await run_tools([call("noisy")], noisy, timeout_seconds=1)
    assert out.is_error and len(out.content) < 1100 and out.content.endswith("[truncated]")
