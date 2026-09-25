"""Every source emits the same RawTransaction model (SPEC §4)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import load_yaml

Source = Literal["aa", "statement", "sms", "manual"]
Direction = Literal["debit", "credit"]
AccountKind = Literal["savings", "current", "credit_card", "loan"]


class AccountHint(BaseModel):
    """How a source identifies an account: institution + masked number. Never a full account number."""

    institution: str  # institution code (see config/institutions.yaml)
    masked_number: str
    kind: AccountKind

    @property
    def last4(self) -> str:
        digits = "".join(c for c in self.masked_number if c.isdigit())
        return digits[-4:]

    @property
    def kind_group(self) -> str:
        return {"savings": "deposit", "current": "deposit"}.get(self.kind, self.kind)


class AccountInfo(BaseModel):
    """Account metadata a source can vouch for (AA summaries, statement headers)."""

    hint: AccountHint
    link_status: Literal["linked", "sms_only", "known_unlinked"] = "linked"
    known_via: Literal["aa", "statement", "sms", "manual", "inferred"]
    credit_limit_paise: int | None = None
    min_balance_paise: int | None = None
    apr_bps: int | None = None
    statement_day: int | None = None
    due_day: int | None = None
    loan_principal_paise: int | None = None
    loan_outstanding_paise: int | None = None
    loan_rate_bps: int | None = None
    loan_tenure_months: int | None = None
    loan_remaining_months: int | None = None
    emi_paise: int | None = None
    emi_next_due: dt.date | None = None
    emi_day: int | None = None
    lender_merchant_key: str | None = None
    balance_paise: int | None = None
    balance_date: dt.date | None = None
    holder_names: list[str] = Field(default_factory=list)  # PII: goes to the masking dictionary only


class RawTransaction(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: Source
    source_ref: str
    account: AccountHint
    txn_date: dt.date
    seq: int = 0
    amount_paise: int = Field(gt=0)
    direction: Direction
    narration: str
    merchant_hint: str | None = None  # structured sources (SMS, manual) give the payee directly
    reference: str | None = None  # UPI ref / UTR, used for cross-source dedupe
    balance_after_paise: int | None = None
    channel_hint: str | None = None
    category_hint: str | None = None

    def payload_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()


class ConsentInfo(BaseModel):
    consent_id: str
    purpose_code: str
    purpose_text: str | None
    fi_types: list[str]
    scope_accounts: list[str]
    data_from: dt.date
    data_to: dt.date
    expires_at: dt.datetime


class IngestBatch(BaseModel):
    source: Source
    accounts: list[AccountInfo] = Field(default_factory=list)
    transactions: list[RawTransaction] = Field(default_factory=list)
    consent: ConsentInfo | None = None


class SourceAdapter(Protocol):
    source: Source

    def parse(self, payload, user_id: str) -> IngestBatch: ...


class IngestError(ValueError):
    pass


def institution_code(name: str) -> str:
    """'HDFC-FIP' / 'HDFC Bank' / 'hdfc' -> 'hdfc'. Unknown names become a slug."""
    up = name.upper().strip()
    insts = load_yaml("institutions")["institutions"]
    if up.lower() in insts:
        return up.lower()
    for code, spec in insts.items():
        if any(a == up or up.startswith(a) for a in spec["aliases"]):
            return code
    for code, spec in insts.items():
        if any(a in up for a in spec["aliases"]):
            return code
    return "".join(c for c in up.lower() if c.isalnum())[:32]
