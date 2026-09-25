"""Money is integer paise. All rupee formatting goes through format_inr."""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

PAISE_PER_RUPEE = 100


def rupees(value: int | str | Decimal) -> int:
    """Rupees (int, Decimal or numeric string like '1,20,000.50') -> paise, half-up."""
    if isinstance(value, int):
        return value * PAISE_PER_RUPEE
    d = value if isinstance(value, Decimal) else parse_decimal(value)
    return int((d * PAISE_PER_RUPEE).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def parse_decimal(text: str) -> Decimal:
    cleaned = re.sub(r"[₹,\s]|Rs\.?|INR", "", str(text).strip(), flags=re.IGNORECASE)
    if cleaned in ("", "-"):
        raise ValueError(f"not an amount: {text!r}")
    try:
        return Decimal(cleaned)
    except InvalidOperation as e:
        raise ValueError(f"not an amount: {text!r}") from e


def parse_inr(text: str) -> int:
    """'₹1,20,000.50' / 'Rs. 450' / '92000.00' -> paise."""
    return rupees(parse_decimal(text))


def group_indian(n: int) -> str:
    """1200000 -> '12,00,000'."""
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-" if n < 0 else "") + s


def _round_rupees(paise: int) -> int:
    return int((Decimal(paise) / PAISE_PER_RUPEE).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _trim(d: Decimal) -> str:
    s = format(d, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def format_inr(paise: int, style: str = "full", symbol: bool = True) -> str:
    """Indian formatting.

    full:    ₹1,20,000 (whole rupees, half-up); amounts under ₹100 keep paise if non-zero (₹49.50)
    compact: ₹1.2 lakh / ₹1.25 crore / ₹14.8k (at most 2 decimals, trailing zeros dropped)
    """
    if not isinstance(paise, int):
        raise TypeError("money must be int paise")
    sign = "-" if paise < 0 else ""
    p = abs(paise)
    cur = "₹" if symbol else ""
    if style == "compact" and p >= 1000 * PAISE_PER_RUPEE:
        r = Decimal(p) / PAISE_PER_RUPEE
        if r >= Decimal(10_000_000):
            num, unit = r / Decimal(10_000_000), " crore"
        elif r >= Decimal(100_000):
            num, unit = r / Decimal(100_000), " lakh"
        else:
            num, unit = r / Decimal(1000), "k"
        q = num.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"{sign}{cur}{_trim(q)}{unit}"
    if p < 100 * PAISE_PER_RUPEE and p % PAISE_PER_RUPEE:
        return f"{sign}{cur}{p // 100}.{p % 100:02d}"
    return f"{sign}{cur}{group_indian(_round_rupees(p))}"
