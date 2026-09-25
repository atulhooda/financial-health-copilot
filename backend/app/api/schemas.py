"""API v1 shapes (docs/API.md). Frozen with docs/openapi.json at the end of Phase 6: the Flutter app builds on these.

Money is always a Quantity: integer paise plus a display string the app shows as is. Every labelled item carries its
registry-style id (F…, P…, R…, A…), and every number in an item's `text` is one of the response's values.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.ingest.manual import ManualTxn
from app.ingest.sms import StructuredSmsTxn

Lang = Literal["en", "hi", "hinglish"]


class Quantity(BaseModel):
    value: int | float | str | None = Field(description="paise (int) for money, percent units for pct, "
                                                        "ISO date for date")
    unit: Literal["paise", "pct", "months", "days", "years", "count", "score", "date"]
    display: str = Field(description="pre-formatted; the app never formats money itself")


class Confidence(BaseModel):
    label: Literal["High", "Medium", "Low"]
    reason: str = Field(description="shown next to the label, e.g. 'band held on 64% of past days, target 80%'")
    coverage: Quantity | None = Field(None, description="measured P10-P90 coverage: for the 'why trust this' "
                                                        "sheet only, never next to a prediction")
    caps: list[str] = []
    origins: int = 0
    days: int = 0
    explain: str = ""


class Assumption(BaseModel):
    id: str
    key: str
    text: str
    value: Quantity | None = Field(None, description="null for a yes/no assumption such as 'you pay in full'")
    source: Literal["config", "statement", "user"]


class Fact(BaseModel):
    id: str
    key: str
    title: str
    value: Quantity | None
    text: str


class Impact(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: Literal["simulation", "data"]
    annual_impact: Quantity | None = None
    monthly_cashflow: Quantity | None = None
    card_interest_saved_12m: Quantity | None = None
    loan_interest_saved_12m: Quantity | None = None
    savings_interest_foregone_12m: Quantity | None = None
    lifetime_cost: Quantity | None = None
    score_now: Quantity | None = None
    score_12m_baseline: Quantity | None = None
    score_12m_with_action: Quantity | None = None
    score_delta_12m: Quantity | None = None
    months_to_clear_card: Quantity | None = None
    buffer_months_now_after: Quantity | None = None
    buffer_months_12m_baseline: Quantity | None = None
    buffer_months_12m_with_action: Quantity | None = None
    bounce_risk_before: Quantity | None = None
    bounce_risk_after: Quantity | None = None
    dip_probability_before: Quantity | None = None
    dip_probability_after: Quantity | None = None
    dip_saturated: bool | None = None
    accounts_linked_before: Quantity | None = Field(None, description="data actions (D40)")
    accounts_linked_after: Quantity | None = None
    accounts_known: Quantity | None = None
    unlocks: list[str] | None = None


class Prediction(BaseModel):
    id: str
    key: str
    title: str
    value: Quantity | None
    confidence: Confidence
    text: str
    what_if: bool = Field(False, description="a conditional prediction: a what-if the user asked about or one "
                                             "Hisaab offers to explore (D10); `text` states its condition")
    impact: Impact | None = None
    assumptions: list[Assumption] = []


class Recommendation(BaseModel):
    id: str
    action_key: str
    type: str
    rank: int | None
    title: str
    text: str = Field(description="the action, its simulated impact and its assumptions, in the chosen language")
    impact: Impact
    confidence: Confidence
    assumptions: list[Assumption] = []
    params: dict[str, Any] = {}
    extra: dict[str, Any] = {}


class Meta(BaseModel):
    engine_version: str
    scoring_version: int
    language: Lang
    generated_at: str


class Envelope[T](BaseModel):
    user_id: str
    snapshot_id: str
    as_of: dt.date
    facts: list[Fact]
    predictions: list[Prediction]
    recommendations: list[Recommendation]
    data: T
    meta: Meta


# ---- endpoint data ------------------------------------------------------------------------------------------
class Pillar(BaseModel):
    key: str
    title: str
    weight: int
    value: Quantity | None
    pillar_score: float | None
    contribution: int
    status: str
    note: str | None = None


class AccountOut(BaseModel):
    account_id: str
    kind: str
    institution: str
    masked: str
    status: Literal["linked", "sms_only", "known_unlinked"]
    role: str | None


class Alert(BaseModel):
    kind: str
    ref: str | None = Field(description="the id of the labelled item it points to")


class HomeData(BaseModel):
    score: Quantity
    band: Literal["Poor", "Fair", "Good"]
    score_coverage: Quantity
    pillars: list[Pillar]
    top_drag: str | None
    top_alert: Alert | None
    linked_accounts: int
    visible_accounts: int
    known_accounts: int
    accounts: list[AccountOut]


class CategoryOut(BaseModel):
    category: str
    title: str
    amount: Quantity
    share: Quantity | None
    usual_month: Quantity | None
    vs_usual: Quantity | None
    drifting: bool


class RecurringOut(BaseModel):
    merchant: str
    kind: str
    cadence: str
    direction: str
    amount: Quantity
    next_due: dt.date | None


class OverlapOut(BaseModel):
    group: str
    members: list[str]
    monthly_total: Quantity


class InsightsData(BaseModel):
    spending: dict[str, Any] = Field(description="{period, categories: [CategoryOut], top_merchants: "
                                                 "[{merchant, amount}]}")
    recurring: list[RecurringOut]
    overlaps: list[OverlapOut]
    ratios: dict[str, Quantity | None]


class SeriesPoint(BaseModel):
    date: dt.date
    p10: int
    p50: int
    p90: int


class BounceRiskOut(BaseModel):
    name: str
    kind: str
    mandate: bool
    due_date: dt.date
    amount: Quantity
    probability: Quantity


class ScheduledOut(BaseModel):
    date: dt.date
    name: str
    kind: str
    direction: str
    amount: Quantity
    source: str
    estimated: bool


class ForecastData(BaseModel):
    available: bool
    horizon_days: int | None = None
    floor: Quantity | None = None
    account: dict[str, Any] | None = None
    opening: Quantity | None = None
    next_income_date: dt.date | None = None
    income_basis: str | None = None
    series: list[SeriesPoint] = Field([], description="paise, end of day; negative = shortfall")
    dip_probability: Quantity | None = None
    dip_saturated: bool | None = None
    likely_dip_date: dt.date | None = None
    projected_low: Quantity | None = None
    pre_income: dict[str, Quantity] | None = None
    bounce_risks: list[BounceRiskOut] = []
    scheduled: list[ScheduledOut] = []
    assumptions: list[dict[str, Any]] = []
    dip_probability_if_kept: Quantity | None = None
    confidence: Confidence | None = None
    backtest: dict[str, Any] | None = None


class RecommendationsData(BaseModel):
    ranked: list[str]
    combined_plan: dict[str, Any] | None
    what_if_offers: list[str] = Field(description="ids of the conditional predictions that are explore-only offers")


class SnapshotOut(BaseModel):
    snapshot_id: str
    seq: int
    trigger: str
    as_of: dt.date
    created_at: str
    score: int
    band: str
    dip_probability: Quantity | None
    max_mandate_bounce: Quantity | None
    top_recs: list[str]


class TimelineData(BaseModel):
    snapshots: list[SnapshotOut]
    diffs: list[dict[str, Any]]


class SimulateData(BaseModel):
    kind: Literal["recommendation", "what_if"]
    emi: Quantity | None = None
    alt_tenure: dict[str, Quantity] | None = None
    total_interest: Quantity | None = None
    emi_to_income_after: Quantity | None = None
    emi_dates: dict[str, Any] | None = None


# ---- requests ------------------------------------------------------------------------------------------------
class SimulateAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["new_emi", "delay_purchase", "change_emi_tenure", "pay_down_card", "redirect_sweep", "auto_sweep",
                  "cancel_overlapping_subs"]
    params: dict[str, Any] = Field({}, description="new_emi {principal_paise, tenure_months, annual_rate_bps?} · "
                                                   "delay_purchase {amount_paise} · change_emi_tenure {loan?} · "
                                                   "the other types take none (they simulate your own "
                                                   "recommendation of that type)")


class SimulateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: SimulateAction


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=1000)


class SmsIngest(BaseModel):
    """Structured only: the phone parses SMS on device. A raw `body` or `text` field is rejected (422)."""

    model_config = ConfigDict(extra="forbid")

    transactions: list[StructuredSmsTxn] = Field(min_length=1, max_length=2000)


class ManualIngest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transactions: list[ManualTxn] = Field(min_length=1, max_length=2000)


class LoanCashRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    use: Literal["reserve", "purpose"]


class MerchantRuleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str
    merchant_key: str | None = None
    txn_id: str | None = None


# ---- other responses -----------------------------------------------------------------------------------------
class IngestResponse(BaseModel):
    ingest_id: str
    raw_count: int
    canonical_new: int
    duplicates_merged: int
    event_id: str | None
    snapshot_id: str | None = Field(None, description="set when ?wait=true and the recompute finished")


class RecomputeResponse(BaseModel):
    event_id: str | None
    snapshot_id: str | None = None


class EraseResponse(BaseModel):
    erased: dict[str, int]
    receipt_id: str
    erased_at: str


class ReplayResponse(BaseModel):
    step: str
    snapshot_id: str
    as_of: dt.date
    diff: dict[str, Any] | None


class HealthResponse(BaseModel):
    db: bool
    redis: bool | None
    llm_provider: str
    demo_mode: bool
    engine_version: str


class AskStatement(BaseModel):
    text: str
    refs: list[str]


class AskResponse(BaseModel):
    ask_id: str
    user_id: str
    as_of: str | None
    language: Lang
    path: Literal["llm", "llm_retry", "template", "guard"]
    fallback_reason: str | None
    guard: str | None
    checkin: str | None
    message: str | None
    suggestions: list[str]
    facts: list[AskStatement]
    predictions: list[AskStatement]
    recommendations: list[AskStatement]
    sources: dict[str, dict[str, str]]
    trace: dict[str, Any] | None = None


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}


class ErrorResponse(BaseModel):
    error: ErrorBody
