"""Inputs and outputs of the lakehouse tools (``sales_vs_baseline``, ``item_trends``).

Same conventions as ``brigada.mcp_server.models``: local calendar days (America/Sao_Paulo),
inclusive; the type aliases are reused in the tool signatures so the JSON schema the model sees
carries the bounds; cross-field rules live only in the input models.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from brigada.mcp_server.models import RestaurantId, _check_range, _Input, _Output

MAX_BASELINE_RANGE_DAYS = 31

TrendDays = Annotated[
    int,
    Field(
        ge=1,
        le=28,
        description="Window length in days: the last `days` days up to end_date "
        "vs the `days` days right before them.",
    ),
]
TrendLimit = Annotated[int, Field(ge=1, le=50)]
TrendOrderBy = Literal["drop", "rise", "revenue"]


class SalesVsBaselineInput(_Input):
    restaurant_id: RestaurantId
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def _valid_range(self) -> Self:
        _check_range(self.start_date, self.end_date)
        span = (self.end_date - self.start_date).days + 1
        if span > MAX_BASELINE_RANGE_DAYS:
            raise ValueError(
                f"date range spans {span} days; the maximum is {MAX_BASELINE_RANGE_DAYS}"
            )
        return self


class ItemTrendsInput(_Input):
    restaurant_id: RestaurantId
    end_date: date
    days: TrendDays = 7
    limit: TrendLimit = 10
    order_by: TrendOrderBy = "drop"


class ChannelVsBaseline(_Output):
    channel: str
    orders: int
    revenue: float
    baseline_orders: float | None
    baseline_revenue: float | None
    revenue_change_pct: float | None = Field(
        description="(revenue - baseline) / baseline in %; null when the baseline is 0 or unknown."
    )


class DayVsBaseline(_Output):
    day: date
    weekday: str
    orders: int
    revenue: float
    baseline_weeks: int = Field(
        description="How many previous same weekdays (up to 4) the baseline averages; 0 = no "
        "history yet, so no baseline."
    )
    baseline_revenue: float | None
    revenue_change_pct: float | None
    channels: list[ChannelVsBaseline] = Field(description="app, telefone, balcao; zero-filled.")


class SalesVsBaselineResult(_Output):
    restaurant_id: int
    start_date: date
    end_date: date
    source: str = Field(description="Where the numbers come from (the lakehouse gold table).")
    data_until: date | None = Field(
        description="Last day the lakehouse has for this restaurant. Days after it are missing "
        "from `days`, not zero."
    )
    days: list[DayVsBaseline]
    note: str | None = None


class ItemTrend(_Output):
    menu_item_id: int
    name: str
    category: str
    active: bool
    qty_current: int
    qty_previous: int
    revenue_current: float
    revenue_previous: float
    revenue_change: float = Field(description="revenue_current - revenue_previous, in R$.")
    revenue_change_pct: float | None = Field(description="null when revenue_previous is 0.")


class ItemTrendsResult(_Output):
    restaurant_id: int
    current_start: date
    current_end: date
    previous_start: date
    previous_end: date
    order_by: TrendOrderBy
    source: str
    data_from: date | None
    data_until: date | None
    total_revenue_current: float
    total_revenue_previous: float
    items: list[ItemTrend]
    note: str | None = None
