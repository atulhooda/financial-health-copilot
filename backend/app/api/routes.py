"""API v1 routes (docs/API.md). Reads are side-effect free; writes commit, then publish a recompute event."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, File, Form, Query, UploadFile
from sqlalchemy import select, text

from app.api import present
from app.api.deps import ApiError, LangQ, Svc, User
from app.api.present import Presenter
from app.api.schemas import AskRequest as AskBody
from app.api.schemas import (
    AskResponse,
    Envelope,
    EraseResponse,
    ErrorResponse,
    ForecastData,
    HealthResponse,
    HomeData,
    IngestResponse,
    InsightsData,
    LoanCashRequest,
    ManualIngest,
    MerchantRuleRequest,
    RecommendationsData,
    RecomputeResponse,
    ReplayResponse,
    SimulateData,
    SimulateRequest,
    SmsIngest,
    TimelineData,
)
from app.copilot.masking import Masker
from app.copilot.tools import ToolError
from app.core.ids import stable_id
from app.db.models import Snapshot, SnapshotDiff, Transaction
from app.db.repo import UserRepo
from app.events.snapshots import latest_snapshot

router = APIRouter(prefix="/v1")
ERRORS: dict[int | str, dict[str, Any]] = {s: {"model": ErrorResponse} for s in (400, 404, 409, 422, 503)}
Wait = Annotated[bool, Query(description="wait (up to a recompute) for the new snapshot, as the demo does")]


def _presenter(svc, user: str, lang: str, simulate: bool = False) -> Presenter:
    from app.copilot.orchestrator import make_simulator

    with svc.sf()() as s:
        snap = latest_snapshot(s, user)
        if snap is None:
            raise ApiError(404, "NO_SNAPSHOT", f"no data for {user!r} yet: ingest something first")
        diff = UserRepo(s, user).get(SnapshotDiff, to_snapshot_id=snap.snapshot_id)
        masker = Masker.for_user(s, user)
    sim = make_simulator(svc.sf(), svc.get_categoriser(), user, snap) if simulate else None
    return Presenter(user, snap, diff.payload if diff else None, lang, masker, sim)


def _recompute(svc, wait: bool) -> str | None:
    """Drain the bus now (demo / ?wait=true); the worker does it otherwise."""
    if not wait:
        return None
    from app.events.worker import process_available

    done = process_available(svc.get_bus(), svc.sf(), svc.get_categoriser(), svc.get_clock())
    return done[-1].snapshot.snapshot_id if done else None


# ---- reads -----------------------------------------------------------------------------------------------------
@router.get("/home", response_model=Envelope[HomeData], responses=ERRORS, tags=["read"])
def home(user: User, lang: LangQ, svc: Svc):
    """Score, band and pillars (facts), the top EMI bounce risk and dip chance (predictions), the top action."""
    return present.home(_presenter(svc, user, lang))


@router.get("/insights", response_model=Envelope[InsightsData], responses=ERRORS, tags=["read"])
def insights(user: User, lang: LangQ, svc: Svc):
    """Spending by category with drift, recurring items, overlapping subscriptions and ratios (all facts)."""
    return present.insights(_presenter(svc, user, lang))


@router.get("/forecast", response_model=Envelope[ForecastData], responses=ERRORS, tags=["read"])
def forecast(user: User, lang: LangQ, svc: Svc):
    """The 45-day forecast: bands, dip chance, bounce risks, confidence and backtest (predictions)."""
    return present.forecast(_presenter(svc, user, lang))


@router.get("/recommendations", response_model=Envelope[RecommendationsData], responses=ERRORS, tags=["read"])
def recommendations(user: User, lang: LangQ, svc: Svc):
    """Ranked actions (recommendations), the combined plan, and explore-only what-ifs (conditional predictions)."""
    return present.recommendations(_presenter(svc, user, lang))


@router.get("/timeline", response_model=Envelope[TimelineData], responses=ERRORS, tags=["read"])
def timeline(user: User, lang: LangQ, svc: Svc):
    """Every snapshot and every diff with its reason codes and recommendation changes."""
    pr = _presenter(svc, user, lang)
    with svc.sf()() as s:
        repo = UserRepo(s, user)
        snaps = repo.select(Snapshot, order_by=(Snapshot.seq,))
        diffs = repo.select(SnapshotDiff)
    order = {x.snapshot_id: x.seq for x in snaps}
    return present.timeline(pr, snaps, sorted(diffs, key=lambda d: order.get(d.to_snapshot_id, 0)))


@router.post("/simulate", response_model=Envelope[SimulateData], responses=ERRORS, tags=["read"])
def simulate(body: SimulateRequest, user: User, lang: LangQ, svc: Svc):
    """Simulate a what-if (a conditional prediction) or one of Hisaab's own action types (a recommendation).
    Nothing is stored."""
    a, params = body.action, body.action.params
    if a.type == "new_emi":
        p = params.get("principal_paise")
        n = params.get("tenure_months", 12)
        if not isinstance(p, int) or p <= 0 or p % 100:
            raise ApiError(422, "BAD_PARAMS", "principal_paise must be a positive whole-rupee amount in paise")
        if not isinstance(n, int) or not 1 <= n <= 120:
            raise ApiError(422, "BAD_PARAMS", "tenure_months must be 1 to 120")
        bps = params.get("annual_rate_bps")
        if bps is not None and (not isinstance(bps, int) or not 0 <= bps <= 6000):
            raise ApiError(422, "BAD_PARAMS", "annual_rate_bps must be 0 to 6000")
    if a.type == "delay_purchase":
        amt = params.get("amount_paise")
        if not isinstance(amt, int) or amt <= 0 or amt % 100:
            raise ApiError(422, "BAD_PARAMS", "amount_paise must be a positive whole-rupee amount in paise")
    pr = _presenter(svc, user, lang, simulate=True)
    try:
        return present.simulate(pr, a.model_dump())
    except ToolError as e:
        raise ApiError(422, "SIMULATION_ERROR", str(e)) from e


@router.post("/ask", response_model=AskResponse, responses=ERRORS, tags=["copilot"])
def ask(body: AskBody, user: User, svc: Svc, debug: Annotated[bool, Query()] = False):
    """The copilot (docs/COPILOT.md): labelled statements, every number traceable to `sources`."""
    from app.copilot.orchestrator import Copilot

    llm, why = svc.get_llm()
    ans = Copilot(svc.sf(), svc.get_categoriser(), llm, clock=svc.get_clock()).ask(user, body.message, debug=debug)
    grouped = {k: [{"text": s["text"], "refs": s["refs"]} for s in ans.statements if s["label"] == label]
               for k, label in (("facts", "FACT"), ("predictions", "PREDICTION"),
                                ("recommendations", "RECOMMENDATION"))}
    return {"ask_id": ans.ask_id, "user_id": ans.user_id, "as_of": ans.as_of, "language": ans.language,
            "path": ans.path, "fallback_reason": ans.fallback_reason or (why if ans.path == "template" else None),
            "guard": ans.guard, "checkin": ans.checkin, "message": ans.message, "suggestions": ans.suggestions,
            **grouped, "sources": {k: {kk: str(vv) for kk, vv in v.items()} for k, v in ans.sources.items()},
            "trace": ans.trace}


# ---- writes -----------------------------------------------------------------------------------------------------
def _ingest(svc, user: str, source: str, payload: dict, wait: bool) -> dict:
    from app.ingest.base import IngestError
    from app.services import ingest_and_publish

    try:
        r = ingest_and_publish(svc.sf(), svc.get_bus(), user, source, payload, svc.get_categoriser(),
                               svc.get_clock(), f"api:{source}")
    except (IngestError, KeyError, ValueError) as e:
        raise ApiError(422, "INGEST_ERROR", str(e)) from e
    return {"ingest_id": r.ingest_id, "raw_count": r.raw_count, "canonical_new": r.raw_new,
            "duplicates_merged": r.duplicates_merged, "event_id": r.event_id, "snapshot_id": _recompute(svc, wait)}


@router.post("/ingest/aa", status_code=202, response_model=IngestResponse, responses=ERRORS, tags=["ingest"])
def ingest_aa(user: User, svc: Svc, payload: Annotated[dict[str, Any], Body(description="the AA mock envelope "
                                                                                          "(SPEC §4)")],
              wait: Wait = False):
    return _ingest(svc, user, "aa", payload, wait)


@router.post("/ingest/statement", status_code=202, response_model=IngestResponse, responses={**ERRORS, 501: {
    "model": ErrorResponse}}, tags=["ingest"])
async def ingest_statement(user: User, svc: Svc, file: Annotated[UploadFile, File(description="CSV statement")],
                           bank: Annotated[str, Form(description="config/banks/<bank>.yaml")],
                           account_masked: Annotated[str, Form(description="e.g. XX4321")], wait: Wait = False):
    if (file.filename or "").lower().endswith(".pdf") or file.content_type == "application/pdf":
        raise ApiError(501, "PDF_NOT_SUPPORTED", "PDF import is stubbed; upload the CSV export")
    content = (await file.read()).decode("utf-8-sig")
    return _ingest(svc, user, "statement", {"bank": bank, "account_masked": account_masked, "content": content}, wait)


@router.post("/ingest/sms", status_code=202, response_model=IngestResponse, responses=ERRORS, tags=["ingest"])
def ingest_sms(body: SmsIngest, user: User, svc: Svc, wait: Wait = False):
    """Structured transactions only: raw SMS never leave the phone (a `body` or `text` field is a 422)."""
    return _ingest(svc, user, "sms", {"transactions": [t.model_dump(mode="json") for t in body.transactions]}, wait)


@router.post("/ingest/manual", status_code=202, response_model=IngestResponse, responses=ERRORS, tags=["ingest"])
def ingest_manual(body: ManualIngest, user: User, svc: Svc, wait: Wait = False):
    return _ingest(svc, user, "manual", {"transactions": [t.model_dump(mode="json") for t in body.transactions]},
                   wait)


def _publish_recompute(svc, user: str, kind: str, trigger: str) -> str:
    with svc.sf()() as s:
        snap = latest_snapshot(s, user)
    as_of = snap.as_of if snap else svc.get_clock().today()  # same data horizon as what the user is looking at
    return svc.get_bus().publish({"type": kind, "user_id": user, "as_of": as_of.isoformat(), "trigger": trigger})


@router.post("/loan-cash/{txn_id}", response_model=RecomputeResponse, responses=ERRORS, tags=["preferences"])
def loan_cash(txn_id: str, body: LoanCashRequest, user: User, svc: Svc, wait: Wait = False):
    """D33: 'reserve' lifts the loan-cash earmark in the buffer and the forecast; 'purpose' restores it."""
    from app.ingest.service import set_loan_cash_use

    with svc.sf()() as s:
        try:
            set_loan_cash_use(s, user, txn_id, body.use, svc.get_clock())
        except ValueError as e:
            raise ApiError(422, "NOT_A_LOAN_DISBURSAL", str(e)) from e
        s.commit()
    eid = _publish_recompute(svc, user, "user.preference_changed", f"user:loan_cash:{body.use}")
    return {"event_id": eid, "snapshot_id": _recompute(svc, wait)}


@router.post("/merchant-rules", response_model=RecomputeResponse, responses=ERRORS, tags=["preferences"])
def merchant_rule(body: MerchantRuleRequest, user: User, svc: Svc, wait: Wait = False):
    """A user correction (D21): the category applies to every past and future transaction of that merchant."""
    from app.ingest.service import set_merchant_rule

    with svc.sf()() as s:
        key = body.merchant_key
        if key is None and body.txn_id:
            t = s.scalars(select(Transaction).where(Transaction.user_id == user,
                                                     Transaction.txn_id == body.txn_id)).first()
            if t is None:
                raise ApiError(404, "NOT_FOUND", f"no transaction {body.txn_id!r}")
            key = t.merchant_key
        if not key:
            raise ApiError(422, "BAD_PARAMS", "send merchant_key or txn_id")
        try:
            set_merchant_rule(s, user, key, body.category, svc.get_categoriser(), svc.get_clock())
        except ValueError as e:
            raise ApiError(422, "UNKNOWN_CATEGORY", str(e)) from e
        s.commit()
    eid = _publish_recompute(svc, user, "user.rule_changed", "user:merchant_rule")
    return {"event_id": eid, "snapshot_id": _recompute(svc, wait)}


@router.delete("/user/data", response_model=EraseResponse, responses=ERRORS, tags=["privacy"])
def erase(user: User, svc: Svc):
    """DPDP erasure (D14): every row for the user, snapshots and logs included. Only the anonymous validator-block
    counter survives."""
    now = svc.get_clock().now()
    with svc.sf()() as s:
        counts = UserRepo(s, user).erase_all()
        s.commit()
    return {"erased": counts, "receipt_id": stable_id("erase", user, now.isoformat()), "erased_at": now.isoformat()}


# ---- system -----------------------------------------------------------------------------------------------------
@router.post("/demo/replay/{step}", response_model=ReplayResponse, responses=ERRORS, tags=["demo"])
def demo_replay(step: str, svc: Svc):
    """DEMO_MODE only: persona A's replay, one step at a time, in order (t0 resets)."""
    from app.core.config import get_settings
    from app.demo.replay import USER, reset, run_step
    from app.demo.scenario import persona_a_steps

    if not get_settings().demo_mode:
        raise ApiError(404, "NOT_FOUND", "demo endpoints are only available with DEMO_MODE=1")
    steps = {s.name: s for s in persona_a_steps()}
    if step not in steps:
        raise ApiError(404, "NOT_FOUND", f"unknown step {step!r}; use t0, 1, 2 or 3")
    order = list(steps)
    if step == "t0":
        reset(svc.sf(), USER)
    else:
        with svc.sf()() as s:
            snap = latest_snapshot(s, USER)
        want = f"demo:{order[order.index(step) - 1]}"
        if snap is None or snap.trigger != want:
            raise ApiError(409, "OUT_OF_ORDER", f"step {step} needs the previous step first",
                           {"latest": snap.trigger if snap else None, "expected": want})
    r = run_step(svc.sf(), svc.get_bus(), svc.get_categoriser(), steps[step])
    return {"step": step, "snapshot_id": r.snapshot.snapshot_id, "as_of": r.snapshot.as_of,
            "diff": r.diff.payload if r.diff else None}


@router.get("/health", response_model=HealthResponse, tags=["system"])
def health(svc: Svc):
    from app.core.config import get_settings
    from app.engines import ENGINE_VERSION

    s = get_settings()
    try:
        with svc.sf()() as db:
            db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:  # noqa: BLE001 - health reports, it doesn't raise
        db_ok = False
    redis_ok = None
    bus = svc.bus
    if bus is not None and hasattr(bus, "r"):
        try:
            redis_ok = bool(bus.r.ping())
        except Exception:  # noqa: BLE001
            redis_ok = False
    return {"db": db_ok, "redis": redis_ok, "llm_provider": s.llm_provider, "demo_mode": s.demo_mode,
            "engine_version": ENGINE_VERSION}

