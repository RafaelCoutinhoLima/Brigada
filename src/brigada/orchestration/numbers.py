"""Deterministic help for the critic: numbers in the answer that no finding contains."""

import re
from decimal import Decimal, InvalidOperation

_DATES = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b")
_NUMBER = re.compile(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?")


def readings(raw: str) -> set[Decimal]:
    """Every value ``raw`` can mean: ``1.234,56`` is pt-BR, ``132.857`` is either 132857
    (pt-BR thousands) or 132.857 (JSON decimal), so both are kept."""
    if "," in raw and "." in raw:
        candidates = [raw.replace(".", "").replace(",", ".")]
    elif "," in raw:
        candidates = [raw.replace(",", ".")]
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", raw):
        candidates = [raw.replace(".", ""), raw] if raw.count(".") == 1 else [raw.replace(".", "")]
    else:
        candidates = [raw]
    out: set[Decimal] = set()
    for c in candidates:
        try:
            out.add(Decimal(c).normalize())
        except InvalidOperation:
            continue
    return out


def numbers(text: str) -> list[tuple[str, set[Decimal]]]:
    """Each number of ``text`` (dates left out) with its possible values."""
    return [(raw, readings(raw)) for raw in _NUMBER.findall(_DATES.sub(" ", text))]


def untraced_numbers(answer: str, sources: list[str]) -> list[str]:
    """Numbers of ``answer`` absent from every source, also after rounding the source's
    numbers to 0, 1 or 2 decimals (an answer may round 132.857 to 132,86)."""
    known: set[Decimal] = set()
    for source in sources:
        for _, values in numbers(source):
            for n in values:
                known.add(n)
                known.update(n.quantize(Decimal(10) ** -p).normalize() for p in (0, 1, 2))
    missing = {raw: values for raw, values in numbers(answer) if not values & known}
    return sorted(missing)
