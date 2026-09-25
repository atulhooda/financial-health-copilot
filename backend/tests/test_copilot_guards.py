"""Guards (COPILOT.md §8, SPEC D9): tier 1 and scope never reach the LLM; tier 2 prepends a check-in."""
import pytest

from app.copilot.guards import check, message
from app.copilot.llm.fake import ExplodingLLM
from app.copilot.orchestrator import Copilot

TIER1 = ["I want to end my life", "thinking about suicide lately", "I don't want to live anymore",
         "Jeena nahi chahta ab", "khudkushi kar lunga", "marna chahta hoon", "मैं आत्महत्या करना चाहता हूँ",
         "अब जीना नहीं चाहता"]
TIER2 = ["This loan is killing me", "EMI ne jaan le li yaar", "is mahine toh mar gaya", "dimaag kharab ho gaya hai",
         "EMI से परेशान हूँ", "इस लोन ने जान ले ली"]
SCOPE = ["Which stock should I buy?", "Is bitcoin a good idea?", "any share tips?", "best mutual fund for me?",
         "I want guaranteed returns", "paisa double kaise kare", "kaunsa share lu?", "कौन सा शेयर खरीदूँ?",
         "F&O trading shuru karun?"]
NEITHER = ["Kill the Netflix subscription?", "I'm dying to know my score", "Should I pause my SIP to clear the card?",
           "Where is my money going?", "Kya main ₹60,000 ka phone le sakta hoon?", "मेरा पैसा कहाँ जा रहा है?",
           "What's the catch with the ₹8,000 transfer?"]


@pytest.mark.parametrize("text", TIER1)
def test_tier1(text):
    g = check(text)
    assert g.tier1 and g.name == "distress"


@pytest.mark.parametrize("text", TIER2)
def test_tier2(text):
    g = check(text)
    assert g.tier2 and not g.tier1 and g.name == "distress_checkin"


@pytest.mark.parametrize("text", SCOPE)
def test_scope(text):
    assert check(text).name == "scope"


@pytest.mark.parametrize("text", NEITHER)
def test_negatives(text):
    assert check(text).name is None


def test_tier1_wins_over_tier2_and_scope():
    assert check("EMI ne jaan le li, ab jeena nahi chahta").name == "distress"
    assert check("I want to end my life, which stock can save me?").name == "distress"


@pytest.mark.parametrize("language", ["en", "hi", "hinglish"])
def test_messages_carry_the_verified_helpline(language):
    for kind in ("distress", "checkin"):
        m = message(kind, language)
        assert "Tele-MANAS" in m and "14416" in m and "1-800-891-4416" in m
    assert "112" in message("distress", language)  # tier 1: "if you are in immediate danger, call 112"
    assert "112" not in message("checkin", language)  # tier 2: Tele-MANAS only; 112 there would be alarming
    assert "SEBI" in message("scope", language)


@pytest.mark.parametrize("text", [TIER1[0], TIER1[3], TIER1[6], SCOPE[0], SCOPE[5], SCOPE[7]])
def test_guards_never_call_the_llm(copilot_world, categoriser, text):
    ans = Copilot(copilot_world.sf, categoriser, ExplodingLLM(), persist=False).ask("demo-a", text)
    assert ans.path == "guard" and ans.statements == [] and ans.message


def test_tier2_prepends_a_checkin_and_still_answers(copilot_world, categoriser):
    from app.copilot.llm.none import NoneClient

    ans = Copilot(copilot_world.sf, categoriser, NoneClient(), persist=False).ask(
        "demo-a", "EMI ne jaan le li yaar, is mahine kam padega kya?")
    assert ans.guard == "distress_checkin" and "Tele-MANAS" in ans.checkin
    assert ans.path == "template" and ans.statements and ans.language == "hinglish"
