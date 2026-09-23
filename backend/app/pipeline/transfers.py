"""Internal transfer matching (SPEC §5.2, D3/D4).

- Debit + credit both visible on the user's own accounts -> transfer_self (neither income nor spend).
- A transfer to an account we can't see counts as spend: card bill to an unlinked card ->
  card_bill_unlinked; self-transfer to an unseen account -> transfer_unseen.
- A payment to a LINKED card is a transfer even if its credit leg is outside the data window.
- Loan legs never pair: the EMI debit stays `emi` (debt service); loan-account legs are excluded.
"""
from __future__ import annotations

from app.core.ids import stable_id
from app.pipeline.types import PAccount, PTxn

MAX_DAYS = 3


def _is_card_bill(t: PTxn) -> bool:
    return t.direction == "debit" and t.account.kind != "credit_card" and "card_bill" in t.signatures


def _is_self(t: PTxn) -> bool:
    return "self_transfer" in t.signatures or t.counterparty_type == "self"


def _pairable(d: PTxn, c: PTxn) -> bool:
    if d.account.account_id == c.account.account_id or "loan" in (d.account.kind, c.account.kind):
        return False
    if d.amount != c.amount or not (-1 <= (c.date - d.date).days <= MAX_DAYS):
        return False
    if c.account.kind == "credit_card":
        return _is_card_bill(d) and (d.last4_ref in (None, c.account.last4)) and \
            ("card_payment_received" in c.signatures or c.counterparty_type in ("self", "bank"))
    return _is_self(d) or _is_self(c) or d.last4_ref == c.account.last4 or c.last4_ref == d.account.last4


def card_for_bill(t: PTxn, accounts: list[PAccount]) -> PAccount | None:
    for a in accounts:
        if a.kind == "credit_card" and t.last4_ref and a.last4 == t.last4_ref:
            return a
    return None


def match_transfers(txns: list[PTxn], user_id: str, accounts: list[PAccount]) -> None:
    """Sets category/transfer_group_id in place on matched and unmatched transfer legs."""
    debits = [t for t in txns if t.direction == "debit"]
    credits = [t for t in txns if t.direction == "credit"]
    used: set[int] = set()
    for d in sorted(debits, key=lambda t: t.raw.sort_key()):
        cands = [c for c in credits if id(c) not in used and _pairable(d, c)]
        if not cands:
            continue
        c = min(cands, key=lambda c: (abs((c.date - d.date).days), c.raw.sort_key()))
        used.add(id(c))
        gid = stable_id("xfer", user_id, d.raw.raw_id, c.raw.raw_id)
        for leg in (d, c):
            leg.category, leg.category_source, leg.category_confidence = "transfer_self", "rule", 1.0
            leg.transfer_group_id = gid

    for t in txns:
        if t.category is not None:
            continue
        if _is_card_bill(t):
            card = card_for_bill(t, accounts)
            linked = card is not None and card.link_status == "linked"
            t.category = "transfer_self" if linked else "card_bill_unlinked"
            t.category_source, t.category_confidence = "rule", 1.0
        elif t.account.kind == "credit_card" and t.direction == "credit" and "card_payment_received" in t.signatures:
            t.category, t.category_source, t.category_confidence = "transfer_in_unseen", "rule", 1.0
        elif t.account.kind != "loan" and _is_self(t):
            t.category = "transfer_unseen" if t.direction == "debit" else "transfer_in_unseen"
            t.category_source, t.category_confidence = "rule", 1.0
