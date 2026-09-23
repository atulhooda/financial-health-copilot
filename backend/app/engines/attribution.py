"""New data vs behaviour (SPEC D24).

Given the previous snapshot's (as_of, ingest watermark) and the new one, we build three views:
  prev       = what the previous snapshot saw
  restricted = the new view WITHOUT data that merely revealed the past
  full       = the new view
prev -> restricted is behaviour/time; restricted -> full is NEW_DATA_REVEALED.

"Revealed" rows arrived after the previous watermark and are either dated before the previous as_of,
or belong to an account that became visible/linked since then and whose history predates it. A brand-new
account (e.g. a loan opened on the previous as_of) is behaviour, not a reveal.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.engines.financial import Metrics, compute_metrics
from app.engines.score import Score, compute_score
from app.engines.view import View, build_view
from app.pipeline.categorise.model import Categoriser

TRACKED = ("income_monthly_paise", "spend_monthly_paise", "savings_rate", "buffer_months", "emi_to_income",
           "credit_utilisation", "revolving_paise", "debt_outstanding_paise")


@dataclass
class Attribution:
    revealed_accounts: list[str]
    score: dict  # {"prev", "restricted", "full", "behaviour", "new_data"}
    pillars: dict  # key -> {"behaviour", "new_data"} in pillar points (contribution)
    metrics: dict  # metric -> {"prev", "restricted", "full"}

    @property
    def new_data_revealed(self) -> bool:
        return bool(self.revealed_accounts) or self.score["new_data"] != 0 or any(
            v["restricted"] != v["full"] for v in self.metrics.values())


def _evaluate(view: View) -> tuple[Metrics, Score]:
    m = compute_metrics(view)
    return m, compute_score(m)


def attribute_change(session: Session, user_id: str, categoriser: Categoriser | None,
                     prev_as_of: dt.date, prev_seq: int, new_as_of: dt.date, new_seq: int) -> Attribution:
    prev = build_view(session, user_id, prev_as_of, categoriser, max_ingest_seq=prev_seq)
    full = build_view(session, user_id, new_as_of, categoriser, max_ingest_seq=new_seq)
    revealed = sorted(
        a.account_id for a in full.accounts.values()
        if a.visible_from is not None and a.visible_from < prev_as_of and a.institution != "cash"
        and (a.account_id not in prev.accounts
             or (a.linked and not prev.accounts[a.account_id].linked)
             or (a.visible and not prev.accounts[a.account_id].visible)))
    rev = set(revealed)
    restricted = build_view(
        session, user_id, new_as_of, categoriser, max_ingest_seq=new_seq,
        exclude_raw=lambda r: r.ingest_seq > prev_seq and (r.txn_date < prev_as_of or r.account_id in rev),
        exclude_state=lambda s: s.ingest_seq > prev_seq and s.account_id in rev)
    (mp, sp), (mr, sr), (mf, sf) = _evaluate(prev), _evaluate(restricted), _evaluate(full)

    def contrib(score: Score) -> dict[str, int]:
        return {p.key: p.contribution for p in score.pillars}

    cp, cr, cf = contrib(sp), contrib(sr), contrib(sf)
    return Attribution(
        revealed_accounts=revealed,
        score={"prev": sp.total, "restricted": sr.total, "full": sf.total,
               "behaviour": sr.total - sp.total, "new_data": sf.total - sr.total},
        pillars={k: {"behaviour": cr[k] - cp[k], "new_data": cf[k] - cr[k]} for k in cf},
        metrics={k: {"prev": getattr(mp, k), "restricted": getattr(mr, k), "full": getattr(mf, k)} for k in TRACKED})
