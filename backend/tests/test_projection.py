import dataclasses

from app.engines.emi import emi_paise
from app.engines.projection import Loan, Modifier, ProjectionInputs, project
from tests.test_score import _metrics


def _inputs(**kw):
    base = dict(income_paise=100_000_00, spend_paise=80_000_00, essential_paise=40_000_00, emi_paise=10_000_00,
                liquid_paise=50_000_00, liquid_trend_paise=5_000_00, revolving_paise=40_000_00,
                card_rate_monthly=0.04, card_purchases_paise=20_000_00, pay_ratio=0.3, card_limit_paise=150_000_00,
                loans=[Loan("l1", "Lender", 1_20_000_00, emi_paise(1_20_000_00, 1300, 14), 1300, 14)],
                savings_rate_monthly=0.0025, unswept_share=0.5,
                metrics=_metrics(savings_rate=0.2, buffer_months=1.25, emi_to_income=0.1, credit_utilisation=0.4,
                                 revolving_paise=40_000_00, cards=[object()], cycle_lows=[{"clean": True}] * 3,
                                 discretionary_ratio=1.0))
    base.update(kw)
    return ProjectionInputs(**base)


def test_card_converges_to_its_observed_steady_state():
    p = project(_inputs())
    steady = 0.7 * 20_000_00 / (1 - 0.7 * 1.04)
    assert abs(p.months[-1].revolving - steady) / steady < 0.03


def test_an_empty_modifier_is_the_baseline():
    a, b = project(_inputs()), project(_inputs(), Modifier())
    assert [dataclasses.astuple(x)[:7] for x in a.months] == [dataclasses.astuple(x)[:7] for x in b.months]


def test_loan_amortises_to_zero_on_schedule():
    p = project(_inputs(loans=[Loan("l1", "L", 1_20_000_00, emi_paise(1_20_000_00, 1300, 10), 1300, 10)]))
    assert p.months[9].emi > 0 and p.months[10].emi == 0


def test_clearing_with_pay_in_full_stays_clear():
    p = project(_inputs(), Modifier(savings_to_card_now=40_000_00, pay_in_full_once_clear=True))
    assert all(m.revolving == 0 for m in p.months) and p.score_12m.total > project(_inputs()).score_12m.total
