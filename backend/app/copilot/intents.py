"""Deterministic intents and slots (COPILOT.md §9), for LLM_PROVIDER=none and the template fallback."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.copilot import numbers
from app.core.config import load_yaml

ORDER = ("afford_emi", "score_change", "tradeoff", "run_short", "where_money", "summary")


@dataclass
class Intent:
    name: str
    slots: dict = field(default_factory=dict)


def _has(text: str, phrases: list[str]) -> bool:
    for p in phrases:
        p = numbers.normalise(str(p)).lower()
        tail = "" if re.search(r"[ऀ-ॿ]", p) else r"(?![\w])"
        if re.search(rf"(?<![\w]){re.escape(p)}{tail}", text):
            return True
    return False


def detect_intent(text: str) -> Intent:
    t = numbers.normalise(text).lower()
    cfg = load_yaml("copilot")
    rules = cfg["intents"]
    found = numbers.extract(text)
    amounts = [n.value for n in found if n.unit == "inr"]
    slots: dict = {}
    for name in ORDER:
        r = rules[name]
        if name == "summary":
            break
        if r.get("needs_amount") and not amounts:
            continue
        if "needs" in r and not _has(t, r["needs"]):
            continue
        if _has(t, r["words"]):
            if name == "afford_emi":
                slots["principal_rupees"] = int(max(amounts))
                months = [n.value for n in found if n.unit == "months"] + \
                    [n.value * 12 for n in found if n.unit == "years"]
                if months:
                    slots["tenure_months"] = int(months[0])
                rates = [n.value for n in found if n.unit == "pct"]
                if rates:
                    slots["annual_rate_pct"] = float(rates[0])
            if name == "tradeoff":
                slots["target_type"] = next((k for k, words in cfg["tradeoff_targets"].items() if _has(t, words)),
                                            None)
            return Intent(name, slots)
    return Intent("summary", slots)


def user_numbers(text: str) -> list[tuple[str, object]]:
    """(unit, value) for each number the user typed, to register as user_input (money in paise)."""
    out: list[tuple[str, object]] = []
    for n in numbers.extract(text):
        if n.unit == "inr":
            out.append(("inr", int(n.value * 100)))
        elif n.unit in ("months", "days", "years", "pct"):
            out.append((n.unit, n.value))
        elif n.unit == "bare":
            out.append(("count", n.value))
        elif n.unit == "score":
            out.append(("score", n.value))
    return out
