"""Persist orchestrations: the plan, the review and the totals of one answered question."""

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb


class OrchestrationStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    async def _execute(self, sql: str, params: Sequence[Any]) -> None:
        async with await psycopg.AsyncConnection.connect(self.database_url) as conn:
            await conn.execute(sql, params)

    async def create(self, oid: str, *, restaurant_id: int, question: str, today: date) -> None:
        await self._execute(
            "INSERT INTO orchestrations (id, restaurant_id, question, today) "
            "VALUES (%s, %s, %s, %s)",
            (oid, restaurant_id, question, today),
        )

    async def restaurant_name(self, restaurant_id: int) -> str | None:
        async with await psycopg.AsyncConnection.connect(self.database_url) as conn:
            cur = await conn.execute("SELECT name FROM restaurants WHERE id = %s", (restaurant_id,))
            row = await cur.fetchone()
        return str(row[0]) if row else None

    async def update(
        self, oid: str, *, plan: Any = None, review: Any = None, rounds: int | None = None
    ) -> None:
        await self._execute(
            "UPDATE orchestrations SET plan = COALESCE(%s, plan), "
            "review = COALESCE(%s, review), rounds = COALESCE(%s, rounds) WHERE id = %s",
            (
                Jsonb(plan) if plan is not None else None,
                Jsonb(review) if review is not None else None,
                rounds,
                oid,
            ),
        )

    async def succeed(
        self, oid: str, *, answer: str, tokens_in: int, tokens_out: int, cost_usd: Decimal
    ) -> None:
        await self._execute(
            "UPDATE orchestrations SET status = 'succeeded', answer = %s, tokens_in = %s, "
            "tokens_out = %s, cost_usd = %s, finished_at = now() WHERE id = %s",
            (answer, tokens_in, tokens_out, cost_usd, oid),
        )

    async def fail(
        self,
        oid: str,
        error: BaseException,
        *,
        tokens_in: int,
        tokens_out: int,
        cost_usd: Decimal,
    ) -> None:
        await self._execute(
            "UPDATE orchestrations SET status = 'failed', error = %s, tokens_in = %s, "
            "tokens_out = %s, cost_usd = %s, finished_at = now() WHERE id = %s",
            (f"{type(error).__name__}: {error}", tokens_in, tokens_out, cost_usd, oid),
        )
