"""Rolling-origin backtest and forecast confidence (SPEC §6.4).

For origins every 15 days from day 60 of the operating account's history up to as_of − 15, we cut the
view at the origin (only transactions dated on or before it; account states only if they existed by then),
re-detect recurring items, forecast, and compare with what actually happened:

- P10–P90 interval coverage = share of actual end-of-day balances that fell inside the band.
  This is THE number behind confidence and the one we quote (Phase 3 point 2). Nominal is 80%.
- Dip calibration (D27): predicted P(dip before next income) vs whether it happened (Brier score).

confidence_pct = 100 × min(1, coverage / 0.80) × min(1, months_of_history / 6) × linked / known.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import polars as pl

from app.engines.forecast import HORIZON_DAYS, N_PATHS, Forecast, run_forecast
from app.engines.recurring import detect_recurring
from app.engines.view import View

STEP_DAYS = 15
MIN_HISTORY_DAYS = 60
NOMINAL_COVERAGE = 0.80
LABELS = ((75, "High"), (50, "Medium"), (0, "Low"))
EXPLAIN = ("Confidence is how often our 10-to-90% band actually contained your real balance when we replayed the "
           "forecast over your own past months, discounted if we have under six months of history or can see you "
           "have accounts you haven't linked.")


@dataclass
class OriginResult:
    origin: dt.date
    days: int
    inside: int
    predicted_dip: float | None
    actual_dip: bool | None


@dataclass
class Backtest:
    origins: list[OriginResult] = field(default_factory=list)

    @property
    def days(self) -> int:
        return sum(o.days for o in self.origins)

    @property
    def coverage(self) -> float | None:
        return sum(o.inside for o in self.origins) / self.days if self.days else None

    @property
    def dip_pairs(self) -> list[tuple[float, bool]]:
        return [(o.predicted_dip, o.actual_dip) for o in self.origins if o.actual_dip is not None]

    @property
    def brier(self) -> float | None:
        p = self.dip_pairs
        return float(np.mean([(q - float(a)) ** 2 for q, a in p])) if p else None

    @property
    def mean_predicted_dip(self) -> float | None:
        p = self.dip_pairs
        return float(np.mean([q for q, _ in p])) if p else None

    @property
    def observed_dip_rate(self) -> float | None:
        p = self.dip_pairs
        return float(np.mean([a for _, a in p])) if p else None


@dataclass
class Confidence:
    pct: int
    label: str
    coverage: float | None  # P10-P90 interval coverage from the backtest
    calibration_factor: float
    history_factor: float
    linkage_factor: float
    origins: int
    explain: str = EXPLAIN


def slice_view(view: View, origin: dt.date) -> View:
    """The view as it would have looked at `origin`: no later transactions, no later account states."""
    tx = view.txns.filter(pl.col("date") <= origin)
    accounts = {}
    for k, a in view.accounts.items():
        rows = tx.filter((pl.col("account_id") == k) & pl.col("balance_after").is_not_null())
        linked = a.linked and a.state_as_of is not None and a.state_as_of <= origin
        first = tx.filter(pl.col("account_id") == k)["date"].min()
        accounts[k] = dataclasses.replace(
            a, linked=linked, state=a.state if linked else {}, visible_from=first,
            balance_paise=int(rows["balance_after"][-1]) if rows.height else None)
    return View(view.user_id, origin, accounts, tx, view.floor_paise, view.ingest_seq)


def _actual_balances(view: View, account_id: str, dates: list[dt.date]) -> list[int | None]:
    rows = view.txns.filter((pl.col("account_id") == account_id) & pl.col("balance_after").is_not_null())
    ds, bs = rows["date"].to_list(), rows["balance_after"].to_list()
    out, j, last = [], 0, None
    for d in dates:
        while j < len(ds) and ds[j] <= d:
            last = bs[j]
            j += 1
        out.append(last)
    return out


def run_backtest(view: View, horizon: int = HORIZON_DAYS, n_paths: int = N_PATHS) -> Backtest:
    bt = Backtest()
    op = view.operating
    if op is None:
        return bt
    rows = view.txns.filter((pl.col("account_id") == op.account_id) & pl.col("balance_after").is_not_null())
    if rows.height == 0:
        return bt
    start = rows["date"].min()
    origin = start + dt.timedelta(days=MIN_HISTORY_DAYS)
    while origin <= view.as_of - dt.timedelta(days=STEP_DAYS):
        past = slice_view(view, origin)
        if past.accounts[op.account_id].balance_paise is None:
            origin += dt.timedelta(days=STEP_DAYS)
            continue
        past.accounts[op.account_id].role = "operating"
        rec = detect_recurring(past)
        h = min(horizon, (view.as_of - origin).days)
        fc, _ = run_forecast(past, rec, horizon=h, n_paths=n_paths, seed_parts=("backtest", origin.isoformat()))
        actual = _actual_balances(view, op.account_id, fc.dates)
        inside = sum(1 for a, lo, hi in zip(actual, fc.p10, fc.p90, strict=True) if a is not None and lo <= a <= hi)
        days = sum(a is not None for a in actual)
        actual_dip = None
        if fc.next_income_date and fc.next_income_date <= view.as_of:
            win = [a for d, a in zip(fc.dates, actual, strict=True) if d < fc.next_income_date and a is not None]
            actual_dip = min([past.accounts[op.account_id].balance_paise, *win]) < view.floor_paise
        bt.origins.append(OriginResult(origin, days, inside, fc.dip_probability, actual_dip))
        origin += dt.timedelta(days=STEP_DAYS)
    return bt


def confidence(view: View, metrics_coverage: dict, bt: Backtest) -> Confidence:
    cov = bt.coverage
    calibration = min(1.0, cov / NOMINAL_COVERAGE) if cov is not None and len(bt.origins) >= 2 else 0.5
    history = min(1.0, metrics_coverage.get("months_of_history", 0) / 6)
    known = metrics_coverage.get("accounts_known", 0)
    linkage = metrics_coverage.get("accounts_linked", 0) / known if known else 0.0
    pct = int(np.floor(100 * calibration * history * linkage + 0.5))
    label = next(name for lo, name in LABELS if pct >= lo)
    return Confidence(pct, label, cov, round(calibration, 4), round(history, 4), round(linkage, 4), len(bt.origins))


def forecast_with_confidence(view: View, recurring, metrics_coverage: dict) -> tuple[Forecast, Confidence, Backtest]:
    fc, _ = run_forecast(view, recurring)
    bt = run_backtest(view)
    return fc, confidence(view, metrics_coverage, bt), bt
