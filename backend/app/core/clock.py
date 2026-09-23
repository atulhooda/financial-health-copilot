"""The only place in the codebase allowed to read wall-clock time (SPEC D5).

Everything else receives a Clock. A test scans the source tree for direct
datetime.now()/date.today()/time.time() calls outside this module.
"""
from __future__ import annotations

import datetime as _dt
from typing import Protocol

from app.core.dates import IST


class Clock(Protocol):
    def now(self) -> _dt.datetime: ...
    def today(self) -> _dt.date: ...


class SystemClock:
    def now(self) -> _dt.datetime:
        return _dt.datetime.now(tz=IST)

    def today(self) -> _dt.date:
        return self.now().date()


class FixedClock:
    """Deterministic clock for the demo and tests. Time only moves when told to."""

    def __init__(self, at: _dt.datetime | _dt.date):
        if isinstance(at, _dt.datetime):
            self._now = at if at.tzinfo else at.replace(tzinfo=IST)
        else:
            self._now = _dt.datetime.combine(at, _dt.time(23, 0), tzinfo=IST)

    def now(self) -> _dt.datetime:
        return self._now

    def today(self) -> _dt.date:
        return self._now.date()

    def set(self, at: _dt.datetime | _dt.date) -> None:
        self.__init__(at)


_default: Clock | None = None


def get_clock() -> Clock:
    """Process default clock: FixedClock if HISAAB_CLOCK=YYYY-MM-DD is set, else system time."""
    global _default
    if _default is None:
        from app.core.config import get_settings

        fixed = get_settings().clock
        _default = FixedClock(_dt.date.fromisoformat(fixed)) if fixed else SystemClock()
    return _default


def set_clock(clock: Clock) -> None:
    global _default
    _default = clock
