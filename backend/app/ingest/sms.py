"""SMS source (SPEC §4, D12/D13).

Raw SMS text NEVER reaches the backend. Two separate things live here:
  (a) reference_parse(): the Python reference implementation of shared/sms_patterns.yaml,
      used only by tests and the demo replay (it plays the role of the phone);
  (b) SmsAdapter: the API-side adapter, which accepts only StructuredSmsTxn. The model
      forbids extra fields, so there is no field that could carry an SMS body.
"""
from __future__ import annotations

import datetime as dt
import re
from functools import lru_cache
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import SHARED_DIR
from app.core.ids import stable_hash
from app.core.money import parse_inr
from app.ingest.base import AccountHint, AccountInfo, IngestBatch, IngestError, RawTransaction


class StructuredSmsTxn(BaseModel):
    """What the phone sends after parsing an SMS on device. No free-text body, by construction."""

    model_config = ConfigDict(extra="forbid")

    client_ref: str = Field(min_length=8, max_length=64, description="opaque on-device id for idempotency")
    pattern_id: str = Field(max_length=64)
    sender_id: str = Field(max_length=16)
    account_last4: str = Field(pattern=r"^[0-9]{3,4}$")
    amount_paise: int = Field(gt=0)
    direction: Literal["debit", "credit"]
    merchant_raw: str = Field(max_length=120)
    txn_date: dt.date
    reference: str | None = Field(default=None, max_length=32)


@lru_cache
def load_patterns() -> dict:
    with open(SHARED_DIR / "sms_patterns.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _strptime_format(tokens: str) -> str:
    out = tokens
    for tok, fmt in (("YYYY", "%Y"), ("MON", "%b"), ("DD", "%d"), ("MM", "%m"), ("YY", "%y")):
        out = out.replace(tok, fmt)
    return out


def normalise_sms(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def reference_parse(text: str, sender: str) -> StructuredSmsTxn | None:
    """Reference implementation of the on-device parser. Returns None for non-transactional SMS."""
    body = normalise_sms(text)
    for p in load_patterns()["patterns"]:
        if not any(s in sender.upper() for s in p["sender_ids"]):
            continue
        flags = re.IGNORECASE if p.get("case_insensitive") else 0
        m = re.match(p["regex"], body, flags)
        if not m:
            continue
        f = {k: m.group(i) for k, i in p["fields"].items()}
        date = dt.datetime.strptime(f["date"], _strptime_format(p["date_format"])).date()
        return StructuredSmsTxn(
            client_ref=stable_hash(sender, body, n=24), pattern_id=p["id"], sender_id=sender[-6:],
            account_last4=f["account_last4"], amount_paise=parse_inr(f["amount"]),
            direction="credit" if p["kind"] == "credit" else "debit",
            merchant_raw=f["merchant"].strip(), txn_date=date, reference=f.get("reference"),
        )
    return None


class SmsAdapter:
    source = "sms"

    def parse(self, payload: dict, user_id: str) -> IngestBatch:
        patterns = {p["id"]: p for p in load_patterns()["patterns"]}
        txns = [StructuredSmsTxn.model_validate(t) for t in payload["transactions"]]
        batch = IngestBatch(source="sms")
        seen: set[tuple] = set()
        for i, t in enumerate(txns):
            p = patterns.get(t.pattern_id)
            if p is None:
                raise IngestError(f"unknown SMS pattern_id {t.pattern_id!r}")
            kind = "credit_card" if p["kind"] == "card_spend" else "savings"
            hint = AccountHint(institution=p["bank"], masked_number=f"XX{t.account_last4}", kind=kind)
            if (hint.institution, hint.last4, kind) not in seen:
                batch.accounts.append(AccountInfo(hint=hint, known_via="sms"))
                seen.add((hint.institution, hint.last4, kind))
            channel = "nach" if p["kind"] == "emi" else ("card" if kind == "credit_card" else
                                                          ("upi" if t.reference else None))
            batch.transactions.append(RawTransaction(
                source="sms", source_ref=t.client_ref, account=hint, txn_date=t.txn_date, seq=i,
                amount_paise=t.amount_paise, direction=t.direction, narration=f"SMS/{t.merchant_raw}",
                merchant_hint=t.merchant_raw, reference=t.reference, channel_hint=channel))
        return batch
