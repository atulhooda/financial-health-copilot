"""Pre-Phase-5 fix 1: the score must never punish paying down debt (property tests, deterministic)."""
import dataclasses

from hypothesis import given, settings
from hypothesis import strategies as st

from app.engines.projection import Loan, Modifier, ProjectionInputs, project
from app.engines.score import compute_score
from tests.test_score import _metrics

SETTINGS = settings(derandomize=True, max_examples=300, deadline=None)
RUPEES = st.integers(min_value=0, max_value=5_00_000).map(lambda r: r * 100)


@SETTINGS
@given(liquid=RUPEES, revolving=RUPEES, extra_statement=RUPEES, limit=st.integers(10_000, 5_00_000),
       essential=st.integers(5_000, 1_50_000), move_share=st.floats(0, 1))
def test_moving_savings_to_revolving_debt_never_lowers_the_current_score(liquid, revolving, extra_statement, limit,
                                                                        essential, move_share):
    limit_p, ess = limit * 100, essential * 100
    statement = revolving + extra_statement

    def metrics(liq, rev, stmt):
        return _metrics(savings_rate=0.1, buffer_months=max(0, liq - rev) / ess, emi_to_income=0.1,
                        credit_utilisation=stmt / limit_p, revolving_paise=rev, revolving_ratio=rev / limit_p,
                        cycle_lows=[{"clean": True}] * 3, discretionary_ratio=1.0, cards=[object()])
    x = int(min(liquid, revolving) * move_share)
    before = compute_score(metrics(liquid, revolving, statement))
    after = compute_score(metrics(liquid - x, revolving - x, statement - x))
    assert after.total >= before.total


@SETTINGS
@given(liquid=RUPEES, revolving=RUPEES, purchases=st.integers(0, 60_000).map(lambda r: r * 100),
       ratio=st.floats(0.05, 1.0), rate=st.floats(0.003, 0.05), trend=st.integers(-20_000, 30_000).map(lambda r: r * 100),
       share=st.floats(0, 1), now=RUPEES, monthly=st.integers(0, 20_000).map(lambda r: r * 100),
       surplus=st.integers(0, 20_000).map(lambda r: r * 100), in_full=st.booleans())
def test_paying_debt_down_faster_never_lowers_the_12_month_score(liquid, revolving, purchases, ratio, rate, trend,
                                                                  share, now, monthly, surplus, in_full):
    inp = ProjectionInputs(
        income_paise=1_00_000_00, spend_paise=80_000_00, essential_paise=40_000_00, emi_paise=10_000_00,
        liquid_paise=liquid, liquid_trend_paise=trend, revolving_paise=revolving, card_rate_monthly=rate,
        card_purchases_paise=purchases, pay_ratio=ratio, card_limit_paise=3_00_000_00,
        loans=[Loan("l", "L", 1_00_000_00, 10_000_00, 1300, 11)], savings_rate_monthly=0.0025, unswept_share=share,
        metrics=_metrics(savings_rate=0.2, buffer_months=1.0, emi_to_income=0.1, credit_utilisation=0.3,
                         revolving_paise=revolving, revolving_ratio=revolving / 3_00_000_00, cards=[object()],
                         cycle_lows=[{"clean": True}] * 3, discretionary_ratio=1.0))
    base = project(inp)
    for mod in (Modifier(savings_to_card_now=min(now, liquid), pay_in_full_once_clear=in_full),
                Modifier(redirect_to_card=monthly), Modifier(extra_to_card_from_surplus=surplus)):
        assert project(inp, mod).score_12m.total >= base.score_12m.total, dataclasses.asdict(mod)
