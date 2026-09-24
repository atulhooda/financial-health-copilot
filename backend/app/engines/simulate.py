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
from app.engines.schedule import ScheduledFlow, build_schedule
from app.engines.score import Score
from app.engines.view import View

SWEEP_MAX_STEPS = 80  # ₹500 x 80 = ₹40,000: upper bound of the sweep search
EMI_DAYS_AFTER_PAYDAY = 2  # "just after payday" option for a new EMI
LONG_HORIZON = 90  # days: post-clearing bounce check and EMI what-ifs look 2-3 months ahead
BOUNCE_TOLERANCE = 0.02
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
    _base90: Forecast | None = None

    def forecast90(self, schedule_fn=None) -> Forecast:
        """A 90-day forecast (same random draws) for checks beyond the 45-day horizon."""
        start, end = self.view.as_of + dt.timedelta(days=1), self.view.as_of + dt.timedelta(days=LONG_HORIZON)
        sched = build_schedule(self.view, self.metrics.recurring, start, end)
        if schedule_fn is None and self._base90 is not None:
            return self._base90
        fc, _ = run_forecast(self.view, self.metrics.recurring, schedule=schedule_fn(sched) if schedule_fn else sched,
                             horizon=LONG_HORIZON, pools=self.pools)
        if schedule_fn is None:
            self._base90 = fc
        return fc

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
    """D30: floor + scheduled debits due before the next income that the salary account can't already cover.

    Refined in the pre-Phase-5 fixes: right after payday the salary account already holds this month's rent,
    EMI and SIP, so counting them against savings too would double-protect them and block clearing the card."""
    nid = ctx.forecast.next_income_date or ctx.view.as_of
    deposits = {a.account_id for a in ctx.view.of_kind("savings", "current")}
    due = sum(f.amount_paise for f in ctx.forecast.schedule if f.direction == "debit" and f.date < nid
              and f.kind != "card_payment" and f.account_id in deposits)
    op = ctx.view.operating
    covered = max(0, (op.balance_paise or 0) - ctx.metrics.earmarked_loan_paise) if op else 0
    return ctx.view.floor_paise + max(0, due - covered)


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
    statement exceeds what the user usually pays, so the next bill stays at the usual amount. Step 2 is explicit:
    set card autopay to the full statement amount. We show the downside of not doing step 2, cap confidence at
    Medium (it depends on a behaviour change), and check bounce risk 90 days out under pay-in-full; if paying
    the full statement raises it, we say so and keep that much more in savings as a cushion."""
    card = ctx.card
    if not card or not card.revolving_paise:
        return []
    cushion = _cushion(ctx)
    reserve = sum(a.balance_paise for a in _reserve(ctx))
    known = next((f for f in ctx.forecast.schedule if f.kind == "card_payment" and f.spread is None), None)
    usual = known.amount_paise if known else 0
    target = max(card.revolving_paise, (card.statement_balance_paise or 0) - usual) if known else card.revolving_paise
    amount, clears = _pay_down_amount(max(0, reserve - cushion), target, card.revolving_paise)
    if amount < 1_000_00:
        return []
    schedule = _pay_down_schedule(card, amount, clears)
    extra, post = 0, {}
    if clears:  # 90-day bounce check under pay-in-full
        base90, act90 = ctx.forecast90(), ctx.forecast90(schedule)
        before, after = max_mandate_bounce(base90), max_mandate_bounce(act90)
        post = {"horizon_days": LONG_HORIZON, "bounce_risk_before": before, "bounce_risk_after": after,
                "raises": after - before > BOUNCE_TOLERANCE, "extra_cushion_paise": 0}
        if post["raises"]:
            bills = {(f.date, f.item_id): f.amount_paise for f in base90.schedule if f.kind == "card_payment"}
            dearer = [(f.date, f.amount_paise - bills.get((f.date, f.item_id), 0)) for f in act90.schedule
                      if f.kind == "card_payment" and f.amount_paise > bills.get((f.date, f.item_id), 0)]
            extra = -(-sum(d for _, d in dearer) // 500_00) * 500_00
            amount, clears = _pay_down_amount(max(0, reserve - cushion - extra), target, card.revolving_paise)
            schedule = _pay_down_schedule(card, amount, clears, top_ups=dearer, operating=ctx.forecast.account_id)
            post.update(extra_cushion_paise=extra,
                        bounce_risk_after_with_cushion=max_mandate_bounce(ctx.forecast90(schedule)))
    cushion += extra
    downside = _rebuild_downside(ctx, amount) if clears else None
    if clears:
        title = (f"Use {format_inr(amount)} of savings to clear your card, then set card autopay to the full "
                 f"statement amount; keep {format_inr(cushion)} as a cushion")
    else:
        title = f"Use {format_inr(amount)} of savings to pay down your card; keep {format_inr(cushion)}"
    steps = [f"Move {format_inr(amount)} from savings to the card"]
    if clears:
        steps.append("Set card autopay to the full statement amount")
    return [Action(f"pay_down_card:{card.account_id}", "pay_down_card", title,
                   {"amount_paise": amount, "cushion_paise": cushion, "clears": clears, "next_bill_paise": usual,
                    "steps": steps},
                   Modifier(savings_to_card_now=amount, pay_in_full_once_clear=clears), schedule,
                   [_card_rate_assumption(ctx), _savings_rate_assumption(ctx)] + ([PAY_IN_FULL] if clears else []),
                   ["HIGH_COST_DEBT_FOUND", "DEBT_GROWING"], confidence_cap="Medium" if clears else None,
                   extra={"downside": downside, "post_clear_check": post})]


def _pay_down_amount(available: int, target: int, revolving: int) -> tuple[int, bool]:
    if available >= target:  # round UP to the next ₹100 so nothing is left revolving
        amount = min(available, -(-target // 100_00) * 100_00)
    else:
        amount = available // 100_00 * 100_00
    return amount, amount >= revolving


def _pay_down_schedule(card: CardMetrics, amount: int, clears: bool, top_ups=(), operating: str | None = None):
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
        out += [ScheduledFlow(d, operating, x, "credit", "Top-up from savings", "topup", "sweep", "simulated", False,
                              False, f"topup:{d.isoformat()}") for d, x in top_ups if operating]
        return sorted(out, key=lambda f: f.sort_key())
    return schedule


def _rebuild_downside(ctx: Context, amount: int) -> dict:
    """If the user clears the card but goes back to paying their usual share, how fast does it come back?"""
    inp = ctx.inputs
    p, r, purchases = inp.pay_ratio, inp.card_rate_monthly, inp.card_purchases_paise
    steady = int(round((1 - p) * purchases / (1 - (1 - p) * (1 + r)))) if (1 - p) * (1 + r) < 1 else None
    back = project(inp, Modifier(savings_to_card_now=amount, pay_in_full_once_clear=False))
    goal = 0.9 * steady if steady else None
    months = next((x.m for x in back.months if goal is not None and x.revolving >= goal), None)
    return {"pay_share": p, "rebuild_to_paise": steady, "months_to_rebuild": months,
            "revolving_after_12m_paise": back.months[-1].revolving,
            "interest_saved_12m_if_back_to_usual_paise":
                card_interest_total(ctx.baseline) - card_interest_total(back)}


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


def gen_link_account(ctx: Context) -> list[Action]:
    """D30(e): while a known card is visible but unlinked, the top suggestion is a data action."""
    out = []
    for c in ctx.metrics.cards:
        if c.revolving_paise is not None:
            continue
        a = ctx.view.accounts.get(c.account_id)
        if a is None:
            continue
        cov = ctx.metrics.coverage
        out.append(Action(f"link_account:{a.account_id}", "link_account",
                          f"Link your {a.institution.upper()} card statement so we can see its balance",
                          {"account_id": a.account_id, "institution": a.institution, "last4": a.last4},
                          Modifier(), None, [], ["HIGH_COST_DEBT_FOUND"],
                          extra={"data_impact": {"accounts_linked_before": cov.get("accounts_linked"),
                                                 "accounts_linked_after": (cov.get("accounts_linked") or 0) + 1,
                                                 "accounts_known": cov.get("accounts_known"),
                                                 "unlocks": ["card balance carried over", "interest rate",
                                                             "credit utilisation"],
                                                 "lifts_confidence_cap": "not linked" in " ".join(ctx.confidence.caps)}}))
    return out


def what_if_change_emi_tenure(ctx: Context, loan_key: str, extra_months: int = 12) -> Action | None:
    """Borrowing for longer: a what-if (same class as D8), never a recommendation. Extra interest up front."""
    loan = next((x for x in ctx.inputs.loans if x.key == loan_key), None)
    if loan is None or loan.remaining_months is None or loan.remaining_months < 3 or not loan.outstanding_paise:
        return None
    new_n = loan.remaining_months + extra_months
    new_emi = emi_paise(loan.outstanding_paise, loan.rate_bps, new_n)
    extra_interest = new_emi * new_n - loan.emi_paise * loan.remaining_months

    def schedule(flows, key=loan.key, amt=new_emi):
        return [dataclasses.replace(f, amount_paise=amt, source="simulated")
                if f.kind == "emi" and f.merchant_key == key else f for f in flows]

    return Action(
        f"change_emi_tenure:{loan.key}", "change_emi_tenure",
        f"Explore: {format_inr(extra_interest)} more interest to extend your {loan.name} loan by {extra_months} "
        f"months (EMI {format_inr(loan.emi_paise)} → {format_inr(new_emi)}); needs the lender's approval",
        {"loan": loan.key, "extra_months": extra_months, "old_emi_paise": loan.emi_paise, "new_emi_paise": new_emi},
        Modifier(loan_terms={loan.key: dataclasses.replace(loan, emi_paise=new_emi, remaining_months=new_n)}),
        schedule,
        [_unswept_assumption(ctx),
         _assumption("lender_approval", "Needs the lender's approval; a restructuring fee may apply (not included)",
                     True, "flag")],
        [], auto=False, extra={"lifetime_cost_paise": extra_interest})


def what_if_offers(ctx: Context) -> list[Action]:
    """What-ifs the app may OFFER to explore (never rank): tenure extension when a mandate's bounce risk >= 20%."""
    if max_mandate_bounce(ctx.forecast) < 0.20:
        return []
    return [a for a in (what_if_change_emi_tenure(ctx, x.key) for x in ctx.inputs.loans) if a is not None]


GENERATORS = (gen_link_account, gen_pay_down_card, gen_redirect_sweep, gen_auto_sweep, gen_cancel_overlapping_subs)


# ---- what-ifs (D8) ---------------------------------------------------------------------------------------
def what_if_new_emi(ctx: Context, principal_paise: int, tenure_months: int,
                    annual_rate_bps: int | None = None) -> Action:
    """A new EMI (D8 what-if). The first-EMI date is not baked in: both options are evaluated (just after payday
    vs 30 days from today) and the gap becomes advice: ask for an EMI date right after your salary date."""
    rate = annual_rate_bps if annual_rate_bps is not None else ctx.cfg["consumer_emi_rate_bps"]
    emi = emi_paise(principal_paise, rate, tenure_months)
    alt_n = tenure_months + tenure_months // 2 if tenure_months >= 6 else tenure_months + 6
    after_payday = (ctx.forecast.next_income_date or ctx.view.as_of) + dt.timedelta(days=EMI_DAYS_AFTER_PAYDAY)
    default_date = ctx.view.as_of + dt.timedelta(days=30)
    assumptions = [_assumption("emi_rate", f"Interest rate {rate / 100:g}% a year" +
                               ("" if annual_rate_bps is not None else " (assumed; you didn't give one)"),
                               rate / 100, "pct_per_year", "user" if annual_rate_bps is not None else "config")]
    return Action("new_emi", "new_emi", f"New EMI of {format_inr(emi)} for {tenure_months} months",
                  {"principal_paise": principal_paise, "tenure_months": tenure_months, "annual_rate_bps": rate},
                  Modifier(new_loans=[Loan("new_emi", "New EMI", principal_paise, emi, rate, tenure_months)]),
                  _emi_schedule(ctx, emi, after_payday), assumptions, [], auto=False,
                  extra={"emi_paise": emi, "total_interest_paise": emi * tenure_months - principal_paise,
                         "alt_tenure": {"tenure_months": alt_n, "emi_paise": emi_paise(principal_paise, rate, alt_n)},
                         "date_options": [("just_after_payday", after_payday), ("30_days_from_today", default_date)]})


def _emi_schedule(ctx: Context, emi: int, first_due: dt.date):
    def schedule(flows):
        adds, k = [], 0
        while (d := add_months(first_due, k)) <= ctx.view.as_of + dt.timedelta(days=LONG_HORIZON):
            adds.append(ScheduledFlow(d, ctx.forecast.account_id, emi, "debit", "New EMI", "new_emi", "emi",
                                      "simulated", True, False, "new_emi"))
            k += 1
        return sorted(flows + adds, key=lambda f: f.sort_key())
    return schedule


def _emi_date_options(ctx: Context, action: Action) -> dict:
    base = ctx.forecast90()
    options = []
    for label, first_due in action.extra["date_options"]:
        fc = ctx.forecast90(_emi_schedule(ctx, action.extra["emi_paise"], first_due))
        own = max([b.probability for b in fc.bounce_risks if b.merchant_key == "new_emi"] or [0.0])
        options.append({"option": label, "first_due": first_due, "new_emi_bounce_risk": own,
                        "max_mandate_bounce_risk": max_mandate_bounce(fc)})
    good, default = options[0], options[1]
    advice = None
    if default["new_emi_bounce_risk"] - good["new_emi_bounce_risk"] >= 0.05:
        advice = {"text": "Ask for an EMI date right after your salary date",
                  "bounce_risk_after_payday": good["new_emi_bounce_risk"],
                  "bounce_risk_30_days_from_today": default["new_emi_bounce_risk"]}
    return {"options": options, "advice": advice, "bounce_risk_before_90d": max_mandate_bounce(base)}


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
    if action.type == "link_account":  # a data action: its impact is on what we can see, not on money
        return Recommendation(action.key, action.type, action.title, rank, action.params,
                              {"kind": "data", **action.extra["data_impact"]},
                              {"label": "High", "reason": "linking only adds data"}, [], action.drivers, {})
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
    rev = ctx.inputs.revolving_paise or 0
    moved = action.modifier.savings_to_card_now
    buffer_now_after = max(0, ctx.inputs.liquid_paise - moved - (rev - min(moved, rev))) / ess
    impact = {
        "kind": "simulation",
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
        impact["emi_dates"] = _emi_date_options(ctx, action)
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
    extra = {k: v for k, v in action.extra.items() if k not in ("base_schedule_fn", "date_options")}
    return Recommendation(action.key, action.type, action.title, rank, action.params, impact, conf,
                          action.assumptions, action.drivers, extra)


def recommend(ctx: Context, top: int = 5) -> tuple[list[Recommendation], dict | None, list[Recommendation]]:
    """(ranked recommendations, combined plan, what-if offers). Data actions come first (D30e)."""
    actions = [a for gen in GENERATORS for a in gen(ctx)]
    data = [evaluate(ctx, a) for a in actions if a.type == "link_account"]
    sims = [a for a in actions if a.type != "link_account"]
    evaluated = [evaluate(ctx, a) for a in sims]
    useful = [r for r in evaluated if r.impact["score_delta_12m"] > 0 or r.impact["annual_impact_paise"] > 0
              or r.impact["bounce_risk_after"] < r.impact["bounce_risk_before"]]
    useful.sort(key=lambda r: (-r.impact["score_delta_12m"], -r.impact["annual_impact_paise"], r.action_key))
    ranked = [dataclasses.replace(r, rank=i + 1) for i, r in enumerate((data + useful)[:top])]
    top_sims = [a for r in ranked if r.type != "link_account" for a in sims if a.key == r.action_key][:3]
    offers = [evaluate(ctx, a) for a in what_if_offers(ctx)]
    return ranked, combined_plan(ctx, top_sims), offers


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


