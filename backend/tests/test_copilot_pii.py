"""No PII in any LLM payload (COPILOT.md §3, D11): a fake adapter records everything an adapter would send."""
import re

import pytest
from sqlalchemy import select

from app.copilot.llm.fake import FakeLLM, call, text
from app.copilot.orchestrator import Copilot
from app.core.pii import find_regex_pii
from app.db.models import Account, Counterparty, User

SEEDED = {"phone": "9876543210", "email": "aarav.deshmukh@gmail.com", "upi": "aarav.d@okicici",
          "account": "50100123456789"}
QUESTIONS = [
    f"My phone is {SEEDED['phone']}, email {SEEDED['email']}, UPI {SEEDED['upi']}, account {SEEDED['account']}. "
    "Where is my money going?",
    "Should I stop paying Ramesh Kulkarni's rent on time? Rohit Joshi says I can delay. Will I run short?",
    "Kya main ₹60,000 ka phone 12 months ki EMI pe le sakta hoon? Meera Deshmukh bolti hai mat lo.",
]
EVERY_TOOL = [call("get_metrics", {}, "a"), call("get_spending", {"period": "last_30d"}, "b"),
              call("get_recurring", {}, "c"), call("forecast", {}, "d"),
              call("list_recommendations", {"limit": 5}, "e"), call("explain_change", {}, "f")]


def known_names(sf) -> list[str]:
    with sf() as s:
        names = [u.display_name for u in s.scalars(select(User).where(User.user_id == "demo-a")) if u.display_name]
        names += [c.name for c in s.scalars(select(Counterparty).where(Counterparty.user_id == "demo-a"))]
    return names


@pytest.mark.parametrize("question", QUESTIONS)
def test_no_pii_in_any_payload(copilot_world, categoriser, question):
    sim = call("simulate_action", {"action": {"type": "new_emi", "params": {"principal_rupees": 60000}}}, "g") \
        if "60,000" in question else call("simulate_action", {"action": {"type": "pay_down_card"}}, "g")
    fake = FakeLLM([*EVERY_TOOL[:5], sim, text("draft one"), text("draft two")])  # 6 tool calls, then no answer
    Copilot(copilot_world.sf, categoriser, fake, persist=False).ask("demo-a", question)
    payloads = fake.payload_strings()
    assert payloads, "the LLM was never called"
    with copilot_world.sf() as s:
        masked_numbers = [a.masked_number for a in s.scalars(select(Account).where(Account.user_id == "demo-a"))]
        vpas = [k.split(":", 1)[1] for c in s.scalars(select(Counterparty).where(Counterparty.user_id == "demo-a"))
                for k in c.match_keys if k.startswith("vpa:")]
    names = known_names(copilot_world.sf)
    assert names, "demo-a should have known names to protect"
    for p in payloads:
        assert not find_regex_pii(p), find_regex_pii(p)
        low = p.lower()
        for name in names:
            for part in name.split():
                if len(part) >= 3:
                    assert not re.search(rf"(?<![\w]){re.escape(part.lower())}(?![\w])", low), part
        for ident in [*SEEDED.values(), *masked_numbers, *vpas]:
            assert ident.lower() not in low, ident


def test_every_tool_output_is_masked_and_carries_no_raw_numbers(copilot_world, categoriser):
    fake = FakeLLM([*EVERY_TOOL, text("x"), text("y")])
    Copilot(copilot_world.sf, categoriser, fake, persist=False).ask("demo-a", "How am I doing overall?")
    last = fake.payloads[-1]
    results = [r["content"] for m in last["messages"] if m["role"] == "tool" for r in m["results"]]
    assert len(results) >= 5
    for content in results:  # tools send display strings and ids, never raw paise integers
        assert not re.search(r"\d{6,}", re.sub(r"₹[\d,]+", "", content)), content[:300]
