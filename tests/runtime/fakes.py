"""Scripted stand-ins for the Claude API, so the loop is tested without network or cost."""

import copy
from typing import Any

from anthropic.types import Message

MODEL = "claude-sonnet-4-6"


def message(
    *blocks: dict[str, Any],
    stop_reason: str = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 20,
    stop_details: dict[str, Any] | None = None,
) -> Message:
    data: dict[str, Any] = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": list(blocks),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }
    if stop_details is not None:
        data["stop_details"] = stop_details
    return Message.model_validate(data)


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def tool_use(id: str, name: str, **input: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": id, "name": name, "input": input}


class FakeMessages:
    def __init__(self, script: list[Message | Exception]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Message:
        # Deep copy: the loop keeps appending to the same list after the call returns.
        self.requests.append(copy.deepcopy(kwargs))
        if not self.script:
            raise AssertionError("model called more times than scripted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, *script: Message | Exception) -> None:
        self.messages = FakeMessages(list(script))
