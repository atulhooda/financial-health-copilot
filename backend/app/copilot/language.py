"""Language and script detection (COPILOT.md §1 step 4). Deterministic, runs before any network call.

hi = Devanagari majority; hinglish = Latin script with Hindi words (config/copilot.yaml); else en.
"""
from __future__ import annotations

import re

from app.copilot.numbers import normalise
from app.core.config import load_yaml

LANGUAGES = ("en", "hi", "hinglish")
DEVANAGARI = re.compile(r"[ऀ-ॿ]")
LATIN_WORD = re.compile(r"[A-Za-z]+")
TOKEN = re.compile(r"\[[A-Z][A-Z_]*\d*\]")  # masked tokens like [CONTACT_2]: script-neutral


def _lexicon() -> tuple[frozenset[str], int]:
    cfg = load_yaml("copilot")["language"]
    return frozenset(cfg["hinglish_words"]), cfg["min_hits"]


def hinglish_hits(text: str) -> int:
    words, _ = _lexicon()
    return sum(1 for w in LATIN_WORD.findall(text.lower()) if w in words)


def _counts(text: str, skip_capitalised: bool) -> tuple[int, int]:
    t = TOKEN.sub(" ", normalise(text))
    dev = len(DEVANAGARI.findall(t))
    latin = sum(len(w) for w in LATIN_WORD.findall(t) if not (skip_capitalised and w[0].isupper()))
    return dev, latin


def detect_language(text: str) -> str:
    dev, latin = _counts(text, skip_capitalised=False)
    if dev and dev >= latin:
        return "hi"
    _, min_hits = _lexicon()
    hits = hinglish_hits(text)
    n_words = len(LATIN_WORD.findall(text))
    if hits >= min_hits or (hits >= 1 and n_words <= 3):
        return "hinglish"
    return "en"


def script_fits(text: str, language: str) -> bool:
    """hi: Devanagari majority, ignoring capitalised Latin words (names like Netflix, acronyms like EMI).
    en / hinglish: Latin script, with no more than a stray Devanagari character."""
    if language == "hi":
        dev, latin = _counts(text, skip_capitalised=True)
        return dev > 0 and dev >= latin
    dev, latin = _counts(text, skip_capitalised=False)
    return dev <= max(2, (dev + latin) // 20)
