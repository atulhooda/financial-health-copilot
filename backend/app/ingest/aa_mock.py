"""Account Aggregator style mock (SPEC §4).

We are NOT an FIU. This mock sits behind the same SourceAdapter interface a licensed
AA/FIU partner adapter would implement. Field names are loosely borrowed from the
ReBIT FI schemas (deposit, credit card, term loan); payloads are not XSD-validated.
"""
from __future__ import annotations

import datetime as dt

from app.core.clock import Clock, get_clock
from app.core.money import parse_inr
from app.ingest.base import (
    AccountHint,
    AccountInfo,
    ConsentInfo,
    IngestBatch,
    IngestError,
    RawTransaction,
    institution_code,
)

FI_KIND = {"DEPOSIT": None, "CREDIT_CARD": "credit_card", "TERM_LOAN": "loan", "RECURRING_DEPOSIT": None}


def _ts_date(ts: str) -> dt.date:
    return dt.datetime.fromisoformat(ts).date()


class AAMockAdapter:
    source = "aa"

    def __init__(self, clock: Clock | None = None):
        self.clock = clock or get_clock()

    def parse(self, payload: dict, user_id: str) -> IngestBatch:
        c = payload["consent"]
        consent = ConsentInfo(
            consent_id=c["id"], purpose_code=c["purpose"]["code"], purpose_text=c["purpose"].get("text"),
            fi_types=list(c["fiTypes"]), scope_accounts=list(c.get("accounts", [])),
            data_from=dt.date.fromisoformat(c["dataRange"]["from"]),
            data_to=dt.date.fromisoformat(c["dataRange"]["to"]),
            expires_at=dt.datetime.fromisoformat(c["expiry"]),
        )
        if consent.expires_at < self.clock.now():
            raise IngestError(f"consent {consent.consent_id} expired at {consent.expires_at.isoformat()}")

        batch = IngestBatch(source="aa", consent=consent)
        for fi in payload.get("fi", []):
            fi_type = fi["fiType"]
            if fi_type not in consent.fi_types:
                raise IngestError(f"FI type {fi_type} is outside consent scope")
            acct = fi["account"]
            masked = acct["maskedAccNumber"]
            if consent.scope_accounts and masked not in consent.scope_accounts:
                raise IngestError("account is outside consent scope")
            kind = FI_KIND.get(fi_type) or ("current" if acct.get("type", "").upper() == "CURRENT" else "savings")
            hint = AccountHint(institution=institution_code(acct["fipName"]), masked_number=masked, kind=kind)
            batch.accounts.append(self._account_info(hint, fi))
            for i, t in enumerate(fi.get("transactions", [])):
                d = _ts_date(t["transactionTimestamp"])
                if not (consent.data_from <= d <= consent.data_to):
                    continue
                batch.transactions.append(RawTransaction(
                    source="aa", source_ref=t["txnId"], account=hint, txn_date=d, seq=i,
                    amount_paise=parse_inr(t["amount"]),
                    direction="debit" if t["type"].upper() == "DEBIT" else "credit",
                    narration=t.get("narration", ""), reference=t.get("reference") or None,
                    balance_after_paise=(parse_inr(t["currentBalance"])
                                         if t.get("currentBalance") not in (None, "") else None),
                    channel_hint=(t.get("mode") or "").lower() or None,
                ))
        return batch

    @staticmethod
    def _account_info(hint: AccountHint, fi: dict) -> AccountInfo:
        s = fi.get("summary", {})
        holders = [h["name"] for h in fi.get("profile", {}).get("holders", []) if h.get("name")]
        holders += [h["nominee"] for h in fi.get("profile", {}).get("holders", []) if h.get("nominee")]
        money = lambda k: parse_inr(s[k]) if s.get(k) not in (None, "") else None
        info = AccountInfo(hint=hint, known_via="aa", holder_names=holders)
        if hint.kind in ("savings", "current"):
            info.balance_paise = money("currentBalance")
            info.balance_date = dt.date.fromisoformat(s["balanceDate"]) if s.get("balanceDate") else None
            info.min_balance_paise = money("minimumBalance")
        elif hint.kind == "credit_card":
            info.credit_limit_paise = money("creditLimit")
            info.balance_paise = money("currentDue")
            info.balance_date = dt.date.fromisoformat(s["balanceDate"]) if s.get("balanceDate") else None
            if s.get("interestRateMonthly"):
                info.apr_bps = round(float(s["interestRateMonthly"]) * 12 * 100)
            info.statement_day = s.get("statementDay")
            info.due_day = s.get("dueDay")
        elif hint.kind == "loan":
            info.loan_principal_paise = money("principalAmount")
            info.loan_outstanding_paise = money("outstandingBalance")
            info.loan_rate_bps = round(float(s["interestRate"]) * 100) if s.get("interestRate") else None
            info.loan_tenure_months = s.get("tenureMonths")
            info.loan_remaining_months = s.get("remainingInstallments")
            info.emi_paise = money("installmentAmount")
            info.emi_next_due = dt.date.fromisoformat(s["nextInstallmentDate"]) if s.get("nextInstallmentDate") else None
            info.emi_day = info.emi_next_due.day if info.emi_next_due else None
            info.lender_merchant_key = s.get("lenderKey")
            info.balance_paise = info.loan_outstanding_paise
        return info
