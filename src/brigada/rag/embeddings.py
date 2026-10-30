"""Text embeddings for review search.

``OpenRouterEmbedder`` calls OpenRouter's OpenAI-compatible ``/embeddings`` endpoint (the
project's model provider). ``HashingEmbedder`` is deterministic and offline: tests and CI use it,
since CI cannot call a paid or rate-limited API.
"""

import hashlib
import logging
import math
import re
import unicodedata
from collections.abc import Sequence
from typing import Protocol

import httpx2
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

BATCH = 64


class EmbeddingError(RuntimeError):
    """The embedding call failed. The message is generic: upstream details go to the log."""


class Embedder(Protocol):
    @property
    def model(self) -> str: ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class OpenRouterEmbedder:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = "https://openrouter.ai/api/v1",
        # Short: a search waits for this inside a tool call, and keyword is the fallback.
        timeout_seconds: float = 5.0,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise EmbeddingError("no embedding API key configured")
        self._model = model
        self._http = httpx2.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}", "X-Title": "Brigada"},
        )

    @property
    def model(self) -> str:
        return self._model

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), BATCH):
            batch = list(texts[start : start + BATCH])
            try:
                response = await self._http.post(
                    "/embeddings", json={"model": self._model, "input": batch}
                )
                if response.status_code >= 400:
                    raise ValueError(f"HTTP {response.status_code}: {response.text[:300]}")
                data = _EmbeddingResponse.model_validate(response.json()).data
                if len(data) != len(batch):
                    raise ValueError(f"{len(data)} embeddings for {len(batch)} inputs")
                # The API may return items out of order; ``index`` is the position in the input.
                out.extend(check_vector(d.embedding) for d in sorted(data, key=lambda d: d.index))
            except (httpx2.HTTPError, ValueError) as exc:  # pydantic's error is a ValueError
                # Details (status, body) go to the log; the caller only learns it failed.
                logger.error("embedding request failed: %s", exc)
                raise EmbeddingError("embedding service unavailable") from exc
        if len({len(v) for v in out}) > 1:
            raise EmbeddingError("embedding service returned vectors of different sizes")
        return out


class _EmbeddingItem(BaseModel):
    index: int
    embedding: list[float] = Field(min_length=1)


class _EmbeddingResponse(BaseModel):
    data: list[_EmbeddingItem]


def check_vector(vector: Sequence[float]) -> list[float]:
    """A vector pgvector can rank: finite numbers, not all zero (cosine of zero is NaN)."""
    v = [float(x) for x in vector]
    if not v or not all(math.isfinite(x) for x in v) or not any(v):
        raise ValueError("embedding is empty, zero or not finite")
    return v


_TOKEN = re.compile(r"\w+")


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


class HashingEmbedder:
    """Bag of words hashed into ``dim`` buckets, L2-normalized. No semantics beyond shared
    words: good enough to exercise the vector path deterministically, not to measure it."""

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    @property
    def model(self) -> str:
        return f"hashing-{self.dim}"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for token in _TOKEN.findall(_normalize(text)):
            h = int.from_bytes(hashlib.sha256(token.encode()).digest()[:4], "big")
            v[h % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]


def default_embedder() -> Embedder:
    from brigada.config import get_settings

    s = get_settings()
    # The embedding key only: falling back to the OpenRouter key would hand a chat model key to
    # whoever embeds (the MCP server child). No key = keyword search, reported as such.
    return OpenRouterEmbedder(
        s.embedding_api_key, s.embedding_model, base_url=s.openrouter_base_url
    )
