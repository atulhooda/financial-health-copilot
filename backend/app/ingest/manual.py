from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.ids import stable_hash
from app.ingest.base import AccountHint, AccountInfo, IngestBatch, RawTransaction

CASH_WALLET = AccountHint(institution="cash", masked_number="CASH0000", kind="savings")


class ManualTxn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_ref: str = Field(min_length=1, max_length=64)
    txn_date: dt.date
    amount_paise: int = Field(gt=0)
    direction: Literal["debit", "credit"]
    merchant: str = Field(min_length=1, max_length=120)
    category: str | None = None
    account: AccountHint | None = None  # default: cash wallet


class ManualAdapter:
    source = "manual"

    def parse(self, payload: dict, user_id: str) -> IngestBatch:
        txns = [ManualTxn.model_validate(t) for t in payload["transactions"]]
        batch = IngestBatch(source="manual")
        seen = set()
        for i, t in enumerate(txns):
            hint = t.account or CASH_WALLET
            if hint.masked_number not in seen:
                batch.accounts.append(AccountInfo(hint=hint, known_via="manual"))
                seen.add(hint.masked_number)
            batch.transactions.append(RawTransaction(
                source="manual", source_ref=stable_hash(user_id, "manual", t.client_ref, n=16), account=hint,
                txn_date=t.txn_date, seq=i, amount_paise=t.amount_paise, direction=t.direction,
                narration=f"MANUAL/{t.merchant.upper()}", merchant_hint=t.merchant, category_hint=t.category,
                channel_hint="cash" if hint is CASH_WALLET else None))
        return batch
