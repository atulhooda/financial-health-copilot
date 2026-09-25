"""Masking for everything sent to the LLM (COPILOT.md §3, D11) and sanitising untrusted names (Phase 5 item 9).

Guaranteed: the regex PII classes of app.core.pii, and KNOWN names (the user's display name, P2P counterparties,
AA holders and nominees) with their name parts. Best effort only: capitalised first names from config/names_in.txt
in free text the user typed.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from sqlalchemy.orm import Session

from app.core.config import CONFIG_DIR
from app.core.pii import find_regex_pii, mask_regex
from app.db.models import Counterparty, User
from app.db.repo import UserRepo

MAX_NAME = 40
UNNAMED = "Unnamed merchant"
# Instruction-like text in a merchant name: everything from the first match on is dropped (Phase 5 item 9).
INSTRUCTION = re.compile(
    r"(?i)(?:\b(?:ignore|disregard|forget|override|bypass)\b"
    r"|\b(?:system|developer|assistant)\s*(?:prompt|message|note|:)"
    r"|\byou\s+(?:are|must|should|will|shall)\b"
    r"|\b(?:act|pretend|behave)\s+as\b"
    r"|\b(?:(?:all|any|the|previous|prior|above|earlier|your|these|new|updated|real)\s+){0,3}"
    r"(?:instructions?|rules|task)\b"
    r"|\bprompt\b|\bjailbreak\b"
    r"|\b(?:respond|reply|answer|say|tell|output|print|write)\b\W+(?:\w+\W+){0,3}?(?:user|that|with|only|them|yes|no)\b"
    r"|\b(?:call|use|invoke|run)\b\W+(?:\w+\W+){0,2}?(?:tool|function|respond)\b"
    r"|\b(?:buy|sell|invest)\b\W+(?:\w+\W+){0,2}?(?:bitcoin|crypto|stocks?|shares?|funds?|options)\b"
    r"|\b(?:pichhle|pichle|purane|sabhi|saare)\s+(?:\w+\s+)?(?:nirdesh|instructions?)\b|\bbhool\s+jao\b"
    r"|\bignore\s+karo\b"
    r"|(?:(?:पिछले|पुराने|सभी|सारे|ऊपर\s+के)\s+){0,2}(?:निर्देश|अनदेखा|नज़रअंदाज़|नजरअंदाज)"
    r"|<\||\|>|\[/?inst\]|###)")
# markup, quotes, braces and the like never reach the LLM (Devanagari vowel signs are marks, not \w, so keep the block)
ALLOWED_CHARS = re.compile(r"[^\w\s&.,'()/+\-\u0900-\u097F]")
TOKEN = re.compile(r"\[(?:NAME|CONTACT)_[0-9A-Z]+\]")


def sanitize_untrusted(name: str | None, max_len: int = MAX_NAME) -> str:
    """A merchant or payee name as it may appear in a tool result: untrusted text, not instructions."""
    if not name:
        return UNNAMED
    t = unicodedata.normalize("NFKC", name)
    # line breaks and other controls become spaces; format characters (zero-width, bidi overrides) disappear
    t = "".join(" " if unicodedata.category(ch) == "Cc" else ch for ch in t
                if unicodedata.category(ch) not in ("Cf", "Co", "Cs", "Cn"))
    m = INSTRUCTION.search(t)
    if m:
        t = t[:m.start()]
    t = re.sub(r"\s+", " ", ALLOWED_CHARS.sub(" ", t)).strip(" .,-/&")
    if len(t) < 2:
        return UNNAMED
    if len(t) > max_len:
        t = t[:max_len].rsplit(" ", 1)[0].rstrip(" .,-/&") + "…"
    return t


@lru_cache
def _first_names() -> frozenset[str]:
    lines = (CONFIG_DIR / "names_in.txt").read_text(encoding="utf-8").splitlines()
    return frozenset(w for line in lines if not line.startswith("#") for w in line.split())


class Masker:
    """name -> token for known names; token -> display name for re-hydration after validation."""

    def __init__(self, names: dict[str, str] | None = None) -> None:
        self.names: dict[str, str] = {}  # lower-case name or name part -> token
        self.display: dict[str, str] = {}  # token -> name shown to the user
        self._unknown = 0
        for name, token in (names or {}).items():
            self.add_name(name, token)

    def add_name(self, name: str, token: str) -> None:
        name = " ".join(name.split())
        if not name:
            return
        self.display.setdefault(token, name.title())
        self.names[name.lower()] = token
        for part in name.split():
            if len(part) >= 3:
                self.names.setdefault(part.lower(), token)

    @classmethod
    def for_user(cls, session: Session, user_id: str) -> Masker:
        repo = UserRepo(session, user_id)
        mk = cls()
        user = repo.get(User, user_id=user_id)
        if user and user.display_name:
            mk.add_name(user.display_name, "[NAME_1]")
        n = 1
        for cp in sorted(repo.select(Counterparty), key=lambda c: c.pseudonym):
            if cp.kind == "contact":
                mk.add_name(cp.name, f"[{cp.pseudonym.upper()}]")
            elif user is None or cp.name.lower() != (user.display_name or "").lower():
                n += 1
                mk.add_name(cp.name, f"[NAME_{n}]")
        return mk

    def mask(self, text: str, free_text: bool = False) -> str:
        """The regex PII classes first (so an email or UPI ID is masked whole), then known names, longest first.
        free_text=True (the user's own message) also masks capitalised first names from the bundled list, best
        effort."""
        text = mask_regex(text)
        for name in sorted(self.names, key=len, reverse=True):
            text = re.sub(rf"(?i)(?<![\w\[]){re.escape(name)}(?![\w\]])", self.names[name], text)
        if free_text:
            text = re.sub(r"\b[A-Z][a-z]{2,}\b", self._unknown_name, text)
        return text

    def _unknown_name(self, m: re.Match) -> str:
        w = m.group(0)
        if w.upper() not in _first_names():
            return w
        if w.lower() not in self.names:
            self._unknown += 1
            self.add_name(w, f"[NAME_X{self._unknown}]")
        return self.names[w.lower()]

    def mask_obj(self, obj, free_text: bool = False, safe_keys: frozenset[str] = frozenset()):
        """Mask every string in a JSON-like object. Values under `safe_keys` (registry display strings and ids,
        which our code generates from numbers) are left alone unless they contain regex PII."""
        if isinstance(obj, str):
            return self.mask(obj, free_text)
        if isinstance(obj, dict):
            return {k: v if k in safe_keys and isinstance(v, str) and not find_regex_pii(v)
                    else self.mask_obj(v, free_text, safe_keys) for k, v in obj.items()}
        if isinstance(obj, list | tuple):
            return [self.mask_obj(v, free_text, safe_keys) for v in obj]
        return obj

    def rehydrate(self, text: str) -> str:
        return TOKEN.sub(lambda m: self.display.get(m.group(0), m.group(0)), text)
