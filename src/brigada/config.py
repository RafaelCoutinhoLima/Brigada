import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Child processes (the MCP server) set BRIGADA_NO_DOTENV so they only see the variables
    # they were explicitly given, not every secret in the parent's .env.
    model_config = SettingsConfigDict(
        env_file=None if os.environ.get("BRIGADA_NO_DOTENV") else ".env", extra="ignore"
    )

    anthropic_api_key: str = ""
    database_url: str = "postgresql://brigada:brigada@localhost:5432/brigada"
    model_strong: str = "claude-sonnet-4-6"
    model_fast: str = "claude-haiku-4-5"
    max_agent_steps: int = 10
    tool_timeout_seconds: float = 15.0
    timezone: str = "America/Sao_Paulo"
    # "key1=1,2;key2=3": each API key and the restaurant ids it may query. Empty = /ask rejects all.
    api_keys: str = ""

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
