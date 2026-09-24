"""Immutable snapshots (SPEC §7): everything the app shows, computed once per data change, never updated."""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import Clock, get_clock
from app.core.ids import stable_id
from app.db.models import AccountState, MerchantRule, RawTransactionRow, Snapshot, SnapshotDiff, UserPreference
from app.db.models import RecurringItem as RecurringRow
from app.db.repo import UserRepo
from app.engines import ENGINE_VERSION
from app.engines.attribution import attribute_change
from app.engines.backtest import forecast_with_confidence
from app.engines.financial import compute_metrics
from app.engines.score import compute_score
from app.engines.simulate import build_context, max_mandate_bounce, recommend
from app.engines.view import build_view
from app.events.diff import diff_payloads
from app.pipeline.categorise.model import Categoriser


def jsonable(x):
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return {k: jsonable(v) for k, v in dataclasses.asdict(x).items()}
    if isinstance(x, dict):
        return {str(k): jsonable(v) for k, v in x.items()}
    if isinstance(x, list | tuple | set):
        return [jsonable(v) for v in x]
    if isinstance(x, dt.date | dt.datetime):
        return x.isoformat()
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, float | np.floating):
        return round(float(x), 6)
    return x


def current_watermark(session: Session, user_id: str) -> int:
    repo = UserRepo(session, user_id)
    seqs = [r.ingest_seq for r in repo.select(RawTransactionRow)] + [s.ingest_seq for s in repo.select(AccountState)]
    return max(seqs, default=0)


def input_hash(session: Session, user_id: str, as_of: dt.date, watermark: int) -> str:
    """Same inputs -> same hash: the data watermark, as_of, user rules and preferences, engine version."""
    repo = UserRepo(session, user_id)
    rules = sorted((r.merchant_key, r.category) for r in repo.select(MerchantRule))
    prefs = sorted((p.key, json.dumps(p.value, sort_keys=True)) for p in repo.select(UserPreference))
    key = json.dumps([user_id, as_of.isoformat(), watermark, rules, prefs, ENGINE_VERSION], sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()


def compute_payload(session: Session, user_id: str, as_of: dt.date, categoriser: Categoriser | None,
                    max_ingest_seq: int | None = None) -> dict:
    view = build_view(session, user_id, as_of, categoriser, max_ingest_seq=max_ingest_seq)
    m = compute_metrics(view)
    sc = compute_score(m)
    fc, conf, bt = forecast_with_confidence(view, m.recurring, m.coverage)
    recs, plan = ([], None)
    if fc.available:
        ctx = build_context(view, m, sc, fc, conf)
        recs, plan = recommend(ctx)
    top_bounce = max((b for b in fc.bounce_risks if b.mandate), key=lambda b: (b.probability, -b.due_date.toordinal()),
                     default=None)
    alerts = []
    if top_bounce and top_bounce.probability >= 0.10:  # D34: lead with bounce risk
        alerts.append({"kind": "bounce_risk", "name": top_bounce.name, "due_date": top_bounce.due_date,
                       "amount_paise": top_bounce.amount_paise, "probability": top_bounce.probability})
    if m.debt_growth:
        alerts.append({"kind": "debt_growth", **m.debt_growth})
    if m.overlaps:
        alerts.append({"kind": "overlap", **m.overlaps[0]})
    payload = {
        "as_of": as_of, "ingest_seq": view.ingest_seq, "engine_version": ENGINE_VERSION,
        "scoring_version": sc.version, "floor_paise": view.floor_paise,
        "accounts": [{"account_id": a.account_id, "kind": a.kind, "institution": a.institution, "last4": a.last4,
                      "status": a.status, "role": a.role, "balance_paise": a.balance_paise}
                     for a in sorted(view.accounts.values(), key=lambda a: a.account_id) if a.institution != "cash"],
        "metrics": {k: getattr(m, k) for k in (
            "income_pattern", "income_monthly_paise", "salary_level_paise", "other_income_monthly_paise",
            "spend_monthly_paise", "essential_monthly_paise", "discretionary_monthly_paise", "savings_rate",
            "liquid_paise", "earmarked_loan_paise", "buffer_months", "emi_monthly_paise", "emi_to_income",
            "debt_outstanding_paise", "debt_to_income", "credit_utilisation", "revolving_paise", "discretionary_ratio",
            "bounces", "coverage")},
        "debt_growth": m.debt_growth,
        "cards": m.cards,
        "recurring": [{k: getattr(r, k) for k in ("item_id", "merchant_key", "merchant_name", "direction", "category",
                                                   "kind", "cadence", "amount_paise", "next_due", "active", "source",
                                                   "amount_variable", "pending_change", "subscription_group")}
                      for r in m.recurring],
        "overlaps": m.overlaps, "spend_by_category": m.spend_by_category, "drift": m.drift,
        "earmarks": m.earmarks,
        "score": sc,
        "forecast": {k: getattr(fc, k) for k in (
            "available", "reason", "account_id", "horizon_days", "floor_paise", "opening_paise", "next_income_date",
            "income_basis", "dates", "p10", "p50", "p90", "dip_probability", "likely_dip_date", "projected_low_paise",
            "pre_income_p10_p50_p90", "bounce_risks", "earmarked_loan_paise", "dip_probability_if_kept",
            "assumptions")} | {
            "dip_saturated": fc.dip_probability is not None and fc.dip_probability >= 0.95,
            "max_mandate_bounce": max_mandate_bounce(fc),
            "scheduled": [{k: getattr(f, k) for k in ("date", "name", "kind", "direction", "amount_paise", "source",
                                                      "estimated", "mandate", "account_id")} for f in fc.schedule]},
        "confidence": conf,
        "backtest": {"coverage": bt.coverage, "calibration_gap": bt.calibration_gap, "brier": bt.brier,
                     "origins": len(bt.origins), "days": bt.days, "side_errors": bt.side_errors()},
        "alerts": alerts,
        "recommendations": recs,
        "combined_plan": plan,
    }
    return jsonable(payload)


def latest_snapshot(session: Session, user_id: str) -> Snapshot | None:
    return session.scalars(select(Snapshot).where(Snapshot.user_id == user_id).order_by(Snapshot.seq.desc())
                           .limit(1)).first()


def take_snapshot(session: Session, user_id: str, as_of: dt.date, trigger: str, categoriser: Categoriser | None,
                  clock: Clock | None = None) -> tuple[Snapshot, SnapshotDiff | None, bool]:
    """Insert a new immutable snapshot (+ its diff vs the previous one), unless the inputs are unchanged.

    Returns (snapshot, diff, created)."""
    clock = clock or get_clock()
    repo = UserRepo(session, user_id)
    wm = current_watermark(session, user_id)
    h = input_hash(session, user_id, as_of, wm)
    prev = latest_snapshot(session, user_id)
    if prev is not None and prev.input_hash == h:
        return prev, repo.get(SnapshotDiff, to_snapshot_id=prev.snapshot_id), False
    payload = compute_payload(session, user_id, as_of, categoriser, max_ingest_seq=wm)
    seq = (session.scalar(select(func.max(Snapshot.seq)).where(Snapshot.user_id == user_id)) or 0) + 1
    snap = Snapshot(user_id=user_id, snapshot_id=stable_id("snap", user_id, seq, h), seq=seq, trigger=trigger,
                    as_of=as_of, created_at=clock.now(), input_hash=h, engine_version=ENGINE_VERSION, payload=payload)
    repo.add(snap)
    for r in payload["recurring"]:
        repo.add(RecurringRow(user_id=user_id, snapshot_id=snap.snapshot_id, item_id=r["item_id"],
                              merchant_key=r["merchant_key"][:80], kind=r["kind"],
                              cadence_days={"weekly": 7, "monthly": 30, "quarterly": 91, "annual": 365}[r["cadence"]],
                              amount_paise=r["amount_paise"], next_due=dt.date.fromisoformat(r["next_due"]),
                              active=r["active"]))
    diff = None
    if prev is not None:
        att = attribute_change(session, user_id, categoriser, prev.as_of, prev.payload["ingest_seq"], as_of, wm)
        body = diff_payloads(prev.payload, payload, jsonable(att), att.new_data_revealed)
        diff = SnapshotDiff(user_id=user_id, to_snapshot_id=snap.snapshot_id, from_snapshot_id=prev.snapshot_id,
                            reason_codes=body["reason_codes"], payload=body)
        repo.add(diff)
    session.flush()
    return snap, diff, True
