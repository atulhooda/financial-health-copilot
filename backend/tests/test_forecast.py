"""Forecast engine, schedule and backtest (SPEC §6.4)."""
import dataclasses
import datetime as dt

import numpy as np
import pytest

from app.core.clock import FixedClock
from app.demo.personas import T0, WORLD_END
from app.demo.scenario import persona_a_steps, seed_payloads
from app.engines.backtest import confidence, forecast_with_confidence, run_backtest
from app.engines.financial import compute_metrics
from app.engines.forecast import build_pool, run_forecast
from app.engines.view import build_view
from app.ingest.registry import adapter_for
from app.ingest.service import ingest_batch

STEPS = persona_a_steps()


def _ingest(session, user, payloads, clock, categoriser):
    for source, payload in payloads:
        ingest_batch(session, user, adapter_for(source, clock).parse(payload, user), categoriser, clock)
    session.commit()


def _replay(session, categoriser, upto):
    for step in STEPS[: upto + 1]:
        _ingest(session, "demo-a", step.payloads, FixedClock(step.as_of), categoriser)


def _fc(session, categoriser, as_of, user="demo-a", **kw):
    v = build_view(session, user, as_of, categoriser, **kw)
    m = compute_metrics(v)
    fc, sims = run_forecast(v, m.recurring)
    return v, m, fc, sims


@pytest.fixture
def a_t0(session, categoriser):
    _replay(session, categoriser, 0)
    return _fc(session, categoriser, T0)


def test_forecast_is_deterministic(session, categoriser, a_t0):
    _, _, fc1, _ = a_t0
    _, _, fc2, _ = _fc(session, categoriser, T0)
    assert dataclasses.asdict(fc1) == dataclasses.asdict(fc2)
    assert len(fc1.p10) == 45 and all(lo <= mid <= hi for lo, mid, hi in zip(fc1.p10, fc1.p50, fc1.p90, strict=True))


def test_common_random_numbers(a_t0):
    """Dropping one fixed debit from the schedule shifts every path by exactly its amount after its date."""
    v, m, fc, _ = a_t0
    netflix = next(f for f in fc.schedule if f.merchant_key == "netflix")
    fc2, _ = run_forecast(v, m.recurring, schedule=[f for f in fc.schedule if f is not netflix])
    for d, a, b in zip(fc.dates, fc.p50, fc2.p50, strict=True):
        assert b - a == (netflix.amount_paise if d >= netflix.date else 0)


def test_persona_a_t0_story_and_schedule(a_t0):
    v, m, fc, _ = a_t0
    assert fc.income_basis == "salary" and fc.next_income_date == dt.date(2026, 10, 1)
    assert fc.dip_probability > 0.5  # story check (SPEC §9)
    kinds = {f.kind for f in fc.schedule}
    assert {"salary", "rent", "emi", "sip", "sweep", "card_payment", "subscription"} <= kinds
    emi = next(f for f in fc.schedule if f.kind == "emi")
    assert (emi.date, emi.source, emi.mandate) == (dt.date(2026, 10, 10), "contract", True)
    card = next(f for f in fc.schedule if f.kind == "card_payment")
    assert card.source == "card_pattern" and card.account_id == v.operating.account_id  # SMS-only card at T0
    risk = next(b for b in fc.bounce_risks if b.kind == "emi")
    assert risk.name == "Bajaj Finance" and 0 <= risk.probability <= 1 and risk.due_date == dt.date(2026, 10, 10)
    sweep_legs = [f for f in fc.schedule if f.kind == "sweep" and f.date == dt.date(2026, 10, 2)]
    assert {f.direction for f in sweep_legs} == {"debit", "credit"}  # the savings leg is scheduled too


def test_linked_card_pays_observed_ratio_of_the_statement(session, categoriser):
    _replay(session, categoriser, 1)
    v, m, fc, _ = _fc(session, categoriser, T0)
    card = next(f for f in fc.schedule if f.kind == "card_payment")
    assert card.source == "card_statement" and card.spread is None  # the Sep 18 statement is already known
    stmt = v.txns.filter((v.txns["account_id"] == card.merchant_key[5:]) & (v.txns["date"] <= dt.date(2026, 9, 18)))
    ratio = card.amount_paise / stmt["balance_after"][-1]
    assert 0.25 <= ratio <= 0.35  # persona A pays ~30% of the statement
    assert card.date in (dt.date(2026, 10, 7), dt.date(2026, 10, 8))


def test_new_loan_emi_is_scheduled_with_bounce_risk(session, categoriser):
    _replay(session, categoriser, 3)
    _, _, fc, _ = _fc(session, categoriser, WORLD_END)
    new = next(b for b in fc.bounce_risks if b.merchant_key == "tata_capital")
    assert new.due_date == dt.date(2026, 12, 5) and new.mandate


def test_irregular_income_forecast(session, categoriser, clock):
    _ingest(session, "demo-b", seed_payloads("demo-b"), clock, categoriser)
    v, m, fc, _ = _fc(session, categoriser, T0, user="demo-b")
    assert fc.income_basis == "irregular" and fc.next_income_date == T0 + dt.timedelta(days=30)
    pool = build_pool(v, v.operating.account_id, m.recurring)
    assert (pool.values > 0).any()  # gig payouts live in the bootstrap pool, not in a salary schedule
    assert fc.available and 0 <= fc.dip_probability <= 1


def test_sweep_into_operating_account_is_scheduled(session, categoriser, clock):
    _ingest(session, "demo-c", seed_payloads("demo-c"), clock, categoriser)
    v, _, fc, _ = _fc(session, categoriser, T0, user="demo-c")
    household = [f for f in fc.schedule if f.kind == "sweep" and f.account_id == v.operating.account_id]
    assert household and all(f.direction == "credit" and f.amount_paise == 40_000_00 for f in household)


def test_backtest_and_confidence(session, categoriser, a_t0):
    v, m, _, _ = a_t0
    bt = run_backtest(v)
    assert len(bt.origins) >= 6 and 0 <= bt.coverage <= 1 and bt.days > 150
    conf = confidence(v, m.coverage, bt)
    expected = np.floor(100 * min(1, bt.coverage / 0.8) * min(1, m.coverage["months_of_history"] / 6)
                        * (m.coverage["accounts_linked"] / m.coverage["accounts_known"]) + 0.5)
    assert conf.pct == expected and conf.coverage == bt.coverage
    assert conf.label == ("High" if conf.pct >= 75 else "Medium" if conf.pct >= 50 else "Low")
    assert conf.linkage_factor == 0.75  # card known but not linked at T0
    fc, conf2, bt2 = forecast_with_confidence(v, m.recurring, m.coverage)
    assert conf2 == conf and bt2.coverage == bt.coverage


def test_forecast_unavailable_without_an_operating_balance(session, categoriser, clock):
    from app.demo.personas import build
    from app.demo.scenario import phone_sms_payload

    w = build("demo-a")  # SMS only: we see transactions but have no balances
    _ingest(session, "sms-only", [("sms", phone_sms_payload(w, dt.date(2026, 3, 21), T0))], clock, categoriser)
    _, _, fc, _ = _fc(session, categoriser, T0, user="sms-only")
    assert not fc.available and "balance" in fc.reason


def test_loan_cash_is_earmarked_not_a_cushion(session, categoriser):
    """D33: the +3 disbursal must not make the forecast look safe; it is excluded and stated as an assumption."""
    _replay(session, categoriser, 3)
    v, _, fc, _ = _fc(session, categoriser, WORLD_END)
    assert fc.earmarked_loan_paise >= 1_90_000_00
    assert fc.opening_paise - fc.earmarked_loan_paise < 1_00_000_00
    assert [a["key"] for a in fc.assumptions] == ["loan_cash_earmarked"]
    seq2 = sum(len(s.payloads) for s in STEPS[:3])
    _, _, before, _ = _fc(session, categoriser, WORLD_END, max_ingest_seq=seq2)
    assert before.earmarked_loan_paise == 0
    assert fc.dip_probability >= before.dip_probability  # the new EMI can only add risk
