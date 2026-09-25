"""Replay T0 -> +3 through the bus and worker: the reason codes docs/DEMO.md says must be emitted (SPEC §7)."""
import copy

import pytest
from sqlalchemy import func, select

from app.db.models import Snapshot
from app.db.session import make_sessionmaker
from app.demo.replay import replay
from app.events.bus import InProcessBus
from app.events.diff import diff_payloads
from app.events.snapshots import take_snapshot

MUST_EMIT = {  # docs/DEMO.md §3, "Must emit (asserted)"
    "1": {"ACCOUNT_LINKED", "NEW_DATA_REVEALED", "HIGH_COST_DEBT_FOUND", "REC_ADDED"},
    "2": {"INCOME_INCREASED"},
    "3": {"NEW_EMI_ADDED", "ACCOUNT_LINKED"},
}


@pytest.fixture(scope="module")
def timeline(tmp_path_factory, categoriser):
    from sqlalchemy import create_engine

    from tests.conftest import migrate

    eng = create_engine(f"sqlite:///{tmp_path_factory.mktemp('replay') / 'r.db'}")
    migrate(eng)
    sf = make_sessionmaker(eng)
    return sf, replay(sf, InProcessBus(), categoriser)


def test_one_snapshot_per_step_and_t0_has_no_diff(timeline):
    sf, results = timeline
    assert [r.step.name for r in results] == ["t0", "1", "2", "3"]
    assert [r.snapshot.seq for r in results] == [1, 2, 3, 4]
    assert results[0].diff is None and results[0].snapshot.trigger == "demo:t0"


@pytest.mark.parametrize("step", ["1", "2", "3"])
def test_must_emit_reason_codes(timeline, step):
    r = next(x for x in timeline[1] if x.step.name == step)
    assert MUST_EMIT[step] <= set(r.diff.reason_codes), r.diff.reason_codes


def test_card_link_is_new_data_and_adds_pay_down(timeline):
    r = timeline[1][1]
    change = next(c for c in r.diff.payload["rec_changes"] if c["action_key"].startswith("pay_down_card:"))
    assert change["change"] == "added" and "HIGH_COST_DEBT_FOUND" in change["caused_by"]
    assert r.diff.payload["score"]["behaviour"] == 0 and r.diff.payload["score"]["new_data"] < 0


def test_t0_leads_with_the_data_action_which_linking_removes(timeline):
    t0, one = timeline[1][0], timeline[1][1]
    assert t0.snapshot.payload["recommendations"][0]["type"] == "link_account"  # fix 5 (D30e)
    gone = next(c for c in one.diff.payload["rec_changes"] if c["type"] == "link_account")
    assert gone["change"] == "removed" and gone["caused_by"] == ["ACCOUNT_LINKED"]


def test_new_loan_is_behaviour_raises_bounce_risk_and_no_sweep_is_pushed(timeline):
    r = timeline[1][3]
    assert "NEW_DATA_REVEALED" not in r.diff.reason_codes and "BOUNCE_RISK_UP" in r.diff.reason_codes
    for step in timeline[1][1:]:  # debt before savings: never a savings sweep while the card revolves
        assert not [x for x in step.snapshot.payload["recommendations"]
                    if x["type"] == "auto_sweep" and x["params"]["target"] == "savings"]


def test_dip_codes_withheld_while_saturated(timeline):
    for r in timeline[1][1:]:
        assert not {"DIP_RISK_UP", "DIP_RISK_DOWN"} & set(r.diff.reason_codes)
        assert r.diff.payload["dip"]["saturated"]


def test_snapshot_is_idempotent(timeline, categoriser):
    sf, results = timeline
    last = results[-1].snapshot
    with sf() as s:
        snap, _, created = take_snapshot(s, "demo-a", last.as_of, "again", categoriser)
        s.commit()
        assert not created and snap.snapshot_id == last.snapshot_id
        assert s.scalar(select(func.count()).select_from(Snapshot).where(Snapshot.user_id == "demo-a")) == 4


def test_dip_codes_are_emitted_when_not_saturated(timeline):
    prev = copy.deepcopy(timeline[1][2].snapshot.payload)
    new = copy.deepcopy(prev)
    for p, dip in ((prev, 0.30), (new, 0.55)):
        p["forecast"].update(dip_probability=dip, dip_saturated=False)
    att = {"score": {"behaviour": 0, "new_data": 0}, "pillars": {}, "revealed_accounts": []}
    assert "DIP_RISK_UP" in diff_payloads(prev, new, att, False)["reason_codes"]
