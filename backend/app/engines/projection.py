"""12-month deterministic roll-forward (SPEC §6.5). Every projected number is a RECOMMENDATION output (D10).

Baseline = the user's observed monthly behaviour carried forward:
- own liquid money changes by its observed monthly trend;
- the card follows its observed dynamics: statement = revolving x (1 + implied monthly rate) + monthly purchases,
  the user pays their observed share of it, the rest revolves;
- loans amortise on reducing balance; an EMI that ends frees cash.
An action run alongside the baseline, month by month, changes only what it touches. Cash the action frees is
subject to D26: unless swept, `unswept_spend_share` of it is assumed spent, the rest stays as liquid.
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field

from app.engines.financial import Metrics
from app.engines.score import Score, compute_score

MONTHS = 12


@dataclass
class Loan:
    key: str
    name: str
    outstanding_paise: int
    emi_paise: int
    rate_bps: int
    remaining_months: int | None  # None: unknown (a detected EMI with no linked loan): runs for the whole horizon

    def step(self) -> tuple[int, int]:
        """One month: returns (emi paid, interest part). Mutates the loan."""
        if self.emi_paise == 0 or (self.remaining_months is not None and self.remaining_months <= 0):
            return 0, 0
        interest = int(round(self.outstanding_paise * self.rate_bps / 10000 / 12))
        pay = min(self.emi_paise, self.outstanding_paise + interest) if self.remaining_months is not None else self.emi_paise
        self.outstanding_paise = max(0, self.outstanding_paise + interest - pay)
        if self.remaining_months is not None:
            self.remaining_months -= 1
            if self.outstanding_paise == 0:
                self.remaining_months = 0
        return pay, interest


@dataclass
class ProjectionInputs:
    income_paise: int
    spend_paise: int  # current monthly spend (includes EMIs, card interest, fees)
    essential_paise: int
    emi_paise: int
    liquid_paise: int  # own liquid (loan cash earmarked out, D29/D33)
    liquid_trend_paise: int  # observed monthly change in own liquid
    revolving_paise: int | None  # None: no linked card
    card_rate_monthly: float  # implied by the statement (FACT) or the configured assumption
    card_purchases_paise: int
    pay_ratio: float
    card_limit_paise: int | None
    loans: list[Loan]
    savings_rate_monthly: float  # assumption
    unswept_share: float  # assumption (D25/D26)
    metrics: Metrics  # for pillars we don't project (pre-salary liquidity, stability)


@dataclass
class Modifier:
    """What an action changes. All amounts per month unless the name says otherwise."""

    savings_to_card_now: int = 0  # one-off, from savings, before the next statement
    pay_in_full_once_clear: bool = False  # assumption attached to clearing actions
    redirect_to_card: int = 0  # an existing savings sweep sent to the card while it revolves
    extra_to_card_from_surplus: int = 0  # a new card sweep funded from money that would partly be spent
    sweep_to_savings_from_surplus: int = 0  # a new savings sweep (debt-free users only)
    consumption_cut: int = 0  # e.g. cancelled subscriptions
    loan_terms: dict[str, Loan] = field(default_factory=dict)  # replaced loan terms (tenure change)
    new_loans: list[Loan] = field(default_factory=list)  # what-if EMIs


@dataclass
class Month:
    m: int
    liquid: int
    revolving: int | None
    card_interest: int
    card_payment: int
    emi: int
    loan_interest: int
    consumption_delta: int  # spend reduced by the action this month (for the savings rate)
    liquid_interest_delta: int = 0  # savings interest gained (+) or foregone (-) vs the baseline this month


@dataclass
class Projection:
    months: list[Month]
    score_12m: Score
    metrics_12m: Metrics


def _card_month(r: int, rate: float, purchases: int, ratio: float, extra: int) -> tuple[int, int, int, int]:
    """One statement cycle: (interest, routine payment, extra payment actually used, next revolving)."""
    interest = int(round(r * rate))
    statement = r + interest + purchases
    routine = min(statement, int(round(ratio * statement)))
    extra_used = min(extra, statement - routine)
    return interest, routine, extra_used, statement - routine - extra_used


def project(inp: ProjectionInputs, mod: Modifier | None = None) -> Projection:
    """Baseline (mod=None) or an action, run in lockstep with the baseline so every difference is attributable."""
    base_loans = [dataclasses.replace(x) for x in inp.loans]
    act_loans = [dataclasses.replace(mod.loan_terms.get(x.key, x)) if mod else dataclasses.replace(x) for x in inp.loans]
    if mod:
        act_loans += [dataclasses.replace(x) for x in mod.new_loans]
    s = inp.unswept_share
    rb = ra = inp.revolving_paise
    liquid_b = liquid_a = inp.liquid_paise
    full_mode = False
    if mod and mod.savings_to_card_now and ra is not None:
        paid = min(mod.savings_to_card_now, ra, max(0, liquid_a))
        ra -= paid
        liquid_a -= paid
        full_mode = mod.pay_in_full_once_clear and ra == 0
    months: list[Month] = []
    for m in range(1, MONTHS + 1):
        emi_b = sum(x.step()[0] for x in base_loans)
        steps_a = [x.step() for x in act_loans]
        emi_a, loan_int_a = sum(p for p, _ in steps_a), sum(i for _, i in steps_a)
        ci_a = routine_b = routine_a = redirect_used = surplus_used = 0
        if rb is not None:
            _, routine_b, _, rb = _card_month(rb, inp.card_rate_monthly, inp.card_purchases_paise, inp.pay_ratio, 0)
            redirect = mod.redirect_to_card if mod and ra > 0 else 0
            surplus = mod.extra_to_card_from_surplus if mod and ra > 0 else 0
            ci_a, routine_a, extra_used, ra = _card_month(ra, inp.card_rate_monthly, inp.card_purchases_paise,
                                                          1.0 if full_mode else inp.pay_ratio, redirect + surplus)
            redirect_used = min(extra_used, redirect)
            surplus_used = extra_used - redirect_used
            if mod and mod.pay_in_full_once_clear and ra == 0:
                full_mode = True
        # routine differences free (or cost) cash: D26 applies to freed cash; explicit moves are separate
        freed = (routine_b - routine_a) + (emi_b - emi_a) + (mod.consumption_cut if mod else 0)
        delta = inp.liquid_trend_paise + (int(round(freed * (1 - s))) if freed > 0 else freed)
        delta -= redirect_used + int(round(surplus_used * (1 - s)))
        consumption_delta = (mod.consumption_cut if mod else 0) + int(round(surplus_used * s))
        if mod and mod.sweep_to_savings_from_surplus:
            delta += int(round(mod.sweep_to_savings_from_surplus * s))
            consumption_delta += int(round(mod.sweep_to_savings_from_surplus * s))
        liquid_b += inp.liquid_trend_paise
        liquid_a += delta
        interest_delta = int(round((liquid_a - liquid_b) * inp.savings_rate_monthly))  # gained or foregone
        liquid_a += interest_delta
        months.append(Month(m, liquid_a, ra, ci_a, routine_a + redirect_used + surplus_used, emi_a, loan_int_a,
                            consumption_delta, interest_delta))
    return Projection(months, *_score_at(inp, months[-1]))


def _score_at(inp: ProjectionInputs, last: Month) -> tuple[Score, Metrics]:
    m0 = inp.metrics
    card_interest_now = int(round((inp.revolving_paise or 0) * inp.card_rate_monthly))
    spend = inp.spend_paise - card_interest_now - inp.emi_paise + last.card_interest + last.emi - last.consumption_delta
    essential = max(1, inp.essential_paise - inp.emi_paise + last.emi)
    util, revolving = m0.credit_utilisation, m0.revolving_paise
    if last.revolving is not None and inp.card_limit_paise:
        statement = last.revolving + int(round(last.revolving * inp.card_rate_monthly)) + inp.card_purchases_paise
        util, revolving = statement / inp.card_limit_paise, last.revolving
    income = inp.income_paise
    m12 = dataclasses.replace(
        m0, spend_monthly_paise=spend, savings_rate=(income - spend) / income if income else None,
        buffer_months=max(0, last.liquid) / essential, emi_monthly_paise=last.emi,
        emi_to_income=last.emi / income if income else None, credit_utilisation=util, revolving_paise=revolving)
    return compute_score(m12), m12


def months_to_clear(p: Projection) -> int | None:
    return next((x.m for x in p.months if x.revolving == 0), None)


def card_interest_total(p: Projection) -> int:
    return sum(x.card_interest for x in p.months)


def loan_interest_total(p: Projection) -> int:
    return sum(x.loan_interest for x in p.months)


def ceil_to(amount: int, step: int) -> int:
    return int(math.ceil(amount / step) * step)
