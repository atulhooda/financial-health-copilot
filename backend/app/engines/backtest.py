"""Rolling-origin backtest and forecast confidence (SPEC §6.4).

For origins every 15 days from day 60 of the operating account's history up to as_of − 15, we cut the
view at the origin (only transactions dated on or before it; account states only if they existed by then),
re-detect recurring items, forecast, and compare with what actually happened:

- P10–P90 interval coverage = share of actual end-of-day balances that fell inside the band.
  This is THE number behind confidence and the one we quote (Phase 3 point 2). Nominal is 80%.
- Dip calibration (D27): predicted P(dip before next income) vs whether it happened (Brier score).

Confidence (Phase 3.5 review): the % we show is the measured coverage itself, never a transformed ratio.
Label: High >= 75%, Medium 60-75%, Low < 60%. History and linkage can only CAP the label, never make a new %.
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
LEVELS = ("Low", "Medium", "High")
EXPLAIN = ("Confidence tells you how often our 10-to-90% band actually held on your own past days when we replayed "
           "the forecast (target 80%): High from 75%, Medium from 60%, Low below that. It can't be High with under "
           "six months of history or with accounts you haven't linked.")


@dataclass
class OriginResult:
    origin: dt.date
    days: int
    inside: int
    predicted_dip: float | None
    actual_dip: bool | None
    # income vs spend split over the evaluated days (Phase 3.5 diagnosis): mean predicted vs actual, paise
    predicted_credits: float = 0.0
    actual_credits: int = 0
    predicted_debits: float = 0.0
    actual_debits: int = 0


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

    @property
    def calibration_gap(self) -> float | None:
        """|mean predicted dip probability − observed breach frequency| (Phase 3.5 acceptance metric)."""
        if self.mean_predicted_dip is None:
            return None
        return abs(self.mean_predicted_dip - self.observed_dip_rate)

    def side_errors(self) -> dict:
        """Income side vs spend side: (predicted − actual) per 30 days, and relative to actual."""
        days = self.days or 1
        pc = sum(o.predicted_credits for o in self.origins)
        ac = sum(o.actual_credits for o in self.origins)
        pd_ = sum(o.predicted_debits for o in self.origins)
        ad = sum(o.actual_debits for o in self.origins)
        return {"income_error_per_30d": (pc - ac) * 30 / days, "income_error_rel": (pc - ac) / ac if ac else None,
                "spend_error_per_30d": (pd_ - ad) * 30 / days, "spend_error_rel": (pd_ - ad) / ad if ad else None,
                "net_error_per_30d": ((pc - pd_) - (ac - ad)) * 30 / days}


@dataclass
class Confidence:
    label: str  # High | Medium | Low
    coverage: float | None  # measured P10-P90 coverage: the only % (shown in the "why trust this" sheet)
    reason: str  # one line for the app, e.g. "band held on 65% of past days, target 80%"
    caps: list[str] = field(default_factory=list)  # why the label was capped, if it was
    origins: int = 0
    days: int = 0
    target: float = NOMINAL_COVERAGE
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
    return View(view.user_id, origin, accounts, tx, view.floor_paise, view.ingest_seq, view.preferences)


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
        fc, sims = run_forecast(past, rec, horizon=h, n_paths=n_paths, seed_parts=("backtest", origin.isoformat()))
        actual = _actual_balances(view, op.account_id, fc.dates)
        inside = sum(1 for a, lo, hi in zip(actual, fc.p10, fc.p90, strict=True) if a is not None and lo <= a <= hi)
        days = sum(a is not None for a in actual)
        actual_dip = None
        if fc.next_income_date and fc.next_income_date <= view.as_of:
            win = [a for d, a in zip(fc.dates, actual, strict=True) if d < fc.next_income_date and a is not None]
            actual_dip = min([past.accounts[op.account_id].balance_paise, *win]) < view.floor_paise
        sim = sims.get(op.account_id)
        span = view.txns.filter((pl.col("account_id") == op.account_id) & (pl.col("date") > origin)
                                & (pl.col("date") <= fc.dates[-1]))
        bt.origins.append(OriginResult(
            origin, days, inside, fc.dip_probability, actual_dip,
            float(sim.mean_credits.sum()) if sim is not None else 0.0,
            int(span.filter(pl.col("direction") == "credit")["amount"].sum() or 0),
            float(sim.mean_debits.sum()) if sim is not None else 0.0,
            int(span.filter(pl.col("direction") == "debit")["amount"].sum() or 0)))
        origin += dt.timedelta(days=STEP_DAYS)
    return bt


def label_for(coverage: float | None) -> str:
    if coverage is None:
        return "Low"
    return "High" if coverage >= 0.75 else ("Medium" if coverage >= 0.60 else "Low")


def confidence(view: View, metrics_coverage: dict, bt: Backtest) -> Confidence:
    cov = bt.coverage if len(bt.origins) >= 2 else None
    label = label_for(cov)
    caps: list[tuple[str, str]] = []  # (max label, reason)
    months = metrics_coverage.get("months_of_history", 0)
    known = metrics_coverage.get("accounts_known", 0)
    linked = metrics_coverage.get("accounts_linked", 0)
    if months < 3:
        caps.append(("Low", f"only {months:g} months of history"))
    elif months < 6:
        caps.append(("Medium", f"only {months:g} months of history"))
    if known and linked / known < 0.5:
        caps.append(("Low", f"{known - linked} of {known} accounts not linked"))
    elif known and linked < known:
        caps.append(("Medium", f"{known - linked} account{'s' if known - linked > 1 else ''} not linked"))
    capped = min([label] + [c for c, _ in caps], key=LEVELS.index)
    applied = [r for c, r in caps if LEVELS.index(c) < LEVELS.index(label)]
    if cov is None:
        reason = "not enough history yet to test the forecast against what really happened"
    else:
        reason = f"band held on {cov:.0%} of past days, target {NOMINAL_COVERAGE:.0%}"
    if applied:
        reason += f"; capped at {capped}: {', '.join(applied)}"
    return Confidence(capped, cov, reason, applied, len(bt.origins), bt.days)


def forecast_with_confidence(view: View, recurring, metrics_coverage: dict) -> tuple[Forecast, Confidence, Backtest]:
    fc, _ = run_forecast(view, recurring)
    bt = run_backtest(view)
    return fc, confidence(view, metrics_coverage, bt), bt
