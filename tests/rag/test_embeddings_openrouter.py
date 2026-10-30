"""OpenRouterEmbedder against a fake transport: every bad answer is an EmbeddingError."""

import json
from typing import Any

import httpx2
import pytest

from brigada.rag.embeddings import EmbeddingError, OpenRouterEmbedder


def embedder(handler: Any) -> OpenRouterEmbedder:
    return OpenRouterEmbedder("sk-or-test", "m", transport=httpx2.MockTransport(handler))


def reply(status: int, body: Any) -> Any:
    return lambda request: httpx2.Response(status, json=body)


async def test_items_are_put_back_in_input_order() -> None:
    body = {"data": [{"index": 1, "embedding": [0.0, 2.0]}, {"index": 0, "embedding": [1.0, 0.0]}]}
    seen: list[Any] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json=body)

    assert await embedder(handler).embed(["a", "b"]) == [[1.0, 0.0], [0.0, 2.0]]
    assert seen == [{"model": "m", "input": ["a", "b"]}]


@pytest.mark.parametrize(
    "status, body",
    [
        (429, {"error": {"message": "rate limited"}}),
        (200, {"data": [{"index": 0, "embedding": [1.0]}]}),  # one vector for two inputs
        (200, {"data": [{"index": 0}, {"index": 1}]}),  # no embedding
        (200, {"data": [{"index": 0, "embedding": [0.0]}, {"index": 1, "embedding": [1.0]}]}),
        (200, {"data": [{"index": 0, "embedding": [1.0]}, {"index": 1, "embedding": [1.0, 2]}]}),
        (200, ["not", "an", "object"]),
    ],
)
async def test_bad_answers_are_embedding_errors_with_a_generic_message(
    status: int, body: Any
) -> None:
    with pytest.raises(EmbeddingError) as info:
        await embedder(reply(status, body)).embed(["a", "b"])
    assert str(info.value).startswith("embedding service")


async def test_no_key_is_an_embedding_error() -> None:
    with pytest.raises(EmbeddingError):
        OpenRouterEmbedder("", "m")
