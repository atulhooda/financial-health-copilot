"""Rule-based categorisation (SPEC §5.4): user > transfers > account-kind > keywords > merchant map > P2P > mandate."""
from __future__ import annotations

from functools import lru_cache

from app.core.config import load_yaml
from app.pipeline.types import PTxn


@lru_cache
def _cfg():
    return load_yaml("categories"), load_yaml("merchants")["merchants"]


def _any(text: str, words: list[str]) -> bool:
    return any(w.upper() in text for w in words)


def categorise_by_rules(t: PTxn) -> bool:
    """Assign a rule category in place. Returns False if the ML fallback is needed."""
    cats, merchants = _cfg()
    if t.category is not None:  # set by transfer matching
        return True
    if t.raw.category_hint and t.raw.category_hint in cats["categories"]:
        t.category, t.category_source, t.category_confidence = t.raw.category_hint, "user", 1.0
        return True

    def set_(cat: str, conf: float = 1.0) -> bool:
        t.category, t.category_source, t.category_confidence = cat, "rule", conf
        return True

    if t.account.kind == "loan":
        return set_("loan_repayment_in" if t.direction == "credit" else "loan_disbursal_out")

    text = f"{t.raw.narration} {t.raw.merchant_hint or ''}".upper()
    for r in cats["keyword_rules"]:
        if r.get("direction") in (None, t.direction) and _any(text, r["any"]):
            t.is_bounce = bool(r.get("bounce"))
            return set_(r["category"])

    if t.counterparty_type == "merchant" and t.merchant_key in merchants:
        m = merchants[t.merchant_key]
        if t.direction == "credit":
            return set_(m.get("credit_category", "refund"))
        return set_(m["category"])

    if t.counterparty_type == "person":
        remark = f"{t.remark} {text}"
        for r in cats["p2p_rules"]:
            if r.get("direction") in (None, t.direction) and _any(remark, r["any"]):
                return set_(r["category"])
        return set_("transfer_person")

    if t.channel == "nach" and t.direction == "debit":
        for r in cats["mandate_rules"]:
            if _any(t.payee_clean, r["any"]):
                return set_(r["category"], 0.9)

    if t.direction == "credit":
        return set_("income_other", 0.5)
    if t.channel == "atm":
        return set_("cash_withdrawal")
    return False
