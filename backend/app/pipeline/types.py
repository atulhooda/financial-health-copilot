from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

SOURCE_PRIORITY = {"aa": 0, "statement": 1, "sms": 2, "manual": 3}


@dataclass
class PAccount:
    account_id: str
    kind: str  # savings | current | credit_card | loan
    institution: str
    last4: str
    link_status: str = "linked"
    lender_merchant_key: str | None = None


@dataclass
class PRaw:
    raw_id: str
    source: str
    account_id: str
    txn_date: dt.date
    seq: int
    amount_paise: int
    direction: str
    narration: str
    merchant_hint: str | None = None
    reference: str | None = None
    balance_after_paise: int | None = None
    channel_hint: str | None = None
    category_hint: str | None = None

    def sort_key(self):
        return (self.txn_date, self.account_id, SOURCE_PRIORITY[self.source], self.seq, self.raw_id)


@dataclass
class PTxn:
    """A canonical transaction as it moves through the pipeline."""

    raw: PRaw  # canonical source row
    sources: list[PRaw]
    account: PAccount
    channel: str
    payee_clean: str
    merchant_key: str
    merchant_name: str
    counterparty_type: str
    remark: str
    reference: str | None
    last4_ref: str | None
    signatures: set[str]
    person_keys: list[str] = field(default_factory=list)
    txn_id: str = ""
    category: str | None = None
    category_source: str | None = None
    category_confidence: float = 0.0
    transfer_group_id: str | None = None
    is_bounce: bool = False

    @property
    def date(self) -> dt.date:
        return self.raw.txn_date

    @property
    def amount(self) -> int:
        return self.raw.amount_paise

    @property
    def direction(self) -> str:
        return self.raw.direction
