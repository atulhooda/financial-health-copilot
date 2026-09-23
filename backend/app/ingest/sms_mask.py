"""Helper for SPEC D28: mask real SMS before they become committed parser fixtures.

Masks account/card digits (keeps the shape: XX1234 -> XX0000), balances/limits (keeps the format),
reference numbers, phone numbers, emails and UPI handles. Names cannot be found reliably, so lines with
capitalised words that aren't known merchants are flagged for MANUAL review. Nothing is sent anywhere.
"""
from __future__ import annotations

import re

from app.core.pii import EMAIL, PHONE, UPI
from app.pipeline.merchants import first_names, match_alias

_ACCT = re.compile(r"((?:A/[Cc]|Acct|AC|Card no\.|card ending|a/c)\s*(?:no\.?\s*)?[X*]*)(\d{3,6})", re.I)
_XX = re.compile(r"\b([X*]{1,})(\d{3,6})\b")
_BAL = re.compile(r"((?:Avl\.?\s*(?:bal|Lmt|Limit)|Bal(?:ance)?|Avl bal)[:\s]*(?:INR|Rs\.?)?\s*)"
                  r"([\d,]+(?:\.\d{1,2})?)", re.I)
_REF = re.compile(r"((?:Ref(?:no)?|UPI(?: Ref)?|UTR)[:\s]*)(\d{6,})", re.I)


def _zeros(m: re.Match) -> str:
    return m.group(1) + "0" * len(m.group(2))


def mask_sms(text: str) -> tuple[str, list[str]]:
    out = EMAIL.sub("user@example.in", text)
    out = UPI.sub("user@upi", out)
    out = _REF.sub(lambda m: m.group(1) + "4" + "0" * (len(m.group(2)) - 1), out)
    out = _BAL.sub(lambda m: m.group(1) + re.sub(r"\d", "9", m.group(2)), out)
    out = _ACCT.sub(_zeros, out)
    out = _XX.sub(_zeros, out)
    out = PHONE.sub("9000000000", out)
    review = []
    for tok in re.findall(r"\b[A-Z][A-Za-z]{2,}(?:\s+[A-Z][A-Za-z]{2,})*\b", out):
        if tok.split()[0].upper() in first_names() and not match_alias(tok):
            review.append(tok)
    return out, review
