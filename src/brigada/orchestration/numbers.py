"""Deterministic help for the critic: numbers and dates in the answer that no tool returned."""

import re
from decimal import Decimal, InvalidOperation

_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DAY_MONTH = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_NUMBER = re.compile(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?")
# Longer digit runs are ids (orders, boletos), not amounts; they would also overflow Decimal.
MAX_DIGITS = 20


def readings(raw: str, *, answer: bool = False) -> set[Decimal]:
    """Every value ``raw`` can mean. ``1.234,56`` is pt-BR. In a source (tool JSON),
    ``132.857`` is either 132857 or the decimal 132.857, so both are kept; in the answer,
    written in pt-BR for the owner, ``5.000`` only means five thousand."""
    if sum(c.isdigit() for c in raw) > MAX_DIGITS:
        return set()
    if "," in raw and "." in raw:
        candidates = [raw.replace(".", "").replace(",", ".")]
    elif "," in raw:
        candidates = [raw.replace(",", ".")]
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", raw):
        dual = raw.count(".") == 1 and not answer
        candidates = [raw.replace(".", ""), raw] if dual else [raw.replace(".", "")]
    else:
        candidates = [raw]
    out: set[Decimal] = set()
    for c in candidates:
        try:
            out.add(Decimal(c).normalize())
        except InvalidOperation:
            continue
    return out


def dates(text: str) -> list[tuple[str, tuple[int, int]]]:
    """``(raw, (day, month))`` for ISO dates and valid dd/mm[/aaaa] in ``text``."""
    found = [(m.group(0), (int(m.group(3)), int(m.group(2)))) for m in _ISO.finditer(text)]
    for m in _DAY_MONTH.finditer(text):
        day, month = int(m.group(1)), int(m.group(2))
        if 1 <= day <= 31 and 1 <= month <= 12:
            found.append((m.group(0), (day, month)))
    return found


def numbers(text: str, *, answer: bool = False) -> list[tuple[str, set[Decimal]]]:
    """Each number of ``text`` with its possible values. Dates are not numbers here; a
    ``d/d`` that is not a valid date (``12/40``) is read as two numbers."""
    text = _ISO.sub(" ", text)
    text = _DAY_MONTH.sub(
        lambda m: (
            " "
            if 1 <= int(m.group(1)) <= 31 and 1 <= int(m.group(2)) <= 12
            else m.group(0).replace("/", " ")
        ),
        text,
    )
    return [(raw, readings(raw, answer=answer)) for raw in _NUMBER.findall(text)]


def untraced_numbers(answer: str, sources: list[str]) -> list[str]:
    """Numbers and dates of ``answer`` absent from every source, also after rounding the
    source's numbers to 0, 1 or 2 decimals (an answer may round 132.857 to 132,86). A
    fraction like ``3/4`` reads as a date, so it is reported unless a source has that day."""
    known: set[Decimal] = set()
    known_days: set[tuple[int, int]] = set()
    for source in sources:
        known_days.update(day for _, day in dates(source))
        for _, values in numbers(source):
            for n in values:
                known.add(n)
                for p in (0, 1, 2):
                    try:
                        known.add(n.quantize(Decimal(10) ** -p).normalize())
                    except InvalidOperation:
                        continue
    missing = {raw for raw, values in numbers(answer, answer=True) if not values & known}
    missing |= {raw for raw, day in dates(answer) if day not in known_days}
    return sorted(missing)
