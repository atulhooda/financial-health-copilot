"""The per-turn number registry (COPILOT.md §6): every number a tool returned, or the user typed.

The LLM only ever sees numbers as registry entries {id, kind, display, desc}; the validator matches every number
in an answer back to an entry of this turn. Kinds: fact | prediction | recommendation | assumption | user_input.
Recommendation and assumption entries of one simulated action share a `group`, so the validator can check that a
recommendation states the assumptions it rests on (Phase 5 item 8).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from app.copilot.numbers import Num, date_display
from app.core.money import format_inr

PREFIX = {"fact": "F", "prediction": "P", "recommendation": "R", "assumption": "A", "user_input": "U"}
UNITS = ("inr", "pct", "months", "days", "years", "date", "score", "count", "flag")
BARE_OK = ("count", "score", "months", "days", "years")  # a number with no unit in the text (COPILOT.md §7)


@dataclass
class Entry:
    id: str
    kind: str
    unit: str
    value: object  # Decimal (rupees, percent, months ...), dt.date, or None for a flag assumption
    desc: str
    group: str | None = None
    key: str | None = None  # a semantic handle for templates, e.g. "income_monthly"
    phrases: dict[str, list[str]] = field(default_factory=dict)  # assumptions: words that state them, per language
    shown: dict[str, str] | None = None  # explicit display per language (e.g. "over 99%"), else computed

    @property
    def numeric(self) -> bool:
        return self.unit != "flag"

    def display(self, language: str = "en") -> str:
        if self.shown:
            return self.shown.get(language) or self.shown["en"]
        v = self.value
        if self.unit == "inr":
            return format_inr(int((v * 100).to_integral_value(ROUND_HALF_UP)))
        if self.unit == "pct":
            q = v.quantize(Decimal("1") if abs(v) >= 10 or v == v.to_integral_value() else Decimal("0.1"),
                           ROUND_HALF_UP)
            return f"{q.normalize():f}%"
        if self.unit == "months":
            q = v.quantize(Decimal("1") if v == v.to_integral_value() else Decimal("0.1"), ROUND_HALF_UP)
            word = {"en": "months", "hi": "महीने", "hinglish": "mahine"}[language]
            return f"{q:f} {word}"
        if self.unit in ("days", "years"):
            word = {("days", "en"): "days", ("days", "hi"): "दिन", ("days", "hinglish"): "din",
                    ("years", "en"): "years", ("years", "hi"): "साल", ("years", "hinglish"): "saal"}[(self.unit, language)]
            return f"{v:f} {word}"
        if self.unit == "date":
            return date_display(v, language)
        if self.unit in ("score", "count"):
            return f"{v:f}"
        return self.desc


class Registry:
    def __init__(self) -> None:
        self.entries: dict[str, Entry] = {}
        self._n = {k: 0 for k in PREFIX}
        self._groups = 0
        self.confidence: dict | None = None  # the forecast's confidence (label + reason) this turn
        self.by_key: dict[str, Entry] = {}

    def new_group(self) -> str:
        self._groups += 1
        return f"S{self._groups}"

    def add(self, kind: str, unit: str, value, desc: str, group: str | None = None, key: str | None = None,
            phrases: dict[str, list[str]] | None = None, shown: dict[str, str] | None = None) -> Entry:
        """Money comes in as int paise and is stored as Decimal rupees; other numbers as Decimal."""
        assert kind in PREFIX and unit in UNITS, (kind, unit)
        if unit == "inr" and isinstance(value, int):
            value = Decimal(value) / 100
        elif unit not in ("date", "flag") and not isinstance(value, Decimal):
            value = Decimal(str(value))
        self._n[kind] += 1
        e = Entry(f"{PREFIX[kind]}{self._n[kind]}", kind, unit, value, desc, group, key, phrases or {}, shown)
        self.entries[e.id] = e
        if key:
            self.by_key[key] = e
        return e

    def ref(self, e: Entry, language: str = "en") -> dict:
        """What a tool returns to the LLM for a number: never the raw value, always id + display."""
        return {"id": e.id, "kind": e.kind, "display": e.display(language), "desc": e.desc}

    def get(self, entry_id: str) -> Entry | None:
        return self.entries.get(entry_id)

    def assumptions(self) -> list[Entry]:
        return [e for e in self.entries.values() if e.kind == "assumption"]

    # ---- matching (COPILOT.md §7) ----
    def matches(self, n: Num) -> list[Entry]:
        return [e for e in self.entries.values() if e.numeric and _matches(e, n)]


def _round_to(value: Decimal, precision: Decimal) -> Decimal:
    return (value / precision).to_integral_value(ROUND_HALF_UP) * precision


def _matches(e: Entry, n: Num) -> bool:
    if n.unit in ("date", "dom") or e.unit == "date":
        if e.unit != "date" or n.unit not in ("date", "dom"):
            return False
        d: dt.date = e.value
        if n.unit == "dom":  # "the 5th"
            return d.day == int(n.value)
        day, month, year = n.value
        return (day is None or d.day == day) and d.month == month and (year is None or year == d.year)
    if n.unit == "bare":
        if e.unit not in BARE_OK:
            return False
    elif n.unit == "score":
        if e.unit not in ("score", "count"):
            return False
    elif n.unit != e.unit and not (n.unit == "months" and e.unit == "count"):
        return False
    target: Decimal = abs(e.value)
    if n.value == target:
        return True
    if n.sig_figs < 2:  # "1 lakh" for ₹1,49,000 is not allowed; exact matches only
        return False
    return _round_to(target, n.precision) == n.value
