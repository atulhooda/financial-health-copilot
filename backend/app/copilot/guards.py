"""Distress and scope guards (COPILOT.md §8, SPEC D9). Deterministic, and run before any network call."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from app.copilot.numbers import normalise
from app.core.config import load_yaml

DEVANAGARI = re.compile(r"[ऀ-ॿ]")


@dataclass
class GuardResult:
    tier1: bool = False  # explicit self-harm intent: stop, helpline only, never call the LLM
    tier2: bool = False  # money-stress idiom: check-in line, then answer
    scope: bool = False  # securities / crypto / tips / promised returns
    matched: list[str] = field(default_factory=list)

    @property
    def name(self) -> str | None:
        """Which guard decides the path: distress (tier 1) beats scope, which beats the tier-2 check-in."""
        if self.tier1:
            return "distress"
        if self.scope:
            return "scope"
        return "distress_checkin" if self.tier2 else None


@lru_cache
def _patterns() -> dict[str, list[tuple[str, re.Pattern]]]:
    cfg = load_yaml("guards")
    out = {}
    for group in ("distress_tier1", "distress_tier2", "scope"):
        pats = []
        for phrases in cfg[group].values():
            for p in phrases:
                p = normalise(str(p)).lower()
                body = r"\s+".join(map(re.escape, p.split()))
                # Hindi inflects by suffix (आत्महत्याएँ), so Devanagari phrases only need a boundary in front.
                tail = "" if DEVANAGARI.search(p) else r"(?![\w])"
                pats.append((p, re.compile(rf"(?<![\w]){body}{tail}")))
        out[group] = pats
    return out


def check(text: str) -> GuardResult:
    t = normalise(text).lower().replace("’", "'")
    pats = _patterns()
    r = GuardResult()
    for group, attr in (("distress_tier1", "tier1"), ("distress_tier2", "tier2"), ("scope", "scope")):
        hits = [p for p, rx in pats[group] if rx.search(t)]
        if hits:
            setattr(r, attr, True)
            r.matched += hits
    return r


def _helpline() -> tuple[str, list[str]]:
    h = load_yaml("helplines")["tele_manas"]
    return h["name"], h["numbers"]


def message(kind: str, language: str) -> str:
    """kind: distress | checkin | scope. Fixed text from config, never generated. Only the tier-1 (distress)
    message carries the emergency number 112; a tier-2 check-in stays Tele-MANAS only."""
    cfg = load_yaml("guards")["messages"]
    name, nums = _helpline()
    numbers = cfg["numbers_joiner"][language].join(nums)
    emergency = load_yaml("helplines")["emergency"]["number"]
    return " ".join(cfg[kind][language].split()).format(helpline=name, numbers=numbers, emergency=emergency)


def scope_suggestions(language: str) -> list[str]:
    return list(load_yaml("guards")["messages"]["scope_suggestions"][language])
