"""An OpenRouter client that speaks the Anthropic Messages shape the agent loop uses.

The loop, the tools, persistence and the evals all work with Anthropic types (``tool_use``
blocks, ``stop_reason``). This adapter translates a ``messages.create(...)`` call into
OpenRouter's OpenAI-compatible chat completions and translates the reply back into an
``anthropic.types.Message``, so nothing above it changes when the provider does.

HTTP failures are raised as the Anthropic SDK's own error types (429 -> ``RateLimitError``,
5xx -> ``InternalServerError``...), so ``with_retry`` and the API error handlers keep working.
"""

import json
import logging
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

import anthropic
import httpx2
from anthropic.types import Message

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

_FINISH_REASONS = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}

_STATUS_ERRORS: dict[int, type[anthropic.APIStatusError]] = {
    400: anthropic.BadRequestError,
    401: anthropic.AuthenticationError,
    403: anthropic.PermissionDeniedError,
    404: anthropic.NotFoundError,
    409: anthropic.ConflictError,
    422: anthropic.UnprocessableEntityError,
    429: anthropic.RateLimitError,
    500: anthropic.InternalServerError,
    503: anthropic.ServiceUnavailableError,
    529: anthropic.OverloadedError,
}


class OpenRouterMessages:
    def __init__(self, http: httpx2.AsyncClient) -> None:
        self._http = http

    async def create(
        self,
        *,
        model: str,
        max_tokens: int,
        messages: Iterable[Mapping[str, Any]],
        system: str | None = None,
        tools: Iterable[Mapping[str, Any]] | None = None,
        tool_choice: Mapping[str, Any] | None = None,
        **ignored: Any,
    ) -> Message:
        if ignored:
            logger.debug("OpenRouter adapter ignores parameters %s", sorted(ignored))
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": to_openai_messages(system, messages),
        }
        if tools:
            body["tools"] = [to_openai_tool(t) for t in tools]
            if tool_choice is not None:
                body["tool_choice"] = to_openai_tool_choice(tool_choice)
        request = self._http.build_request("POST", "/chat/completions", json=body)
        try:
            response = await self._http.send(request)
        except httpx2.TimeoutException as exc:
            raise anthropic.APITimeoutError(request=request) from exc
        except httpx2.TransportError as exc:
            raise anthropic.APIConnectionError(request=request) from exc
        payload = _json(response)
        # OpenRouter sometimes reports a provider error inside a 200 response.
        if response.status_code >= 400 or "error" in payload:
            raise _status_error(response, payload)
        return to_anthropic_message(payload, model)


class OpenRouterClient:
    """Drop-in for the slice of ``anthropic.AsyncAnthropic`` the agent loop uses."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 60.0,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is not set")
        self._http = httpx2.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            transport=transport,
            headers={
                "Authorization": f"Bearer {api_key}",
                # OpenRouter's optional attribution headers.
                "X-Title": "Brigada",
            },
        )
        self.messages = OpenRouterMessages(self._http)


# ---- request translation --------------------------------------------------------------


def to_openai_messages(
    system: str | None, messages: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        blocks = [b if isinstance(b, dict) else b.to_dict() for b in content]
        if m["role"] == "assistant":
            out.append(_assistant(blocks))
        else:
            out.extend(_user(blocks))
    return out


def _assistant(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    text = "".join(b["text"] for b in blocks if b.get("type") == "text")
    calls = [
        {
            "id": b["id"],
            "type": "function",
            "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}))},
        }
        for b in blocks
        if b.get("type") == "tool_use"
    ]
    msg: dict[str, Any] = {"role": "assistant", "content": text or None}
    if calls:
        msg["tool_calls"] = calls
    return msg  # thinking blocks have no equivalent and are dropped


def _user(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Tool results become "tool" messages, which must come right after the assistant turn
    # that asked for them; any user text goes after.
    out: list[dict[str, Any]] = []
    for b in blocks:
        if b.get("type") == "tool_result":
            content = b.get("content", "")
            if not isinstance(content, str):
                content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
            if b.get("is_error"):
                content = f"ERROR: {content}"
            out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": content})
    text = "".join(b["text"] for b in blocks if b.get("type") == "text")
    if text:
        out.append({"role": "user", "content": text})
    return out


def to_openai_tool(tool: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
        },
    }


def to_openai_tool_choice(choice: Mapping[str, Any]) -> Any:
    match choice.get("type"):
        case "tool":
            return {"type": "function", "function": {"name": choice["name"]}}
        case "any":
            return "required"
        case "none":
            return "none"
        case _:
            return "auto"


# ---- response translation -------------------------------------------------------------


def to_anthropic_message(payload: Mapping[str, Any], model: str) -> Message:
    choices = payload.get("choices") or []
    if not choices:
        raise anthropic.APIResponseValidationError(
            response=httpx2.Response(502, request=httpx2.Request("POST", DEFAULT_BASE_URL)),
            body=payload,
            message="OpenRouter response has no choices",
        )
    choice = choices[0]
    msg = choice.get("message") or {}
    content: list[dict[str, Any]] = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    calls = msg.get("tool_calls") or []
    for call in calls:
        fn = call.get("function") or {}
        content.append(
            {
                "type": "tool_use",
                "id": call.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                "name": fn.get("name", ""),
                "input": _arguments(fn.get("arguments")),
            }
        )
    reason = choice.get("finish_reason")
    # Some providers say "stop" while returning tool calls: the calls decide.
    stop_reason = "tool_use" if calls else _FINISH_REASONS.get(reason, reason)
    usage = payload.get("usage") or {}
    return Message.model_validate(
        {
            "id": payload.get("id") or f"msg_{uuid.uuid4().hex[:12]}",
            "type": "message",
            "role": "assistant",
            "model": payload.get("model") or model,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
            },
        }
    )


def _arguments(raw: Any) -> dict[str, Any]:
    """Tool arguments are model-written JSON: never trusted. Invalid JSON is passed on as a
    single unknown argument, which the MCP server rejects and the model then sees and fixes."""
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {"_invalid_arguments": str(raw)[:500]}
    return value if isinstance(value, dict) else {"_invalid_arguments": str(raw)[:500]}


def _json(response: httpx2.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError:
        return {"error": {"message": response.text[:500], "code": response.status_code}}
    return value if isinstance(value, dict) else {"error": {"message": str(value)[:500]}}


def _status_error(response: httpx2.Response, payload: Mapping[str, Any]) -> anthropic.APIError:
    error = payload.get("error") or {}
    code = error.get("code") if isinstance(error.get("code"), int) else response.status_code
    if code < 400:
        code = 502  # an error reported inside a 200: treat as an upstream failure
    message = str(error.get("message") or f"OpenRouter returned HTTP {code}")
    # "Provider returned error" alone says nothing; the upstream reason is in metadata.raw
    # (e.g. a free model's shared pool being rate-limited). It goes to logs and runs.error,
    # never to the model or the HTTP client.
    metadata = error.get("metadata")
    if isinstance(metadata, dict) and metadata.get("raw"):
        message = f"{message}: {str(metadata['raw'])[:300]}"
    cls = _STATUS_ERRORS.get(code) or (
        anthropic.InternalServerError if code >= 500 else anthropic.APIStatusError
    )
    shaped = httpx2.Response(code, headers=response.headers, request=response.request)
    return cls(message, response=shaped, body=payload)
