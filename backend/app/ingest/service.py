"""Persist an IngestBatch, then recompute the user's canonical transactions (SPEC §4-5)."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.clock import Clock, get_clock
from app.core.ids import stable_id
from app.db.models import (
    Account,
    AccountState,
    Balance,
    Consent,
    Counterparty,
    MerchantRule,
    RawTransactionRow,
    Transaction,
    TransactionSource,
    User,
    UserPreference,
)
from app.db.repo import UserRepo
from app.ingest.base import AccountHint, AccountInfo, IngestBatch
from app.pipeline.categorise.model import Categoriser
from app.pipeline.run import CounterpartyOut, masked_narration, run_pipeline
from app.pipeline.types import PAccount, PRaw

META_FIELDS = [
    "credit_limit_paise", "min_balance_paise", "apr_bps", "statement_day", "due_day", "loan_principal_paise",
    "loan_outstanding_paise", "loan_rate_bps", "loan_tenure_months", "loan_remaining_months", "emi_paise",
    "emi_next_due", "emi_day", "lender_merchant_key",
]


@dataclass
class IngestResult:
    ingest_id: str
    raw_count: int
    raw_new: int
    canonical_total: int
    duplicates_merged: int


LINK_RANK = {"known_unlinked": 0, "sms_only": 1, "linked": 2}


def account_id_for(user_id: str, hint: AccountHint) -> str:
    return stable_id("acc", user_id, hint.institution, hint.kind_group, hint.last4)


def _upsert_account(repo: UserRepo, info: AccountInfo) -> Account:
    aid = account_id_for(repo.user_id, info.hint)
    row = repo.get(Account, account_id=aid)
    if row is None:
        row = Account(user_id=repo.user_id, account_id=aid, kind=info.hint.kind, institution=info.hint.institution,
                      masked_number=f"XX{info.hint.last4}", last4=info.hint.last4,
                      role="liability" if info.hint.kind in ("credit_card", "loan") else None,
                      link_status=info.link_status, known_via=info.known_via, extra={})
        repo.add(row)
    elif LINK_RANK[info.link_status] > LINK_RANK.get(row.link_status, 0):
        row.link_status, row.known_via = info.link_status, info.known_via
    for f in META_FIELDS:
        v = getattr(info, f)
        if v is not None:
            setattr(row, f, v)
    if info.balance_paise is not None and info.balance_date is not None:
        bal = repo.get(Balance, account_id=aid, as_of_date=info.balance_date)
        if bal is None:
            repo.add(Balance(user_id=repo.user_id, account_id=aid, as_of_date=info.balance_date,
                             balance_paise=info.balance_paise, source=info.known_via))
        else:
            bal.balance_paise = info.balance_paise
    return row


def _upsert_holders(repo: UserRepo, names: list[str]) -> None:
    existing = repo.select(Counterparty, Counterparty.kind == "holder")
    known = {n for c in existing for n in c.match_keys}
    n = len(existing)
    for name in names:
        key = f"name:{name.upper()}"
        if key in known:
            continue
        n += 1
        repo.add(Counterparty(user_id=repo.user_id, pseudonym=f"holder_{n}", kind="holder", name=name,
                              match_keys=[key], first_seen=None))
        known.add(key)


def next_ingest_seq(repo: UserRepo) -> int:
    seqs = [r.ingest_seq for r in repo.select(RawTransactionRow)] + [s.ingest_seq for s in repo.select(AccountState)]
    return max(seqs, default=0) + 1


def _state_fields(info: AccountInfo) -> dict:
    out = {f: getattr(info, f) for f in META_FIELDS if getattr(info, f) is not None}
    if info.balance_paise is not None:
        out["balance_paise"] = info.balance_paise
        out["balance_date"] = info.balance_date
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in out.items()}


def ingest_batch(session: Session, user_id: str, batch: IngestBatch, categoriser: Categoriser | None,
                 clock: Clock | None = None) -> IngestResult:
    clock = clock or get_clock()
    repo = UserRepo(session, user_id)
    if repo.get(User) is None and session.get(User, user_id) is None:
        repo.add(User(user_id=user_id, created_at=clock.now()))
    user = session.get(User, user_id)

    if batch.consent:
        c = batch.consent
        if repo.get(Consent, consent_id=c.consent_id) is None:
            repo.add(Consent(user_id=user_id, consent_id=c.consent_id, purpose_code=c.purpose_code,
                             purpose_text=c.purpose_text, fi_types=c.fi_types, scope_accounts=c.scope_accounts,
                             data_from=c.data_from, data_to=c.data_to, expires_at=c.expires_at, status="ACTIVE"))

    seq = next_ingest_seq(repo)
    holder_names: list[str] = []
    for info in batch.accounts:
        _upsert_account(repo, info)
        holder_names += info.holder_names
        if info.known_via in ("aa", "statement"):
            state_date = info.balance_date or (batch.consent.data_to if batch.consent else None) or clock.today()
            repo.add(AccountState(user_id=user_id, account_id=account_id_for(user_id, info.hint), ingest_seq=seq,
                                  as_of_date=state_date, received_at=clock.now(), source=info.known_via,
                                  fields=_state_fields(info)))
    if holder_names:
        _upsert_holders(repo, holder_names)
        if not user.display_name:
            user.display_name = holder_names[0]
    for t in batch.transactions:
        if account_id_for(user_id, t.account) not in {a.account_id for a in repo.select(Account)}:
            _upsert_account(repo, AccountInfo(hint=t.account, known_via=batch.source))
    session.flush()

    ingest_id = stable_id("ing", user_id, batch.source, clock.now().isoformat(), len(batch.transactions))
    existing = {r.raw_id for r in repo.select(RawTransactionRow)}
    new = 0
    for t in batch.transactions:
        rid = stable_id("raw", user_id, t.source, t.source_ref)
        if rid in existing:
            continue
        existing.add(rid)
        new += 1
        repo.add(RawTransactionRow(
            user_id=user_id, raw_id=rid, source=t.source, source_ref=t.source_ref, ingest_id=ingest_id,
            account_id=account_id_for(user_id, t.account), txn_date=t.txn_date, seq=t.seq,
            amount_paise=t.amount_paise, direction=t.direction, narration=t.narration, merchant_hint=t.merchant_hint,
            reference=t.reference, balance_after_paise=t.balance_after_paise, channel_hint=t.channel_hint,
            category_hint=t.category_hint, payload_hash=t.payload_hash(), received_at=clock.now(), ingest_seq=seq))
    session.flush()
    canonical, merged = recompute_user(session, user_id, categoriser)
    return IngestResult(ingest_id, len(batch.transactions), new, canonical, merged)


def recompute_user(session: Session, user_id: str, categoriser: Categoriser | None) -> tuple[int, int]:
    """Full-history recompute of canonical transactions for one user."""
    repo = UserRepo(session, user_id)
    accounts = repo.select(Account)
    paccs = [PAccount(a.account_id, a.kind, a.institution, a.last4, a.link_status, a.lender_merchant_key)
             for a in accounts]
    raws = [PRaw(r.raw_id, r.source, r.account_id, r.txn_date, r.seq, r.amount_paise, r.direction, r.narration,
                 r.merchant_hint, r.reference, r.balance_after_paise, r.channel_hint, r.category_hint)
            for r in repo.select(RawTransactionRow)]
    cps = repo.select(Counterparty)
    holders = [c.name for c in cps if c.kind == "holder"]
    user = repo.get(User)
    if user and user.display_name:
        holders.append(user.display_name)
    existing = [CounterpartyOut(c.pseudonym, c.kind, c.name, list(c.match_keys), c.first_seen)
                for c in cps if c.kind == "contact"]

    rules = {r.merchant_key: r.category for r in repo.select(MerchantRule)}
    res = run_pipeline(user_id, raws, paccs, categoriser, holders, existing, rules)

    known_ids = {a.account_id for a in accounts}
    for a in res.inferred_accounts:
        if a.account_id not in known_ids:
            repo.add(Account(user_id=user_id, account_id=a.account_id, kind=a.kind, institution=a.institution,
                             masked_number=f"XX{a.last4}", last4=a.last4, role="liability",
                             link_status="known_unlinked", known_via="inferred", extra={}))
    by_pseudo = {c.pseudonym: c for c in cps if c.kind == "contact"}
    for c in res.counterparties:
        row = by_pseudo.get(c.pseudonym)
        if row is None:
            repo.add(Counterparty(user_id=user_id, pseudonym=c.pseudonym, kind="contact", name=c.name,
                                  match_keys=list(c.match_keys), first_seen=c.first_seen))
        else:
            row.match_keys, row.name = list(c.match_keys), c.name

    names = {h: "[SELF]" for h in holders}
    names.update({c.name: f"[{c.pseudonym.upper()}]" for c in res.counterparties if c.name != "UNKNOWN"})
    repo.delete_where(TransactionSource)
    repo.delete_where(Transaction)
    session.flush()
    for t in res.txns:
        repo.add(Transaction(
            user_id=user_id, txn_id=t.txn_id, account_id=t.account.account_id, txn_date=t.date, seq=t.raw.seq,
            amount_paise=t.amount, direction=t.direction, channel=t.channel[:8],
            narration_masked=masked_narration(t, names), merchant_key=t.merchant_key[:80],
            merchant_name=t.merchant_name[:120], counterparty_type=t.counterparty_type, category=t.category,
            category_source=t.category_source, category_confidence=t.category_confidence,
            transfer_group_id=t.transfer_group_id, is_bounce=t.is_bounce,
            balance_after_paise=t.raw.balance_after_paise, source_count=len(t.sources)))
        for s in t.sources:
            repo.add(TransactionSource(user_id=user_id, txn_id=t.txn_id, raw_id=s.raw_id, source=s.source,
                                       is_canonical=s is t.raw))
    session.flush()
    return len(res.txns), res.duplicates_merged


def set_merchant_rule(session: Session, user_id: str, merchant_key: str, category: str,
                      categoriser: Categoriser | None, clock: Clock | None = None) -> None:
    """A user's correction becomes a per-user merchant rule (D21), then history is recomputed."""
    from app.core.config import load_yaml

    if category not in load_yaml("categories")["categories"]:
        raise ValueError(f"unknown category {category!r}")
    clock = clock or get_clock()
    repo = UserRepo(session, user_id)
    row = repo.get(MerchantRule, merchant_key=merchant_key)
    if row is None:
        repo.add(MerchantRule(user_id=user_id, merchant_key=merchant_key, category=category, created_at=clock.now()))
    else:
        row.category, row.created_at = category, clock.now()
    session.flush()
    recompute_user(session, user_id, categoriser)


def set_loan_cash_use(session: Session, user_id: str, txn_id: str, use: str, clock: Clock | None = None) -> None:
    """D33: the user says what the loan money is for. "reserve" lifts the earmark in the buffer and the forecast;
    "purpose" restores it. Stored as a point-in-time preference (views only see it from its creation time)."""
    from app.engines.earmark import pref_key

    if use not in ("reserve", "purpose"):
        raise ValueError("use must be 'reserve' or 'purpose'")
    clock = clock or get_clock()
    repo = UserRepo(session, user_id)
    if not repo.select(Transaction, Transaction.txn_id == txn_id, Transaction.category == "loan_disbursal"):
        raise ValueError(f"{txn_id} is not a loan disbursal")
    row = repo.get(UserPreference, key=pref_key(txn_id))
    if row is None:
        repo.add(UserPreference(user_id=user_id, key=pref_key(txn_id), value={"use": use}, created_at=clock.now()))
    else:
        row.value, row.created_at = {"use": use}, clock.now()
    session.flush()
