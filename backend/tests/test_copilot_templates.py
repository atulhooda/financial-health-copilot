"""Every template passes the validator: A at every replay step, B and C, all intents, all three languages."""
import pytest

from app.copilot.intents import Intent, user_numbers
from app.copilot.orchestrator import Copilot, run_plan
from app.copilot.registry import Registry
from app.copilot.render import render
from app.copilot.tools import ToolContext
from app.copilot.validator import validate

QUESTION = {"afford_emi": "₹60,000 12 months", "tradeoff": "", "where_money": "", "run_short": "",
            "score_change": "", "summary": ""}
SLOTS = {"afford_emi": {"principal_rupees": 60000, "tenure_months": 12}}


def snapshots(world):
    for r in world.steps:
        yield f"demo-a@{r.step.name}", "demo-a", r.snapshot, r.diff.payload if r.diff else None
    for uid, snap in world.others.items():
        yield uid, uid, snap, None


@pytest.mark.parametrize("language", ["en", "hi", "hinglish"])
def test_every_template_statement_passes(copilot_world, categoriser, language):
    cp = Copilot(copilot_world.sf, categoriser, None, persist=False)
    checked = 0
    for label, uid, snap, diff in snapshots(copilot_world):
        recs = [r["type"] for r in snap.payload["recommendations"]] + \
               [o["type"] for o in snap.payload.get("what_if_offers") or []]
        cases = [(i, None) for i in QUESTION] + [("tradeoff", t) for t in dict.fromkeys(recs)]
        for intent, target in cases:
            reg = Registry()
            ctx = ToolContext(uid, language, snap.payload, diff, reg, simulator=cp._simulator(uid, snap))
            for unit, value in user_numbers(QUESTION.get(intent, "")):
                reg.add("user_input", unit, value, "a number you typed")
            slots = dict(SLOTS.get(intent, {}), target_type=target)
            statements = render(intent, ctx, run_plan(ctx, Intent(intent, slots)))
            if intent == "score_change" and diff is None:
                assert all(s["label"] == "RECOMMENDATION" for s in statements)  # nothing to compare with
            else:
                assert statements, (label, intent, target)
            if statements:
                v = validate({"language": language, "statements": statements}, reg, language)
                assert v.ok, (label, intent, target, v.errors, statements)
                checked += len(statements)
    assert checked > 100


def test_trade_off_questions_explain_the_downside(copilot_world, categoriser):
    """The +1 story: clearing the card from savings states the rebuild downside and its assumptions."""
    one = next(r for r in copilot_world.steps if r.step.name == "1")
    cp = Copilot(copilot_world.sf, categoriser, None, persist=False)
    ctx = ToolContext("demo-a", "en", one.snapshot.payload, one.diff.payload, Registry(),
                      simulator=cp._simulator("demo-a", one.snapshot))
    st = render("tradeoff", ctx, run_plan(ctx, Intent("tradeoff", {"target_type": "pay_down_card"})))
    assert len(st) == 2 and "rebuilds to" in st[1]["text"] and "in full" in st[1]["text"]
