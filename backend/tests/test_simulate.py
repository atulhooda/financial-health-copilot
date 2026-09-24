"""Simulation engine (SPEC §6.5, D7, D8, D26, D30, D34) on persona A's replay states."""
import datetime as dt

import pytest

from app.core.clock import FixedClock
from app.demo.scenario import persona_a_steps
from app.engines.backtest import forecast_with_confidence
from app.engines.financial import compute_metrics
from app.engines.projection import Modifier, project
from app.engines.score import compute_score
from app.engines.simulate import (
    _size_sweep,
    _with_sweep,
    build_context,
    evaluate,
    gen_auto_sweep,
    gen_pay_down_card,
    gen_redirect_sweep,
    max_mandate_bounce,
    recommend,
    what_if_new_emi,
)
from app.engines.view import build_view
from app.ingest.registry import adapter_for
from app.ingest.service import ingest_batch

STEPS = persona_a_steps()
SEQ = {0: 2, 1: 3, 2: 5, 3: 6}  # ingest watermark after each step


@pytest.fixture
def replayed(session, categoriser):
    for step in STEPS:
        for source, payload in step.payloads:
            c = FixedClock(step.as_of)
            ingest_batch(session, "demo-a", adapter_for(source, c).parse(payload, "demo-a"), categoriser, c)
    session.commit()

    def ctx(i):
        as_of = STEPS[i].as_of
        v = build_view(session, "demo-a", as_of, categoriser, max_ingest_seq=SEQ[i])
        m = compute_metrics(v)
        s = compute_score(m)
        fc, conf, _ = forecast_with_confidence(v, m.recurring, m.coverage)
        return build_context(v, m, s, fc, conf)
    return ctx


def test_new_emi_what_if_matches_the_formula_and_states_its_rate(replayed):
    ctx = replayed(3)
    r = evaluate(ctx, what_if_new_emi(ctx, 60_000_00, 12))
    assert abs(r.extra["emi_paise"] - 5_415_00) <= 100
    assert r.extra["alt_tenure"]["tenure_months"] == 18 and abs(r.extra["alt_tenure"]["emi_paise"] - 3_743_00) <= 100
    rate = next(a for a in r.assumptions if a["key"] == "emi_rate")
    assert rate["value"] == 15 and rate["source"] == "config" and "assumed" in rate["text"]
    assert "first_emi" not in {a["key"] for a in r.assumptions}  # fix 4: the EMI date is not baked in
    opts = {o["option"]: o for o in r.impact["emi_dates"]["options"]}
    assert opts["just_after_payday"]["first_due"] == ctx.forecast.next_income_date + dt.timedelta(days=2)
    assert opts["30_days_from_today"]["first_due"] == ctx.view.as_of + dt.timedelta(days=30)
    gap = opts["30_days_from_today"]["new_emi_bounce_risk"] - opts["just_after_payday"]["new_emi_bounce_risk"]
    advice = r.impact["emi_dates"]["advice"]
    assert (advice is not None) == (gap >= 0.05)
    if advice:
        assert advice["text"] == "Ask for an EMI date right after your salary date"
    assert r.impact["emi_to_income_after"] > r.impact["emi_to_income_before"]
    assert r.rank is None  # a what-if is never auto-recommended (D8)
    user_rate = evaluate(ctx, what_if_new_emi(ctx, 60_000_00, 12, annual_rate_bps=1200))
    assert next(a for a in user_rate.assumptions if a["key"] == "emi_rate")["source"] == "user"


def test_clearing_the_card_is_cash_neutral_explicit_and_honest(replayed):
    ctx = replayed(1)
    (a,) = gen_pay_down_card(ctx)
    nid = ctx.forecast.next_income_date
    due_before = sum(f.amount_paise for f in ctx.forecast.schedule if f.direction == "debit" and f.date < nid
                     and f.kind != "card_payment")
    uncovered = max(0, due_before - ctx.view.operating.balance_paise)
    post = a.extra["post_clear_check"]
    assert a.params["cushion_paise"] == ctx.view.floor_paise + uncovered + post["extra_cushion_paise"]
    assert a.params["clears"] and a.params["amount_paise"] >= ctx.card.revolving_paise
    assert a.params["steps"][1] == "Set card autopay to the full statement amount"  # fix 3: step 2 explicit
    r = evaluate(ctx, a)
    assert r.confidence["label"] in ("Medium", "Low")  # depends on a behaviour change
    assert abs(r.impact["bounce_risk_after"] - r.impact["bounce_risk_before"]) <= 0.02  # next bill stays usual
    assert r.impact["months_to_clear_card"] == 1 and r.impact["revolving_12m_with_action_paise"] == 0
    down = r.extra["downside"]  # going back to paying ~30% rebuilds the balance
    assert down["rebuild_to_paise"] > 0 and 1 <= down["months_to_rebuild"] <= 12
    assert down["interest_saved_12m_if_back_to_usual_paise"] < r.impact["card_interest_saved_12m_paise"]
    assert post["horizon_days"] == 90 and (not post["raises"] or post["extra_cushion_paise"] > 0)
    assert "pay_in_full_after" in {x["key"] for x in r.assumptions}
    assert next(x for x in r.assumptions if x["key"] == "card_rate")["source"] == "statement"  # a FACT, not assumed


def test_existing_sweep_is_redirected_never_re_recommended(replayed):
    ctx = replayed(1)
    redirect = gen_redirect_sweep(ctx)
    assert len(redirect) == 1 and redirect[0].params["amount_paise"] == 8_000_00
    assert all(a.params.get("target") != "savings" for a in gen_auto_sweep(ctx))  # no savings push while revolving


def test_unknown_card_debt_blocks_savings_sweeps(replayed):
    ctx = replayed(0)  # card visible via SMS, not linked: its revolving balance is unknown
    assert ctx.card is None and any(c.revolving_paise is None for c in ctx.metrics.cards)
    assert gen_auto_sweep(ctx) == []


def test_d26_freed_cash_is_half_spent_unless_swept(replayed):
    ctx = replayed(1)
    base = project(ctx.inputs)
    cut = project(ctx.inputs, Modifier(consumption_cut=1_000_00))
    gain = cut.months[-1].liquid - base.months[-1].liquid
    expected = 12 * 1_000_00 * (1 - ctx.inputs.unswept_share)
    assert expected <= gain <= expected * 1.02  # plus savings interest compounding on the difference


def test_sweep_size_is_the_largest_step_within_the_bounce_limit(replayed):
    from app.engines.simulate import _rerun

    ctx = replayed(2)
    x = _size_sweep(ctx, to_card=True)
    base = max_mandate_bounce(ctx.forecast)

    def rise(amount):
        return max_mandate_bounce(_rerun(ctx, _with_sweep(ctx, ctx.forecast.schedule, amount, True))) - base
    step = ctx.cfg["sweep_step_paise"]
    assert (x == 0 or rise(x) <= 0.02 + 1e-9) and rise(x + step) > 0.02
    assert [a.params["amount_paise"] for a in gen_auto_sweep(ctx)] == ([x] if x >= 1_000_00 else [])


def test_tenure_extension_is_only_an_offered_what_if(replayed):
    ctx = replayed(3)
    ranked, _, offers = recommend(ctx)
    assert not [r for r in ranked if r.type == "change_emi_tenure"]  # fix 2: never a recommendation
    assert max_mandate_bounce(ctx.forecast) >= 0.20 and offers
    for o in offers:
        assert o.rank is None and o.title.startswith("Explore: ₹") and "more interest" in o.title
        assert o.extra["lifetime_cost_paise"] > 0 and "lender_approval" in {a["key"] for a in o.assumptions}


def test_recommendations_are_deterministic_and_ranked(replayed):
    ctx = replayed(1)
    r1, p1, _ = recommend(ctx)
    r2, p2, _ = recommend(ctx)
    assert [(r.action_key, r.impact) for r in r1] == [(r.action_key, r.impact) for r in r2] and p1 == p2
    assert r1[0].type == "pay_down_card"  # D32: at +1 the top action is clearing the card from savings
    keys = [(-r.impact["score_delta_12m"], -r.impact["annual_impact_paise"], r.action_key) for r in r1]
    assert keys == sorted(keys) and [r.rank for r in r1] == list(range(1, len(r1) + 1))
    assert p1 and p1["actions"] == [r.action_key for r in r1[:3]]
