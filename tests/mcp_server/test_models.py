"""Input validation of the MCP tool models (no database needed)."""

from datetime import date

import pytest
from pydantic import ValidationError

from brigada.mcp_server.models import (
    CreatePromotionInput,
    DeliveryTimesInput,
    ListMenuItemsInput,
    PauseMenuItemInput,
    ReviewsForItemInput,
    SalesByPeriodInput,
    TopItemsInput,
)

D1 = date(2026, 10, 1)
D2 = date(2026, 10, 27)


def test_sales_valid_defaults() -> None:
    inp = SalesByPeriodInput(restaurant_id=1, start_date=D1, end_date=D2)
    assert inp.group_by == "none"


def test_single_day_range_is_valid() -> None:
    SalesByPeriodInput(restaurant_id=1, start_date=D2, end_date=D2, group_by="channel")


def test_dates_parse_from_iso_strings() -> None:
    inp = SalesByPeriodInput.model_validate(
        {"restaurant_id": 1, "start_date": "2026-10-01", "end_date": "2026-10-27"}
    )
    assert inp.start_date == D1


@pytest.mark.parametrize("model", [SalesByPeriodInput, TopItemsInput, DeliveryTimesInput])
def test_end_before_start_rejected(model: type[SalesByPeriodInput]) -> None:
    with pytest.raises(ValidationError, match="end_date"):
        model(restaurant_id=1, start_date=D2, end_date=D1)


def test_range_longer_than_a_year_rejected() -> None:
    with pytest.raises(ValidationError, match="maximum"):
        SalesByPeriodInput(restaurant_id=1, start_date=date(2025, 1, 1), end_date=D2)


@pytest.mark.parametrize("rid", [0, -1])
def test_non_positive_restaurant_id_rejected(rid: int) -> None:
    with pytest.raises(ValidationError):
        ListMenuItemsInput(restaurant_id=rid)


def test_bad_sales_group_by_rejected() -> None:
    with pytest.raises(ValidationError):
        SalesByPeriodInput.model_validate(
            {"restaurant_id": 1, "start_date": D1, "end_date": D2, "group_by": "month"}
        )


def test_bad_delivery_group_by_rejected() -> None:
    with pytest.raises(ValidationError):
        DeliveryTimesInput.model_validate(
            {"restaurant_id": 1, "start_date": D1, "end_date": D2, "group_by": "channel"}
        )


@pytest.mark.parametrize("limit", [0, 51])
def test_top_items_limit_bounds(limit: int) -> None:
    with pytest.raises(ValidationError):
        TopItemsInput(restaurant_id=1, start_date=D1, end_date=D2, limit=limit)


def test_top_items_bad_order_by() -> None:
    with pytest.raises(ValidationError):
        TopItemsInput.model_validate(
            {"restaurant_id": 1, "start_date": D1, "end_date": D2, "order_by": "price"}
        )


def test_extra_fields_forbidden() -> None:
    # The model must not be able to smuggle extra parameters (e.g. raw SQL).
    with pytest.raises(ValidationError):
        ListMenuItemsInput.model_validate({"restaurant_id": 1, "sql": "DROP TABLE orders"})


def test_reviews_all_filters_optional() -> None:
    inp = ReviewsForItemInput(restaurant_id=2)
    assert inp.menu_item_id is None and inp.limit == 50


def test_reviews_one_sided_range_allowed() -> None:
    ReviewsForItemInput(restaurant_id=2, start_date=D1)
    ReviewsForItemInput(restaurant_id=2, end_date=D1)


def test_reviews_inverted_range_rejected() -> None:
    with pytest.raises(ValidationError):
        ReviewsForItemInput(restaurant_id=2, start_date=D2, end_date=D1)


@pytest.mark.parametrize("rating", [0, 6])
def test_reviews_max_rating_bounds(rating: int) -> None:
    with pytest.raises(ValidationError):
        ReviewsForItemInput(restaurant_id=2, max_rating=rating)


@pytest.mark.parametrize("limit", [0, 201])
def test_reviews_limit_bounds(limit: int) -> None:
    with pytest.raises(ValidationError):
        ReviewsForItemInput(restaurant_id=2, limit=limit)


@pytest.mark.parametrize("pct", [0, 51, 90])
def test_promotion_discount_bounds(pct: int) -> None:
    with pytest.raises(ValidationError):
        CreatePromotionInput(
            restaurant_id=1,
            menu_item_id=101,
            discount_pct=pct,
            start_date=D1,
            end_date=D2,
            reason="teste de limite",
        )


@pytest.mark.parametrize("pct", [1, 50])
def test_promotion_discount_edges_valid(pct: int) -> None:
    CreatePromotionInput(
        restaurant_id=1,
        menu_item_id=101,
        discount_pct=pct,
        start_date=D1,
        end_date=D2,
        reason="teste de limite",
    )


def test_promotion_inverted_dates_rejected() -> None:
    with pytest.raises(ValidationError):
        CreatePromotionInput(
            restaurant_id=1,
            menu_item_id=101,
            discount_pct=10,
            start_date=D2,
            end_date=D1,
            reason="datas invertidas",
        )


@pytest.mark.parametrize("reason", ["", "  ", "ab", "x" * 501])
def test_reason_length_enforced_after_strip(reason: str) -> None:
    with pytest.raises(ValidationError):
        PauseMenuItemInput(restaurant_id=1, menu_item_id=101, reason=reason)


def test_reason_is_stripped() -> None:
    inp = PauseMenuItemInput(restaurant_id=1, menu_item_id=101, reason="  acabou o estoque  ")
    assert inp.reason == "acabou o estoque"
