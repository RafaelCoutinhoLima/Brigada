"""Holidays that matter to the baseline of ``gold_daily_channel_sales``.

A holiday is not a normal day: inside the "same weekday in the previous 4 weeks" baseline it
makes the following weeks look like a fall (or a rise) that did not happen. Gold leaves these
days out of every baseline and flags them on their own row.

Only the periods the data covers, only legal holidays (pontos facultativos such as Carnaval are
left out: commerce opens, and in 2021 many cities cancelled it), each with its legal source.
A drop in the data is never a source: in 2021 Rio de Janeiro moved São Jorge (23/04) to 30/03,
so 23/04/2021 was a normal day there even though sales fell.

Scope: ``state`` None = the whole country; ``city`` set = that city only, and then ``state``
must match too. Restaurants without a state (Brigada's synthetic ones) only get national days.
Delhi NCR spans Delhi, Haryana and Uttar Pradesh; the Zomato file has no state, so the Delhi
government list is applied to the whole region (an approximation, recorded in ADR 0009).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Holiday:
    country: str  # ISO 3166-1 alpha-2, as in the restaurant contract
    state: str | None
    city: str | None
    day: date
    name: str
    source: str


_BR = "Lei 662/1949 (Lei 10.607/2002)"
_BR_RELIGIOUS = "Lei 9.093/1995 (Sexta-feira da Paixão, adotada em todo o país)"
_RJ_2021 = "Lei estadual RJ 9.224/2021 (feriados de 26/03 a 01/04/2021; antecipa 21/04 e 23/04)"
_SP_2021 = "Decreto municipal SP 60.131/2021 (antecipa 5 feriados para 26/03 a 01/04/2021)"
_DELHI_2024 = "GNCTD, Notificação F.53/538/GAD/CN/2022 (lista de 2024)"
_DELHI_CHHATH = "GNCTD, Notificação F.53/711/GAD/CN/2024 (Chhath Puja, 07/11/2024)"
_DELHI_2025 = "GNCTD, Notificação F.53/689/GAD/CN/2024-25 (lista de 2025)"


def _br(day: str, name: str, source: str = _BR) -> Holiday:
    return Holiday("BR", None, None, date.fromisoformat(day), name, source)


def _city(state: str, city: str, day: str, name: str, source: str) -> Holiday:
    return Holiday("BR", state, city, date.fromisoformat(day), name, source)


def _delhi(day: str, name: str, source: str) -> Holiday:
    return Holiday("IN", None, "Delhi NCR", date.fromisoformat(day), name, source)


HOLIDAYS: tuple[Holiday, ...] = (
    # Brazil, Delivery Center (2020-12-31 to 2021-04-30)
    _br("2021-01-01", "Confraternização Universal"),
    _br("2021-04-02", "Sexta-feira da Paixão", _BR_RELIGIOUS),
    # Tiradentes is federal, but RJ moved it to 29/03 by state law: listed per state.
    *(Holiday("BR", uf, None, date(2021, 4, 21), "Tiradentes", _BR) for uf in ("SP", "RS", "PR")),
    _city("RJ", "Rio de Janeiro", "2021-01-20", "São Sebastião", "Feriado municipal do Rio"),
    _city("RJ", "Rio de Janeiro", "2021-03-26", "Feriado da pandemia", _RJ_2021),
    _city("RJ", "Rio de Janeiro", "2021-03-29", "Tiradentes (antecipado)", _RJ_2021),
    _city("RJ", "Rio de Janeiro", "2021-03-30", "São Jorge (antecipado)", _RJ_2021),
    _city("RJ", "Rio de Janeiro", "2021-03-31", "Feriado da pandemia", _RJ_2021),
    _city("RJ", "Rio de Janeiro", "2021-04-01", "Feriado da pandemia", _RJ_2021),
    _city("SP", "São Paulo", "2021-01-25", "Aniversário de São Paulo", "Feriado municipal"),
    _city("SP", "São Paulo", "2021-03-26", "Corpus Christi 2021 (antecipado)", _SP_2021),
    _city("SP", "São Paulo", "2021-03-29", "Consciência Negra 2021 (antecipado)", _SP_2021),
    _city("SP", "São Paulo", "2021-03-30", "Aniversário de SP 2022 (antecipado)", _SP_2021),
    _city("SP", "São Paulo", "2021-03-31", "Corpus Christi 2022 (antecipado)", _SP_2021),
    _city("SP", "São Paulo", "2021-04-01", "Consciência Negra 2022 (antecipado)", _SP_2021),
    _city("RS", "Porto Alegre", "2021-02-02", "Nossa Senhora dos Navegantes", "Feriado municipal"),
    # Brazil, Brigada synthetic data (around August to October 2026)
    _br("2026-09-07", "Independência do Brasil"),
    _br("2026-10-12", "Nossa Senhora Aparecida"),
    # India, Zomato (2024-09-01 to 2025-01-31)
    _delhi("2024-09-16", "Milad-un-Nabi", _DELHI_2024),
    _delhi("2024-10-02", "Gandhi Jayanti", _DELHI_2024),
    _delhi("2024-10-12", "Dussehra", _DELHI_2024),
    _delhi("2024-10-17", "Maharishi Valmiki Jayanti", _DELHI_2024),
    _delhi("2024-10-31", "Diwali", _DELHI_2024),
    _delhi("2024-11-07", "Chhath Puja", _DELHI_CHHATH),
    _delhi("2024-11-15", "Guru Nanak Jayanti", _DELHI_2024),
    _delhi("2024-12-25", "Christmas", _DELHI_2024),
    _delhi("2025-01-26", "Republic Day", _DELHI_2025),
)
