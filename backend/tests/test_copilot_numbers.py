"""Number extraction for the validator (COPILOT.md §7), in English, Hindi and Hinglish."""
import datetime as dt
from decimal import Decimal

import pytest

from app.copilot.numbers import date_display, extract, number_words
from app.copilot.registry import Registry


def one(text):
    found = extract(text)
    assert len(found) == 1, found
    return found[0]


@pytest.mark.parametrize("text,value,precision", [
    ("EMI ₹5,415", Decimal(5415), Decimal(1)),
    ("₹1,20,000 bache", Decimal(120000), Decimal(1)),
    ("Rs. 450 only", Decimal(450), Decimal(1)),
    ("INR 92000", Decimal(92000), Decimal(1)),
    ("₹1.2 lakh", Decimal(120000), Decimal(10000)),
    ("1.19 lakh rupaye", Decimal(119000), Decimal(1000)),
    ("₹14.8k", Decimal(14800), Decimal(100)),
    ("5,415 रुपये", Decimal(5415), Decimal(1)),
    ("₹२०,०००", Decimal(20000), Decimal(1)),
    ("२ लाख", Decimal(200000), Decimal(100000)),
    ("₹1.25 crore", Decimal(12500000), Decimal(100000)),
])
def test_money(text, value, precision):
    n = one(text)
    assert (n.unit, n.value, n.precision) == ("inr", value, precision)


@pytest.mark.parametrize("text,unit,value", [
    ("52.5% chance", "pct", Decimal("52.5")), ("50 pratishat", "pct", Decimal(50)), ("50 प्रतिशत", "pct", Decimal(50)),
    ("12 months", "months", Decimal(12)), ("a 12-month EMI", "months", Decimal(12)), ("18 mahine", "months", 18),
    ("6 महीने", "months", Decimal(6)), ("45 days", "days", Decimal(45)), ("10 din", "days", 10),
    ("2 saal", "years", 2), ("53/100", "score", Decimal(53)), ("rank 2", "bare", Decimal(2)),
])
def test_units(text, unit, value):
    n = one(text)
    assert n.unit == unit and n.value == Decimal(value)


@pytest.mark.parametrize("text,value", [
    ("due 10 Dec 2026", (10, 12, 2026)), ("December 10, 2026", (10, 12, 2026)), ("on 2026-12-10", (10, 12, 2026)),
    ("10/12/2026", (10, 12, 2026)), ("10 दिसंबर 2026", (10, 12, 2026)), ("from Dec 2026", (None, 12, 2026)),
    ("10 Dec", (10, 12, None)),
])
def test_dates(text, value):
    n = one(text)
    assert n.unit == "date" and n.value == value


def test_ordinals_names_and_money_that_looks_like_a_date():
    assert one("EMI on the 5th").unit == "dom" and one("5 तारीख को").unit == "dom"
    assert [n.unit for n in extract("Zee5 and 1mg, open 24x7")] == []  # digits inside names are not quantities
    assert one("₹50 may be saved").unit == "inr"  # money, not "50 May"


def test_number_words_are_flagged_once():
    assert number_words("about do lakh rupees") == ["do lakh"]
    assert number_words("दो महीने में") == ["दो महीने"]
    assert number_words("two thousand a month") == ["two thousand"]
    assert number_words("lakh rupaye bache") == ["lakh"]
    assert number_words("₹2 lakh and 12 months") == []


def test_matching_honours_stated_precision_and_two_significant_figures():
    reg = Registry()
    e = reg.add("fact", "inr", 11854000, "income")  # ₹1,18,540
    assert reg.matches(one("₹1,18,540")) == [e]
    assert reg.matches(one("₹1.19 lakh")) == [e] and reg.matches(one("1.2 lakh")) == [e]
    assert reg.matches(one("₹1 lakh")) == []  # one significant figure: exact matches only
    assert reg.matches(one("₹1,20,000")) == []  # written to the rupee, so it must be exact
    d = reg.add("prediction", "date", dt.date(2026, 12, 10), "due")
    assert reg.matches(one("10 Dec")) == [d] and reg.matches(one("the 10th")) == [d]
    assert reg.matches(one("11 Dec 2026")) == []
    assert date_display(dt.date(2026, 12, 10), "hi") == "10 दिसंबर 2026"
