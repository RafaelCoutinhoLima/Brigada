"""Build ``evals/cases_real.jsonl``: eval cases over the real datasets (ADR 0009).

The answers are computed by ``evals.real_truth`` from the conformed silver of the Kaggle
downloads, never typed by hand and never read from the gold tables the tools query. The data is
not ours, so neither is the file: it is built locally and git-ignored. The agents answer from the
lakehouse in Databricks (same pipeline over the same files), so a disagreement is a finding.

Usage::

    uv run python -m evals.build_real_cases \\
        --delivery-center ~/Downloads/delivery-center \\
        --zomato ~/Downloads/zomato/order_history_kaggle_data.csv
    uv run python -m evals.run --cases evals/cases_real.jsonl

Every "which one" case asserts a 10% margin between the 1st and 2nd place and fails the build
otherwise (``MarginTooSmallError``): a near tie is rewritten, never silently dropped.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from evals.real_truth import (
    Silver,
    baseline,
    canceled_orders,
    change_pct,
    completed_orders,
    delivered,
    holidays_of,
    item_change,
    item_qty,
    mean_of,
    range_vs_baseline,
    revenue,
    slowest_group,
    top_with_margin,
)
from evals.schema import (
    Case,
    ContainsAllExpected,
    ContainsAnyExpected,
    EntityExpected,
    NoDataExpected,
    NumberExpected,
)

ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = Path(__file__).with_name("cases_real.jsonl")

DC_TODAY = date(2021, 5, 1)  # Delivery Center ends on 2021-04-30
ZOMATO_TODAY = date(2025, 2, 1)  # Zomato ends on 2025-01-31
# The two busiest restaurants of each dataset (by completed orders) and two special ones.
PAPA_SUCIS, IUMPICA = 1_000_053, 1_000_676  # Rio de Janeiro, São Paulo
DC_STOPPED = 1_000_361  # last order on 2021-03-02
AURA_S4, AURA_GK2 = 120_659_868, 120_635_699
SWAAD_GK2 = 120_554_001

WEEKDAYS = {
    1: ["segunda", "segunda-feira"],
    2: ["terça", "terça-feira"],
    3: ["quarta", "quarta-feira"],
    4: ["quinta", "quinta-feira"],
    5: ["sexta", "sexta-feira"],
    6: ["sábado"],
    7: ["domingo"],
}
MONTHS = ["", "janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
          "setembro", "outubro", "novembro", "dezembro"]  # fmt: skip
HOLIDAY_WORDS = ["feriado"]


def day_words(d: date) -> list[str]:
    return [f"{d.day:02d}/{d.month:02d}", f"{d.day}/{d.month}", f"{d.day} de {MONTHS[d.month]}"]


def month(year: int, m: int) -> tuple[date, date]:
    start = date(year, m, 1)
    nxt = date(year + (m == 12), m % 12 + 1, 1)
    return start, nxt - timedelta(days=1)


@dataclass(frozen=True)
class Spec:
    id: str
    restaurant_id: int
    today: date
    question: str
    category: str
    tools: tuple[str, ...]
    build: Callable[[Silver, Sequence], tuple[object, str]]  # -> (expected, notes)
    subsets: tuple[str, ...] = ("real",)


def _pct(value: float | None, note: str) -> tuple[NumberExpected, str]:
    if value is None:
        raise ValueError(f"no percentage: {note}")
    return NumberExpected(value=value, tolerance=0.0, abs_tolerance=0.3, unit="percent"), note


def _money(value: float, currency: str, note: str) -> tuple[NumberExpected, str]:
    unit = "brl" if currency == "BRL" else "inr"
    return NumberExpected(value=value, tolerance=0.005, unit=unit), note


def _minutes(value: float | None, note: str) -> tuple[NumberExpected, str]:
    if value is None:
        raise ValueError(f"no minutes: {note}")
    return NumberExpected(value=value, tolerance=0.0, abs_tolerance=0.5, unit="minutes"), note


def _count(value: int, note: str) -> tuple[NumberExpected, str]:
    return NumberExpected(value=float(value), tolerance=0.0, unit="count"), note


def _no_data(note: str) -> tuple[NoDataExpected, str]:
    return NoDataExpected(), note


# Delivery Center ----------------------------------------------------------------------------

APRIL, MARCH, JAN = month(2021, 4), month(2021, 3), (date(2021, 1, 2), date(2021, 1, 31))
YESTERDAY_DC = DC_TODAY - timedelta(days=1)
LAST7_DC = (DC_TODAY - timedelta(days=7), YESTERDAY_DC)


def dc_yesterday(s: Silver, h: Sequence) -> tuple[object, str]:
    v = revenue(s, PAPA_SUCIS, YESTERDAY_DC, YESTERDAY_DC)
    return _money(v, "BRL", f"revenue of completed orders on {YESTERDAY_DC}")


def dc_last7_vs_normal(s: Silver, h: Sequence) -> tuple[object, str]:
    rev, base, pct = range_vs_baseline(s, IUMPICA, *LAST7_DC, h)
    return _pct(pct, f"{LAST7_DC[0]}..{LAST7_DC[1]}: revenue {rev} vs summed baseline {base}")


def dc_own_channel_share(s: Silver, h: Sequence) -> tuple[object, str]:
    own = revenue(s, PAPA_SUCIS, *APRIL, channel="canal_proprio")
    total = revenue(s, PAPA_SUCIS, *APRIL)
    return _pct(round(own / total * 100, 2), f"canal_proprio {own} / total {total}, April")


def dc_april_vs_march(s: Silver, h: Sequence) -> tuple[object, str]:
    apr, mar = revenue(s, PAPA_SUCIS, *APRIL), revenue(s, PAPA_SUCIS, *MARCH)
    return _pct(change_pct(apr, mar), f"month totals: April {apr} vs March {mar}")


def dc_ticket_april(s: Silver, h: Sequence) -> tuple[object, str]:
    t = {
        k: revenue(s, PAPA_SUCIS, *p) / completed_orders(s, PAPA_SUCIS, *p)
        for k, p in (("jan", JAN), ("apr", APRIL))
    }
    note = f"ticket January {t['jan']:.2f} -> April {t['apr']:.2f}"
    return _money(round(t["apr"], 2), "BRL", note)


def dc_canceled_april(s: Silver, h: Sequence) -> tuple[object, str]:
    return _count(canceled_orders(s, PAPA_SUCIS, *APRIL), "canceled orders, April")


def dc_cancel_rate_april(s: Silver, h: Sequence) -> tuple[object, str]:
    c, done = canceled_orders(s, IUMPICA, *APRIL), completed_orders(s, IUMPICA, *APRIL)
    rate = c / (c + done) * 100
    note = f"{c} canceled / {c + done} orders (canceled / completed would be {c / done:.4%})"
    return NumberExpected(value=round(rate, 2), abs_tolerance=0.02, unit="percent"), note


def dc_delivery_april(s: Silver, h: Sequence) -> tuple[object, str]:
    d = delivered(s, PAPA_SUCIS, *APRIL)
    return _minutes(mean_of(d, "delivery_minutes"), f"mean over {d.delivery_minutes.count()}")


def dc_slowest_weekday(s: Silver, h: Sequence) -> tuple[object, str]:
    start, _ = s.data_range(IUMPICA)
    (wd,), mean, margin = slowest_group(s, IUMPICA, start, YESTERDAY_DC, ["iso_weekday"])
    names = WEEKDAYS[int(wd)]
    exp = EntityExpected(value=names[0], aliases=names[1:])
    return exp, f"{names[0]}: {mean} min, margin {margin:.1%}, {start}..{YESTERDAY_DC}"


def dc_slowest_slot_march(s: Silver, h: Sequence) -> tuple[object, str]:
    (wd, hour), mean, margin = slowest_group(s, IUMPICA, *MARCH, ["iso_weekday", "local_hour"])
    hh = int(hour)
    groups = [WEEKDAYS[int(wd)], [f"{hh}h", f"{hh} h", f"{hh}:00", f"{hh} horas", f"{hh}hs"]]
    exp = ContainsAllExpected(groups=groups)
    return exp, f"{groups[0][0]} {int(hour)}h: {mean} min, margin {margin:.1%}"


def dc_weakest_weekday_april(s: Silver, h: Sequence) -> tuple[object, str]:
    days = [APRIL[0] + timedelta(days=i) for i in range(30)]
    by_wd: dict[int, list[float]] = {}
    for d in days:
        by_wd.setdefault(d.isoweekday(), []).append(revenue(s, IUMPICA, d, d))
    import pandas as pd

    means = pd.Series({wd: sum(v) / len(v) for wd, v in by_wd.items()})
    wd, margin = top_with_margin(means, largest=False)
    names = WEEKDAYS[int(wd)]
    note = f"mean revenue per day in April: {names[0]} {means[wd]:.2f}, margin {margin:.1%}"
    return EntityExpected(value=names[0], aliases=names[1:]), note


def dc_stopped(s: Silver, h: Sequence) -> tuple[object, str]:
    _, last = s.data_range(DC_STOPPED)
    return _no_data(f"data_until {last}: later days are missing, not zero")


def dc_promise(s: Silver, h: Sequence) -> tuple[object, str]:
    return _no_data("Delivery Center records no promised time")


def dc_kitchen_april(s: Silver, h: Sequence) -> tuple[object, str]:
    d = delivered(s, IUMPICA, *APRIL)
    return _minutes(mean_of(d, "prep_minutes"), f"prep over {d.prep_minutes.count()} orders")


def dc_friday_night_culprit(s: Silver, h: Sequence) -> tuple[object, str]:
    start, _ = s.data_range(IUMPICA)
    d = delivered(s, IUMPICA, start, YESTERDAY_DC).dropna(subset=["prep_minutes"])
    slot = (d.iso_weekday == 5) & (d.local_hour == 20)
    import pandas as pd

    rise = pd.Series(
        {
            p: d[slot][f"{p}_minutes"].mean() - d[~slot][f"{p}_minutes"].mean()
            for p in ("prep", "pickup", "transit")
        }
    )
    phase, margin = top_with_margin(rise)
    words = {
        "prep": ["cozinha", "preparo", "produção"],
        "pickup": ["coleta", "retirada", "entregador demora a sair"],
        "transit": ["trânsito", "percurso", "entregador"],
    }[str(phase)]
    note = f"Friday 20h minus the rest: {rise.round(2).to_dict()}, margin {margin:.1%}"
    return ContainsAnyExpected(values=words), note


def dc_holiday_drop(s: Silver, h: Sequence) -> tuple[object, str]:
    day = date(2021, 4, 2)
    name = holidays_of(s, PAPA_SUCIS, h)[day]
    base, _ = baseline(s, PAPA_SUCIS, day, h)
    pct = change_pct(revenue(s, PAPA_SUCIS, day, day), base)
    exp = ContainsAnyExpected(values=[*HOLIDAY_WORDS, "Paixão", "Sexta-feira Santa"])
    return exp, f"{day} was {name}: {pct}% vs normal Fridays"


def dc_april_30_vs_normal(s: Silver, h: Sequence) -> tuple[object, str]:
    day = YESTERDAY_DC
    base, n = baseline(s, PAPA_SUCIS, day, h)
    rev = revenue(s, PAPA_SUCIS, day, day)
    return _pct(change_pct(rev, base), f"{rev} vs baseline {base} ({n} normal Fridays)")


# Zomato -------------------------------------------------------------------------------------

YESTERDAY_Z = ZOMATO_TODAY - timedelta(days=1)
LAST7_Z = (ZOMATO_TODAY - timedelta(days=7), YESTERDAY_Z)
JAN_Z = month(2025, 1)


def z_yesterday_vs_normal(s: Silver, h: Sequence) -> tuple[object, str]:
    rev, base, pct = range_vs_baseline(s, AURA_S4, YESTERDAY_Z, YESTERDAY_Z, h)
    return _pct(pct, f"{rev} vs baseline {base}")


def z_last_week(s: Silver, h: Sequence) -> tuple[object, str]:
    v = revenue(s, AURA_GK2, *LAST7_Z)
    return _money(v, "INR", f"{LAST7_Z[0]}..{LAST7_Z[1]}, rupees")


def z_top_dish(s: Silver, h: Sequence) -> tuple[object, str]:
    qty = item_qty(s, AURA_S4, YESTERDAY_Z - timedelta(days=27), YESTERDAY_Z)
    name, margin = top_with_margin(qty)
    return EntityExpected(value=str(name)), f"{name}: {qty[name]} units, margin {margin:.1%}"


def z_dish_drop(s: Silver, h: Sequence) -> tuple[object, str]:
    change = item_change(s, AURA_GK2, YESTERDAY_Z, 28)
    name, margin = top_with_margin(change, largest=False)
    return EntityExpected(value=str(name)), f"{name}: {change[name]:+d}, margin {margin:.1%}"


def z_dish_rise(s: Silver, h: Sequence) -> tuple[object, str]:
    change = item_change(s, AURA_GK2, YESTERDAY_Z, 28)
    name, margin = top_with_margin(change)
    return EntityExpected(value=str(name)), f"{name}: {change[name]:+d}, margin {margin:.1%}"


def z_dish_drop_swaad(s: Silver, h: Sequence) -> tuple[object, str]:
    change = item_change(s, SWAAD_GK2, YESTERDAY_Z, 28)
    name, margin = top_with_margin(change, largest=False)
    return EntityExpected(value=str(name)), f"{name}: {change[name]:+d}, margin {margin:.1%}"


def z_canceled_january(s: Silver, h: Sequence) -> tuple[object, str]:
    return _count(canceled_orders(s, AURA_GK2, *JAN_Z), "canceled (rejected, returned, timed out)")


def z_dish_revenue(s: Silver, h: Sequence) -> tuple[object, str]:
    return _no_data("Zomato has no price per dish: only quantities")


def z_delivery_time(s: Silver, h: Sequence) -> tuple[object, str]:
    return _no_data("Zomato records no delivery time (only distance and kitchen time)")


def z_kitchen_january(s: Silver, h: Sequence) -> tuple[object, str]:
    d = delivered(s, AURA_GK2, *JAN_Z)
    return _minutes(mean_of(d, "prep_minutes"), f"KPT over {d.prep_minutes.count()} orders")


def z_rider_wait_january(s: Silver, h: Sequence) -> tuple[object, str]:
    d = delivered(s, AURA_S4, *JAN_Z)
    return _minutes(mean_of(d, "rider_wait_minutes"), "courier waiting for the food")


def z_diwali(s: Silver, h: Sequence) -> tuple[object, str]:
    day = date(2024, 10, 31)
    assert holidays_of(s, AURA_S4, h)[day] == "Diwali"
    pct = change_pct(revenue(s, AURA_S4, day, day), baseline(s, AURA_S4, day, h)[0])
    return ContainsAnyExpected(values=["Diwali", *HOLIDAY_WORDS]), f"{day}: {pct}% vs normal"


# Gaps: in scope, in the data, but no tool exposes them yet. The right answer is "not available".


def gap(note: str) -> Callable[[Silver, Sequence], tuple[object, str]]:
    return lambda s, h: _no_data(f"no tool exposes it yet: {note}")


SPECS: tuple[Spec, ...] = (
    Spec("real-dc-001", PAPA_SUCIS, DC_TODAY, "Quanto eu vendi ontem?", "sales",
         ("sales_vs_baseline",), dc_yesterday),
    Spec("real-dc-002", IUMPICA, DC_TODAY,
         "Como foram as vendas dos últimos 7 dias comparado com o normal?", "diagnosis",
         ("sales_vs_baseline",), dc_last7_vs_normal),
    Spec("real-dc-003", PAPA_SUCIS, DC_TODAY,
         "Quanto do faturamento de abril veio do canal próprio?", "sales",
         ("sales_vs_baseline",), dc_own_channel_share),
    Spec("real-dc-004", PAPA_SUCIS, DC_TODAY,
         "O total de vendas de abril caiu em relação ao total de março? Quanto, em porcentagem?",
         "diagnosis",
         ("sales_vs_baseline",), dc_april_vs_march),
    Spec("real-dc-005", PAPA_SUCIS, DC_TODAY,
         "Qual foi o meu ticket médio em abril, e ele caiu desde janeiro?", "sales",
         ("sales_vs_baseline",), dc_ticket_april),
    Spec("real-dc-006", PAPA_SUCIS, DC_TODAY, "Quantos pedidos foram cancelados em abril?",
         "sales", ("sales_vs_baseline",), dc_canceled_april),
    Spec("real-dc-007", IUMPICA, DC_TODAY, "Qual foi minha taxa de cancelamento em abril?",
         "sales", ("sales_vs_baseline",), dc_cancel_rate_april),
    Spec("real-dc-008", PAPA_SUCIS, DC_TODAY, "Qual o tempo médio de entrega em abril?",
         "operations", ("delivery_performance",), dc_delivery_april),
    Spec("real-dc-009", IUMPICA, DC_TODAY, "Em que dia da semana a entrega demora mais?",
         "operations", ("delivery_performance",), dc_slowest_weekday),
    Spec("real-dc-010", IUMPICA, DC_TODAY,
         "Em março, qual dia da semana e horário teve a entrega mais lenta?", "operations",
         ("delivery_performance",), dc_slowest_slot_march),
    Spec("real-dc-011", IUMPICA, DC_TODAY, "Em abril, qual dia da semana vendeu menos?",
         "sales", ("sales_vs_baseline",), dc_weakest_weekday_april),
    Spec("real-dc-012", DC_STOPPED, DC_TODAY, "Quanto eu vendi ontem?", "sales",
         ("sales_vs_baseline",), dc_stopped),
    Spec("real-dc-013", PAPA_SUCIS, DC_TODAY,
         "Quantos pedidos atrasaram em relação ao prazo prometido em abril?", "operations",
         ("delivery_performance",), dc_promise),
    Spec("real-dc-014", IUMPICA, DC_TODAY,
         "Quanto tempo minha cozinha leva, em média, para preparar os pedidos em abril?",
         "operations", ("delivery_performance",), dc_kitchen_april),
    Spec("real-dc-015", IUMPICA, DC_TODAY,
         "Na sexta às 20h a entrega demora muito: é culpa da cozinha ou do entregador?",
         "diagnosis", ("delivery_performance",), dc_friday_night_culprit),
    Spec("real-dc-016", PAPA_SUCIS, DC_TODAY, "Por que as vendas caíram no dia 2 de abril?",
         "diagnosis", ("sales_vs_baseline",), dc_holiday_drop),
    Spec("real-dc-017", PAPA_SUCIS, DC_TODAY,
         "Como foi ontem comparado com uma sexta normal, em porcentagem?", "diagnosis",
         ("sales_vs_baseline",), dc_april_30_vs_normal),
    Spec("real-zomato-001", AURA_S4, ZOMATO_TODAY,
         "Como foram as vendas de ontem comparado com o normal, em porcentagem?", "diagnosis",
         ("sales_vs_baseline",), z_yesterday_vs_normal),
    Spec("real-zomato-002", AURA_GK2, ZOMATO_TODAY, "Quanto eu vendi na última semana?",
         "sales", ("sales_vs_baseline",), z_last_week),
    Spec("real-zomato-003", AURA_S4, ZOMATO_TODAY,
         "Qual prato mais vendeu nas últimas 4 semanas?", "menu", ("item_trends",), z_top_dish),
    Spec("real-zomato-004", AURA_GK2, ZOMATO_TODAY,
         "Qual prato mais caiu nas últimas 4 semanas?", "menu", ("item_trends",), z_dish_drop),
    Spec("real-zomato-005", AURA_GK2, ZOMATO_TODAY,
         "Qual prato mais cresceu nas últimas 4 semanas?", "menu", ("item_trends",),
         z_dish_rise),
    Spec("real-zomato-006", SWAAD_GK2, ZOMATO_TODAY,
         "Que prato está perdendo saída nas últimas 4 semanas?", "menu", ("item_trends",),
         z_dish_drop_swaad),
    Spec("real-zomato-007", AURA_GK2, ZOMATO_TODAY, "Quantos pedidos foram cancelados em janeiro?",
         "sales", ("sales_vs_baseline",), z_canceled_january),
    Spec("real-zomato-008", AURA_S4, ZOMATO_TODAY,
         "Qual prato me deu mais faturamento nas últimas 4 semanas?", "menu", ("item_trends",),
         z_dish_revenue),
    Spec("real-zomato-009", AURA_S4, ZOMATO_TODAY, "Qual o tempo médio de entrega em janeiro?",
         "operations", ("delivery_performance",), z_delivery_time),
    Spec("real-zomato-010", AURA_GK2, ZOMATO_TODAY,
         "Quanto tempo a cozinha levou, em média, para preparar os pedidos em janeiro?",
         "operations", ("delivery_performance",), z_kitchen_january),
    Spec("real-zomato-011", AURA_S4, ZOMATO_TODAY,
         "Quanto tempo o entregador esperou no balcão, em média, em janeiro?", "operations",
         ("delivery_performance",), z_rider_wait_january),
    Spec("real-zomato-012", AURA_S4, ZOMATO_TODAY,
         "Como foram as vendas no dia 31 de outubro de 2024?", "diagnosis",
         ("sales_vs_baseline",), z_diwali),
    Spec("real-gap-001", AURA_GK2, ZOMATO_TODAY, "Qual a minha nota média em janeiro?",
         "menu", (), gap("rating is in silver, not in any tool"), ("real", "gap")),
    Spec("real-gap-002", AURA_S4, ZOMATO_TODAY, "Quanto eu dei de desconto em janeiro?",
         "sales", (), gap("discount columns stay in bronze"), ("real", "gap")),
    Spec("real-gap-003", AURA_S4, ZOMATO_TODAY, "Quantos clientes voltaram a pedir em janeiro?",
         "sales", (), gap("customer id stays in bronze (personal data)"), ("real", "gap")),
    Spec("real-gap-004", AURA_GK2, ZOMATO_TODAY,
         "Por que os pedidos foram rejeitados no dia 1º de janeiro?", "operations", (),
         gap("rejection reason stays in bronze"), ("real", "gap")),
    Spec("real-gap-005", PAPA_SUCIS, DC_TODAY, "Em que horário os clientes mais cancelam?",
         "operations", (), gap("canceled orders per hour are not in gold"), ("real", "gap")),
    Spec("real-gap-006", IUMPICA, DC_TODAY,
         "Quanto faturamento eu perdi com pedidos cancelados em abril?", "sales", (),
         gap("canceled revenue is not in gold"), ("real", "gap")),
)  # fmt: skip


def build_cases(s: Silver, holidays: Sequence) -> list[Case]:
    cases = []
    for spec in SPECS:
        try:
            expected, note = spec.build(s, holidays)
        except Exception as exc:
            raise RuntimeError(f"{spec.id}: {exc}") from exc
        cases.append(
            Case(
                id=spec.id,
                restaurant_id=spec.restaurant_id,
                today=spec.today,
                question=spec.question,
                category=spec.category,  # type: ignore[arg-type]
                expected=expected,  # type: ignore[arg-type]
                expected_tools=list(spec.tools),  # type: ignore[arg-type]
                subsets=list(spec.subsets),
                notes=note,
            )
        )
    return cases


def silver_from_downloads(delivery_center: Path, zomato_file: Path) -> Silver:
    """Run the pipeline's own bronze -> silver on a local Spark over the downloads."""
    sys.path.insert(0, str(ROOT / "lakehouse" / "src"))
    from brigada_lakehouse.build import build
    from pyspark.sql import SparkSession

    from scripts.upload_source import find_files, lay_out

    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp)
        lay_out("delivery_center", find_files("delivery_center", delivery_center), raw)
        lay_out("zomato", find_files("zomato", zomato_file.parent), raw)
        spark = (
            SparkSession.builder.master("local[4]")
            .config("spark.driver.memory", "4g")
            .config("spark.ui.enabled", "false")
            .getOrCreate()
        )
        spark.sparkContext.setLogLevel("ERROR")
        t = build(spark, str(raw), ["delivery_center", "zomato"])
        frames = [
            t[name].toPandas()
            for name in ("silver_orders", "silver_order_items", "silver_menu_items")
        ]
        restaurants = t["silver_restaurants"].toPandas()
        spark.stop()
    return Silver.from_frames(*frames, restaurants)


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build the real-dataset eval cases.")
    p.add_argument("--delivery-center", type=Path, required=True, help="unzipped folder")
    p.add_argument("--zomato", type=Path, required=True, help="order_history_kaggle_data.csv")
    p.add_argument("--out", type=Path, default=OUT_PATH)
    args = p.parse_args(argv)

    sys.path.insert(0, str(ROOT / "lakehouse" / "src"))
    from brigada_lakehouse.holidays import HOLIDAYS

    silver = silver_from_downloads(args.delivery_center.expanduser(), args.zomato.expanduser())
    cases = build_cases(silver, HOLIDAYS)
    args.out.write_text(
        "".join(c.model_dump_json(exclude_none=True) + "\n" for c in cases), encoding="utf-8"
    )
    for c in cases:
        print(f"{c.id:18} {c.restaurant_id:>10}  {c.notes}")
    print(f"\n{len(cases)} cases -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
