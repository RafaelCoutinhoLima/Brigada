import os
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Child processes (the MCP server) set BRIGADA_NO_DOTENV so they only see the variables
    # they were explicitly given, not every secret in the parent's .env.
    model_config = SettingsConfigDict(
        env_file=None if os.environ.get("BRIGADA_NO_DOTENV") else ".env", extra="ignore"
    )

    # Which API answers the model calls. "openrouter" uses its OpenAI-compatible endpoint
    # through an adapter (runtime/openrouter.py); MODEL_STRONG/MODEL_FAST are then OpenRouter
    # model ids, e.g. "google/gemma-4-31b-it:free".
    model_provider: Literal["anthropic", "openrouter"] = "anthropic"
    anthropic_api_key: str = ""
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    database_url: str = "postgresql://brigada:brigada@localhost:5432/brigada"
    # Review search (rag/): embeddings through OpenRouter's /embeddings. The MCP server child
    # gets only this key (empty = the OpenRouter key's value): give it a separate, credit-limited
    # key in production, since an OpenRouter key also opens the chat models.
    embedding_model: str = "nvidia/nemotron-3.5-embed-1b:free"
    embedding_api_key: str = ""
    model_strong: str = "claude-sonnet-4-6"
    model_fast: str = "claude-haiku-4-5"
    max_agent_steps: int = 10
    tool_timeout_seconds: float = 15.0
    timezone: str = "America/Sao_Paulo"
    # "key1=1,2;key2=3": each API key and the restaurant ids it may query. Empty = /ask rejects all.
    api_keys: str = ""
    # Local development only: the console fetches this key by itself when opened from this
    # machine (127.0.0.1, no proxy). Must be one of API_KEYS. Empty = off (the default).
    console_local_key: str = ""

    @property
    def model_api_key(self) -> str:
        """The key of the configured provider ("" when it is not set)."""
        if self.model_provider == "openrouter":
            return self.openrouter_api_key
        return self.anthropic_api_key

    def api_key_scopes(self) -> dict[str, frozenset[int]]:
        scopes: dict[str, frozenset[int]] = {}
        for entry in filter(None, (e.strip() for e in self.api_keys.split(";"))):
            key, sep, ids = entry.partition("=")
            if not sep or not key.strip() or not ids.strip():
                raise ValueError(f"Malformed API_KEYS entry: {key.strip()[:4]}...")
            scopes[key.strip()] = frozenset(int(i) for i in ids.split(","))
        return scopes


@lru_cache
def get_settings() -> Settings:
    return Settings()
