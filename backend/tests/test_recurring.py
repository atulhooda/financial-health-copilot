"""Recurring detection robustness (SPEC §6.2, D23)."""
import datetime as dt

import numpy as np

from app.core.dates import roll_back_weekend
from app.engines.recurring import amount_level, detect_recurring
from app.engines.view import AccountView
from tests.helpers import make_view

AS_OF = dt.date(2026, 9, 20)


def _monthly(day: int, months: range, year: int = 2026) -> list[dt.date]:
    return [dt.date(year, m, day) for m in months]


def _salary_rows(amounts):
    # paydays on the 1st, rolled back over weekends: gaps vary between 28 and 33 days
    days = [roll_back_weekend(dt.date(2026, m, 1)) for m in range(10 - len(amounts), 10)]
    return [{"date": d, "amount": a * 100, "direction": "credit", "category": "income_salary",
             "merchant_key": "acme_tech"} for d, a in zip(days, amounts, strict=True)]


def _only(items, key):
    found = [i for i in items if i.merchant_key == key]
    assert len(found) == 1, [i.merchant_key for i in items]
    return found[0]


def test_salary_series_survives_weekend_rollbacks():
    rows = _salary_rows([92000] * 6)
    assert {(b["date"] - a["date"]).days for a, b in zip(rows, rows[1:], strict=False)} != {30}
    s = _only(detect_recurring(make_view(rows, AS_OF)), "acme_tech")
    assert (s.kind, s.cadence, s.amount_paise) == ("salary", "monthly", 92000_00)
    assert s.next_due == dt.date(2026, 10, 1) and s.active


def test_salary_hike_needs_two_credits():
    s = _only(detect_recurring(make_view(_salary_rows([92000] * 5 + [103040]), AS_OF)), "acme_tech")
    assert s.amount_paise == 92000_00 and s.pending_change
    s = _only(detect_recurring(make_view(_salary_rows([92000] * 4 + [103040] * 2), AS_OF)), "acme_tech")
    assert s.amount_paise == 103040_00 and not s.pending_change and s.changed_at == roll_back_weekend(dt.date(2026, 8, 1))


def test_subscription_price_change():
    def rows(amts):
        return [{"date": d, "amount": a * 100, "category": "ott_subscription", "merchant_key": "netflix"}
                for d, a in zip(_monthly(12, range(10 - len(amts), 10)), amts, strict=True)]
    assert _only(detect_recurring(make_view(rows([649] * 4 + [799] * 2), AS_OF)), "netflix").amount_paise == 79900
    one = _only(detect_recurring(make_view(rows([649] * 5 + [799]), AS_OF)), "netflix")
    assert one.amount_paise == 64900 and one.pending_change


def test_one_skipped_month_is_tolerated_two_are_not():
    base = [{"date": d, "amount": 24000_00, "category": "rent", "merchant_key": "p2p:contact_01",
             "counterparty_type": "person"} for d in _monthly(5, range(3, 10))]
    skip1 = [r for r in base if r["date"].month != 6]
    skip2 = [r for r in base if r["date"].month not in (5, 7)]
    assert _only(detect_recurring(make_view(skip1, AS_OF)), "p2p:contact_01").cadence == "monthly"
    assert not [i for i in detect_recurring(make_view(skip2, AS_OF)) if i.merchant_key == "p2p:contact_01"]


def test_annual_plan_detected_from_one_charge():
    rows = [{"date": dt.date(2026, 5, 14), "amount": 1499_00, "category": "ott_subscription",
             "merchant_key": "amazon_prime", "account_id": "acc_sal"}]
    item = _only(detect_recurring(make_view(rows, AS_OF)), "amazon_prime")
    assert (item.cadence, item.next_due, item.monthly_paise) == ("annual", dt.date(2027, 5, 14), 1499_00 // 12)
    rows[0]["amount"] = 523_00  # a one-off Amazon Prime charge that isn't a plan price
    assert not detect_recurring(make_view(rows, AS_OF))


def test_variable_bills_allowed_but_irregular_income_is_not():
    rng = np.random.default_rng(7)
    bills = [{"date": d, "amount": int(1800 * (1 + rng.uniform(-0.3, 0.3))) * 100, "category": "utilities",
              "merchant_key": "msedcl"} for d in _monthly(15, range(4, 10))]
    item = _only(detect_recurring(make_view(bills, AS_OF)), "msedcl")
    assert item.kind == "bill" and item.amount_variable
    # weekly gig payouts with 35% amount spread and missed weeks: not a salary
    days = [dt.date(2026, 3, 23) + dt.timedelta(weeks=w) for w in range(26) if w % 7 != 3]
    payouts = [{"date": d, "amount": int(2800 * np.exp(rng.normal(0, 0.35))) * 100, "direction": "credit",
                "category": "income_gig", "merchant_key": "swiggy_partner"} for d in days]
    assert not detect_recurring(make_view(payouts, AS_OF))


def test_contract_schedule_wins_and_is_known_immediately():
    loan = AccountView("acc_loan", "loan", "tatacap", "5520", True, None,
                       state={"emi_paise": 9_603_00, "emi_next_due": "2026-12-05",
                              "lender_merchant_key": "tata_capital", "loan_remaining_months": 24})
    rows = [{"date": dt.date(2026, 9, 1), "amount": 100_00, "category": "groceries", "merchant_key": "blinkit"}]
    view = make_view(rows, dt.date(2026, 11, 20))
    view.accounts["acc_loan"] = loan
    item = _only(detect_recurring(view), "tata_capital")
    assert (item.source, item.amount_paise, item.next_due, item.kind) == ("contract", 9_603_00, dt.date(2026, 12, 5), "emi")


def test_amount_level_rejects_noise():
    assert amount_level([100, 180, 90, 240]) is None
