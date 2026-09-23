import datetime as dt

from sqlalchemy import select

from app.db.models import Account, Transaction, TransactionSource
from app.demo.scenario import persona_a_steps
from app.ingest.registry import adapter_for
from app.ingest.service import ingest_batch
from app.pipeline.merchants import parse_narration, resolve
from app.pipeline.run import run_pipeline
from app.pipeline.types import PAccount, PRaw

D = dt.date(2026, 9, 12)
SAL = PAccount("acc_sal", "savings", "hdfc", "4321")
SAV = PAccount("acc_sav", "savings", "icici", "7788")


def raw(rid, source, narration, amount=45000, direction="debit", date=D, account="acc_sal", **kw):
    return PRaw(rid, source, account, date, 0, amount, direction, narration, **kw)


def _run(raws, accounts=(SAL, SAV), holders=("AARAV DESHMUKH",)):
    return run_pipeline("u1", raws, list(accounts), None, list(holders))


# ---- normalisation --------------------------------------------------------------------------------
def test_merchant_normalisation_across_formats():
    cases = {
        "UPI/DR/412345678901/SWIGGY/YESB/swiggy@axisbank/Payment": ("swiggy", "upi"),
        "UPI-ZOMATO-zomato@hdfcbank-HDFC0000001-412345678901-UPI": ("zomato", "upi"),
        "NACH-DR-BAJAJ FINANCE LTD-HDFC7021807230034567": ("bajaj_finance", "nach"),
        "NEFT CR-CITI0000001-ACME TECHNOLOGIES PVT LTD-SALARY SEP26": ("acme_tech", "neft"),
        "POS 4321XXXXXXXX1234 AMAZON PAY INDIA BENGALURU": ("amazon", "pos"),
        "BIL/ONL/412345/AXIS BANK CREDIT CARD/XXXXXXXXXXXX9012": ("m:axis_bank_credit_card", "billpay"),
    }
    for narration, (key, channel) in cases.items():
        p = parse_narration(narration)
        assert (resolve(p).merchant_key, p.channel) == (key, channel), narration


def test_person_detection_and_self():
    p = parse_narration("NEFT-HDFC0412345-RAMESH KULKARNI-RENT SEP-412345678901")
    r = resolve(p)
    assert r.counterparty_type == "person" and "name:RAMESH KULKARNI" in r.person_keys
    assert resolve(parse_narration("UPI/DR/1/SHREE SAI KIRANA/YESB/saikirana@ybl/x")).counterparty_type == "merchant"
    p = parse_narration("IMPS/P2A/412345678901/AARAV DESHMUKH/HDFC/FROM XX4321")
    assert resolve(p, frozenset({"AARAV DESHMUKH"})).counterparty_type == "self"


def test_gig_payout_is_income_not_refund():
    res = _run([raw("c", "aa", "NEFT CR-YESB0000001-BUNDL TECHNOLOGIES-BUNDL TECH PAYOUT", amount=300000,
                    direction="credit"),
                raw("r", "aa", "UPI/CR/412345678901/SWIGGY/YESB/swiggy@axisbank/Refund", amount=45000,
                    direction="credit")])
    assert {t.raw.raw_id: t.category for t in res.txns} == {"c": "income_gig", "r": "refund"}


# ---- dedupe ---------------------------------------------------------------------------------------
def test_same_txn_from_aa_and_sms_becomes_one_canonical():
    res = _run([raw("r_aa", "aa", "UPI/DR/412345678901/SWIGGY/YESB/swiggy@axisbank/Payment", reference="412345678901"),
                raw("r_sms", "sms", "SMS/SWIGGY", merchant_hint="SWIGGY", reference="412345678901", channel_hint="upi")])
    assert len(res.txns) == 1 and res.duplicates_merged == 1
    t = res.txns[0]
    assert t.raw.source == "aa" and sorted(s.source for s in t.sources) == ["aa", "sms"]


def test_dedupe_without_reference_uses_merchant_and_one_day_window():
    res = _run([raw("r_aa", "aa", "NACH-DR-BAJAJ FINANCE LTD-HDFC7021807230034567", amount=879500),
                raw("r_sms", "sms", "SMS/NACH-DR-BAJAJ FINANCE LTD", amount=879500,
                    merchant_hint="NACH-DR-BAJAJ FINANCE LTD", date=D + dt.timedelta(days=1))])
    assert len(res.txns) == 1


def test_no_dedupe_when_different_amount_date_or_same_source():
    base = "UPI/DR/1/SWIGGY/YESB/swiggy@axisbank/Payment"
    res = _run([raw("a1", "aa", base), raw("a2", "aa", base),  # two real ₹450 orders, same day, same source
                raw("s1", "sms", "SMS/SWIGGY", merchant_hint="SWIGGY", amount=45100),
                raw("s2", "sms", "SMS/SWIGGY", merchant_hint="SWIGGY", date=D + dt.timedelta(days=2))])
    assert len(res.txns) == 4


def test_sms_dedupes_against_only_one_of_two_identical_aa_rows():
    base = "UPI/DR/1/SWIGGY/YESB/swiggy@axisbank/Payment"
    res = _run([raw("a1", "aa", base), raw("a2", "aa", base),
                raw("s1", "sms", "SMS/SWIGGY", merchant_hint="SWIGGY")])
    assert len(res.txns) == 2 and res.duplicates_merged == 1


# ---- transfers (D3/D4) ------------------------------------------------------------------------------
def test_self_transfer_between_visible_accounts():
    res = _run([raw("d", "aa", "IMPS/P2A/412345678901/SELF/ICICI/XX7788 SAVINGS", amount=400000),
                raw("c", "aa", "IMPS/P2A/412345678901/AARAV DESHMUKH/HDFC/FROM XX4321", amount=400000,
                    direction="credit", account="acc_sav")])
    assert [t.category for t in res.txns] == ["transfer_self", "transfer_self"]
    assert res.txns[0].transfer_group_id == res.txns[1].transfer_group_id


def test_transfer_to_unseen_account_counts_as_spend():
    res = _run([raw("d", "aa", "IMPS/P2A/412345678901/SELF/KOTAK/XX9999", amount=400000)])
    assert res.txns[0].category == "transfer_unseen"


def test_card_bill_to_unlinked_card_is_spend_and_infers_the_card():
    res = _run([raw("d", "aa", "BIL/ONL/412345/AXIS BANK CREDIT CARD/XXXXXXXXXXXX9012", amount=2000000)])
    assert res.txns[0].category == "card_bill_unlinked"
    assert [(a.institution, a.last4, a.link_status) for a in res.inferred_accounts] == [("axis", "9012", "known_unlinked")]


def test_card_bill_to_linked_card_is_a_transfer():
    card = PAccount("acc_card", "credit_card", "axis", "9012")
    res = _run([raw("d", "aa", "BIL/ONL/412345/AXIS BANK CREDIT CARD/XXXXXXXXXXXX9012", amount=2000000),
                raw("c", "aa", "PAYMENT RECEIVED - THANK YOU", amount=2000000, direction="credit", account="acc_card"),
                raw("p", "aa", "AMAZON PAY INDIA PUNE", amount=250000, account="acc_card"),
                raw("i", "aa", "FINANCE CHARGES", amount=120000, account="acc_card"),
                raw("g", "aa", "IGST ON FINANCE CHARGES", amount=21600, account="acc_card")],
               accounts=(SAL, SAV, card))
    cats = {t.raw.raw_id: t.category for t in res.txns}
    assert cats == {"d": "transfer_self", "c": "transfer_self", "p": "shopping", "i": "card_interest", "g": "fees_charges"}


def test_loan_emi_is_not_a_transfer():
    loan = PAccount("acc_loan", "loan", "bajaj", "0931")
    res = _run([raw("d", "aa", "NACH-DR-BAJAJ FINANCE LTD-HDFC7021807230034567", amount=879500),
                raw("c", "aa", "EMI RECEIVED - THANK YOU", amount=879500, direction="credit", account="acc_loan")],
               accounts=(SAL, loan))
    assert {t.raw.raw_id: t.category for t in res.txns} == {"d": "emi", "c": "loan_repayment_in"}


# ---- end to end through the DB: retroactive reclassification at +1 ------------------------------------
def _apply(session, step, clock, categoriser):
    for source, payload in step.payloads:
        ingest_batch(session, "demo-a", adapter_for(source, clock).parse(payload, "demo-a"), categoriser, clock)
    session.commit()


def test_linking_the_card_reclassifies_history(session, clock, categoriser):
    steps = persona_a_steps()
    _apply(session, steps[0], clock, categoriser)
    q = lambda: list(session.scalars(select(Transaction).where(Transaction.user_id == "demo-a")))
    t0 = q()
    bills = [t for t in t0 if t.category == "card_bill_unlinked"]
    assert len(bills) >= 5
    card = session.scalars(select(Account).where(Account.user_id == "demo-a", Account.kind == "credit_card")).one()
    assert card.link_status == "known_unlinked"
    # every structured SMS was a duplicate of an AA row
    srcs = list(session.scalars(select(TransactionSource).where(TransactionSource.user_id == "demo-a")))
    assert sum(s.source == "sms" for s in srcs) > 20 and all(
        s.is_canonical is False for s in srcs if s.source == "sms")

    _apply(session, steps[1], clock, categoriser)
    t1 = q()
    session.refresh(card)
    assert card.link_status == "linked"
    assert not [t for t in t1 if t.category == "card_bill_unlinked"]
    bill_ids = {t.txn_id for t in bills}
    assert all(t.category == "transfer_self" for t in t1 if t.txn_id in bill_ids)
    card_txns = [t for t in t1 if t.account_id == card.account_id]
    assert any(t.category == "card_interest" for t in card_txns)
    purchases = sum(t.amount_paise for t in card_txns if t.direction == "debit")
    assert purchases > sum(t.amount_paise for t in bills)  # revolving debt was funding spend (D4)
