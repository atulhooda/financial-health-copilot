"""The as-of view (SPEC §6.0, D22): the only way engines see data.

A view is built from raw rows and account states that were (a) dated on or before `as_of` and
(b) received by the end of `as_of` and within the ingest watermark. The pipeline runs on exactly
that slice, so visibility, transfers and categories are point-in-time. Engines never read the stored
`transactions` table (that is the "latest" view for the app) and never import app.demo (D25).
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field

import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import load_yaml
from app.core.dates import IST
from app.db.models import Account, AccountState, Counterparty, MerchantRule, RawTransactionRow, User
from app.db.repo import UserRepo
from app.pipeline.categorise.model import Categoriser
from app.pipeline.run import CounterpartyOut, run_pipeline
from app.pipeline.types import PAccount, PRaw

DEPOSIT = ("savings", "current")
TXN_SCHEMA = {
    "txn_id": pl.Utf8, "account_id": pl.Utf8, "date": pl.Date, "seq": pl.Int64, "amount": pl.Int64,
    "direction": pl.Utf8, "category": pl.Utf8, "category_source": pl.Utf8, "confidence": pl.Float64,
    "merchant_key": pl.Utf8, "merchant_name": pl.Utf8, "counterparty_type": pl.Utf8, "channel": pl.Utf8,
    "is_bounce": pl.Boolean, "balance_after": pl.Int64, "account_kind": pl.Utf8,
    "transfer_group_id": pl.Utf8, "counter_account_id": pl.Utf8, "is_card_payment": pl.Boolean,
    "card_account_id": pl.Utf8,
    "spend": pl.Boolean, "essential": pl.Boolean, "discretionary": pl.Boolean, "income": pl.Boolean,
}


@dataclass
class AccountView:
    account_id: str
    kind: str
    institution: str
    last4: str
    linked: bool  # AA/statement data (a state) by the cutoff
    visible_from: dt.date | None  # first transaction we hold, any source (D19)
    state: dict = field(default_factory=dict)
    state_as_of: dt.date | None = None
    role: str | None = None  # operating | reserve | liability
    balance_paise: int | None = None  # at as_of

    @property
    def visible(self) -> bool:
        return self.visible_from is not None

    @property
    def status(self) -> str:
        return "linked" if self.linked else ("sms_only" if self.visible else "known_unlinked")


@dataclass
class View:
    user_id: str
    as_of: dt.date
    accounts: dict[str, AccountView]
    txns: pl.DataFrame
    floor_paise: int
    ingest_seq: int  # watermark actually used

    @property
    def operating(self) -> AccountView | None:
        return next((a for a in self.accounts.values() if a.role == "operating"), None)

    def of_kind(self, *kinds: str) -> list[AccountView]:
        return [a for a in self.accounts.values() if a.kind in kinds and a.institution != "cash"]


def _cutoff(as_of: dt.date) -> dt.datetime:
    return dt.datetime.combine(as_of, dt.time(23, 59, 59), tzinfo=IST)


def _aware(ts: dt.datetime) -> dt.datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=IST)


def category_flags() -> dict[str, dict]:
    cats = load_yaml("categories")["categories"]
    out = {}
    for k, v in cats.items():
        spend, essential = bool(v.get("spend")), bool(v.get("essential"))
        out[k] = {"spend": spend, "essential": essential, "income": bool(v.get("income")),
                  "discretionary": bool(v.get("discretionary", spend and not essential))}
    return out


def build_view(session: Session, user_id: str, as_of: dt.date, categoriser: Categoriser | None,
               max_ingest_seq: int | None = None,
               exclude_raw: Callable[[RawTransactionRow], bool] | None = None,
               exclude_state: Callable[[AccountState], bool] | None = None) -> View:
    repo = UserRepo(session, user_id)
    cutoff = _cutoff(as_of)
    seq_ok = (lambda s: True) if max_ingest_seq is None else (lambda s: s <= max_ingest_seq)

    raw_rows = [r for r in repo.select(RawTransactionRow, RawTransactionRow.txn_date <= as_of)
                if _aware(r.received_at) <= cutoff and seq_ok(r.ingest_seq) and not (exclude_raw and exclude_raw(r))]
    states = [s for s in repo.select(AccountState, AccountState.as_of_date <= as_of)
              if _aware(s.received_at) <= cutoff and seq_ok(s.ingest_seq) and not (exclude_state and exclude_state(s))]
    latest_state: dict[str, AccountState] = {}
    for s in sorted(states, key=lambda s: (s.as_of_date, s.ingest_seq)):
        latest_state[s.account_id] = s
    rules = {r.merchant_key: r.category for r in repo.select(MerchantRule) if _aware(r.created_at) <= cutoff}

    in_slice = {r.account_id for r in raw_rows} | set(latest_state)
    acc_rows = {a.account_id: a for a in repo.select(Account) if a.account_id in in_slice}
    paccs = [PAccount(a.account_id, a.kind, a.institution, a.last4,
                      "linked" if a.account_id in latest_state else "sms_only",
                      latest_state[a.account_id].fields.get("lender_merchant_key") if a.account_id in latest_state else None)
             for a in acc_rows.values()]
    raws = [PRaw(r.raw_id, r.source, r.account_id, r.txn_date, r.seq, r.amount_paise, r.direction, r.narration,
                 r.merchant_hint, r.reference, r.balance_after_paise, r.channel_hint, r.category_hint)
            for r in raw_rows]
    cps = repo.select(Counterparty)
    holders = [c.name for c in cps if c.kind == "holder"]
    existing = [CounterpartyOut(c.pseudonym, c.kind, c.name, list(c.match_keys), c.first_seen)
                for c in cps if c.kind == "contact"]
    res = run_pipeline(user_id, raws, paccs, categoriser, holders, existing, rules)

    flags = category_flags()
    legs: dict[str, list[str]] = {}
    for t in res.txns:
        if t.transfer_group_id:
            legs.setdefault(t.transfer_group_id, []).append(t.account.account_id)
    cards = {(a.last4): a.account_id for a in [*paccs, *res.inferred_accounts] if a.kind == "credit_card"}
    records = []
    for t in res.txns:
        f = flags.get(t.category, flags["other"])
        is_card_payment = t.direction == "debit" and t.account.kind in DEPOSIT and "card_bill" in t.signatures
        other = [a for a in legs.get(t.transfer_group_id or "", []) if a != t.account.account_id]
        records.append({
            "txn_id": t.txn_id, "account_id": t.account.account_id, "date": t.date, "seq": t.raw.seq,
            "amount": t.amount, "direction": t.direction, "category": t.category,
            "category_source": t.category_source, "confidence": float(t.category_confidence),
            "merchant_key": t.merchant_key, "merchant_name": t.merchant_name,
            "counterparty_type": t.counterparty_type, "channel": t.channel, "is_bounce": t.is_bounce,
            "balance_after": t.raw.balance_after_paise, "account_kind": t.account.kind,
            "transfer_group_id": t.transfer_group_id, "counter_account_id": other[0] if other else None,
            "is_card_payment": is_card_payment,
            "card_account_id": cards.get(t.last4_ref) if is_card_payment else None,
            **f})
    txns = pl.DataFrame(records, schema=TXN_SCHEMA).sort(["date", "account_id", "seq", "txn_id"])

    # ---- account views -----------------------------------------------------------------------
    accounts: dict[str, AccountView] = {}
    first_seen = {r["account_id"]: r["date"] for r in txns.group_by("account_id").agg(pl.col("date").min()).to_dicts()}
    for a in [*paccs, *res.inferred_accounts]:
        st = latest_state.get(a.account_id)
        accounts[a.account_id] = AccountView(a.account_id, a.kind, a.institution, a.last4, linked=st is not None,
                                             visible_from=first_seen.get(a.account_id),
                                             state=dict(st.fields) if st else {},
                                             state_as_of=st.as_of_date if st else None)
    for av in accounts.values():
        rows = txns.filter((pl.col("account_id") == av.account_id) & pl.col("balance_after").is_not_null())
        if rows.height:
            av.balance_paise = int(rows["balance_after"][-1])
        elif av.state.get("balance_paise") is not None:
            av.balance_paise = int(av.state["balance_paise"])
    _assign_roles(accounts, txns, as_of)
    floor = _floor(session, user_id, accounts)
    watermark = max([r.ingest_seq for r in raw_rows] + [s.ingest_seq for s in states], default=0)
    return View(user_id, as_of, accounts, txns, floor, watermark)


def _assign_roles(accounts: dict[str, AccountView], txns: pl.DataFrame, as_of: dt.date) -> None:
    """Operating = the account receiving the primary salary; without salary, the most spend debits (D6)."""
    recent = txns.filter(pl.col("date") > as_of - dt.timedelta(days=90))
    best, best_key = None, None
    for a in accounts.values():
        if a.kind in ("credit_card", "loan"):
            a.role = "liability"
            continue
        if a.institution == "cash" or not a.visible:
            continue
        mine = recent.filter(pl.col("account_id") == a.account_id)
        salary = int(mine.filter(pl.col("category") == "income_salary")["amount"].sum() or 0)
        key = (salary, mine.filter((pl.col("direction") == "debit") & pl.col("spend")).height, a.account_id)
        if best_key is None or key > best_key:
            best, best_key = a, key
    for a in accounts.values():
        if a.kind in DEPOSIT and a.institution != "cash" and a.visible:
            a.role = "operating" if a is best else "reserve"


def _floor(session: Session, user_id: str, accounts: dict[str, AccountView]) -> int:
    """D17: user override, else the operating account's minimum balance, else ₹5,000."""
    user = session.scalar(select(User).where(User.user_id == user_id))
    if user and user.safety_floor_paise is not None:
        return int(user.safety_floor_paise)
    inst = load_yaml("institutions")
    op = next((a for a in accounts.values() if a.role == "operating"), None)
    if op is not None:
        if op.state.get("min_balance_paise"):
            return int(op.state["min_balance_paise"])
        mb = inst["institutions"].get(op.institution, {}).get("min_balance_paise")
        if mb:
            return int(mb)
    return int(inst["default_floor_paise"])
