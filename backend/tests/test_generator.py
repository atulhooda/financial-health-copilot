import datetime as dt
import hashlib
import json

from app.demo.personas import T0, build
from app.demo.scenario import persona_a_steps, seed_payloads


def _h(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def test_generator_is_deterministic():
    assert _h([s.payloads for s in persona_a_steps()]) == _h([s.payloads for s in persona_a_steps()])
    assert _h(seed_payloads("demo-b")) == _h(seed_payloads("demo-b"))


def test_persona_a_story_inputs():
    w = build("demo-a")
    # at T0 (Sep 3) the last statement whose due date has passed is Jul 18 (due Aug 7)
    jul_stmt = w.balance_at("card", dt.date(2026, 7, 18))
    paid = sum(t.amount for t in w.txns if t.account == "card" and t.direction == "credit"
               and dt.date(2026, 7, 19) <= t.date <= dt.date(2026, 8, 7))
    assert 36_000_00 <= jul_stmt - paid <= 44_000_00 and T0 == dt.date(2026, 9, 3)  # revolving ~₹40k at +1
    osts = {t.narration.split("/")[3] for t in w.txns if "AutoPay" in t.narration}
    assert osts == {"NETFLIX", "AMAZON PRIME", "JIOHOTSTAR"}


def test_replay_steps_hold_back_the_new_loan_until_step_3():
    steps = persona_a_steps()
    assert [s.name for s in steps] == ["t0", "1", "2", "3"]
    assert steps[2].as_of == steps[3].as_of  # no second clock jump (D5)
    text = json.dumps([s.payloads for s in steps[:3]])
    assert "LOAN DISB" not in text and "XXXXXX5520" not in text
    assert "LOAN DISB" in json.dumps(steps[3].payloads)


def test_raw_sms_text_never_in_payloads():
    for s in persona_a_steps():
        for source, payload in s.payloads:
            if source == "sms":
                for t in payload["transactions"]:
                    assert set(t) <= {"client_ref", "pattern_id", "sender_id", "account_last4", "amount_paise",
                                      "direction", "merchant_raw", "txn_date", "reference"}
