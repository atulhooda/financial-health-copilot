"""Cross-source de-duplication (SPEC §5.3).

Two rows from DIFFERENT sources are the same transaction if: same account, amount and direction,
|date diff| <= 1 day, and either the same UPI/UTR reference or similar merchant (same merchant key,
or token-set similarity >= 0.8). Two rows from the same source are never merged (two ₹100 chai
payments on one day are two payments). Canonical = highest-priority source (AA > statement > SMS > manual).
"""
from __future__ import annotations

from rapidfuzz import fuzz

from app.pipeline.types import SOURCE_PRIORITY, PTxn

SIMILARITY_MIN = 80


def _similar(a: PTxn, b: PTxn) -> bool:
    if a.reference and b.reference and a.reference == b.reference:
        return True
    if a.counterparty_type == "person" and b.counterparty_type == "person":
        if set(a.person_keys) & set(b.person_keys):
            return True
    if a.merchant_key == b.merchant_key and a.merchant_key not in ("p2p",) and not a.merchant_key.startswith("m:unknown"):
        return True
    return fuzz.token_set_ratio(a.payee_clean, b.payee_clean) >= SIMILARITY_MIN


def dedupe(items: list[PTxn]) -> tuple[list[PTxn], int]:
    """items: one PTxn per raw row (sources=[raw]). Returns canonical PTxns (sources merged) and merge count."""
    groups: dict[tuple, list[PTxn]] = {}
    for t in items:
        groups.setdefault((t.account.account_id, t.direction, t.amount), []).append(t)

    out: list[PTxn] = []
    merged = 0
    for key in sorted(groups):
        rows = sorted(groups[key], key=lambda t: (SOURCE_PRIORITY[t.raw.source], t.raw.sort_key()))
        clusters: list[PTxn] = []
        for t in rows:
            best = None
            for c in clusters:
                if any(s.source == t.raw.source for s in c.sources):
                    continue
                gap = abs((c.date - t.date).days)
                if gap <= 1 and _similar(c, t):
                    if best is None or gap < abs((best.date - t.date).days):
                        best = c
            if best is None:
                clusters.append(t)
            else:
                best.sources.append(t.raw)
                if t.reference and not best.reference:
                    best.reference = t.reference
                for k in t.person_keys:
                    if k not in best.person_keys:
                        best.person_keys.append(k)
                merged += 1
        out.extend(clusters)
    return out, merged
