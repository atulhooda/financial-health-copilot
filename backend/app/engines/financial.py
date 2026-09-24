"""Financial engine: observed FACTS (SPEC §6.1). Pure: View -> Metrics. Money in paise; ratios as floats."""
from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass, field

import polars as pl

from app.core.dates import add_months
from app.engines.earmark import Earmark, loan_earmarks
from app.engines.recurring import RecurringItem, detect_recurring, overlapping_subscriptions
from app.engines.view import View

TRAILING = 3
P5_CYCLES = 6


@dataclass
class Cycle:
    start: dt.date
    end: dt.date  # exclusive
    complete: bool


@dataclass
class CardMetrics:
    account_id: str
    limit_paise: int | None
    statement_date: dt.date | None
    statement_balance_paise: int | None
    utilisation: float | None
    revolving_paise: int | None
    outstanding_paise: int | None
    apr_bps: int | None
    # D30/D31: per-statement history and the rate implied by the statement's own finance charges (a FACT)
    history: list[dict] = field(default_factory=list)  # [{statement, due, balance, paid, revolving}], oldest first
    implied_monthly_rate: float | None = None  # (finance charges + GST on them) / balance carried into the cycle
    purchases_monthly_paise: int | None = None  # median card spend per statement cycle
    pay_ratio: float | None = None  # median share of the statement actually paid by the due date


@dataclass
class Metrics:
    as_of: dt.date
    cycle_basis: str  # salary | calendar
    cycles: list[Cycle]
    income_pattern: str  # salaried | irregular | none
    income_monthly_paise: int
    salary_level_paise: int
    other_income_monthly_paise: int
    spend_monthly_paise: int
    essential_monthly_paise: int
    discretionary_monthly_paise: int
    savings_rate: float | None
    liquid_paise: int | None
    earmarked_loan_paise: int  # unspent recent loan cash excluded from the buffer (D29/D33)
    buffer_months: float | None
    emi_monthly_paise: int
    emi_to_income: float | None
    debt_outstanding_paise: int
    debt_to_income: float | None
    cards: list[CardMetrics]
    credit_utilisation: float | None
    revolving_paise: int | None
    debt_growth: dict | None  # D31 alert (a FACT): revolving grew across statements while income rose or held
    recurring: list[RecurringItem]
    overlaps: list[dict]
    spend_by_category: list[dict]
    drift: list[dict]
    discretionary_ratio: float | None
    cycle_lows: list[dict]
    bounces: int
    floor_paise: int
    coverage: dict = field(default_factory=dict)
    earmarks: list[Earmark] = field(default_factory=list)


# ---- helpers -------------------------------------------------------------------------------------
def _median(xs: list[int]) -> int:
    return int(statistics.median(xs)) if xs else 0


def _cycles(view: View, recurring: list[RecurringItem], history_start: dt.date) -> tuple[str, list[Cycle]]:
    salaries = [r for r in recurring if r.kind == "salary" and r.direction == "credit" and r.source == "detected"]
    if salaries:
        primary = max(salaries, key=lambda r: (r.amount_paise, r.merchant_key))
        ds = [d for d in primary.dates if d <= view.as_of]
        cyc = [Cycle(a, b, True) for a, b in zip(ds, ds[1:], strict=False)]
        cyc.append(Cycle(ds[-1], view.as_of + dt.timedelta(days=1), False))
        return "salary", cyc
    cyc = []
    m = dt.date(history_start.year, history_start.month, 1)
    if m < history_start:
        m = add_months(m, 1, 1)
    while m <= view.as_of:
        end = add_months(m, 1, 1)
        cyc.append(Cycle(m, min(end, view.as_of + dt.timedelta(days=1)), end <= view.as_of + dt.timedelta(days=1)))
        m = end
    return "calendar", cyc


def _in(df: pl.DataFrame, start: dt.date, end: dt.date) -> pl.DataFrame:
    return df.filter((pl.col("date") >= start) & (pl.col("date") < end))


def _spend(df: pl.DataFrame, flag: str = "spend") -> int:
    debits = df.filter((pl.col("direction") == "debit") & pl.col(flag))["amount"].sum()
    refunds = df.filter((pl.col("direction") == "credit") & (pl.col("category") == "refund"))["amount"].sum()
    return int(debits or 0) - (int(refunds or 0) if flag == "spend" else 0)


def _balance_on(df: pl.DataFrame, account_id: str, day: dt.date) -> int | None:
    rows = df.filter((pl.col("account_id") == account_id) & (pl.col("date") <= day)
                     & pl.col("balance_after").is_not_null())
    return int(rows["balance_after"][-1]) if rows.height else None


def _last_day_on_or_before(as_of: dt.date, day: int) -> dt.date:
    d = add_months(as_of, 0, day)
    return d if d <= as_of else add_months(as_of, -1, day)


def _due_after(statement: dt.date, due_day: int) -> dt.date:
    d = add_months(statement, 0, due_day)
    return d if d > statement else add_months(statement, 1, due_day)


def _card(view: View, a) -> CardMetrics:
    st = a.state
    limit = st.get("credit_limit_paise")
    sday, dday = st.get("statement_day"), st.get("due_day")
    tx = view.txns
    out = CardMetrics(a.account_id, limit, None, None, None, None, a.balance_paise, st.get("apr_bps"))
    if not (a.linked and sday and dday):
        return out
    s_latest = _last_day_on_or_before(view.as_of, sday)
    out.statement_date = s_latest
    out.statement_balance_paise = _balance_on(tx, a.account_id, s_latest)
    if limit and out.statement_balance_paise is not None:
        out.utilisation = max(out.statement_balance_paise, 0) / limit
    # revolving: the latest statement whose due date has passed, minus payments made by that due date
    s = s_latest
    while _due_after(s, dday) > view.as_of:
        s = add_months(s, -1, sday)
    stmt_bal = _balance_on(tx, a.account_id, s)
    if stmt_bal is not None:
        due = _due_after(s, dday)
        paid = tx.filter((pl.col("account_id") == a.account_id) & (pl.col("direction") == "credit")
                         & (pl.col("date") > s) & (pl.col("date") <= due)
                         & pl.col("category").is_in(["transfer_self", "transfer_in_unseen"]))["amount"].sum()
        out.revolving_paise = max(0, stmt_bal - int(paid or 0))
    _card_history(view, a, out, s, sday, dday)
    return out


def _card_history(view: View, a, out: CardMetrics, last_due_passed: dt.date, sday: int, dday: int) -> None:
    """Per-statement revolving, implied interest rate, purchases and pay ratio, from the card's own data."""
    tx = view.txns.filter(pl.col("account_id") == a.account_id)
    card_start = tx["date"].min()
    statements, s = [], last_due_passed
    while card_start is not None and s > card_start and len(statements) < 6:
        statements.append(s)
        s = add_months(s, -1, sday)
    statements.reverse()
    hist = []
    for s in statements:
        bal = _balance_on(view.txns, a.account_id, s)
        if bal is None:
            continue
        due = _due_after(s, dday)
        paid = int(tx.filter((pl.col("direction") == "credit") & (pl.col("date") > s) & (pl.col("date") <= due)
                             & pl.col("category").is_in(["transfer_self", "transfer_in_unseen"]))["amount"].sum() or 0)
        hist.append({"statement": s, "due": due, "balance": bal, "paid": paid, "revolving": max(0, bal - paid)})
    out.history = hist
    rates, buys = [], []
    for prev, cur in zip(hist, hist[1:], strict=False):
        cycle = tx.filter((pl.col("date") > prev["statement"]) & (pl.col("date") <= cur["statement"])
                          & (pl.col("direction") == "debit"))
        charges = int(cycle.filter(pl.col("category").is_in(["card_interest"]))["amount"].sum() or 0)
        gst = int(cycle.filter((pl.col("category") == "fees_charges") & ~pl.col("is_bounce"))["amount"].sum() or 0)
        if prev["revolving"] > 0 and charges > 0:
            rates.append((charges + gst) / prev["revolving"])
        buys.append(int(cycle.filter(pl.col("spend") & ~pl.col("category").is_in(["card_interest", "fees_charges"]))
                        ["amount"].sum() or 0))
    out.implied_monthly_rate = statistics.median(rates[-3:]) if rates else None
    out.purchases_monthly_paise = int(statistics.median(buys[-3:])) if buys else None
    ratios = [h["paid"] / h["balance"] for h in hist if h["balance"] > 0]
    out.pay_ratio = statistics.median(ratios[-3:]) if ratios else None


def _debt_growth(cards: list[CardMetrics], income_now: int, recurring: list[RecurringItem]) -> dict | None:
    """D31: revolving grew across consecutive statements while income rose or held. A FACT, stated with amounts."""
    for c in cards:
        h = [x for x in c.history if x["revolving"] > 0]
        if len(h) < 3:
            continue
        last = h[-3:]
        if not (last[0]["revolving"] < last[1]["revolving"] < last[2]["revolving"]):
            continue
        salary = next((r for r in recurring if r.kind == "salary" and r.direction == "credit" and r.active), None)
        income_then = (salary.amounts[0] if salary and salary.changed_at and salary.changed_at > last[0]["statement"]
                       else income_now)
        if income_now >= income_then:
            return {"account_id": c.account_id, "from_statement": last[0]["statement"],
                    "to_statement": last[-1]["statement"], "revolving_from_paise": last[0]["revolving"],
                    "revolving_to_paise": last[-1]["revolving"], "statements": len(last),
                    "income_then_paise": income_then, "income_now_paise": income_now}
    return None


# ---- engine --------------------------------------------------------------------------------------
def compute_metrics(view: View, recurring: list[RecurringItem] | None = None) -> Metrics:
    tx = view.txns
    recurring = detect_recurring(view) if recurring is None else recurring
    deposit_ids = [a.account_id for a in view.of_kind("savings", "current")]
    base = tx.filter(pl.col("account_id").is_in(deposit_ids)) if deposit_ids else tx
    history_start = base["date"].min() if base.height else view.as_of
    basis, cycles = _cycles(view, recurring, history_start)
    complete = [c for c in cycles if c.complete]
    trailing = complete[-TRAILING:]

    # ---- income ----
    salaries = [r for r in recurring if r.kind == "salary" and r.active and r.direction == "credit"]
    salary_keys = [r.merchant_key for r in salaries]
    salary_level = sum(r.amount_paise for r in salaries)
    income_tx = tx.filter(pl.col("income") & (pl.col("direction") == "credit"))
    if salaries:
        other = [int(_in(income_tx.filter(~pl.col("merchant_key").is_in(salary_keys)), c.start, c.end)["amount"].sum()
                     or 0) for c in trailing]
        pattern, other_income = "salaried", _median(other)
        income = salary_level + other_income
    else:
        monthly = [int(_in(income_tx, c.start, c.end)["amount"].sum() or 0) for c in trailing]
        income = _median(monthly)
        pattern, other_income = ("irregular" if income > 0 else "none"), income

    # ---- spend ----
    per_cycle = [_in(tx, c.start, c.end) for c in trailing]
    spend_m = _median([_spend(d) for d in per_cycle])
    essential_m = _median([_spend(d, "essential") for d in per_cycle])
    discretionary_m = _median([_spend(d, "discretionary") for d in per_cycle])
    savings_rate = (income - spend_m) / income if income > 0 and trailing else None

    # ---- balances, buffer ----
    bal = [a.balance_paise for a in view.of_kind("savings", "current") if a.visible and a.balance_paise is not None]
    liquid = sum(bal) if bal else None
    # D29/D33: borrowed cash still sitting in the account is not a buffer (one shared earmark rule).
    earmarks = loan_earmarks(view)
    borrowed = sum(e.earmarked_paise for e in earmarks)
    own_liquid = max(0, liquid - borrowed) if liquid is not None else None
    buffer = own_liquid / essential_m if own_liquid is not None and essential_m > 0 else None

    # ---- debt ----
    emi_m = sum(r.monthly_paise for r in recurring if r.kind == "emi" and r.active and r.direction == "debit")
    cards = [_card(view, a) for a in view.of_kind("credit_card") if a.visible or a.linked]
    loans_out = sum(int(a.state.get("loan_outstanding_paise") or 0) for a in view.of_kind("loan") if a.linked)
    card_out = sum(c.outstanding_paise or 0 for c in cards if c.outstanding_paise is not None)
    debt = loans_out + card_out
    lim = sum(c.limit_paise for c in cards if c.limit_paise and c.statement_balance_paise is not None)
    util = (sum(max(c.statement_balance_paise, 0) for c in cards if c.limit_paise and c.statement_balance_paise
                is not None) / lim) if lim else None
    revs = [c.revolving_paise for c in cards if c.revolving_paise is not None]
    revolving = sum(revs) if revs else None

    # ---- categories, drift ----
    last30 = tx.filter(pl.col("date") > view.as_of - dt.timedelta(days=30))
    spend_cats = sorted(set(tx.filter(pl.col("spend") & (pl.col("direction") == "debit"))["category"].to_list()))
    by_cat, drift = [], []
    total30 = _spend(last30) or 1
    for cat in spend_cats:
        v30 = _spend(last30.filter(pl.col("category") == cat))
        med = _median([_spend(d.filter(pl.col("category") == cat)) for d in per_cycle])
        by_cat.append({"category": cat, "last_30d_paise": v30, "trailing_median_paise": med,
                       "share_last_30d": v30 / total30})
        if med > 0 and v30 > med * 1.25 and v30 - med > 1_000_00:
            drift.append({"category": cat, "last_30d_paise": v30, "trailing_median_paise": med,
                          "change_pct": (v30 - med) / med * 100})
    by_cat.sort(key=lambda r: (-r["last_30d_paise"], r["category"]))
    disc30 = _spend(last30, "discretionary")
    disc_ratio = disc30 / discretionary_m if discretionary_m > 0 and trailing else None

    # ---- pre-salary liquidity (observed), bounces ----
    op = view.operating
    lows, bounces = [], 0
    for c in complete[-P5_CYCLES:]:
        if op is None:
            break
        seg = _in(tx, c.start, c.end).filter(pl.col("account_id") == op.account_id)
        opening = _balance_on(tx, op.account_id, c.start - dt.timedelta(days=1))
        vals = [v for v in [opening, *seg["balance_after"].to_list()] if v is not None]
        b = seg.filter(pl.col("is_bounce")).height
        bounces += b
        if vals:
            lows.append({"start": c.start, "end": c.end, "low_paise": min(vals), "bounces": b,
                         "clean": min(vals) >= view.floor_paise and b == 0})

    visible = [a for a in view.accounts.values() if a.visible and a.institution != "cash"]
    known = [a for a in view.accounts.values() if a.institution != "cash"]
    spend_all = _spend(tx.filter(pl.col("date") >= (trailing[0].start if trailing else history_start)))
    unc = _spend(tx.filter((pl.col("category") == "uncategorised")
                           & (pl.col("date") >= (trailing[0].start if trailing else history_start))))
    coverage = {"months_of_history": round((view.as_of - history_start).days / 30.44, 1),
                "history_start": history_start, "accounts_known": len(known), "accounts_visible": len(visible),
                "accounts_linked": sum(a.linked for a in known), "complete_cycles": len(complete),
                "uncategorised_share": (unc / spend_all) if spend_all > 0 else 0.0}

    return Metrics(
        as_of=view.as_of, cycle_basis=basis, cycles=cycles, income_pattern=pattern, income_monthly_paise=income,
        salary_level_paise=salary_level, other_income_monthly_paise=other_income, spend_monthly_paise=spend_m,
        essential_monthly_paise=essential_m, discretionary_monthly_paise=discretionary_m, savings_rate=savings_rate,
        liquid_paise=liquid, earmarked_loan_paise=borrowed, buffer_months=buffer, emi_monthly_paise=emi_m,
        emi_to_income=emi_m / income if income > 0 else None, debt_outstanding_paise=debt,
        debt_to_income=debt / (12 * income) if income > 0 else None, cards=cards, credit_utilisation=util,
        revolving_paise=revolving, debt_growth=_debt_growth(cards, salary_level or income, recurring),
        recurring=recurring, overlaps=overlapping_subscriptions(recurring),
        spend_by_category=by_cat, drift=drift, discretionary_ratio=disc_ratio, cycle_lows=lows, bounces=bounces,
        floor_paise=view.floor_paise, coverage=coverage, earmarks=earmarks)
