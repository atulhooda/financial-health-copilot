"""The validator's adversarial suite (COPILOT.md §7) and the assumption rule (Phase 5 item 8)."""
import copy
import datetime as dt

import pytest

from app.copilot.registry import Registry
from app.copilot.validator import validate
from app.core.config import load_yaml


@pytest.fixture
def reg():
    """The golden question's registry: ₹60,000 over 12 months for demo-a."""
    r = Registry()
    r.add("user_input", "inr", 6000000, "phone price you gave", key="u.price")
    r.add("user_input", "months", 12, "tenure you gave", key="u.tenure")
    r.add("fact", "inr", 10304000, "monthly income", key="income")
    r.add("fact", "pct", 17.9, "EMIs as a share of income now", key="eti")
    r.confidence = {"label": "Medium", "reason": "band held on 64% of past days, target 80%"}
    r.add("fact", "pct", 64, "backtest coverage", key="cov")
    r.add("fact", "pct", 80, "coverage target", key="target")
    r.add("prediction", "pct", 50, "largest EMI bounce risk", key="bounce")
    r.add("fact", "date", dt.date(2026, 12, 10), "Bajaj Finance EMI due date", key="due")
    r.add("prediction", "pct", 99, "dip probability", key="dip")
    g = r.new_group()
    r.add("recommendation", "inr", 541500, "the new EMI", g, key="emi")
    r.add("recommendation", "pct", 23.1, "EMIs as a share of income with the new EMI", g, key="eti_after")
    r.add("recommendation", "score", 58, "12-month score without", g, key="score_base")
    r.add("recommendation", "score", 50, "12-month score with", g, key="score_with")
    r.add("recommendation", "months", 18, "longer tenure", g, key="alt")
    r.add("recommendation", "inr", 374300, "EMI at 18 months", g, key="alt_emi")
    r.add("assumption", "pct", 15, "interest rate a year (assumed)", g, key="rate")
    return r


def ids(reg, *keys):
    return [reg.by_key[k].id for k in keys]


def golden(reg, language):
    texts = {
        "en": ["Your monthly income is ₹1,03,040 and EMIs already take 18% of it.",
               "Your Bajaj Finance EMI on 10 Dec has a 50% chance of bouncing (Medium confidence: the band held on "
               "64% of past days, target 80%).",
               "A ₹60,000 phone over 12 months means an EMI of ₹5,415; EMIs would take 23% of your income and your "
               "score a year from now would be 50 instead of 58. Assumes 15% interest a year.",
               "Over 18 months the EMI would be ₹3,743 (at 15% a year)."],
        "hinglish": ["Aapki monthly income ₹1,03,040 hai aur EMIs abhi income ka 18% hain.",
                     "10 Dec ko Bajaj Finance EMI bounce hone ka chance 50% hai (confidence: Medium; pichhle dinon "
                     "mein se 64% par band sahi raha, target 80%).",
                     "₹60,000 ka phone 12 mahine ki EMI pe: EMI ₹5,415 hogi, EMIs income ka 23% ho jayengi aur saal "
                     "bhar baad score 58 ki jagah 50 hoga. Ye saalana 15% byaaj maan kar hai.",
                     "18 mahine ke liye EMI ₹3,743 hogi (saalana 15% byaaj par)."],
        "hi": ["आपकी मासिक आय ₹1,03,040 है और EMI अभी आय का 18% हैं।",
               "10 दिसंबर को Bajaj Finance की EMI बाउंस होने की संभावना 50% है (भरोसा: मध्यम; पिछले दिनों में से 64% "
               "पर बैंड सही रहा, लक्ष्य 80%)।",
               "₹60,000 का फ़ोन 12 महीने की EMI पर: EMI ₹5,415 होगी, EMI आय का 23% हो जाएँगी और साल भर बाद स्कोर 58 की "
               "जगह 50 होगा। यह सालाना 15% ब्याज मानकर है।",
               "18 महीने के लिए EMI ₹3,743 होगी (सालाना 15% ब्याज पर)।"],
    }[language]
    refs = [ids(reg, "income", "eti"), ids(reg, "bounce", "due", "cov", "target"),
            ids(reg, "u.price", "u.tenure", "emi", "eti_after", "score_base", "score_with", "rate"),
            ids(reg, "alt", "alt_emi", "rate")]
    labels = ["FACT", "PREDICTION", "RECOMMENDATION", "RECOMMENDATION"]
    return {"language": language, "statements": [{"label": lb, "text": t, "refs": rf}
                                                 for lb, t, rf in zip(labels, texts, refs, strict=True)]}


def codes(candidate, reg, language="en"):
    return sorted({e.code for e in validate(candidate, reg, language).errors})


@pytest.mark.parametrize("language", ["en", "hi", "hinglish"])
def test_golden_answer_passes_in_all_three_languages(reg, language):
    v = validate(golden(reg, language), reg, language)
    assert v.ok, v.errors


def edit(reg, i, text=None, refs=None, label=None):
    c = golden(reg, "en")
    st = c["statements"][i]
    if text is not None:
        st["text"] = text
    if refs is not None:
        st["refs"] = refs
    if label is not None:
        st["label"] = label
    return c


def test_blocks_a_wrong_amount(reg):
    c = edit(reg, 2, text=golden(reg, "en")["statements"][2]["text"].replace("₹5,415", "₹5,500"))
    assert codes(c, reg) == ["NUM_UNMATCHED"]


def test_blocks_an_invented_percentage(reg):
    c = edit(reg, 0, text="Your monthly income is ₹1,03,040 and EMIs already take 18% of it, 12% above average.")
    assert codes(c, reg) == ["NUM_UNMATCHED"]


def test_blocks_a_fact_citing_a_dip_probability(reg):
    c = edit(reg, 0, text="There is a 99% chance you dip below your floor.", refs=ids(reg, "dip"))
    assert "LABEL_KIND" in codes(c, reg)


def test_blocks_a_projected_score_as_fact(reg):  # D10
    c = edit(reg, 0, text="Your score a year from now will be 50.", refs=ids(reg, "score_with"))
    assert "LABEL_KIND" in codes(c, reg)


def test_blocks_a_prediction_without_or_with_the_wrong_confidence(reg):
    c = edit(reg, 1, text="Your Bajaj Finance EMI on 10 Dec has a 50% chance of bouncing.")
    assert codes(c, reg) == ["PRED_NO_CONF"]
    c = edit(reg, 1, text="Your Bajaj Finance EMI on 10 Dec has a 50% chance of bouncing (High confidence).")
    assert codes(c, reg) == ["PRED_NO_CONF"]


def test_blocks_confidence_as_a_percentage(reg):
    for text in ("Your Bajaj Finance EMI on 10 Dec has a 50% chance of bouncing; we are 64% confident (Medium "
                 "confidence).", "Medium confidence of 64%: your Bajaj Finance EMI on 10 Dec has a 50% chance of "
                                 "bouncing."):
        assert "CONF_AS_PCT" in codes(edit(reg, 1, text=text), reg), text


def test_reason_line_form_is_allowed(reg):
    assert validate(golden(reg, "en"), reg, "en").ok  # "band held on 64% of past days, target 80%"


def test_blocks_a_recommendation_without_impact(reg):
    c = golden(reg, "en")
    c["statements"].append({"label": "RECOMMENDATION", "text": "Consider a cheaper phone.", "refs": []})
    assert codes(c, reg) == ["REC_NO_IMPACT"]


def test_blocks_a_ref_from_another_turn(reg):
    c = edit(reg, 0, refs=[*ids(reg, "income", "eti"), "F30"])  # F30 exists only in some other turn's registry
    assert codes(c, reg) == ["REF_UNKNOWN"]


def test_blocks_a_number_without_its_ref(reg):
    assert codes(edit(reg, 0, refs=ids(reg, "income")), reg) == ["REF_MISSING"]


def test_blocks_number_words(reg):
    c = edit(reg, 0, text="Your monthly income is about do lakh and EMIs already take 18% of it.")
    assert codes(c, reg) == ["NUM_WORDS"]


def test_blocks_rounding_below_two_significant_figures(reg):
    c = edit(reg, 0, text="Your monthly income is about ₹1 lakh and EMIs already take 18% of it.")
    assert codes(c, reg) == ["NUM_UNMATCHED"]
    c = edit(reg, 0, text="Your monthly income is about ₹1.03 lakh and EMIs already take 18% of it.")
    assert codes(c, reg) == []


def test_only_a_script_mismatch_blocks(reg):
    """Fix 7: English vs Hinglish is style (a soft note for the eval); Devanagari vs Latin is a hard failure."""
    c = golden(reg, "en")
    v = validate(c, reg, "hinglish")  # the user wrote Hinglish, the answer is English: allowed, noted
    assert v.ok and {n.code for n in v.notes} == {"LANG_STYLE"}
    h = golden(reg, "hi")
    h["statements"][0]["text"] = "Aapki monthly income ₹1,03,040 hai aur EMIs abhi income ka 18% hain."
    assert codes(h, reg, "hi") == ["LANG_MISMATCH"]  # Hindi must be in Devanagari
    assert codes(golden(reg, "hinglish"), reg, "hi") == ["LANG_MISMATCH"]  # Latin script for a Hindi question


def test_blocks_pii_in_the_output(reg):
    c = edit(reg, 0, text="Your monthly income is ₹1,03,040 and EMIs take 18% of it. Mail me at ramesh@example.com")
    assert "PII_LEAK" in codes(c, reg)


def test_schema_limits_are_checked_client_side(reg):
    c = golden(reg, "en")
    c["statements"] = c["statements"] * 3  # 12 statements
    assert codes(c, reg) == ["SCHEMA"]
    c = edit(reg, 0, text="x" * 401)
    assert codes(c, reg) == ["SCHEMA"]


# ---- Phase 5 item 8: a recommendation states the assumptions of its simulation ------------------------------
def test_blocks_a_recommendation_that_drops_its_assumption(reg):
    c = edit(reg, 3, text="Over 18 months the EMI would be ₹3,743.", refs=ids(reg, "alt", "alt_emi"))
    assert codes(c, reg) == ["ASSUMPTION_NOT_CITED"]


def test_stated_but_uncited_assumption_is_still_blocked(reg):
    c = edit(reg, 3, refs=ids(reg, "alt", "alt_emi"))  # says "15% a year" but doesn't cite it
    assert "REF_MISSING" in codes(c, reg) or "ASSUMPTION_NOT_CITED" in codes(c, reg)


def test_flag_assumptions_are_stated_in_words_per_language():
    phrases = load_yaml("copilot")["assumptions"]["pay_in_full_after"]
    r = Registry()
    g = r.new_group()
    save = r.add("recommendation", "inr", 2266900, "saved over 12 months", g)
    flag = r.add("assumption", "flag", None, "you pay the card in full once it's clear", g, phrases=phrases)
    base = {"label": "RECOMMENDATION", "refs": [save.id, flag.id]}
    ok = {"en": "Clear the card from savings: you save ₹22,669 over a year, assuming you pay the card in full "
                "after.",
          "hinglish": "Bachat se card clear kijiye: saal bhar mein ₹22,669 bachenge, agar aap har mahine poora bill "
                      "bharein.",
          "hi": "बचत से कार्ड चुकाएँ: साल भर में ₹22,669 बचेंगे, अगर आप हर महीने पूरा बिल भरें।"}
    for lang, text in ok.items():
        assert validate({"language": lang, "statements": [{**base, "text": text}]}, r, lang).ok, lang
    silent = {"language": "en", "statements": [{**base, "text": "Clear the card from savings: you save ₹22,669 "
                                                                "over a year."}]}
    assert codes(silent, r) == ["ASSUMPTION_NOT_CITED"]


def test_numbers_inside_names_and_tokens_are_not_claims(reg):
    c = golden(reg, "en")
    c["statements"][0]["text"] += " Rent goes to [CONTACT_01]; Zee5 is one of your apps."
    assert validate(c, reg, "en").ok


def test_adversarial_rounding_abuse_with_a_copy(reg):
    c = copy.deepcopy(golden(reg, "en"))
    c["statements"][2]["text"] = c["statements"][2]["text"].replace("₹5,415", "₹5k")
    assert codes(c, reg) == ["NUM_UNMATCHED"]


# ---- D10 (Phase 5 review): label by who proposed the action ------------------------------------------------
@pytest.fixture
def what_if():
    """The user's what-if: a new EMI. Its numbers are prediction-kind in a what-if group."""
    r = Registry()
    r.confidence = {"label": "Medium", "reason": ""}
    r.add("user_input", "inr", 6000000, "phone price you gave", key="price")
    r.add("user_input", "months", 12, "tenure you gave", key="tenure")
    g = r.new_group()
    r.what_if_groups.add(g)
    r.group_confidence[g] = "Medium"
    r.add("prediction", "inr", 541500, "the new EMI", g, key="emi")
    r.add("prediction", "pct", 66, "largest EMI bounce risk with it", g, key="bounce_after")
    r.add("assumption", "pct", 15, "interest rate a year (assumed)", g, key="rate")
    h = r.new_group()
    r.group_confidence[h] = "Medium"
    r.add("recommendation", "inr", 2266900, "saved over 12 months by clearing the card", h, key="saved")
    return r


def st(label, text, reg, *keys):
    return {"language": "en", "statements": [{"label": label, "text": text, "refs": ids(reg, *keys)}]}


def test_a_what_if_is_a_conditional_prediction(what_if):
    ok = st("PREDICTION", "If you take the ₹60,000 phone over 12 months, the EMI would be ₹5,415 and your biggest "
                          "EMI bounce risk 66% (Medium confidence). Assumes 15% interest a year.",
            what_if, "price", "tenure", "emi", "bounce_after", "rate")
    assert validate(ok, what_if, "en").ok, validate(ok, what_if, "en").errors
    as_rec = st("RECOMMENDATION", "Take the ₹60,000 phone: the EMI would be ₹5,415 at 15% a year.",
                what_if, "price", "emi", "rate")
    assert "LABEL_KIND" in codes(as_rec, what_if) and "REC_NO_IMPACT" in codes(as_rec, what_if)
    no_condition = st("PREDICTION", "The EMI would be ₹5,415 (Medium confidence). Assumes 15% interest a year.",
                      what_if, "emi", "rate")
    assert codes(no_condition, what_if) == ["CONDITION_MISSING"]
    no_assumption = st("PREDICTION", "If you take it, the EMI would be ₹5,415 (Medium confidence).", what_if, "emi")
    assert codes(no_assumption, what_if) == ["ASSUMPTION_NOT_CITED"]
    no_confidence = st("PREDICTION", "If you take it, the EMI would be ₹5,415. Assumes 15% interest a year.",
                       what_if, "emi", "rate")
    assert codes(no_confidence, what_if) == ["PRED_NO_CONF"]


def test_only_hisaabs_own_proposal_is_a_recommendation(what_if):
    ok = st("RECOMMENDATION", "Clear the card from savings: you save ₹22,669 over a year.", what_if, "saved")
    assert validate(ok, what_if, "en").ok
    as_prediction = st("PREDICTION", "If you clear the card, you save ₹22,669 over a year (Medium confidence).",
                       what_if, "saved")
    assert codes(as_prediction, what_if) == ["LABEL_KIND"]
