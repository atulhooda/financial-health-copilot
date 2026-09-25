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
            elif intent == "tradeoff" and not recs:
                assert statements == []  # nothing to weigh up: the orchestrator answers with the summary
            else:
                assert statements, (label, intent, target)
            for st in statements:  # D10: label by who proposed the action
                kinds = {ctx.reg.get(r).kind for r in st["refs"]}
                if st["label"] == "RECOMMENDATION":
                    assert "recommendation" in kinds and "prediction" not in kinds, st
                if kinds & {"prediction"} and any(ctx.reg.get(r).group in ctx.reg.what_if_groups for r in st["refs"]):
                    assert st["label"] == "PREDICTION", st
            if statements:
                v = validate({"language": language, "statements": statements}, reg, language)
                assert v.ok, (label, intent, target, v.errors, statements)
                checked += len(statements)
    assert checked > 100


def test_what_ifs_are_conditional_predictions(copilot_world, categoriser):
    """D10 as corrected in the Phase 5 review: the user's what-if is a PREDICTION phrased with its condition, with
    its confidence and assumptions; only Hisaab's own proposal is a RECOMMENDATION."""
    last = copilot_world.steps[-1]
    cp = Copilot(copilot_world.sf, categoriser, None, persist=False)
    for language, cond in (("en", "If you"), ("hi", "अगर"), ("hinglish", "Agar")):
        reg = Registry()
        ctx = ToolContext("demo-a", language, last.snapshot.payload, last.diff.payload, reg,
                          simulator=cp._simulator("demo-a", last.snapshot))
        for unit, value in user_numbers("₹60,000 12 months"):
            reg.add("user_input", unit, value, "a number you typed")
        st = render("afford_emi", ctx, run_plan(ctx, Intent("afford_emi", SLOTS["afford_emi"])))
        what_if = [s for s in st if any(reg.get(r).group in reg.what_if_groups for r in s["refs"])
                   and s["label"] == "PREDICTION"]
        assert len(what_if) >= 2 and all(s["text"].startswith(cond) for s in what_if), st
        assert any("₹5,415" in s["text"] for s in what_if) and any("₹3,743" in s["text"] for s in what_if)
        assert all("15%" in s["text"] for s in what_if)  # the assumed rate is stated
        assert [s["label"] for s in st].count("RECOMMENDATION") >= 1  # Hisaab's own proposal
        offers = [o for o in last.snapshot.payload["what_if_offers"]]
        if offers:
            reg2 = Registry()
            ctx2 = ToolContext("demo-a", language, last.snapshot.payload, None, reg2)
            t2 = render("tradeoff", ctx2, run_plan(ctx2, Intent("tradeoff", {"target_type": "change_emi_tenure"})))
            assert t2 and all(s["label"] == "PREDICTION" and s["text"].startswith(cond) for s in t2), t2


def test_trade_off_questions_explain_the_downside(copilot_world, categoriser):
    """The +1 story: clearing the card from savings states the rebuild downside and its assumptions."""
    one = next(r for r in copilot_world.steps if r.step.name == "1")
    cp = Copilot(copilot_world.sf, categoriser, None, persist=False)
    ctx = ToolContext("demo-a", "en", one.snapshot.payload, one.diff.payload, Registry(),
                      simulator=cp._simulator("demo-a", one.snapshot))
    st = render("tradeoff", ctx, run_plan(ctx, Intent("tradeoff", {"target_type": "pay_down_card"})))
    assert len(st) == 2 and "rebuilds to" in st[1]["text"] and "in full" in st[1]["text"]
