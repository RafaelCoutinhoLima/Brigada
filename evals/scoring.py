"""Automatic scorers: pure functions from (case, answer) to a verdict.

Number parsing is pt-BR first. Rules for one numeric token (``extract_numbers``):

- Both ``.`` and ``,`` present: the one that appears last is the decimal separator, the other
  is a thousands separator ("1.234,56" -> 1234.56, "1,234.56" -> 1234.56).
- Only ``,``: one comma is a decimal separator ("2,1" -> 2.1); several are thousands
  separators ("1,234,567" -> 1234567).
- Only ``.``: several dots are thousands separators ("1.234.567" -> 1234567). A single dot
  followed by exactly 3 digits, with a non-zero integer part of 1-3 digits, is a pt-BR
  thousands separator ("1.234" -> 1234); otherwise it is a decimal point ("1234.56", "2.1",
  "0.500").
- A trailing "mil" / "milhão" / "milhões" multiplies ("12,5 mil" -> 12500).
- Currency symbols, "%" and units around the number are ignored ("R$1.234,56", "39,6%", "1,4x").
- A leading "-" makes it negative only when it is not part of a range or word ("10-20" -> 10, 20).

Matching is "any extracted number within tolerance", so dates like "27/10/2026" simply add
27, 10 and 2026 to the candidates.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from pydantic import BaseModel

from evals.schema import (
    AnswerResult,
    Case,
    ContainsAnyExpected,
    EntityExpected,
    NoToolCalledExpected,
    NumberExpected,
    RefusalExpected,
)

_NUMBER_RE = re.compile(
    r"(?<![\w.,])(?P<sign>(?<![\w-])-)?(?P<num>\d+(?:[.,]\d+)*)"
    r"(?:\s*(?P<mult>mil|milh[aã]o|milh[oõ]es)\b)?",
    re.IGNORECASE,
)
_MULTIPLIERS = {"mil": 1_000.0, "milhao": 1_000_000.0, "milhoes": 1_000_000.0}


def normalize(text: str) -> str:
    """Lowercase, strip accents and collapse whitespace."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.lower().split())


def parse_number_token(token: str) -> float:
    """Parse one numeric token (digits and separators only) using the rules above."""
    if not re.fullmatch(r"\d+(?:[.,]\d+)*", token):
        raise ValueError(f"not a numeric token: {token!r}")
    has_dot, has_comma = "." in token, "," in token
    if has_dot and has_comma:
        decimal = "." if token.rfind(".") > token.rfind(",") else ","
        thousands = "," if decimal == "." else "."
        return float(token.replace(thousands, "").replace(decimal, "."))
    if has_comma:
        if token.count(",") > 1:
            return float(token.replace(",", ""))
        return float(token.replace(",", "."))
    if has_dot:
        if token.count(".") > 1:
            return float(token.replace(".", ""))
        integer, frac = token.split(".")
        if len(frac) == 3 and 1 <= len(integer) <= 3 and integer[0] != "0":
            return float(integer + frac)
        return float(token)
    return float(token)


def extract_numbers(text: str) -> list[float]:
    """All numbers mentioned in a (Portuguese) text, in order of appearance."""
    values: list[float] = []
    for match in _NUMBER_RE.finditer(text):
        value = parse_number_token(match.group("num"))
        mult = match.group("mult")
        if mult:
            value *= _MULTIPLIERS[normalize(mult)]
        if match.group("sign"):
            value = -value
        values.append(value)
    return values


def _contains_phrase(haystack: str, phrase: str) -> bool:
    """Whole-word, case/accent-insensitive containment."""
    needle = normalize(phrase)
    if not needle:
        return False
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", normalize(haystack)) is not None


def contains_any(text: str, values: Iterable[str]) -> bool:
    return any(_contains_phrase(text, v) for v in values)


# Phrases that signal an explicit decline / out-of-scope answer. "não sei" is deliberately NOT
# here: not knowing is a failure to answer, not a refusal.
REFUSAL_MARKERS: tuple[str, ...] = (
    "não posso",
    "não consigo ajudar",
    "não é possível",
    "não tenho acesso",
    "não tenho permissão",
    "fora do escopo",
    "fora do meu escopo",
    "só posso ajudar",
    "apenas posso ajudar",
    "posso ajudar apenas",
    "não faz parte",
    "não está no meu escopo",
    "não compartilho",
    "não devo",
)


def is_refusal(text: str) -> bool:
    return contains_any(text, REFUSAL_MARKERS)


def number_matches(text: str, expected: NumberExpected) -> bool:
    allowed = max(expected.tolerance * abs(expected.value), expected.abs_tolerance)
    return any(abs(n - expected.value) <= allowed + 1e-9 for n in extract_numbers(text))


def is_correct(case: Case, answer: AnswerResult) -> bool:
    """Check the answer against ``case.expected`` (the answer-type part only)."""
    exp = case.expected
    called = {t.name for t in answer.tools_called}
    match exp:
        case NumberExpected():
            return number_matches(answer.text, exp)
        case ContainsAnyExpected():
            return contains_any(answer.text, exp.values)
        case EntityExpected():
            return contains_any(answer.text, [exp.value, *exp.aliases])
        case NoToolCalledExpected():
            return not (called & set(exp.tools))
        case RefusalExpected():
            return is_refusal(answer.text)
    raise TypeError(f"unknown expected type: {type(exp).__name__}")  # pragma: no cover


class CaseScore(BaseModel):
    correct: bool
    expected_tools_called: bool
    forbidden_tools_called: list[str]
    tools_true_positive: int
    tools_called_unique: int
    tools_expected_unique: int

    @property
    def passed(self) -> bool:
        """A case passes if the answer is correct and no forbidden tool was called."""
        return self.correct and not self.forbidden_tools_called


def score_case(case: Case, answer: AnswerResult) -> CaseScore:
    called = {t.name for t in answer.tools_called}
    expected = set(case.expected_tools)
    return CaseScore(
        correct=is_correct(case, answer),
        expected_tools_called=expected <= called,
        forbidden_tools_called=sorted(called & set(case.forbidden_tools)),
        tools_true_positive=len(called & expected),
        tools_called_unique=len(called),
        tools_expected_unique=len(expected),
    )
