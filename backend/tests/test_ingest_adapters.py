import datetime as dt

import pytest

from app.core.clock import FixedClock
from app.demo.personas import T0, build
from app.demo.scenario import seed_payloads
from app.ingest.aa_mock import AAMockAdapter
from app.ingest.base import IngestError, institution_code
from app.ingest.manual import ManualAdapter
from app.ingest.statements import CsvStatementAdapter, PdfStatementAdapter


def _aa_payload():
    w = build("demo-a")
    return w.aa_payload(["sal", "loan1"], dt.date(2026, 8, 1), T0)


def test_aa_parses_deposit_and_loan(clock):
    batch = AAMockAdapter(clock).parse(_aa_payload(), "demo-a")
    kinds = {a.hint.kind for a in batch.accounts}
    assert kinds == {"savings", "loan"}
    loan = next(a for a in batch.accounts if a.hint.kind == "loan")
    assert loan.emi_paise and loan.emi_next_due == dt.date(2026, 9, 10) and loan.loan_rate_bps == 1300
    assert "Aarav Deshmukh" in next(a for a in batch.accounts if a.hint.kind == "savings").holder_names
    assert all(t.amount_paise > 0 for t in batch.transactions)
    assert batch.consent.purpose_code == "102"


def test_aa_rejects_expired_consent():
    with pytest.raises(IngestError, match="expired"):
        AAMockAdapter(FixedClock(dt.date(2028, 1, 1))).parse(_aa_payload(), "demo-a")


def test_aa_rejects_fi_type_outside_consent(clock):
    p = _aa_payload()
    p["consent"]["fiTypes"] = ["DEPOSIT"]
    with pytest.raises(IngestError, match="outside consent"):
        AAMockAdapter(clock).parse(p, "demo-a")


def test_aa_drops_transactions_outside_data_range(clock):
    p = _aa_payload()
    p["consent"]["dataRange"]["from"] = "2026-09-01"
    batch = AAMockAdapter(clock).parse(p, "demo-a")
    assert min(t.txn_date for t in batch.transactions) >= dt.date(2026, 9, 1)


def test_csv_statement_uses_config_mapping():
    source, payload = [x for x in seed_payloads("demo-c") if x[0] == "statement"][0]
    batch = CsvStatementAdapter().parse(payload, "demo-c")
    assert batch.accounts[0].hint.institution == "icici" and batch.accounts[0].hint.last4 == "3344"
    assert any("INITECH" in t.narration and t.direction == "credit" for t in batch.transactions)
    assert batch.accounts[0].balance_paise is not None


def test_csv_statement_missing_column_is_rejected():
    with pytest.raises(IngestError, match="missing columns"):
        CsvStatementAdapter().parse({"bank": "hdfc_savings", "account_masked": "XX1111",
                                     "content": "Date,Foo\n01/09/26,1\n"}, "u1")


def test_pdf_is_stubbed():
    with pytest.raises(NotImplementedError):
        PdfStatementAdapter().parse(b"%PDF", "u1")


def test_manual_entries_default_to_cash_wallet():
    batch = ManualAdapter().parse({"transactions": [{"client_ref": "c1", "txn_date": "2026-09-01",
                                                     "amount_paise": 15000, "direction": "debit",
                                                     "merchant": "Chai", "category": "dining"}]}, "u1")
    t = batch.transactions[0]
    assert t.account.institution == "cash" and t.category_hint == "dining"


def test_institution_codes():
    assert institution_code("HDFC-FIP") == "hdfc"
    assert institution_code("AXIS BANK CREDIT CARD") == "axis"
    assert institution_code("icici") == "icici"
