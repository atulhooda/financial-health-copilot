"""The copilot turn (COPILOT.md §1): LLM path, retry, template fallback, tool cap, tool-arg checks, logging."""
import json

import pytest
from sqlalchemy import func, select

from app.copilot.llm.base import LLMError, LLMReply
from app.copilot.llm.fake import FakeLLM, call, text
from app.copilot.llm.none import NoneClient
from app.copilot.orchestrator import Copilot
from app.db.models import AskLog, GlobalCounter, ValidatorBlock

GOLDEN = "Kya main ₹60,000 ka phone 12 months ki EMI pe le sakta hoon?"
SIM = call("simulate_action", {"action": {"type": "new_emi", "params": {"principal_rupees": 60000,
                                                                        "tenure_months": 12}}}, "sim")


def results(messages) -> dict:
    out = {}
    for m in messages:
        if m["role"] == "tool":
            for r in m["results"]:
                out[r["id"]] = json.loads(r["content"])
    return out


def golden_respond(emi_display=None):
    """A model that answers from the tool results it was given: the user's what-if as conditional PREDICTIONs."""
    def step(system, messages):
        sim = results(messages)["sim"]
        rate = next(a for a in sim["assumptions"] if a["kind"] == "assumption")
        emi = emi_display or sim["emi"]["display"]
        return call("respond", {"language": "hinglish", "statements": [
            {"label": "PREDICTION",
             "text": f"Agar aap {sim['tenure']['display']} ke liye {sim['principal']['display']} udhaar lete hain, "
                     f"toh EMI {emi} hogi, aur EMIs aapki income ka {sim['emis_share_of_income_after']['display']} ho "
                     f"jayengi (confidence: Medium). Ye saalana {rate['display']} byaaj maan kar hai.",
             "refs": [sim["principal"]["id"], sim["tenure"]["id"], sim["emi"]["id"],
                      sim["emis_share_of_income_after"]["id"], rate["id"]]},
            {"label": "PREDICTION",
             "text": f"Agar aap ise {sim['longer_tenure_option']['tenure']['display']} ke liye lete hain, toh EMI "
                     f"{sim['longer_tenure_option']['emi']['display']} hogi (confidence: Medium; saalana "
                     f"{rate['display']} byaaj par).",
             "refs": [sim["longer_tenure_option"]["tenure"]["id"], sim["longer_tenure_option"]["emi"]["id"],
                      rate["id"]]}]}, "resp")
    return step


@pytest.fixture
def copilot(copilot_world, categoriser):
    def make(llm, persist=True):
        return Copilot(copilot_world.sf, categoriser, llm, persist=persist)
    return make


def test_llm_answer_passes_first_time(copilot):
    ans = copilot(FakeLLM([SIM, golden_respond()])).ask("demo-a", GOLDEN)
    assert ans.path == "llm" and ans.fallback_reason is None and ans.tool_calls == 2
    assert "₹5,415" in ans.statements[0]["text"] and "₹3,743" in ans.statements[1]["text"]
    assert ans.final_errors == [] and all(r in ans.sources for s in ans.statements for r in s["refs"])


def test_a_blocked_draft_is_retried_with_the_errors(copilot, copilot_world):
    seen = {}

    def check_then_fix(system, messages):
        seen["result"] = messages[-1]["results"][0]
        return golden_respond()(system, messages)

    with copilot_world.sf() as s:
        before = (s.get(GlobalCounter, "validator_blocks_total") or GlobalCounter(value=0)).value
    ans = copilot(FakeLLM([SIM, golden_respond("₹5,500"), check_then_fix])).ask("demo-a", GOLDEN)
    assert ans.path == "llm_retry" and [a["ok"] for a in ans.attempts] == [False, True]
    assert seen["result"]["is_error"] and "NUM_UNMATCHED" in seen["result"]["content"]
    assert "₹5,500" in seen["result"]["content"] and "₹5,415" in seen["result"]["content"]  # says what to use
    with copilot_world.sf() as s:
        blocks = list(s.scalars(select(ValidatorBlock).where(ValidatorBlock.ask_id == ans.ask_id)))
        assert [b.attempt for b in blocks] == [1] and blocks[0].errors[0]["code"] == "NUM_UNMATCHED"
        assert s.get(GlobalCounter, "validator_blocks_total").value == before + 1
        log = s.scalars(select(AskLog).where(AskLog.ask_id == ans.ask_id)).one()
        assert log.path == "llm_retry" and log.user_id == "demo-a"


def test_two_blocked_drafts_fall_back_to_the_template(copilot):
    ans = copilot(FakeLLM([SIM, golden_respond("₹5,500"), golden_respond("₹5,000")])).ask("demo-a", GOLDEN)
    assert ans.path == "template" and ans.fallback_reason == "validator"
    assert any("₹5,415" in s["text"] for s in ans.statements) and ans.final_errors == []


def test_tool_call_cap(copilot):
    ans = copilot(FakeLLM([call("get_metrics", {}, f"m{i}") for i in range(7)])).ask("demo-a", GOLDEN)
    assert ans.path == "template" and ans.fallback_reason == "tool_cap" and ans.tool_calls == 7


def test_plain_text_counts_as_a_failed_draft(copilot):
    ans = copilot(FakeLLM([text("Sure, you can afford it!"), text("Yes.")])).ask("demo-a", GOLDEN)
    assert ans.path == "template" and ans.fallback_reason == "no_respond"
    assert [a["errors"][0]["code"] for a in ans.attempts] == ["NO_RESPOND", "NO_RESPOND"]


def test_refusal_and_transport_errors_fall_back(copilot):
    ans = copilot(FakeLLM([LLMReply("", [], "refusal", raw=[])])).ask("demo-a", GOLDEN)
    assert ans.path == "template" and ans.fallback_reason == "refusal"

    def boom(system, messages):
        raise LLMError("APIConnectionError: offline")
    ans = copilot(FakeLLM([boom])).ask("demo-a", GOLDEN)
    assert ans.path == "template" and ans.fallback_reason.startswith("llm_error")


def test_tool_numbers_must_come_from_the_registry(copilot):
    seen = {}

    def inspect_error(system, messages):
        seen["result"] = messages[-1]["results"][0]
        return text("giving up")

    bad = call("simulate_action", {"action": {"type": "new_emi", "params": {"principal_rupees": 65000}}}, "x")
    copilot(FakeLLM([bad, inspect_error, text("still no")]), persist=False).ask("demo-a", GOLDEN)
    assert seen["result"]["is_error"] and "not an amount the user gave" in seen["result"]["content"]


def test_payee_tokens_are_rehydrated_only_after_validation(copilot):
    fake = FakeLLM([call("get_recurring", {"kind": "rent"}, "rc"), lambda system, messages: call("respond", {
        "language": "en", "statements": [{"label": "FACT", "text": f"Your rent of "
                                          f"{results(messages)['rc']['items'][0]['amount']['display']} goes to "
                                          f"{results(messages)['rc']['items'][0]['name']}.",
                                          "refs": [results(messages)["rc"]["items"][0]["amount"]["id"]]}]}, "r")])
    ans = copilot(fake, persist=False).ask("demo-a", "Who do I pay rent to?")
    assert ans.path == "llm" and "Ramesh Kulkarni" in ans.statements[0]["text"]
    assert not any("ramesh" in p.lower() or "kulkarni" in p.lower() for p in fake.payload_strings())


def test_golden_question_without_an_llm(copilot, copilot_world):
    with copilot_world.sf() as s:
        before = s.scalar(select(func.count()).select_from(AskLog))
    ans = copilot(NoneClient()).ask("demo-a", GOLDEN)
    assert ans.path == "template" and ans.language == "hinglish" and ans.intent == "afford_emi"
    labels = [s["label"] for s in ans.statements]
    assert labels[0] == "FACT" and "PREDICTION" in labels and "RECOMMENDATION" in labels
    what_ifs = [s["text"] for s in ans.statements if s["label"] == "PREDICTION" and s["text"].startswith("Agar")]
    assert any("EMI ₹5,415" in t for t in what_ifs) and any("18 mahine" in t and "₹3,743" in t for t in what_ifs)
    assert all("15%" in t and "confidence: Medium" in t for t in what_ifs)  # condition, confidence, assumption
    assert ans.final_errors == [] and all(r in ans.sources for s in ans.statements for r in s["refs"])
    with copilot_world.sf() as s:
        assert s.scalar(select(func.count()).select_from(AskLog)) == before + 1


def test_the_question_deadline_serves_the_template(copilot_world, categoriser):
    """Fix 8: a slow model can't hold the user past the per-question deadline."""
    import time

    def slow(system, messages):
        time.sleep(0.4)
        return call("get_metrics", {}, "m1")
    fake = FakeLLM([slow, slow, slow])
    ans = Copilot(copilot_world.sf, categoriser, fake, persist=False, deadline_s=0.6).ask("demo-a", GOLDEN)
    assert ans.path == "template" and ans.fallback_reason == "deadline" and ans.statements
    assert fake.timeouts[0] <= 0.6 and len(fake.timeouts) <= 2  # each call gets at most what is left
