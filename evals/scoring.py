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
    ContainsAllExpected,
    ContainsAnyExpected,
    EntityExpected,
    NoDataExpected,
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


# Markdown emphasis and code marks around words ("o canal *app* zerou") are formatting, not
# content: they must not hide a phrase from the scorer.
_MARKDOWN = re.compile(r"[*_`~]+")


def _contains_phrase(haystack: str, phrase: str) -> bool:
    """Whole-word, case/accent-insensitive containment, ignoring Markdown emphasis."""
    needle = normalize(phrase)
    if not needle:
        return False
    text = normalize(_MARKDOWN.sub("", haystack))
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", text) is not None


def contains_any(text: str, values: Iterable[str]) -> bool:
    return any(_contains_phrase(text, v) for v in values)


# Phrases that signal an explicit decline / out-of-scope answer. "não sei" is deliberately NOT
# here: not knowing is a failure to answer, not a refusal.
REFUSAL_MARKERS: tuple[str, ...] = (
    "não posso",
    "não consigo ajudar",
    "não é possível",
    "não tenho acesso",
    "só tenho acesso",
    "tenho acesso apenas",
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


# "The dataset does not have it" phrasings (case/accent-insensitive, whole words).
NO_DATA_MARKERS: tuple[str, ...] = (
    "não existe",
    "não existem",
    "não há dado",
    "não há dados",
    "não há registro",
    "não tenho esse dado",
    "não tenho essa informação",
    "não tenho dados",
    "não temos esse dado",
    "não temos essa informação",
    "não registra",
    "não está disponível",
    "não estão disponíveis",
    "não disponível",
    "sem dados",
    "sem esse dado",
    "não traz",
    "não informa",
    "não possui",
    "não tem esse dado",
    "não tem essa informação",
    "não tem dados",
)
# "não foi possível verificar" is what the synthesizer says when a tool failed: an outage must not
# pass as "the dataset does not record it".

_BRL = re.compile(r"r\$|\breais\b|\bbrl\b", re.IGNORECASE)
_INR = re.compile(r"₹|\brs\.?\s*\d|\binr\b|\brupias?\b|\brúpias?\b", re.IGNORECASE)
# An amount of money, or a zero said in words: a "no data" answer must not give either.
_MONEY = re.compile(r"(?:r\$|₹|\brs\.?|\binr\b|\bbrl\b)\s*-?\d", re.IGNORECASE)
_ZERO_WORDS = re.compile(
    r"\bzero\b|\bnenhum(?:a)?\s+(?:pedido|venda|entrega)|\b0+(?:[.,]0+)?\s*(?:pedidos|vendas|reais)",
    re.IGNORECASE,
)


def says_no_data(text: str) -> bool:
    """Says the data is missing, without making up an amount or a zero for it: "vendeu R$ 0,00;
    não há registro" and "faturou ₹ 12.400; preço não disponível" both fail. Hours and dates
    ("das 0h às 23h", "02/03/2021") are fine; so are minutes and km, which a right answer may
    give for what the dataset does have (kitchen time, distance)."""
    return (
        contains_any(text, NO_DATA_MARKERS)
        and not _MONEY.search(text)
        and not _ZERO_WORDS.search(text)
    )


def currency_matches(text: str, unit: str | None) -> bool:
    """A money answer in the right currency: rupees need a rupee marker and no R$; reais must
    not be called rupees. Other units are not money."""
    if unit == "inr":
        return bool(_INR.search(text)) and not _BRL.search(text)
    if unit == "brl":
        return not _INR.search(text)
    return True


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
            return number_matches(answer.text, exp) and currency_matches(answer.text, exp.unit)
        case ContainsAnyExpected():
            return contains_any(answer.text, exp.values)
        case ContainsAllExpected():
            return all(contains_any(answer.text, group) for group in exp.groups)
        case EntityExpected():
            return contains_any(answer.text, [exp.value, *exp.aliases])
        case NoToolCalledExpected():
            return not (called & set(exp.tools))
        case RefusalExpected():
            return is_refusal(answer.text)
        case NoDataExpected():
            return says_no_data(answer.text)
    raise TypeError(f"unknown expected type: {type(exp).__name__}")  # pragma: no cover


class CaseScore(BaseModel):
    correct: bool
    expected_tools_called: bool
    # "the data does not exist" only counts after asking the tool that would have it: with the
    # lakehouse down the agent would say the same thing for the wrong reason.
    requires_expected_tools: bool = False
    forbidden_tools_called: list[str]
    tools_true_positive: int
    tools_called_unique: int
    tools_expected_unique: int

    @property
    def passed(self) -> bool:
        """A case passes if the answer is correct and no forbidden tool was called."""
        tools_ok = self.expected_tools_called or not self.requires_expected_tools
        return self.correct and not self.forbidden_tools_called and tools_ok


def score_case(case: Case, answer: AnswerResult) -> CaseScore:
    called = {t.name for t in answer.tools_called}
    expected = set(case.expected_tools)
    return CaseScore(
        correct=is_correct(case, answer),
        expected_tools_called=expected <= called,
        requires_expected_tools=isinstance(case.expected, NoDataExpected) and bool(expected),
        forbidden_tools_called=sorted(called & set(case.forbidden_tools)),
        tools_true_positive=len(called & expected),
        tools_called_unique=len(called),
        tools_expected_unique=len(expected),
    )
