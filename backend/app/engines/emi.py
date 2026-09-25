"""Reducing-balance EMI (SPEC §6.5). EMI = P·r·(1+r)^n / ((1+r)^n − 1), r = annual/12."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, getcontext

getcontext().prec = 40


def emi_exact(principal_paise: int, annual_rate_bps: int, tenure_months: int) -> Decimal:
    if tenure_months <= 0:
        raise ValueError("tenure must be positive")
    p = Decimal(principal_paise)
    if annual_rate_bps == 0:
        return p / tenure_months
    r = Decimal(annual_rate_bps) / Decimal(10000) / Decimal(12)
    f = (1 + r) ** tenure_months
    return p * r * f / (f - 1)


def emi_paise(principal_paise: int, annual_rate_bps: int, tenure_months: int) -> int:
    """EMI rounded to the nearest whole rupee (lenders quote whole-rupee EMIs), in paise."""
    rupees = (emi_exact(principal_paise, annual_rate_bps, tenure_months) / 100).quantize(Decimal(1), ROUND_HALF_UP)
    return int(rupees) * 100


def total_interest_paise(principal_paise: int, annual_rate_bps: int, tenure_months: int) -> int:
    return emi_paise(principal_paise, annual_rate_bps, tenure_months) * tenure_months - principal_paise
