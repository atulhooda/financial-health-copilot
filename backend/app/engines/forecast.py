"""Forecast engine: PREDICTIONS (SPEC §6.4).

Daily end-of-day balance for each deposit account over a 45-day horizon:
    scheduled flows (schedule.py) + discretionary net flow bootstrapped from the account's own history,
    stratified by (day of week, day-of-month bucket), 1,000 seeded Monte Carlo paths.
Paths are *cash-need* paths: every obligation is deducted even when the balance can't cover it, so a
negative balance is the shortfall. Bounce risk for a debit = share of paths whose balance just before
it (after that day's credits and earlier debits) is below its amount (D18).
The pool is independent of the schedule, and draws are made in a fixed order, so a simulation that only
changes the schedule sees the same random draws (common random numbers, SPEC §6.5).
"""
from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field

import numpy as np
import polars as pl

from app.core.seeding import rng_for
from app.engines.earmark import earmarked_by_account
from app.engines.recurring import RecurringItem
from app.engines.schedule import ScheduledFlow, build_schedule
from app.engines.view import View

HORIZON_DAYS = 45
N_PATHS = 1000
POOL_DAYS = 180
MIN_STRATUM = 4
IRREGULAR_INCOME_WINDOW = 30
POOL_EXCLUDED = {"transfer_self", "transfer_in_unseen", "loan_disbursal", "loan_repayment_in", "loan_disbursal_out"}


def dom_bucket(d: dt.date) -> int:
    return 0 if d.day <= 10 else (1 if d.day <= 20 else 2)


@dataclass
class Pool:
    """Historical daily discretionary flows for one account (credits and debits kept apart for diagnostics)."""

    account_id: str
    days: list[dt.date]
    credits: np.ndarray  # paise per day
    debits: np.ndarray  # paise per day, positive

    @property
    def values(self) -> np.ndarray:  # net per day (credits positive)
        return self.credits - self.debits

    def candidates(self, day: dt.date) -> np.ndarray:
        key = (day.weekday(), dom_bucket(day))
        idx = [i for i, d in enumerate(self.days) if (d.weekday(), dom_bucket(d)) == key]
        if len(idx) < MIN_STRATUM:
            idx = [i for i, d in enumerate(self.days) if d.weekday() == day.weekday()]
        if len(idx) < MIN_STRATUM:
            idx = list(range(len(self.days)))
        return np.asarray(idx)


def build_pool(view: View, account_id: str, recurring: list[RecurringItem]) -> Pool | None:
    acc = view.accounts[account_id]
    if acc.visible_from is None:
        return None
    scheduled_keys = {(r.merchant_key, r.direction) for r in recurring if r.account_id == account_id}
    tx = view.txns.filter((pl.col("account_id") == account_id) & ~pl.col("category").is_in(list(POOL_EXCLUDED))
                          & ~pl.col("is_card_payment"))
    rows = [r for r in tx.select(["date", "amount", "direction", "merchant_key"]).iter_rows(named=True)
            if (r["merchant_key"], r["direction"]) not in scheduled_keys]
    start = max(acc.visible_from, view.as_of - dt.timedelta(days=POOL_DAYS - 1))
    n = (view.as_of - start).days + 1
    if n < 28:
        return None
    credits, debits = np.zeros(n, dtype=np.int64), np.zeros(n, dtype=np.int64)
    for r in rows:
        if r["date"] >= start:
            (credits if r["direction"] == "credit" else debits)[(r["date"] - start).days] += r["amount"]
    return Pool(account_id, [start + dt.timedelta(days=i) for i in range(n)], credits, debits)


@dataclass
class AccountPaths:
    account_id: str
    dates: list[dt.date]
    opening_paise: int
    paths: np.ndarray  # (n_paths, horizon) end-of-day balances
    bounce: dict[int, float]  # index in schedule -> probability of shortfall
    mean_credits: np.ndarray | None = None  # per day, mean over paths (scheduled + discretionary): diagnostics
    mean_debits: np.ndarray | None = None


def simulate_account(view: View, account_id: str, opening: int, schedule: list[ScheduledFlow], pool: Pool | None,
                     horizon: int = HORIZON_DAYS, n_paths: int = N_PATHS, seed_parts: tuple = ()) -> AccountPaths:
    rng = rng_for(view.user_id, "forecast", account_id, *seed_parts)
    dates = [view.as_of + dt.timedelta(days=i + 1) for i in range(horizon)]
    # Draw all discretionary flows first, in a fixed order, independent of the schedule (CRN).
    disc_c = np.zeros((n_paths, horizon), dtype=np.int64)
    disc_d = np.zeros((n_paths, horizon), dtype=np.int64)
    if pool is not None:
        for i, d in enumerate(dates):
            cands = pool.candidates(d)
            pick = cands[rng.integers(0, len(cands), size=n_paths)]
            disc_c[:, i], disc_d[:, i] = pool.credits[pick], pool.debits[pick]
    mine = [(k, f) for k, f in enumerate(schedule) if f.account_id == account_id]
    by_day: dict[dt.date, list[tuple[int, ScheduledFlow]]] = {}
    for k, f in mine:
        by_day.setdefault(f.date, []).append((k, f))
    bal = np.full(n_paths, opening, dtype=np.int64)
    paths = np.zeros((n_paths, horizon), dtype=np.int64)
    bounce: dict[int, float] = {}
    sched_c = np.zeros(horizon)
    sched_d = np.zeros(horizon)

    def amounts(f: ScheduledFlow) -> np.ndarray | int:
        """Estimated amounts vary per path, resampled from the item's own history. Each flow has its own
        seeded stream, so adding or removing a flow never shifts anyone else's draws (CRN)."""
        if not f.spread:
            return f.amount_paise
        r = rng_for(view.user_id, "forecast-amount", f.item_id, f.date.isoformat(), *seed_parts)
        factors = np.asarray(f.spread)[r.integers(0, len(f.spread), size=n_paths)]
        return np.rint(f.amount_paise * factors).astype(np.int64)

    for i, d in enumerate(dates):
        todays = sorted(by_day.get(d, []), key=lambda kf: kf[1].sort_key())
        for _k, f in todays:
            if f.direction == "credit":
                amt = amounts(f)
                sched_c[i] += float(np.mean(amt))
                bal = bal + amt
        for k, f in todays:
            if f.direction == "debit":
                amt = amounts(f)
                bounce[k] = float(np.mean(bal < amt))
                sched_d[i] += float(np.mean(amt))
                bal = bal - amt
        bal = bal + disc_c[:, i] - disc_d[:, i]
        paths[:, i] = bal
    return AccountPaths(account_id, dates, opening, paths, bounce,
                        sched_c + disc_c.mean(axis=0), sched_d + disc_d.mean(axis=0))


@dataclass
class BounceRisk:
    name: str
    merchant_key: str
    kind: str
    account_id: str
    due_date: dt.date
    amount_paise: int
    probability: float
    mandate: bool


@dataclass
class Forecast:
    as_of: dt.date
    account_id: str | None
    horizon_days: int
    floor_paise: int
    opening_paise: int | None
    next_income_date: dt.date | None
    income_basis: str  # salary | irregular
    dates: list[dt.date] = field(default_factory=list)
    p10: list[int] = field(default_factory=list)
    p50: list[int] = field(default_factory=list)
    p90: list[int] = field(default_factory=list)
    dip_probability: float | None = None
    likely_dip_date: dt.date | None = None
    projected_low_paise: int | None = None
    pre_income_p10_p50_p90: tuple[int, int, int] | None = None
    bounce_risks: list[BounceRisk] = field(default_factory=list)
    schedule: list[ScheduledFlow] = field(default_factory=list)
    n_paths: int = N_PATHS
    available: bool = True
    reason: str | None = None
    earmarked_loan_paise: int = 0  # D33: excluded from the opening balance, returned as an assumption
    dip_probability_if_kept: float | None = None  # D33 alternative: the same forecast if the loan cash is kept
    assumptions: list[dict] = field(default_factory=list)


def next_income_date(view: View, recurring: list[RecurringItem]) -> tuple[dt.date, str]:
    op = view.operating
    salaries = [r for r in recurring if r.kind == "salary" and r.active and r.direction == "credit"
                and (op is None or r.account_id == op.account_id)]
    if salaries:
        primary = max(salaries, key=lambda r: (r.amount_paise, r.merchant_key))
        d = primary.next_due
        while d <= view.as_of:  # overdue salary: expected tomorrow
            d = view.as_of + dt.timedelta(days=1)
        return d, "salary"
    return view.as_of + dt.timedelta(days=IRREGULAR_INCOME_WINDOW), "irregular"


def _pct(paths: np.ndarray, q: float) -> list[int]:
    return [int(v) for v in np.rint(np.percentile(paths, q, axis=0, method="linear"))]


def run_forecast(view: View, recurring: list[RecurringItem], schedule: list[ScheduledFlow] | None = None,
                 horizon: int = HORIZON_DAYS, n_paths: int = N_PATHS, seed_parts: tuple = (),
                 pools: dict[str, Pool | None] | None = None) -> tuple[Forecast, dict[str, AccountPaths]]:
    """The forecast plus the raw paths (kept for simulations; never serialised)."""
    start, end = view.as_of + dt.timedelta(days=1), view.as_of + dt.timedelta(days=horizon)
    schedule = build_schedule(view, recurring, start, end) if schedule is None else schedule
    op = view.operating
    nid, basis = next_income_date(view, recurring)
    fc = Forecast(view.as_of, op.account_id if op else None, horizon, view.floor_paise,
                  op.balance_paise if op else None, nid, basis, schedule=schedule, n_paths=n_paths)
    if op is None or op.balance_paise is None:
        fc.available, fc.reason = False, "no operating account balance (link the salary account)"
        return fc, {}

    accounts = [op.account_id] + sorted({f.account_id for f in schedule if f.direction == "debit"} - {op.account_id})
    pools = pools if pools is not None else {a: build_pool(view, a, recurring) for a in accounts}
    sims: dict[str, AccountPaths] = {}
    earmarks = earmarked_by_account(view)
    for a in accounts:
        opening = view.accounts[a].balance_paise
        if opening is None:
            continue
        earmarked = min(earmarks.get(a, 0), max(0, opening))
        if earmarked:
            fc.earmarked_loan_paise += earmarked
            fc.assumptions.append({"key": "loan_cash_earmarked", "account_id": a, "amount_paise": earmarked,
                                   "text": "Assumes the recent loan money goes to its purpose, so it is left out of "
                                           "the forecast. If you keep it, see the alternative."})
        sims[a] = simulate_account(view, a, opening - earmarked, schedule, pools.get(a), horizon, n_paths, seed_parts)

    paths = sims[op.account_id].paths
    fc.dates = sims[op.account_id].dates
    fc.p10, fc.p50, fc.p90 = _pct(paths, 10), _pct(paths, 50), _pct(paths, 90)

    # dip before the next income (the opening balance is observed and counts)
    window = [i for i, d in enumerate(fc.dates) if d < nid]
    opening = np.full((n_paths, 1), sims[op.account_id].opening_paise, dtype=np.int64)
    win = np.hstack([opening, paths[:, window]]) if window else opening
    below = win < view.floor_paise
    dipped = below.any(axis=1)
    fc.dip_probability = float(dipped.mean())
    if fc.dip_probability >= 0.05:
        first = below.argmax(axis=1)[dipped]
        day_of = [view.as_of if j == 0 else fc.dates[window[j - 1]] for j in first]
        counts = Counter(day_of)
        top = max(counts.values())
        fc.likely_dip_date = min(d for d, c in counts.items() if c == top)
    if window:
        pre = paths[:, window[-1]]
        fc.pre_income_p10_p50_p90 = tuple(int(v) for v in np.rint(np.percentile(pre, [10, 50, 90], method="linear")))
    fc.projected_low_paise = int(np.rint(np.percentile(paths[:, :30].min(axis=1), 50, method="linear")))

    for k, f in enumerate(schedule):
        for a, sim in sims.items():
            if k in sim.bounce:
                fc.bounce_risks.append(BounceRisk(f.name, f.merchant_key, f.kind, a, f.date, f.amount_paise,
                                                  sim.bounce[k], f.mandate))
    fc.bounce_risks.sort(key=lambda b: (-b.probability, b.due_date, b.name))

    # D33: the alternative next to the assumption: the same forecast (same draws) if the loan cash is kept
    if earmarks.get(op.account_id):
        kept = simulate_account(view, op.account_id, op.balance_paise, schedule, pools.get(op.account_id), horizon,
                                n_paths, seed_parts)
        fc.dip_probability_if_kept = _dip(kept.paths, op.balance_paise, window, view.floor_paise)
        for a in fc.assumptions:
            if a["key"] == "loan_cash_earmarked" and a["account_id"] == op.account_id:
                a["alternative"] = {"dip_probability_if_kept": fc.dip_probability_if_kept}
    return fc, sims


def _dip(paths: np.ndarray, opening: int, window: list[int], floor: int) -> float:
    start = np.full((paths.shape[0], 1), opening, dtype=np.int64)
    win = np.hstack([start, paths[:, window]]) if window else start
    return float((win < floor).any(axis=1).mean())
