"""Simulation engine: RECOMMENDATIONS (SPEC §6.5, D7, D8, D26, D30, D34).

Every action is evaluated two ways, each against its own baseline:
- the FORECAST, re-run on a modified schedule with the same random draws, for bounce risk and dip changes;
- the 12-month PROJECTION (projection.py), for rupee impact and the projected score.
Actions are ranked by the 12-month score delta, then annual rupee impact, then key (stable).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field

from app.core.config import load_yaml
from app.core.dates import add_months
from app.core.money import format_inr
from app.engines.backtest import Confidence
from app.engines.earmark import earmarked_by_account
from app.engines.emi import emi_paise
from app.engines.financial import CardMetrics, Metrics, _balance_on
from app.engines.forecast import Forecast, Pool, build_pool, next_income_date, run_forecast
from app.engines.projection import (
    Loan,
    Modifier,
    Projection,
    ProjectionInputs,
    card_interest_total,
    loan_interest_total,
    months_to_clear,
    project,
)
from app.engines.recurring import RecurringItem
from app.engines.schedule import ScheduledFlow
from app.engines.score import Score
from app.engines.view import View

SWEEP_MAX_STEPS = 80  # ₹500 x 80 = ₹40,000: upper bound of the sweep search
LEVELS = ("Low", "Medium", "High")


@dataclass
class Context:
    view: View
    metrics: Metrics
    score: Score
    forecast: Forecast
    confidence: Confidence
    pools: dict[str, Pool | None]
    inputs: ProjectionInputs
    baseline: Projection
    card: CardMetrics | None
    cfg: dict

    @property
    def saturated(self) -> bool:
        """D34: a dip metric this close to certain carries no information and can't be moved."""
        return self.forecast.dip_probability is not None and self.forecast.dip_probability >= 0.95


@dataclass
class Action:
    key: str
    type: str
    title: str
    params: dict
    modifier: Modifier
    schedule_fn: Callable[[list[ScheduledFlow]], list[ScheduledFlow]] | None = None
    assumptions: list[dict] = field(default_factory=list)
    drivers: list[str] = field(default_factory=list)  # reason codes that motivate it (diff caused_by)
    auto: bool = True  # False: a what-if (D8), never auto-recommended
    confidence_cap: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class Recommendation:
    action_key: str
    type: str
    title: str
    rank: int | None
    params: dict
    impact: dict
    confidence: dict
    assumptions: list[dict]
    drivers: list[str]
    extra: dict = field(default_factory=dict)


# ---- context ----------------------------------------------------------------------------------------
def _assumption(key: str, text: str, value, unit: str, source: str = "config") -> dict:
    return {"key": key, "text": text, "value": value, "unit": unit, "source": source}


def _linked_card(m: Metrics) -> CardMetrics | None:
    return next((c for c in m.cards if c.revolving_paise is not None), None)


def _liquid_trend(view: View) -> int:
    """Observed monthly change in own liquid money over the last ~3 months (loan cash excluded)."""
    ear = earmarked_by_account(view)
    now = then = 0
    for a in view.of_kind("savings", "current"):
        b_now, b_then = a.balance_paise, _balance_on(view.txns, a.account_id, view.as_of - dt.timedelta(days=91))
        if b_now is None or b_then is None:
            continue
        now += b_now - ear.get(a.account_id, 0)
        then += b_then
    return int(round((now - then) / 3))


def _loans(view: View, recurring: list[RecurringItem]) -> list[Loan]:
    loans, covered = [], set()
    for a in view.of_kind("loan"):
        st = a.state
        if not a.linked or not st.get("emi_paise"):
            continue
        key = st.get("lender_merchant_key") or f"loan:{a.account_id}"
        covered.add(key)
        rate = st.get("loan_rate_bps") or load_yaml("assumptions")["loan_rate_bps_fallback"]
        name = load_yaml("merchants")["merchants"].get(key, {}).get("name", a.institution.upper())
        loans.append(Loan(key, name, int(st.get("loan_outstanding_paise") or 0), int(st["emi_paise"]), int(rate),
                          st.get("loan_remaining_months")))
    for r in recurring:  # a detected EMI with no linked loan: amount known, term unknown
        if r.kind == "emi" and r.active and r.merchant_key not in covered:
            loans.append(Loan(r.merchant_key, r.merchant_name, 0, r.amount_paise, 0, None))
    return loans


def build_context(view: View, metrics: Metrics, score: Score, forecast: Forecast, conf: Confidence) -> Context:
    cfg = load_yaml("assumptions")
    card = _linked_card(metrics)
    gst = 1 + cfg["gst_on_finance_charges_bps"] / 10000
    rate = card.implied_monthly_rate if card and card.implied_monthly_rate else cfg["card_apr_bps"] / 10000 / 12 * gst
    liquid = (metrics.liquid_paise or 0) - metrics.earmarked_loan_paise
    inputs = ProjectionInputs(
        income_paise=metrics.income_monthly_paise, spend_paise=metrics.spend_monthly_paise,
        essential_paise=metrics.essential_monthly_paise, emi_paise=metrics.emi_monthly_paise, liquid_paise=liquid,
        liquid_trend_paise=_liquid_trend(view), revolving_paise=card.revolving_paise if card else None,
        card_rate_monthly=rate, card_purchases_paise=(card.purchases_monthly_paise or 0) if card else 0,
        pay_ratio=(card.pay_ratio if card and card.pay_ratio is not None else 1.0),
        card_limit_paise=card.limit_paise if card else None, loans=_loans(view, metrics.recurring),
        savings_rate_monthly=cfg["savings_interest_bps"] / 10000 / 12, unswept_share=cfg["unswept_spend_share"],
        metrics=metrics)
    accounts = {f.account_id for f in forecast.schedule} | ({forecast.account_id} if forecast.account_id else set())
    pools = {a: build_pool(view, a, metrics.recurring) for a in accounts if a in view.accounts}
    return Context(view, metrics, score, forecast, conf, pools, inputs, project(inputs), card, cfg)


# ---- helpers ------------------------------------------------------------------------------------------
def max_mandate_bounce(fc: Forecast) -> float:
    return max([b.probability for b in fc.bounce_risks if b.mandate] or [0.0])


def _rerun(ctx: Context, schedule: list[ScheduledFlow]) -> Forecast:
    fc, _ = run_forecast(ctx.view, ctx.metrics.recurring, schedule=schedule, pools=ctx.pools)
    return fc


def _salary_days(ctx: Context) -> list[dt.date]:
    days = sorted({f.date for f in ctx.forecast.schedule if f.kind == "salary" and f.direction == "credit"
                   and f.account_id == ctx.forecast.account_id})
    return days or [next_income_date(ctx.view, ctx.metrics.recurring)[0]]


def _reserve(ctx: Context) -> list:
    return [a for a in ctx.view.of_kind("savings", "current") if a.role == "reserve" and a.balance_paise is not None]


def _cushion(ctx: Context) -> int:
    """D30: floor + scheduled debits due before the next income, on any deposit account."""
    nid = ctx.forecast.next_income_date or ctx.view.as_of
    deposits = {a.account_id for a in ctx.view.of_kind("savings", "current")}
    due = sum(f.amount_paise for f in ctx.forecast.schedule if f.direction == "debit" and f.date < nid
              and f.kind != "card_payment" and f.account_id in deposits)
    return ctx.view.floor_paise + due


def _card_rate_assumption(ctx: Context) -> dict:
    if ctx.card and ctx.card.implied_monthly_rate:
        return _assumption("card_rate", "Card interest at the rate your own statements charge (finance charges + GST)",
                           round(ctx.card.implied_monthly_rate * 100, 2), "pct_per_month", "statement")
    return _assumption("card_rate", "Card interest at a typical 3.5% a month + GST (no statement rate available)",
                       round(ctx.inputs.card_rate_monthly * 100, 2), "pct_per_month")


def _savings_rate_assumption(ctx: Context) -> dict:
    return _assumption("savings_interest", "Savings earn interest at this assumed rate",
                       ctx.cfg["savings_interest_bps"] / 100, "pct_per_year")


def _unswept_assumption(ctx: Context) -> dict:
    return _assumption("unswept_spend_share", "Half of any cash freed up and not moved away gets spent anyway (D26)",
                       ctx.cfg["unswept_spend_share"], "share")


PAY_IN_FULL = _assumption("pay_in_full_after", "Once the card is clear, you switch its autopay to the full amount "
                          "(otherwise the balance rebuilds)", True, "flag")


# ---- generators (auto) ---------------------------------------------------------------------------------
def gen_pay_down_card(ctx: Context) -> list[Action]:
    """D30(c): use part of savings to clear the card, keep a cushion.

    Cash-neutral for the salary account: savings cover the carried balance AND whatever part of the current
    statement exceeds what the user usually pays, so the next bill stays at the usual amount. After that the
    user pays each month's purchases in full (an explicit assumption)."""
    card = ctx.card
    if not card or not card.revolving_paise:
        return []
    cushion = _cushion(ctx)
    reserve = sum(a.balance_paise for a in _reserve(ctx))
    available = max(0, reserve - cushion)
    known = next((f for f in ctx.forecast.schedule if f.kind == "card_payment" and f.spread is None), None)
    usual = known.amount_paise if known else 0
    target = max(card.revolving_paise, (card.statement_balance_paise or 0) - usual) if known else card.revolving_paise
    if available >= target:  # round UP to the next ₹100 so nothing is left revolving
        amount = min(available, -(-target // 100_00) * 100_00)
    else:
        amount = available // 100_00 * 100_00
    if amount < 1_000_00:
        return []
    clears = amount >= card.revolving_paise
    if clears:
        title = (f"Use {format_inr(amount)} of savings to clear your card; keep {format_inr(cushion)} as a cushion"
                 + (f" (your next bill stays at the usual {format_inr(usual)})" if known else ""))
    else:
        title = f"Use {format_inr(amount)} of savings to pay down your card; keep {format_inr(cushion)}"

    def schedule(flows):
        out = []
        for f in flows:
            if f.kind == "card_payment":
                if f.spread is None:  # the statement already generated: what is left of it after today's payment
                    left = max(0, (card.statement_balance_paise or 0) - amount)
                    new = left if clears else int(round((card.pay_ratio or 1.0) * left))
                else:  # future statements: just the month's purchases, paid in full once clear
                    new = (card.purchases_monthly_paise or f.amount_paise) if clears else f.amount_paise
                out.append(dataclasses.replace(f, amount_paise=new, source="simulated"))
            else:
                out.append(f)
        return out

    return [Action(f"pay_down_card:{card.account_id}", "pay_down_card", title,
                   {"amount_paise": amount, "cushion_paise": cushion, "clears": clears, "next_bill_paise": usual},
                   Modifier(savings_to_card_now=amount, pay_in_full_once_clear=clears), schedule,
                   [_card_rate_assumption(ctx), _savings_rate_assumption(ctx)] + ([PAY_IN_FULL] if clears else []),
                   ["HIGH_COST_DEBT_FOUND", "DEBT_GROWING"])]


def gen_redirect_sweep(ctx: Context) -> list[Action]:
    """D30(d): an existing savings sweep is recognised; while the card revolves, redirect it."""
    if not ctx.card or not ctx.card.revolving_paise:
        return []
    out = []
    for it in ctx.metrics.recurring:
        if it.kind != "sweep" or not it.active or it.direction != "debit":
            continue
        dest = ctx.view.accounts.get(it.counter_account_id or "")
        if dest is None or dest.kind not in ("savings", "current"):
            continue
        x = it.amount_paise

        def schedule(flows, item=it):
            return [f for f in flows if f.item_id != item.item_id + ":in"]  # the money no longer lands in savings

        out.append(Action(
            f"redirect_sweep:{it.item_id}", "redirect_sweep",
            f"Send your {format_inr(x)} monthly transfer to the card instead of savings until it's clear",
            {"amount_paise": x, "sweep_item_id": it.item_id, "to_account": ctx.card.account_id},
            Modifier(redirect_to_card=x, pay_in_full_once_clear=True), schedule,
            [_card_rate_assumption(ctx), _savings_rate_assumption(ctx), PAY_IN_FULL],
            ["HIGH_COST_DEBT_FOUND", "DEBT_GROWING"], confidence_cap="Medium"))
    return out


def _with_sweep(ctx: Context, flows: list[ScheduledFlow], x: int, to_card: bool) -> list[ScheduledFlow]:
    """A payday debit of x from the operating account (and its savings leg, if it goes to savings)."""
    reserve = next(iter(_reserve(ctx)), None)
    adds = []
    for d in _salary_days(ctx):
        adds.append(ScheduledFlow(d, ctx.forecast.account_id, x, "debit", "Payday sweep", "sweep:new", "sweep",
                                  "simulated", False, False, "sweep:new"))
        if not to_card and reserve is not None:
            adds.append(ScheduledFlow(d, reserve.account_id, x, "credit", "Payday sweep", "sweep:new", "sweep",
                                      "simulated", False, False, "sweep:new:in"))
    return sorted(flows + adds, key=lambda f: f.sort_key())


def _size_sweep(ctx: Context, to_card: bool) -> int:
    """D7 with D34: the largest ₹500 step whose payday debit raises mandate bounce risk by <= 2 pp
    (and dip probability by <= 2 pp while the dip metric isn't saturated)."""
    step, limit = ctx.cfg["sweep_step_paise"], ctx.cfg["sweep_max_dip_increase_pp"] / 100
    base_b, base_d = max_mandate_bounce(ctx.forecast), ctx.forecast.dip_probability or 0.0

    def ok(x: int) -> bool:
        fc = _rerun(ctx, _with_sweep(ctx, ctx.forecast.schedule, x, to_card))
        good = max_mandate_bounce(fc) - base_b <= limit + 1e-9
        if not ctx.saturated:
            good = good and (fc.dip_probability or 0.0) - base_d <= limit + 1e-9
        return good

    lo, hi = 0, SWEEP_MAX_STEPS  # monotone: a bigger sweep never lowers bounce risk (same random draws)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ok(mid * step):
            lo = mid
        else:
            hi = mid - 1
    return lo * step


def gen_auto_sweep(ctx: Context) -> list[Action]:
    """D7 + D30(b): while the card revolves, a payday sweep targets the card; otherwise savings."""
    if ctx.forecast.account_id is None or not ctx.forecast.available:
        return []
    to_card = bool(ctx.card and ctx.card.revolving_paise)
    unknown_card_debt = any(c.revolving_paise is None for c in ctx.metrics.cards)
    if not to_card and unknown_card_debt:
        return []  # D30(e): a visible but unlinked card may be revolving; don't push savings before we know
    has_savings_sweep = any(it.kind == "sweep" and it.active for it in ctx.metrics.recurring)
    x = _size_sweep(ctx, to_card)
    if x < 1_000_00:
        return []

    def schedule(flows):
        return _with_sweep(ctx, flows, x, to_card)

    if to_card:
        return [Action("auto_sweep:card", "auto_sweep", f"On payday, send {format_inr(x)} extra to the card",
                       {"amount_paise": x, "target": "card"}, Modifier(extra_to_card_from_surplus=x,
                                                                       pay_in_full_once_clear=True), schedule,
                       [_card_rate_assumption(ctx), _unswept_assumption(ctx), PAY_IN_FULL],
                       ["INCOME_INCREASED", "HIGH_COST_DEBT_FOUND", "DEBT_GROWING"], confidence_cap="Medium")]
    verb = "Increase your payday transfer to savings by" if has_savings_sweep else "On payday, move"
    tail = "" if has_savings_sweep else " to savings"
    return [Action("auto_sweep:savings", "auto_sweep", f"{verb} {format_inr(x)}{tail}",
                   {"amount_paise": x, "target": "savings", "increase": has_savings_sweep},
                   Modifier(sweep_to_savings_from_surplus=x), schedule,
                   [_unswept_assumption(ctx), _savings_rate_assumption(ctx)], ["INCOME_INCREASED"],
                   confidence_cap="Medium")]


def gen_cancel_overlapping_subs(ctx: Context) -> list[Action]:
    out = []
    for ov in ctx.metrics.overlaps:
        items = [i for i in ctx.metrics.recurring if i.merchant_key in ov["merchant_keys"] and i.active]
        keep = min(items, key=lambda i: (i.dates[0] if i.dates else ctx.view.as_of, i.monthly_paise, i.merchant_key))
        cancel = [i for i in items if i is not keep]
        cut = sum(i.monthly_paise for i in cancel)
        ids = {i.item_id for i in cancel}

        def schedule(flows, ids=ids):
            return [f for f in flows if f.item_id not in ids]

        names = ", ".join(i.merchant_name for i in cancel)
        out.append(Action(f"cancel_overlapping_subs:{ov['group']}", "cancel_overlapping_subs",
                          f"Keep {keep.merchant_name}; cancel {names} ({format_inr(cut)} a month)",
                          {"group": ov["group"], "keep": keep.merchant_key, "cancel": [i.merchant_key for i in cancel],
                           "monthly_paise": cut},
                          Modifier(consumption_cut=cut), schedule, [_unswept_assumption(ctx)],
                          ["SUBSCRIPTION_OVERLAP_FOUND"]))
    return out


def gen_change_emi_tenure(ctx: Context) -> list[Action]:
    """Generated when EMI-to-income > 30% or a mandate's bounce risk >= 20% (dip-based trigger withheld, D34)."""
    if not ((ctx.metrics.emi_to_income or 0) > 0.30 or max_mandate_bounce(ctx.forecast) >= 0.20):
        return []
    out = []
    for loan in ctx.inputs.loans:
        if loan.remaining_months is None or loan.remaining_months < 3 or not loan.outstanding_paise:
            continue
        new_n = loan.remaining_months + 12
        new_emi = emi_paise(loan.outstanding_paise, loan.rate_bps, new_n)
        old_total, new_total = loan.emi_paise * loan.remaining_months, new_emi * new_n

        def schedule(flows, key=loan.key, amt=new_emi):
            return [dataclasses.replace(f, amount_paise=amt, source="simulated")
                    if f.kind == "emi" and f.merchant_key == key else f for f in flows]

        out.append(Action(
            f"change_emi_tenure:{loan.key}", "change_emi_tenure",
            f"Extend your {loan.name} loan by 12 months: EMI {format_inr(loan.emi_paise)} → {format_inr(new_emi)}",
            {"loan": loan.key, "extra_months": 12, "old_emi_paise": loan.emi_paise, "new_emi_paise": new_emi},
            Modifier(loan_terms={loan.key: dataclasses.replace(loan, emi_paise=new_emi, remaining_months=new_n)}),
            schedule, [_unswept_assumption(ctx)], ["NEW_EMI_ADDED", "BOUNCE_RISK_UP"],
            extra={"lifetime_cost_paise": new_total - old_total}))
    return out


GENERATORS = (gen_pay_down_card, gen_redirect_sweep, gen_auto_sweep, gen_cancel_overlapping_subs,
              gen_change_emi_tenure)


# ---- what-ifs (D8) ---------------------------------------------------------------------------------------
def what_if_new_emi(ctx: Context, principal_paise: int, tenure_months: int,
                    annual_rate_bps: int | None = None) -> Action:
    rate = annual_rate_bps if annual_rate_bps is not None else ctx.cfg["consumer_emi_rate_bps"]
    emi = emi_paise(principal_paise, rate, tenure_months)
    alt_n = tenure_months + tenure_months // 2 if tenure_months >= 6 else tenure_months + 6
    first_due = (ctx.forecast.next_income_date or ctx.view.as_of) + dt.timedelta(days=5)
    assumptions = [_assumption("emi_rate", f"Interest rate {rate / 100:g}% a year" +
                               ("" if annual_rate_bps is not None else " (assumed; you didn't give one)"),
                               rate / 100, "pct_per_year", "user" if annual_rate_bps is not None else "config"),
                   _assumption("first_emi", "First EMI 5 days after your next salary, then monthly on that date "
                               "(lenders usually let you pick the EMI date)", first_due.isoformat(), "date")]

    def schedule(flows):
        adds, k = [], 0
        while (d := add_months(first_due, k)) <= ctx.view.as_of + dt.timedelta(days=90):
            adds.append(ScheduledFlow(d, ctx.forecast.account_id, emi, "debit", "New EMI", "new_emi", "emi",
                                      "simulated", True, False, "new_emi"))
            k += 1
        return sorted(flows + adds, key=lambda f: f.sort_key())

    return Action("new_emi", "new_emi", f"New EMI of {format_inr(emi)} for {tenure_months} months",
                  {"principal_paise": principal_paise, "tenure_months": tenure_months, "annual_rate_bps": rate},
                  Modifier(new_loans=[Loan("new_emi", "New EMI", principal_paise, emi, rate, tenure_months)]),
                  schedule, assumptions, [], auto=False,
                  extra={"emi_paise": emi, "total_interest_paise": emi * tenure_months - principal_paise,
                         "alt_tenure": {"tenure_months": alt_n, "emi_paise": emi_paise(principal_paise, rate, alt_n)}})


def what_if_delay_purchase(ctx: Context, amount_paise: int, from_date: dt.date, to_date: dt.date) -> Action:
    def schedule(flows):
        adds = [ScheduledFlow(to_date, ctx.forecast.account_id, amount_paise, "debit", "Purchase", "purchase", "other",
                              "simulated", False, False, "purchase:later")]
        return sorted(flows + adds, key=lambda f: f.sort_key())

    def base_schedule(flows):
        adds = [ScheduledFlow(from_date, ctx.forecast.account_id, amount_paise, "debit", "Purchase", "purchase",
                              "other", "simulated", False, False, "purchase:now")]
        return sorted(flows + adds, key=lambda f: f.sort_key())

    return Action("delay_purchase", "delay_purchase",
                  f"Buy on {to_date:%d %b} instead of {from_date:%d %b} ({format_inr(amount_paise)})",
                  {"amount_paise": amount_paise, "from_date": from_date.isoformat(), "to_date": to_date.isoformat()},
                  Modifier(), schedule, [], [], auto=False, extra={"base_schedule_fn": base_schedule})


# ---- evaluation --------------------------------------------------------------------------------------------
def evaluate(ctx: Context, action: Action, rank: int | None = None) -> Recommendation:
    base_fc = ctx.forecast
    if "base_schedule_fn" in action.extra:  # delay_purchase: compare buying later with buying now
        base_fc = _rerun(ctx, action.extra["base_schedule_fn"](ctx.forecast.schedule))
    fc = _rerun(ctx, action.schedule_fn(ctx.forecast.schedule)) if action.schedule_fn else base_fc
    proj = project(ctx.inputs, action.modifier)
    base = ctx.baseline
    card_saved = card_interest_total(base) - card_interest_total(proj)
    loan_saved = loan_interest_total(base) - loan_interest_total(proj)
    savings_foregone = -sum(m.liquid_interest_delta for m in proj.months)
    cut = action.modifier.consumption_cut
    annual = card_saved + loan_saved - savings_foregone + 12 * cut
    ess = max(1, ctx.inputs.essential_paise)
    buffer_now_after = max(0, ctx.inputs.liquid_paise - action.modifier.savings_to_card_now) / ess
    impact = {
        "annual_impact_paise": annual,
        "monthly_cashflow_paise": cut + (action.params.get("old_emi_paise", 0) - action.params.get("new_emi_paise", 0)
                                         if action.type == "change_emi_tenure" else 0)
        - (action.params.get("amount_paise", 0) if action.type == "auto_sweep" else 0)
        - (action.extra.get("emi_paise", 0) if action.type == "new_emi" else 0),
        "card_interest_saved_12m_paise": card_saved,
        "loan_interest_saved_12m_paise": loan_saved,
        "savings_interest_foregone_12m_paise": savings_foregone,
        "lifetime_cost_paise": action.extra.get("lifetime_cost_paise"),
        "score_now": ctx.score.total,
        "score_12m_baseline": base.score_12m.total,
        "score_12m_with_action": proj.score_12m.total,
        "score_delta_12m": proj.score_12m.total - base.score_12m.total,
        "revolving_12m_baseline_paise": base.months[-1].revolving,
        "revolving_12m_with_action_paise": proj.months[-1].revolving,
        "months_to_clear_card": months_to_clear(proj) if ctx.card else None,
        "buffer_months_now": ctx.metrics.buffer_months,
        "buffer_months_now_after": buffer_now_after,
        "buffer_months_12m_baseline": base.metrics_12m.buffer_months,
        "buffer_months_12m_with_action": proj.metrics_12m.buffer_months,
        "bounce_risk_before": max_mandate_bounce(base_fc),
        "bounce_risk_after": max_mandate_bounce(fc),
        "dip_probability_before": base_fc.dip_probability,
        "dip_probability_after": fc.dip_probability,
        "dip_saturated": ctx.saturated,
    }
    if action.type == "new_emi":
        inc = ctx.metrics.income_monthly_paise
        impact["emi_to_income_before"] = ctx.metrics.emi_to_income
        impact["emi_to_income_after"] = (ctx.metrics.emi_monthly_paise + action.extra["emi_paise"]) / inc if inc else None
        new_risk = next((b for b in fc.bounce_risks if b.merchant_key == "new_emi"), None)
        impact["new_emi_bounce_risk"] = new_risk.probability if new_risk else None
    label = ctx.confidence.label
    if action.confidence_cap and LEVELS.index(action.confidence_cap) < LEVELS.index(label):
        label = action.confidence_cap
    conf = {"label": label, "reason": ctx.confidence.reason + (
        "; capped at Medium: depends on a behavioural assumption" if label != ctx.confidence.label else "")}
    extra = {k: v for k, v in action.extra.items() if k != "base_schedule_fn"}
    return Recommendation(action.key, action.type, action.title, rank, action.params, impact, conf,
                          action.assumptions, action.drivers, extra)


def recommend(ctx: Context, top: int = 5) -> tuple[list[Recommendation], dict | None]:
    actions = [a for gen in GENERATORS for a in gen(ctx)]
    evaluated = [evaluate(ctx, a) for a in actions]
    useful = [r for r in evaluated if r.impact["score_delta_12m"] > 0 or r.impact["annual_impact_paise"] > 0
              or r.impact["bounce_risk_after"] < r.impact["bounce_risk_before"]]
    useful.sort(key=lambda r: (-r.impact["score_delta_12m"], -r.impact["annual_impact_paise"], r.action_key))
    ranked = [dataclasses.replace(r, rank=i + 1) for i, r in enumerate(useful[:top])]
    return ranked, combined_plan(ctx, [a for r in ranked[:3] for a in actions if a.key == r.action_key])


def combined_plan(ctx: Context, actions: list[Action]) -> dict | None:
    """The top actions together: one modifier, one schedule, so cash used by one isn't spent twice."""
    if len(actions) < 2:
        return None
    mod = Modifier()
    for a in actions:
        m = a.modifier
        mod.savings_to_card_now += m.savings_to_card_now
        mod.pay_in_full_once_clear = mod.pay_in_full_once_clear or m.pay_in_full_once_clear
        mod.redirect_to_card += m.redirect_to_card
        mod.extra_to_card_from_surplus += m.extra_to_card_from_surplus
        mod.sweep_to_savings_from_surplus += m.sweep_to_savings_from_surplus
        mod.consumption_cut += m.consumption_cut
        mod.loan_terms.update(m.loan_terms)
        mod.new_loans += m.new_loans

    def schedule(flows):
        for a in actions:
            if a.schedule_fn:
                flows = a.schedule_fn(flows)
        return flows

    combo = Action("combined_plan", "combined_plan", "Do the top actions together", {"actions": [a.key for a in actions]},
                   mod, schedule, [x for a in actions for x in a.assumptions], [])
    rec = evaluate(ctx, combo)
    seen, assumptions = set(), []
    for x in rec.assumptions:
        if x["key"] not in seen:
            seen.add(x["key"])
            assumptions.append(x)
    return {"actions": [a.key for a in actions], "impact": rec.impact, "confidence": rec.confidence,
            "assumptions": assumptions}


