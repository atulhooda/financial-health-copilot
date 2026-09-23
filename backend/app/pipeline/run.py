"""Pipeline: normalise -> de-dupe -> infer accounts -> match transfers -> categorise (SPEC §5).

`run_pipeline` is pure (no DB, no clock). The whole user history is recomputed on every
ingest; that is what makes the D4 card reclassification retroactive.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.ids import stable_id
from app.core.pii import mask_names, mask_regex
from app.ingest.base import institution_code
from app.pipeline.categorise.model import CHANNELS, Categoriser
from app.pipeline.categorise.rules import categorise_by_rules
from app.pipeline.dedupe import dedupe
from app.pipeline.merchants import clean, parse_narration, resolve
from app.pipeline.transfers import card_for_bill, match_transfers
from app.pipeline.types import PAccount, PRaw, PTxn

ML_MIN_CONFIDENCE = 0.5


@dataclass
class CounterpartyOut:
    pseudonym: str
    kind: str
    name: str
    match_keys: list[str]
    first_seen: object = None


@dataclass
class PipelineResult:
    txns: list[PTxn]
    duplicates_merged: int
    inferred_accounts: list[PAccount] = field(default_factory=list)
    counterparties: list[CounterpartyOut] = field(default_factory=list)


def _infer_card_accounts(user_id: str, txns: list[PTxn], accounts: list[PAccount]) -> list[PAccount]:
    """A card bill payment to a card we don't have tells us the card exists (known_unlinked)."""
    inferred: list[PAccount] = []
    for t in sorted(txns, key=lambda t: t.raw.sort_key()):
        if t.direction != "debit" or "card_bill" not in t.signatures or not t.last4_ref:
            continue
        if card_for_bill(t, accounts + inferred):
            continue
        inst = institution_code(t.payee_clean or "unknown")
        acc = PAccount(account_id=stable_id("acc", user_id, inst, "credit_card", t.last4_ref), kind="credit_card",
                       institution=inst, last4=t.last4_ref, link_status="known_unlinked")
        inferred.append(acc)
    return inferred


def _assign_pseudonyms(txns: list[PTxn], existing: list[CounterpartyOut]) -> list[CounterpartyOut]:
    """Stable per-user pseudonyms (contact_01, ...) for P2P counterparties. Names never leave the DB."""
    by_key: dict[str, CounterpartyOut] = {}
    out = [c for c in existing]
    for c in existing:
        for k in c.match_keys:
            by_key[k] = c
    n = max([int(c.pseudonym.split("_")[1]) for c in existing if c.kind == "contact"] + [0])
    for t in sorted(txns, key=lambda t: t.raw.sort_key()):
        if t.counterparty_type != "person" or not t.person_keys:
            continue
        cp = next((by_key[k] for k in t.person_keys if k in by_key), None)
        if cp is None:
            n += 1
            name = next((k[5:] for k in t.person_keys if k.startswith("name:")), t.payee_clean or "UNKNOWN")
            cp = CounterpartyOut(f"contact_{n:02d}", "contact", name, [], t.date)
            out.append(cp)
        for k in t.person_keys:
            if k not in cp.match_keys:
                cp.match_keys.append(k)
            by_key[k] = cp
        if cp.name == "UNKNOWN" and any(k.startswith("name:") for k in t.person_keys):
            cp.name = next(k[5:] for k in t.person_keys if k.startswith("name:"))
        t.merchant_key = f"p2p:{cp.pseudonym}"
        t.merchant_name = cp.pseudonym.replace("_", " ").title()
    return out


def masked_narration(t: PTxn, names: dict[str, str]) -> str:
    text = t.raw.narration if not t.raw.narration.startswith("SMS/") else f"SMS {t.raw.merchant_hint}"
    return mask_regex(mask_names(text, names))


def run_pipeline(user_id: str, raws: list[PRaw], accounts: list[PAccount], categoriser: Categoriser | None,
                 holder_names: list[str] = (), existing_counterparties: list[CounterpartyOut] = ()) -> PipelineResult:
    acc = {a.account_id: a for a in accounts}
    holders = frozenset(clean(h) for h in holder_names)
    items = []
    for r in sorted(raws, key=lambda r: r.sort_key()):
        a = acc[r.account_id]
        p = parse_narration(r.narration, r.merchant_hint, r.channel_hint, a.kind)
        res = resolve(p, holders)
        items.append(PTxn(raw=r, sources=[r], account=a, channel=p.channel, payee_clean=res.payee_clean,
                          merchant_key=res.merchant_key, merchant_name=res.merchant_name,
                          counterparty_type=res.counterparty_type, remark=p.remark,
                          reference=p.reference or r.reference, last4_ref=p.last4_ref, signatures=p.signatures,
                          person_keys=res.person_keys))

    txns, merged = dedupe(items)
    inferred = _infer_card_accounts(user_id, txns, accounts)
    match_transfers(txns, user_id, accounts + inferred)

    need_ml = [t for t in txns if not categorise_by_rules(t)]
    if need_ml:
        if categoriser is None:
            for t in need_ml:
                t.category, t.category_source, t.category_confidence = "other", "rule", 0.0
        else:
            labels, confs = categoriser.predict([t.payee_clean for t in need_ml], [t.amount for t in need_ml],
                                                [t.channel if t.channel in CHANNELS else "other" for t in need_ml])
            for t, lab, conf in zip(need_ml, labels, confs, strict=True):
                t.category = lab if conf >= ML_MIN_CONFIDENCE else "other"
                t.category_source, t.category_confidence = "ml", conf

    cps = _assign_pseudonyms(txns, list(existing_counterparties))
    for t in txns:
        t.txn_id = stable_id("txn", user_id, t.raw.raw_id)
    txns.sort(key=lambda t: t.raw.sort_key())
    return PipelineResult(txns=txns, duplicates_merged=merged, inferred_accounts=inferred, counterparties=cps)
