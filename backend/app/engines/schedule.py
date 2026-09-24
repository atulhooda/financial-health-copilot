"""Scheduled flows for the forecast (SPEC §6.4): what we expect to hit each deposit account, and when.

Sources, all observed or contractual:
- active recurring items (salary, EMIs, SIPs, rent, bills, subscriptions, sweeps), each on its due dates;
- loan contracts (D5a), already merged into the recurring items;
- card payments (Phase 3 point 1): for a linked card, the next due dates from the statement cycle and the
  amount the user ACTUALLY pays, from their observed payment ratio; for an SMS-only card, the observed
  monthly payment pattern. Card purchases never hit cash directly, only through these payments.
Simulations (Phase 4) modify this list; the discretionary pool stays the same (common random numbers).
"""
from __future__ import annotations

import datetime as dt
import statistics
from collections import Counter
from dataclasses import dataclass

import polars as pl

from app.core.dates import add_months, roll_back_weekend
from app.core.ids import stable_id
from app.engines.financial import _balance_on, _due_after, _last_day_on_or_before
from app.engines.recurring import MANDATE_KINDS, RecurringItem, _anchor_day, month_end
from app.engines.view import View

DEBIT_ORDER = {"sweep": 0, "emi": 1, "sip": 2, "rent": 3, "card_payment": 4, "fees": 5, "bill": 6,
               "subscription": 7, "transfer": 8, "other": 9}
LATE_GRACE_DAYS = 3  # an expected item this many days late is still expected tomorrow


@dataclass(frozen=True)
class ScheduledFlow:
    date: dt.date
    account_id: str
    amount_paise: int  # always positive
    direction: str  # credit | debit
    name: str
    merchant_key: str
    kind: str
    source: str  # detected | contract | card_statement | card_pattern | simulated
    mandate: bool
    estimated: bool  # amount estimated (variable bill, card payment)
    item_id: str
    spread: tuple[float, ...] | None = None  # per-path multiplicative factors for estimated amounts
    @property
    def signed(self) -> int:
        return self.amount_paise if self.direction == "credit" else -self.amount_paise

    def sort_key(self) -> tuple:
        return (self.date, 0 if self.direction == "credit" else 1, DEBIT_ORDER.get(self.kind, 9), self.name,
                self.item_id)


def _occurrences(item: RecurringItem, start: dt.date, end: dt.date) -> list[dt.date]:
    """Due dates of `item` in [start, end]. A recent miss (<= 3 days late) is expected at `start`."""
    out: list[dt.date] = []
    d = item.next_due
    anchor = _anchor_day(item.dates) if item.dates and item.source == "detected" else item.next_due.day
    k = 0
    while d < start:
        if (start - d).days <= LATE_GRACE_DAYS + 1:
            out.append(start)
        k += 1
        d = _step(item, anchor, k)
    while d <= end:
        if d >= start and d not in out:
            out.append(d)
        k += 1
        d = _step(item, anchor, k)
    return out


def _step(item: RecurringItem, anchor: int, k: int) -> dt.date:
    if item.cadence == "weekly":
        return item.next_due + dt.timedelta(days=7 * k)
    months = {"monthly": 1, "quarterly": 3, "annual": 12}[item.cadence]
    if item.anchor == "month_end":
        return month_end(add_months(item.next_due, months * k, 1), item.kind)
    d = add_months(item.next_due, months * k, anchor if item.cadence == "monthly" else None)
    return roll_back_weekend(d) if item.kind == "salary" else d


def spread_of(amounts: list[int], last: int = 6) -> tuple[float, ...] | None:
    """Relative dispersion of an item's own history: amount_i / median. None if it never varies."""
    xs = [a for a in amounts[-last:] if a > 0]
    if len(xs) < 2:
        return None
    m = statistics.median(xs)
    factors = tuple(round(x / m, 4) for x in xs)
    return None if all(f == 1.0 for f in factors) else factors


def _is_deposit(view: View, account_id: str | None) -> bool:
    a = view.accounts.get(account_id) if account_id else None
    return a is not None and a.kind in ("savings", "current") and a.institution != "cash"


def recurring_flows(view: View, recurring: list[RecurringItem], start: dt.date, end: dt.date) -> list[ScheduledFlow]:
    op = view.operating
    out = []
    for it in recurring:
        if not it.active:
            continue
        account = it.account_id or (op.account_id if op else None)
        if not _is_deposit(view, account):
            continue  # card-side subscriptions reach cash through the card payment
        spread = spread_of(it.amounts) if it.amount_variable else None
        name = _display_name(view, it)
        for d in _occurrences(it, start, end):
            out.append(ScheduledFlow(d, account, it.amount_paise, it.direction, name, it.merchant_key, it.kind,
                                     it.source, it.kind in MANDATE_KINDS, it.amount_variable, it.item_id, spread))
            if it.kind == "sweep" and _is_deposit(view, it.counter_account_id):  # the other leg lands here
                out.append(ScheduledFlow(d, it.counter_account_id, it.amount_paise, "credit", name,
                                         it.merchant_key, "sweep", it.source, False, False, it.item_id + ":in"))
    return out


def _display_name(view: View, it: RecurringItem) -> str:
    if it.kind == "sweep" and it.counter_account_id in view.accounts:
        a = view.accounts[it.counter_account_id]
        return f"Transfer to {a.institution.upper()} ••{a.last4}"
    if it.merchant_key.startswith("p2p:"):
        return f"{it.kind.title()} to {it.merchant_name}"
    return it.merchant_name


def card_payment_flows(view: View, start: dt.date, end: dt.date) -> list[ScheduledFlow]:
    tx = view.txns
    out = []
    for card in view.of_kind("credit_card"):
        pays = tx.filter(pl.col("is_card_payment") & (pl.col("card_account_id") == card.account_id)).sort(["date", "seq"])
        if pays.height == 0:
            continue  # we have never seen this card paid: nothing observed to schedule
        payer = Counter(pays["account_id"].to_list()).most_common(1)[0][0]
        name = f"Card bill ({card.institution.upper()} ••{card.last4})"
        st = card.state
        sday, dday = st.get("statement_day"), st.get("due_day")
        item_id = stable_id("sched", view.user_id, card.account_id)
        if card.linked and sday and dday:
            flows = _statement_based(view, card, pays, sday, dday, start, end)
            source = "card_statement"
        else:
            flows = [(d, a, False) for d, a in _pattern_based(pays, start, end)]
            source = "card_pattern"
        spread = spread_of(pays["amount"].to_list())
        out += [ScheduledFlow(d, payer, amt, "debit", name, f"card:{card.account_id}", "card_payment", source,
                              False, True, item_id, None if known else spread) for d, amt, known in flows if amt > 0]
    return out


def _paid_between(pays: pl.DataFrame, a: dt.date, b: dt.date) -> int:
    return int(pays.filter((pl.col("date") > a) & (pl.col("date") <= b))["amount"].sum() or 0)


def _statement_based(view, card, pays, sday, dday, start, end) -> list[tuple[dt.date, int, bool]]:
    """Amount = the user's observed payment ratio x statement balance; date = due date + observed offset.

    Returns (date, amount, known): known = the statement is already generated, so the amount isn't spread."""
    tx = view.txns
    s_latest = _last_day_on_or_before(view.as_of, sday)
    ratios, offsets = [], []
    s = s_latest
    while _due_after(s, dday) > view.as_of:
        s = add_months(s, -1, sday)
    for _ in range(3):  # the last three statements whose due date has passed
        bal = _balance_on(tx, card.account_id, s)
        due = _due_after(s, dday)
        window = pays.filter((pl.col("date") > s) & (pl.col("date") <= due))
        if bal and bal > 0:
            ratios.append(min(1.0, int(window["amount"].sum() or 0) / bal))
            if window.height:
                offsets.append((window["date"][-1] - due).days)
        s = add_months(s, -1, sday)
    ratio = statistics.median(ratios) if ratios else 1.0
    offset = int(statistics.median(offsets)) if offsets else 0
    latest_bal = max(0, _balance_on(tx, card.account_id, s_latest) or 0)
    out = []
    stmt = s_latest
    for _ in range(4):
        due = _due_after(stmt, dday) + dt.timedelta(days=offset)
        if due > end:
            break
        if due >= start:
            if stmt <= view.as_of:  # statement already generated: its balance is known
                amt, known = int(round(ratio * latest_bal)) - _paid_between(pays, stmt, view.as_of), True
            else:  # future statement: steady-state estimate (same as the latest), spread by past payments
                amt, known = int(round(ratio * latest_bal)), False
            out.append((due, max(0, amt), known))
        stmt = add_months(stmt, 1, sday)
    return out


def _pattern_based(pays: pl.DataFrame, start: dt.date, end: dt.date) -> list[tuple[dt.date, int]]:
    """Card with no statement (SMS-only/unlinked): the monthly payment pattern we observe from the bank side.

    Amount = the most recent payment. A revolving user's payments grow with the balance, so a median lags."""
    dates = pays["date"].to_list()
    if len(dates) < 2:
        return []
    anchor = _anchor_day(dates)
    amount = int(pays["amount"][-1])  # "about what you paid last time": responsive when payments trend upward
    out = []
    d = add_months(dates[-1], 1, anchor)
    while d <= end:
        if d >= start:
            out.append((d, amount))
        d = add_months(d, 1, anchor)
    return out


def build_schedule(view: View, recurring: list[RecurringItem], start: dt.date, end: dt.date) -> list[ScheduledFlow]:
    flows = recurring_flows(view, recurring, start, end) + card_payment_flows(view, start, end)
    return sorted(flows, key=lambda f: f.sort_key())
