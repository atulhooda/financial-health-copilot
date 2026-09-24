"""SQLAlchemy 2 models (SPEC §3). Money = BIGINT paise. user_id on every table except global_counters (D14)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import JSON, BigInteger, Boolean, Date, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSONType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(200))  # PII: never sent to the LLM
    safety_floor_paise: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class Consent(Base):
    __tablename__ = "consents"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    consent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    purpose_code: Mapped[str] = mapped_column(String(16))
    purpose_text: Mapped[str | None] = mapped_column(String(300))
    fi_types: Mapped[list] = mapped_column(JSONType)
    scope_accounts: Mapped[list] = mapped_column(JSONType)
    data_from: Mapped[dt.date] = mapped_column(Date)
    data_to: Mapped[dt.date] = mapped_column(Date)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))


class Account(Base):
    __tablename__ = "accounts"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # savings | current | credit_card | loan
    institution: Mapped[str] = mapped_column(String(32))  # institution code
    masked_number: Mapped[str] = mapped_column(String(32))  # e.g. XX4321, never full
    last4: Mapped[str] = mapped_column(String(4))
    role: Mapped[str | None] = mapped_column(String(16))  # operating | reserve | liability
    link_status: Mapped[str] = mapped_column(String(16))  # linked | sms_only | known_unlinked (D19)
    known_via: Mapped[str] = mapped_column(String(16))  # aa | statement | sms | manual | inferred
    credit_limit_paise: Mapped[int | None] = mapped_column(BigInteger)
    min_balance_paise: Mapped[int | None] = mapped_column(BigInteger)
    apr_bps: Mapped[int | None] = mapped_column(Integer)
    statement_day: Mapped[int | None] = mapped_column(Integer)
    due_day: Mapped[int | None] = mapped_column(Integer)
    loan_principal_paise: Mapped[int | None] = mapped_column(BigInteger)
    loan_outstanding_paise: Mapped[int | None] = mapped_column(BigInteger)
    loan_rate_bps: Mapped[int | None] = mapped_column(Integer)
    loan_tenure_months: Mapped[int | None] = mapped_column(Integer)
    loan_remaining_months: Mapped[int | None] = mapped_column(Integer)
    emi_paise: Mapped[int | None] = mapped_column(BigInteger)
    emi_next_due: Mapped[dt.date | None] = mapped_column(Date)
    emi_day: Mapped[int | None] = mapped_column(Integer)
    lender_merchant_key: Mapped[str | None] = mapped_column(String(64))
    extra: Mapped[dict] = mapped_column(JSONType, default=dict)


class Balance(Base):
    __tablename__ = "balances"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    as_of_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    balance_paise: Mapped[int] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(16))


class RawTransactionRow(Base):
    """One row per transaction per source. Never holds raw SMS text (SPEC §4)."""

    __tablename__ = "raw_transactions"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    raw_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source: Mapped[str] = mapped_column(String(16))
    source_ref: Mapped[str] = mapped_column(String(128))
    ingest_id: Mapped[str] = mapped_column(String(32))
    account_id: Mapped[str] = mapped_column(String(32))
    txn_date: Mapped[dt.date] = mapped_column(Date)
    seq: Mapped[int] = mapped_column(Integer, default=0)  # intra-day order within the source
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    direction: Mapped[str] = mapped_column(String(6))
    narration: Mapped[str] = mapped_column(Text)
    merchant_hint: Mapped[str | None] = mapped_column(String(200))
    reference: Mapped[str | None] = mapped_column(String(64))
    balance_after_paise: Mapped[int | None] = mapped_column(BigInteger)
    channel_hint: Mapped[str | None] = mapped_column(String(16))
    category_hint: Mapped[str | None] = mapped_column(String(32))  # manual entries only
    payload_hash: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    ingest_seq: Mapped[int] = mapped_column(Integer, default=0)  # per-user ingest watermark (D22/D24)


class AccountState(Base):
    """Append-only account metadata as reported by a source at a date: the point-in-time truth (D22)."""

    __tablename__ = "account_states"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ingest_seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    as_of_date: Mapped[dt.date] = mapped_column(Date)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(16))  # aa | statement
    fields: Mapped[dict] = mapped_column(JSONType)


class MerchantRule(Base):
    """A user's category correction for a merchant; applies to past and future transactions (D21)."""

    __tablename__ = "merchant_rules"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    category: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class UserPreference(Base):
    """A user's explicit choice that engines must respect, e.g. "loan_cash:<txn_id>" -> {"use": "reserve"} (D33)."""

    __tablename__ = "user_preferences"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONType)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class Transaction(Base):
    """Canonical transaction after normalise -> transfer match -> dedupe -> categorise."""

    __tablename__ = "transactions"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    txn_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(32))
    txn_date: Mapped[dt.date] = mapped_column(Date)
    seq: Mapped[int] = mapped_column(Integer)
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    direction: Mapped[str] = mapped_column(String(6))
    channel: Mapped[str] = mapped_column(String(8))
    narration_masked: Mapped[str] = mapped_column(Text)
    merchant_key: Mapped[str] = mapped_column(String(80))
    merchant_name: Mapped[str] = mapped_column(String(120))
    counterparty_type: Mapped[str] = mapped_column(String(10))  # merchant | person | self | bank
    category: Mapped[str] = mapped_column(String(32))
    category_source: Mapped[str] = mapped_column(String(8))  # rule | ml | user
    category_confidence: Mapped[float] = mapped_column(Float)
    transfer_group_id: Mapped[str | None] = mapped_column(String(32))
    is_bounce: Mapped[bool] = mapped_column(Boolean, default=False)
    balance_after_paise: Mapped[int | None] = mapped_column(BigInteger)
    source_count: Mapped[int] = mapped_column(Integer)


class TransactionSource(Base):
    __tablename__ = "transaction_sources"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    txn_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    raw_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source: Mapped[str] = mapped_column(String(16))
    is_canonical: Mapped[bool] = mapped_column(Boolean)


class Counterparty(Base):
    """Pseudonym <-> name. The name is PII and never leaves the DB towards the LLM."""

    __tablename__ = "counterparties"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    pseudonym: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))  # contact | holder
    match_keys: Mapped[list] = mapped_column(JSONType)  # e.g. ["vpa:x@okaxis", "name:RAMESH KULKARNI"]
    name: Mapped[str] = mapped_column(String(200))
    first_seen: Mapped[dt.date | None] = mapped_column(Date)


class RecurringItem(Base):
    __tablename__ = "recurring_items"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    merchant_key: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(16))
    cadence_days: Mapped[int] = mapped_column(Integer)
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    next_due: Mapped[dt.date | None] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean)


class Snapshot(Base):
    """Immutable (insert-only; Postgres trigger rejects UPDATE). Erasure deletes (D14)."""

    __tablename__ = "snapshots"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer)
    trigger: Mapped[str] = mapped_column(String(64))
    as_of: Mapped[dt.date] = mapped_column(Date)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    input_hash: Mapped[str] = mapped_column(String(64))
    engine_version: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSONType)


class SnapshotDiff(Base):
    __tablename__ = "snapshot_diffs"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    to_snapshot_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    from_snapshot_id: Mapped[str | None] = mapped_column(String(32))
    reason_codes: Mapped[list] = mapped_column(JSONType)
    payload: Mapped[dict] = mapped_column(JSONType)


class AskLog(Base):
    __tablename__ = "ask_logs"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ask_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    language: Mapped[str] = mapped_column(String(10))
    path: Mapped[str] = mapped_column(String(12))
    verdicts: Mapped[list] = mapped_column(JSONType)
    trace: Mapped[dict] = mapped_column(JSONType)  # masked content only
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class ValidatorBlock(Base):
    __tablename__ = "validator_blocks"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ask_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    attempt: Mapped[int] = mapped_column(Integer, primary_key=True)
    errors: Mapped[list] = mapped_column(JSONType)
    masked_candidate: Mapped[dict] = mapped_column(JSONType)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class GlobalCounter(Base):
    """The one table without user_id (SPEC D14): anonymous counts only, no content."""

    __tablename__ = "global_counters"
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger, default=0)


USER_SCOPED_MODELS = [
    User, Consent, Account, Balance, RawTransactionRow, AccountState, MerchantRule, UserPreference, Transaction,
    TransactionSource, Counterparty,
    RecurringItem, Snapshot, SnapshotDiff, AskLog, ValidatorBlock,
]

Index("ix_raw_user_date", RawTransactionRow.user_id, RawTransactionRow.txn_date)
Index("ix_txn_user_date", Transaction.user_id, Transaction.txn_date)
Index("ix_txn_user_account", Transaction.user_id, Transaction.account_id)
Index("ix_snap_user_seq", Snapshot.user_id, Snapshot.seq, unique=True)
