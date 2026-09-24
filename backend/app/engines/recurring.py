"""Recurring detection (SPEC §6.2, D5a, D23). Pure: View -> list[RecurringItem]."""
from __future__ import annotations

import datetime as dt
import statistics
from collections import Counter
from dataclasses import dataclass, field

import polars as pl

from app.core.config import load_yaml
from app.core.dates import add_months, roll_back_weekend
from app.core.ids import stable_id
from app.engines.view import View

# Not recurring obligations or income. transfer_self is handled separately: a regular move from the operating
# account to another deposit account is a SWEEP (kind "sweep", D30); card payments get their own schedule.
EXCLUDED = {"transfer_self", "transfer_in_unseen", "loan_repayment_in", "loan_disbursal_out", "card_bill_unlinked",
            "refund", "loan_disbursal", "cash_withdrawal", "fees_charges", "card_interest"}
CADENCE_DAYS = {"weekly": 7, "monthly": 30, "quarterly": 91, "annual": 365}
KIND = {"income_salary": "salary", "income_gig": "income", "income_other": "income", "emi": "emi",
        "sip_investment": "sip", "rent": "rent", "ott_subscription": "subscription", "utilities": "bill",
        "telecom": "bill", "insurance": "bill", "education": "fees", "transfer_unseen": "transfer"}
MANDATE_KINDS = {"emi", "sip"}  # NACH/ACH mandates: a shortfall is a bounce with a return charge
# Contractual-type kinds: two identical (±1%) monthly charges are enough evidence (short histories, new users).
EXACT_TWO_KINDS = {"salary", "emi", "sip", "rent", "subscription", "fees", "sweep"}
VARIABLE_BILL_KINDS = {"bill"}


@dataclass
class RecurringItem:
    item_id: str
    merchant_key: str
    merchant_name: str
    direction: str
    category: str
    kind: str
    cadence: str
    amount_paise: int
    next_due: dt.date
    last_date: dt.date
    active: bool
    source: str = "detected"  # detected | contract
    occurrences: int = 0
    dates: list[dt.date] = field(default_factory=list)
    amount_variable: bool = False
    pending_change: bool = False  # latest amount differs but only once (D23)
    changed_at: dt.date | None = None
    subscription_group: str | None = None
    account_id: str | None = None
    counter_account_id: str | None = None  # sweeps: the own account receiving the money
    amounts: list[int] = field(default_factory=list)  # observed amounts, for the forecast's amount spread

    @property
    def cadence_days(self) -> int:
        return CADENCE_DAYS[self.cadence]

    @property
    def monthly_paise(self) -> int:
        return {"weekly": self.amount_paise * 52 // 12, "monthly": self.amount_paise,
                "quarterly": self.amount_paise // 3, "annual": self.amount_paise // 12}[self.cadence]


def _cadence(dates: list[dt.date]) -> str | None:
    gaps = [(b - a).days for a, b in zip(dates, dates[1:], strict=False)]
    n = len(dates)
    if n >= 3 and all(24 <= g <= 38 or 52 <= g <= 68 for g in gaps) and sum(52 <= g <= 68 for g in gaps) <= 1:
        return "monthly"
    if n >= 4 and all(5 <= g <= 9 for g in gaps):
        return "weekly"
    if n >= 2 and all(80 <= g <= 100 for g in gaps):
        return "quarterly"
    return None


def _within(xs: list[int], tol: float) -> bool:
    m = statistics.median(xs)
    return m > 0 and all(abs(x - m) / m <= tol for x in xs)


def amount_level(amounts: list[int], variable_ok: bool = False) -> dict | None:
    """D23: stable, or one change-point (both segments within 5%). The old level needs >= 2 occurrences;
    the new level needs >= 2 to be adopted, otherwise it is a pending change."""
    n = len(amounts)
    for i in range(n - 1, 1, -1):  # latest change-point first; the old segment has >= 2 values
        a, b = amounts[:i], amounts[i:]
        if _within(a, 0.05) and _within(b, 0.05):
            ma, mb = statistics.median(a), statistics.median(b)
            if abs(mb - ma) / ma > 0.05:
                if len(b) >= 2:
                    return {"amount": int(mb), "changed_index": i, "pending": False}
                return {"amount": int(ma), "changed_index": None, "pending": True}
    if _within(amounts, 0.15):
        return {"amount": int(statistics.median(amounts[-3:])), "changed_index": None,
                "pending": abs(amounts[-1] - statistics.median(amounts)) / statistics.median(amounts) > 0.05}
    if variable_ok and _within(amounts, 0.40):
        return {"amount": int(statistics.median(amounts[-3:])), "changed_index": None, "pending": False,
                "variable": True}
    return None


def _anchor_day(dates: list[dt.date]) -> int:
    counts = Counter(d.day for d in dates)
    top = max(counts.values())
    return min(day for day, c in counts.items() if c == top)


def next_due(cadence: str, dates: list[dt.date], kind: str) -> dt.date:
    last = dates[-1]
    if cadence == "monthly":
        anchor = _anchor_day(dates)
        d = add_months(last + dt.timedelta(days=10), 0, anchor)
        if d <= last + dt.timedelta(days=10):
            d = add_months(d, 1, anchor)
        return roll_back_weekend(d) if kind == "salary" else d
    if cadence == "weekly":
        return last + dt.timedelta(days=7)
    if cadence == "quarterly":
        return add_months(last, 3)
    return add_months(last, 12)


def detect_recurring(view: View) -> list[RecurringItem]:
    merchants = load_yaml("merchants")["merchants"]
    subs = load_yaml("subscriptions")
    deposit_ids = [a.account_id for a in view.accounts.values() if a.kind in ("savings", "current")]
    sweeps = ((pl.col("category") == "transfer_self") & (pl.col("direction") == "debit")
              & pl.col("account_id").is_in(deposit_ids) & pl.col("counter_account_id").is_in(deposit_ids)
              & ~pl.col("is_card_payment"))
    tx = view.txns.filter((~pl.col("category").is_in(list(EXCLUDED)) | sweeps) & (pl.col("account_kind") != "loan")
                          & ~((pl.col("account_kind") == "credit_card") & (pl.col("direction") == "credit"))
                          & (pl.col("merchant_key") != "atm") & ~pl.col("is_card_payment"))
    items: list[RecurringItem] = []
    for (mkey, direction), g in sorted(tx.group_by(["merchant_key", "direction"]), key=lambda kv: kv[0]):
        g = g.sort(["date", "seq"])
        dates, amounts = g["date"].to_list(), g["amount"].to_list()
        category = Counter(g["category"].to_list()).most_common(1)[0][0]
        kind = "sweep" if category == "transfer_self" else KIND.get(category, "other")
        group = merchants.get(mkey, {}).get("subscription_group")
        cadence = _cadence(dates)
        if (cadence is None and len(dates) == 2 and kind in EXACT_TWO_KINDS and 24 <= (dates[1] - dates[0]).days <= 38
                and abs(amounts[1] - amounts[0]) <= 0.01 * amounts[0]):
            cadence = "monthly"
        level = amount_level(amounts, variable_ok=kind in VARIABLE_BILL_KINDS) if cadence else None
        if cadence and level:
            changed = level["changed_index"]
            item = RecurringItem(
                item_id=stable_id("rec", view.user_id, mkey, direction), merchant_key=mkey,
                merchant_name=g["merchant_name"][-1], direction=direction, category=category, kind=kind,
                cadence=cadence, amount_paise=level["amount"], next_due=next_due(cadence, dates, kind),
                last_date=dates[-1], active=(view.as_of - dates[-1]).days <= 1.5 * CADENCE_DAYS[cadence],
                occurrences=len(dates), dates=dates, amount_variable=bool(level.get("variable")),
                pending_change=level["pending"], changed_at=dates[changed] if changed is not None else None,
                subscription_group=group, account_id=Counter(g["account_id"].to_list()).most_common(1)[0][0],
                counter_account_id=Counter([c for c in g["counter_account_id"].to_list() if c]).most_common(1)[0][0]
                if kind == "sweep" and any(g["counter_account_id"].to_list()) else None,
                amounts=amounts)
            items.append(item)
            continue
        # Annual plans: a single charge that matches a known annual price (D23)
        plans = subs["annual_plans"].get(mkey)
        if plans and direction == "debit":
            last_amt, last_date = amounts[-1], dates[-1]
            if any(abs(last_amt - p * 100) / (p * 100) <= subs["annual_price_tolerance"] for p in plans):
                items.append(RecurringItem(
                    item_id=stable_id("rec", view.user_id, mkey, direction), merchant_key=mkey,
                    merchant_name=g["merchant_name"][-1], direction=direction, category=category,
                    kind="subscription", cadence="annual", amount_paise=last_amt, next_due=add_months(last_date, 12),
                    last_date=last_date, active=(view.as_of - last_date).days <= 365, occurrences=1,
                    dates=[last_date], subscription_group=group, account_id=g["account_id"][-1]))
    return _merge_contracts(view, items)


def _merge_contracts(view: View, items: list[RecurringItem]) -> list[RecurringItem]:
    """D5a: a linked loan's EMI schedule is known immediately; the contract wins over a detected series."""
    by_key = {(i.merchant_key, i.direction): i for i in items}
    for a in view.accounts.values():
        st = a.state
        if a.kind != "loan" or not a.linked or not st.get("emi_paise") or not st.get("emi_next_due"):
            continue
        if st.get("loan_remaining_months") == 0:
            continue
        due = dt.date.fromisoformat(st["emi_next_due"])
        while due <= view.as_of:
            due = add_months(due, 1)
        key = st.get("lender_merchant_key") or f"loan:{a.account_id}"
        detected = by_key.get((key, "debit"))
        by_key[(key, "debit")] = RecurringItem(
            item_id=stable_id("rec", view.user_id, key, "debit"), merchant_key=key,
            merchant_name=load_yaml("merchants")["merchants"].get(key, {}).get("name", key),
            direction="debit", category="emi", kind="emi", cadence="monthly", amount_paise=int(st["emi_paise"]),
            next_due=due, last_date=detected.last_date if detected else due, active=True, source="contract",
            occurrences=detected.occurrences if detected else 0, dates=detected.dates if detected else [],
            account_id=detected.account_id if detected else None)
    return sorted(by_key.values(), key=lambda i: (i.kind, i.merchant_key, i.direction))


def overlapping_subscriptions(items: list[RecurringItem]) -> list[dict]:
    groups: dict[str, list[RecurringItem]] = {}
    for i in items:
        if i.active and i.kind == "subscription" and i.subscription_group:
            groups.setdefault(i.subscription_group, []).append(i)
    return [{"group": g, "count": len(v), "members": [i.merchant_name for i in v],
             "merchant_keys": [i.merchant_key for i in v], "monthly_total_paise": sum(i.monthly_paise for i in v)}
            for g, v in sorted(groups.items()) if len(v) >= 2]
