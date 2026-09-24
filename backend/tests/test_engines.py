"""Financial engine, score and attribution on the personas, through the real DB and as-of views."""
import ast
import dataclasses
import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from app.core.clock import FixedClock
from app.db.repo import UserRepo
from app.db.session import make_sessionmaker
from app.demo.personas import T0, WORLD_END
from app.demo.scenario import persona_a_steps, seed_payloads
from app.engines.attribution import attribute_change
from app.engines.financial import compute_metrics
from app.engines.score import compute_score
from app.engines.view import build_view
from app.ingest.registry import adapter_for
from app.ingest.service import ingest_batch, next_ingest_seq, set_merchant_rule
from tests.conftest import migrate

STEPS = persona_a_steps()


def _ingest(session, user, payloads, clock, categoriser):
    for source, payload in payloads:
        ingest_batch(session, user, adapter_for(source, clock).parse(payload, user), categoriser, clock)
    session.commit()


def _replay(session, categoriser, upto: int):
    for step in STEPS[: upto + 1]:
        _ingest(session, "demo-a", step.payloads, FixedClock(step.as_of), categoriser)


def _eval(session, categoriser, as_of, user="demo-a", **kw):
    v = build_view(session, user, as_of, categoriser, **kw)
    m = compute_metrics(v)
    return v, m, compute_score(m)


def test_engines_never_import_the_generator():
    offenders = []
    for path in (Path(__file__).resolve().parents[1] / "app" / "engines").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            mods = [node.module] if isinstance(node, ast.ImportFrom) else (
                [a.name for a in node.names] if isinstance(node, ast.Import) else [])
            offenders += [f"{path.name}: {m}" for m in mods if m and m.startswith("app.demo")]
    assert not offenders


# ---- D22 point-in-time -------------------------------------------------------------------------
def _snapshot(v, m, s) -> tuple:
    from app.engines.backtest import forecast_with_confidence

    fc, conf, bt = forecast_with_confidence(v, m.recurring, m.coverage)
    return (v.txns.to_dicts(), {k: dataclasses.asdict(a) for k, a in v.accounts.items()},
            dataclasses.asdict(m), dataclasses.asdict(s), dataclasses.asdict(fc), dataclasses.asdict(conf),
            dataclasses.asdict(bt))


@pytest.mark.parametrize("late_clock", [WORLD_END, T0], ids=["received-later", "backfilled-early"])
def test_t0_output_identical_whether_or_not_oct_nov_exist(tmp_path, categoriser, late_clock):
    def db(name):
        eng = create_engine(f"sqlite:///{tmp_path / name}")
        migrate(eng)
        return make_sessionmaker(eng)()

    a, b = db("a.db"), db("b.db")
    _ingest(a, "demo-a", STEPS[0].payloads, FixedClock(T0), categoriser)
    _ingest(b, "demo-a", STEPS[0].payloads, FixedClock(T0), categoriser)
    # +2 carries Oct/Nov data for every account, card included; ingest it with a later or an early clock
    _ingest(b, "demo-a", STEPS[2].payloads, FixedClock(late_clock), categoriser)
    assert _snapshot(*_eval(a, categoriser, T0)) == _snapshot(*_eval(b, categoriser, T0))


# ---- D19 visibility and the persona A story -------------------------------------------------------
def test_t0_card_is_visible_via_sms_and_not_double_counted(session, categoriser):
    _replay(session, categoriser, 0)
    v, m, s = _eval(session, categoriser, T0)
    card = next(a for a in v.accounts.values() if a.kind == "credit_card")
    assert card.status == "sms_only" and card.visible_from <= dt.date(2026, 4, 1)
    cats = set(v.txns["category"].to_list())
    assert "card_bill_unlinked" not in cats
    bills = v.txns.filter(v.txns["merchant_key"].str.contains("credit_card"))
    assert bills.height >= 5 and set(bills["category"]) == {"transfer_self"}
    assert v.txns.filter(v.txns["account_id"] == card.account_id)["category"].is_in(["shopping", "fuel", "dining"]).any()
    credit = next(p for p in s.pillars if p.key == "credit")
    assert credit.status == "insufficient_data" and credit.reason == "card not linked"
    assert m.revolving_paise is None


def test_persona_a_story_checks(session, categoriser):
    """SPEC §9 story checks. If these fail, change persona PARAMETERS (log in DEMO.md), never the engine."""
    _replay(session, categoriser, 0)
    _, m0, s0 = _eval(session, categoriser, T0)
    assert s0.band == "Fair"
    ott = [o for o in m0.overlaps if o["group"] == "ott_video"]
    assert ott and ott[0]["count"] == 3
    assert m0.income_pattern == "salaried" and m0.coverage["accounts_linked"] == 3 and m0.coverage["accounts_known"] == 4
    _replay_from(session, categoriser, 1)
    _, m1, s1 = _eval(session, categoriser, T0)
    assert 36_000_00 <= m1.revolving_paise <= 44_000_00
    assert next(p for p in s1.pillars if p.key == "credit").score <= 40
    assert m1.spend_monthly_paise > m0.spend_monthly_paise and m1.savings_rate < m0.savings_rate  # interest now visible
    assert s1.total < s0.total


def _replay_from(session, categoriser, i):
    _ingest(session, "demo-a", STEPS[i].payloads, FixedClock(STEPS[i].as_of), categoriser)


def test_salary_hike_and_new_loan(session, categoriser):
    _replay(session, categoriser, 3)
    _, m2, _ = _eval(session, categoriser, WORLD_END, max_ingest_seq=_seq_after_step(session, 2))
    sal = next(r for r in m2.recurring if r.kind == "salary")
    assert sal.amount_paise == 103_040_00 and m2.income_monthly_paise >= 103_040_00
    _, m3, _ = _eval(session, categoriser, WORLD_END)
    new = next(r for r in m3.recurring if r.merchant_key == "tata_capital")
    assert new.source == "contract" and new.next_due == dt.date(2026, 12, 5)
    assert m3.emi_monthly_paise > m2.emi_monthly_paise
    assert m3.income_monthly_paise == m2.income_monthly_paise  # the disbursal is not income
    assert m3.liquid_paise > m2.liquid_paise + 1_90_000_00  # the cash landed...
    # ...but isn't a buffer (D29)
    assert m3.borrowed_liquid_paise >= 1_90_000_00 and abs(m3.buffer_months - m2.buffer_months) < 0.05


def _seq_after_step(session, step_index: int) -> int:
    # steps 0..3 each ingest len(payloads) batches; watermark = cumulative batch count
    return sum(len(s.payloads) for s in STEPS[: step_index + 1])


# ---- D24 attribution -----------------------------------------------------------------------------
def test_linking_the_card_is_new_data_not_behaviour(session, categoriser):
    _replay(session, categoriser, 1)
    t0_seq = len(STEPS[0].payloads)
    att = attribute_change(session, "demo-a", categoriser, T0, t0_seq, T0, next_ingest_seq(UserRepo(session, "demo-a")) - 1)
    assert len(att.revealed_accounts) == 1
    assert att.score["behaviour"] == 0 and att.score["new_data"] < 0
    assert att.metrics["revolving_paise"]["restricted"] is None and att.metrics["revolving_paise"]["full"] > 0
    assert att.new_data_revealed


def test_new_loan_is_behaviour_not_a_reveal(session, categoriser):
    _replay(session, categoriser, 3)
    seq2, seq3 = _seq_after_step(session, 2), _seq_after_step(session, 3)
    att = attribute_change(session, "demo-a", categoriser, WORLD_END, seq2, WORLD_END, seq3)
    assert att.revealed_accounts == []
    assert att.score["new_data"] == 0 and att.metrics["emi_to_income"]["restricted"] > att.metrics["emi_to_income"]["prev"]


# ---- D21 user corrections --------------------------------------------------------------------------
def test_user_correction_becomes_a_merchant_rule(session, categoriser, clock):
    _replay(session, categoriser, 0)
    v, _, _ = _eval(session, categoriser, T0)
    target = v.txns.filter(v.txns["merchant_key"] == "m:vaishali_restaurant")
    assert target.height and set(target["category_source"]) == {"ml"}
    set_merchant_rule(session, "demo-a", "m:vaishali_restaurant", "food_delivery", categoriser, clock)
    session.commit()
    v2, _, _ = _eval(session, categoriser, T0)
    after = v2.txns.filter(v2.txns["merchant_key"] == "m:vaishali_restaurant")
    assert set(after["category"]) == {"food_delivery"} and set(after["category_source"]) == {"user"}
    with pytest.raises(ValueError):
        set_merchant_rule(session, "demo-a", "m:vaishali_restaurant", "not_a_category", categoriser, clock)


# ---- personas B and C ------------------------------------------------------------------------------
def test_persona_b_irregular_income_is_not_forced_into_salary(session, categoriser, clock):
    _ingest(session, "demo-b", seed_payloads("demo-b"), clock, categoriser)
    _, m, s = _eval(session, categoriser, T0, user="demo-b")
    assert m.income_pattern == "irregular" and m.cycle_basis == "calendar"
    assert not [r for r in m.recurring if r.direction == "credit" and r.kind in ("salary", "income")]
    assert m.income_monthly_paise > 0 and next(p for p in s.pillars if p.key == "credit").reason == "no credit card"


def test_persona_c_annual_subscription_and_two_salaries(session, categoriser, clock):
    _ingest(session, "demo-c", seed_payloads("demo-c"), clock, categoriser)
    _, m, _ = _eval(session, categoriser, T0, user="demo-c")
    prime = next(r for r in m.recurring if r.merchant_key == "amazon_prime")
    assert prime.cadence == "annual"
    assert {o["group"] for o in m.overlaps} == {"ott_video"}
    assert len([r for r in m.recurring if r.kind == "salary"]) == 2
    assert len([r for r in m.recurring if r.kind == "emi"]) == 2 and m.revolving_paise == 0
