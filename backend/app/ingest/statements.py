"""Bank statement import. Column mappings live in config/banks/<bank>.yaml, not code (SPEC §4)."""
from __future__ import annotations

import csv
import datetime as dt
import io

from app.core.config import load_yaml
from app.core.ids import stable_hash
from app.core.money import parse_inr
from app.ingest.base import AccountHint, AccountInfo, IngestBatch, IngestError, RawTransaction


def load_bank_config(bank: str) -> dict:
    try:
        return load_yaml(f"banks/{bank}")
    except FileNotFoundError as e:
        raise IngestError(f"unknown bank format {bank!r}; add config/banks/{bank}.yaml") from e


class CsvStatementAdapter:
    source = "statement"

    def parse(self, payload: dict, user_id: str) -> IngestBatch:
        """payload: {bank, account_masked, content (str)}."""
        cfg = load_bank_config(payload["bank"])
        cols = cfg["columns"]
        hint = AccountHint(institution=cfg["institution"], masked_number=payload["account_masked"],
                           kind=cfg["account_kind"])
        text = payload["content"]
        lines = text.splitlines()[cfg.get("skip_rows", 0):]
        reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter=cfg.get("delimiter", ","))
        missing = [c for c in cols.values() if c not in (reader.fieldnames or [])]
        if missing:
            raise IngestError(f"statement is missing columns {missing} for bank format {payload['bank']!r}")

        batch = IngestBatch(source="statement", accounts=[AccountInfo(hint=hint, known_via="statement")])
        last_date, last_bal = None, None
        for i, row in enumerate(reader):
            if not (row.get(cols["date"]) or "").strip():
                continue
            d = dt.datetime.strptime(row[cols["date"]].strip(), cfg["date_format"]).date()
            if "amount" in cols:
                amount = parse_inr(row[cols["amount"]])
                marker = row[cols["dr_cr"]].strip()
                direction = "debit" if marker == cfg["dr_cr_values"]["debit"] else "credit"
            else:
                deb, cred = (row.get(cols["debit"]) or "").strip(), (row.get(cols["credit"]) or "").strip()
                if deb and parse_inr(deb) > 0:
                    amount, direction = parse_inr(deb), "debit"
                elif cred and parse_inr(cred) > 0:
                    amount, direction = parse_inr(cred), "credit"
                else:
                    continue
            bal_text = (row.get(cols["balance"]) or "").strip() if cols.get("balance") else ""
            bal = parse_inr(bal_text) if bal_text else None
            narration = row[cols["narration"]].strip()
            ref = (row.get(cols.get("reference", ""), "") or "").strip() or None
            # A statement row has no id: derive a stable one from its content and position.
            src_ref = stable_hash(payload["bank"], hint.last4, d, i, amount, direction, narration, n=16)
            batch.transactions.append(RawTransaction(
                source="statement", source_ref=src_ref, account=hint, txn_date=d, seq=i, amount_paise=amount,
                direction=direction, narration=narration, reference=ref, balance_after_paise=bal))
            last_date, last_bal = d, bal
        if last_bal is not None:
            batch.accounts[0].balance_paise, batch.accounts[0].balance_date = last_bal, last_date
        return batch


class PdfStatementAdapter:
    """Same interface as CSV; stubbed for the prototype (SPEC §4)."""

    source = "statement"

    def parse(self, payload, user_id: str) -> IngestBatch:
        raise NotImplementedError("PDF statement import is stubbed in the prototype; use CSV")
