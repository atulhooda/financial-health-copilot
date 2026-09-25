from app.engines.emi import emi_exact, emi_paise


def test_emi_60000_at_15pct():
    assert abs(emi_paise(60_000_00, 1500, 12) - 5_415_00) <= 100
    assert abs(emi_paise(60_000_00, 1500, 18) - 3_743_00) <= 100
    assert round(float(emi_exact(60_000_00, 1500, 12)) / 100, 2) == 5415.50


def test_zero_rate():
    assert emi_paise(12_000_00, 0, 12) == 1_000_00
