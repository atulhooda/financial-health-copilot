"""PII regex classes shared by pipeline narration masking and the LLM masking layer (COPILOT §3).

Order matters: longer / more specific patterns run first so a card number
isn't half-eaten by the account-number rule.
"""
from __future__ import annotations

import re


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


_VD = [[0,1,2,3,4,5,6,7,8,9],[1,2,3,4,0,6,7,8,9,5],[2,3,4,0,1,7,8,9,5,6],[3,4,0,1,2,8,9,5,6,7],
       [4,0,1,2,3,9,5,6,7,8],[5,9,8,7,6,0,4,3,2,1],[6,5,9,8,7,1,0,4,3,2],[7,6,5,9,8,2,1,0,4,3],
       [8,7,6,5,9,3,2,1,0,4],[9,8,7,6,5,4,3,2,1,0]]
_VP = [[0,1,2,3,4,5,6,7,8,9],[1,5,7,6,2,8,3,0,9,4],[5,8,0,3,7,9,6,1,4,2],[8,9,1,6,0,4,3,5,2,7],
       [9,4,5,3,1,2,6,8,7,0],[4,2,8,6,5,7,3,9,0,1],[2,7,9,3,8,0,6,4,1,5],[7,0,4,6,9,1,3,2,5,8]]


def _verhoeff_ok(digits: str) -> bool:
    """Aadhaar numbers carry a Verhoeff check digit and never start with 0 or 1."""
    if len(digits) != 12 or digits[0] in "01":
        return False
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _VD[c][_VP[i % 8][int(ch)]]
    return c == 0


EMAIL = re.compile(r"\b[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+\b")
# UPI VPA: handle after @ has no dot (okaxis, ybl, paytm, oksbi, hdfcbank, axl, ibl, apl, icici ...)
UPI = re.compile(r"\b[\w.\-]{2,}@[A-Za-z]{2,}\b(?!\.)")
CARD = re.compile(r"\b(?:\d[ \-]?){12,18}\d\b")
AADHAAR = re.compile(r"\b\d{4}[ \-]?\d{4}[ \-]?\d{4}\b")
PHONE = re.compile(r"(?<![\w])(?:\+91[\s\-]?|0)?[6-9]\d{9}\b")
MASKED_ACCOUNT = re.compile(r"(?i)\b(?:[X*]{2,}[\-\s]?)+\d{3,6}\b|\bA/?c\.?\s*(?:no\.?\s*)?[X*]*\d{3,6}\b")
ACCOUNT = re.compile(r"\b\d{9,18}\b")
PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
IFSC = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")


def _card_sub(m: re.Match) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    return "[CARD]" if 13 <= len(digits) <= 19 and _luhn_ok(digits) else m.group(0)


def _aadhaar_sub(m: re.Match) -> str:
    return "[AADHAAR]" if _verhoeff_ok(re.sub(r"\D", "", m.group(0))) else m.group(0)


RULES: list[tuple[str, re.Pattern, object]] = [
    ("email", EMAIL, "[EMAIL]"),
    ("upi", UPI, "[UPI]"),
    ("card", CARD, _card_sub),
    ("aadhaar", AADHAAR, _aadhaar_sub),
    ("phone", PHONE, "[PHONE]"),
    ("pan", PAN, "[PAN]"),
    ("ifsc", IFSC, "[IFSC]"),
    ("masked_account", MASKED_ACCOUNT, "[ACCOUNT]"),
    ("account", ACCOUNT, "[ACCOUNT]"),
]


def mask_regex(text: str) -> str:
    for _, pat, repl in RULES:
        text = pat.sub(repl, text)
    return text


def find_regex_pii(text: str) -> list[tuple[str, str]]:
    """(class, match) for every regex PII hit; used by tests and the output-side validator check."""
    hits = []
    for name, pat, _ in RULES:
        for m in pat.finditer(text):
            if name == "card" and _card_sub(m) == m.group(0):
                continue
            if name == "aadhaar" and _aadhaar_sub(m) == m.group(0):
                continue
            hits.append((name, m.group(0)))
    return hits


def mask_names(text: str, names: dict[str, str]) -> str:
    """Replace known names (case-insensitive, whole tokens), longest first. names: name -> token."""
    for name in sorted(names, key=len, reverse=True):
        if name.strip():
            text = re.sub(rf"(?i)(?<![\w]){re.escape(name)}(?![\w])", names[name], text)
    return text
