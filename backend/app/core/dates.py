"""Business dates are IST calendar dates."""
from __future__ import annotations

import calendar
import datetime as dt
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def to_ist(ts: dt.datetime) -> dt.datetime:
    return ts.astimezone(IST) if ts.tzinfo else ts.replace(tzinfo=IST)


def ist_date(ts: dt.datetime) -> dt.date:
    return to_ist(ts).date()


def add_months(d: dt.date, months: int, day: int | None = None) -> dt.date:
    """Same (or given) day-of-month, `months` later, clamped to month length."""
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    last = calendar.monthrange(y, m)[1]
    return dt.date(y, m, min(day or d.day, last))


def roll_back_weekend(d: dt.date) -> dt.date:
    """Salary convention: a Saturday/Sunday payday moves to the preceding Friday."""
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def roll_forward_weekend(d: dt.date) -> dt.date:
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


def daterange(start: dt.date, end: dt.date):
    """Inclusive range of dates."""
    for i in range((end - start).days + 1):
        yield start + dt.timedelta(days=i)
