"""The fixed eval world: one dataset, one "today", one set of date conventions.

Every expected answer in ``evals/cases.jsonl`` is computed against this world by
``evals/build_cases.py``. The database used for a real eval run must be seeded with the same
parameters::

    uv run python scripts/seed_data.py --reset --end-date 2026-10-27 --seed 42

and the answerer receives ``today=TODAY`` so that relative dates ("ontem", "últimos 7 dias")
resolve to the same calendar days the ground truth used.
"""

from __future__ import annotations

from datetime import date, timedelta

SEED = 42
END_DATE = date(2026, 10, 27)  # last day with data (a Tuesday: restaurant 1's app outage)
TODAY = END_DATE + timedelta(days=1)  # 2026-10-28, a Wednesday

# Date conventions shared by the cases, the ground truth and the README. All ranges are
# inclusive and bucketed by local date (America/Sao_Paulo).
YESTERDAY = TODAY - timedelta(days=1)  # "ontem" = 2026-10-27
LAST_7_DAYS = (TODAY - timedelta(days=7), YESTERDAY)  # 2026-10-21 .. 2026-10-27
LAST_30_DAYS = (TODAY - timedelta(days=30), YESTERDAY)  # 2026-09-28 .. 2026-10-27
THIS_MONTH = (TODAY.replace(day=1), YESTERDAY)  # 2026-10-01 .. 2026-10-27
LAST_SATURDAY = TODAY - timedelta(days=(TODAY.weekday() - 5) % 7 or 7)  # 2026-10-24

RESTAURANT_IDS = (1, 2, 3)
