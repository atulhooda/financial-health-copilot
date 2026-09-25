import pytest

from app.core.money import format_inr, group_indian, parse_inr, rupees


@pytest.mark.parametrize("n,out", [(0, "0"), (999, "999"), (1000, "1,000"), (120000, "1,20,000"),
                                   (12000000, "1,20,00,000"), (-45000, "-45,000")])
def test_indian_grouping(n, out):
    assert group_indian(n) == out


def test_format_full_and_compact():
    assert format_inr(rupees(120000)) == "₹1,20,000"
    assert format_inr(541550) == "₹5,416"  # half-up to whole rupees
    assert format_inr(4950) == "₹49.50"  # paise kept under ₹100
    assert format_inr(rupees(118540), "compact") == "₹1.19 lakh"
    assert format_inr(rupees(14800), "compact") == "₹14.8k"
    assert format_inr(rupees(12500000), "compact") == "₹1.25 crore"


def test_parse_inr():
    assert parse_inr("₹1,20,000.50") == 12000050
    assert parse_inr("Rs. 450") == 45000
    assert parse_inr("92000.00") == 9200000


def test_money_must_be_int():
    with pytest.raises(TypeError):
        format_inr(100.0)  # type: ignore[arg-type]
