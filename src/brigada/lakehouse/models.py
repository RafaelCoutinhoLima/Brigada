"""Inputs and outputs of the lakehouse tools (``sales_vs_baseline``, ``item_trends``,
``delivery_performance``).

Same conventions as ``brigada.mcp_server.models``: calendar days in the restaurant's own time
zone, inclusive; money in the restaurant's own currency (``currency``), never converted; the
type aliases are reused in the tool signatures so the JSON schema the model sees carries the
bounds; cross-field rules live only in the input models.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from brigada.mcp_server.models import RestaurantId, _check_range, _Input, _Output

MAX_BASELINE_RANGE_DAYS = 31
EARLIEST_DATE = date(2000, 1, 1)

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
DeliveryPerfGroupBy = Literal["none", "day", "weekday", "hour", "weekday_hour"]


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


class DeliveryPerformanceInput(_Input):
    restaurant_id: RestaurantId
    start_date: date
    end_date: date
    group_by: DeliveryPerfGroupBy = "none"

    @model_validator(mode="after")
    def _valid_range(self) -> Self:
        _check_range(self.start_date, self.end_date)
        return self


class ItemTrendsInput(_Input):
    restaurant_id: RestaurantId
    end_date: date
    days: TrendDays = 7
    limit: TrendLimit = 10
    order_by: TrendOrderBy = "drop"

    @model_validator(mode="after")
    def _plausible_end(self) -> Self:
        # date(1, 1, 3) - 56 days overflows: a validation error, not a fake "warehouse down"
        if self.end_date < EARLIEST_DATE:
            raise ValueError(f"end_date must be on or after {EARLIEST_DATE.isoformat()}")
        return self


CURRENCY_FIELD = Field(
    description="ISO 4217 code of every amount in this result (BRL = reais, INR = Indian "
    "rupees). Never converted: report amounts in this currency."
)


class ChannelVsBaseline(_Output):
    channel: str
    orders: int = Field(description="Completed orders.")
    revenue: float = Field(description="Revenue of completed orders, in `currency`.")
    canceled_orders: int
    baseline_orders: float | None
    baseline_revenue: float | None
    revenue_change_pct: float | None = Field(
        description="(revenue - baseline) / baseline in %; null when the baseline is 0 or unknown."
    )


class DayVsBaseline(_Output):
    day: date
    weekday: str
    holiday: str | None = Field(
        default=None,
        description="Holiday name when this day was a holiday for the restaurant; it is "
        "compared with normal days, so a big change is expected and is not a problem.",
    )
    orders: int
    revenue: float
    canceled_orders: int
    baseline_weeks: int = Field(
        description="How many previous same weekdays (up to 4, holidays skipped, at most 8 "
        "weeks back) the baseline averages; 0 = no history yet, so no baseline."
    )
    baseline_revenue: float | None
    revenue_change_pct: float | None
    channels: list[ChannelVsBaseline] = Field(
        description="Every channel of the restaurant's source, zero-filled (Brigada: app, "
        "telefone, balcao; Delivery Center: marketplace, canal_proprio; Zomato: "
        "zomato_delivery)."
    )


class SalesVsBaselineResult(_Output):
    restaurant_id: int
    start_date: date
    end_date: date
    source: str = Field(description="Where the numbers come from (the lakehouse gold table).")
    data_source: str | None = Field(
        description="The restaurant's raw dataset, e.g. brigada, delivery_center, zomato."
    )
    currency: str | None = CURRENCY_FIELD
    data_until: date | None = Field(
        description="Last day the lakehouse has for this restaurant. Days after it are missing "
        "from `days`, not zero."
    )
    days: list[DayVsBaseline]
    note: str | None = None


class ItemTrend(_Output):
    menu_item_id: int
    name: str
    category: str | None = Field(description="null: the source has no menu categories.")
    active: bool | None = Field(description="null: the source does not say if it is on the menu.")
    qty_current: int
    qty_previous: int
    qty_change: int = Field(description="qty_current - qty_previous.")
    revenue_current: float | None = Field(
        description="In `currency`. null: the source has no item prices (unknown, not zero)."
    )
    revenue_previous: float | None
    revenue_change: float | None = Field(
        description="revenue_current - revenue_previous, in `currency`; null without prices."
    )
    revenue_change_pct: float | None = Field(
        description="null when revenue_previous is 0 or unknown."
    )


class ItemTrendsResult(_Output):
    restaurant_id: int
    current_start: date
    current_end: date
    previous_start: date
    previous_end: date
    order_by: TrendOrderBy
    source: str
    data_source: str | None
    currency: str | None = CURRENCY_FIELD
    data_from: date | None
    data_until: date | None
    total_revenue_current: float | None = Field(description="null: the source has no prices.")
    total_revenue_previous: float | None
    items: list[ItemTrend]
    note: str | None = None


class DeliveryGroup(_Output):
    key: str = Field(
        description="all | day YYYY-MM-DD | weekday monday..sunday | hour 0-23 (local) | "
        "weekday_hour like 'saturday 20'."
    )
    delivered_orders: int = Field(
        description="Completed orders known to be delivered (with a delivery time or distance)."
    )
    timed_orders: int = Field(description="How many of them have a delivery time.")
    mean_delivery_minutes: float | None = Field(
        description="Mean over timed_orders; null: the source records no delivery time."
    )
    mean_promised_minutes: float | None = Field(description="null: the source has no promise.")
    late_share: float | None = Field(
        description="Share (0-1) of orders with a promise that arrived after it; null when none."
    )
    mean_distance_km: float | None = Field(description="null: the source has no distance.")
    prep_orders: int = Field(default=0, description="How many have the kitchen time.")
    mean_prep_minutes: float | None = Field(
        default=None, description="Kitchen: order to food ready. null: the source has none."
    )
    mean_pickup_minutes: float | None = Field(
        default=None,
        description="Food ready until the courier leaves (the food waits for the courier). "
        "null: the source has none.",
    )
    mean_transit_minutes: float | None = Field(
        default=None, description="Courier leaving to the order closed. null: none."
    )
    mean_rider_wait_minutes: float | None = Field(
        default=None,
        description="The courier waiting at the counter for the food: high means the kitchen "
        "is late for the courier. null: the source has none.",
    )


class DeliveryPerformanceResult(_Output):
    restaurant_id: int
    start_date: date
    end_date: date
    group_by: DeliveryPerfGroupBy
    source: str
    data_source: str | None
    currency: str | None = CURRENCY_FIELD
    data_until: date | None
    overall: DeliveryGroup | None = Field(description="The whole period; null with no deliveries.")
    groups: list[DeliveryGroup] = Field(description="Empty when group_by is 'none'.")
    note: str | None = None
