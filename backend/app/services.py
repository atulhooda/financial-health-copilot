"""Application services shared by the CLI and the API: ingest -> commit -> publish (SPEC §7)."""
from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from app.core.clock import Clock
from app.db.repo import UserRepo
from app.events.bus import EventBus
from app.ingest.registry import adapter_for
from app.ingest.service import IngestResult, ingest_batch, next_ingest_seq
from app.pipeline.categorise.model import Categoriser


def ingest_and_publish(session_factory: sessionmaker, bus: EventBus, user_id: str, source: str, payload,
                       categoriser: Categoriser | None, clock: Clock, trigger: str | None = None) -> IngestResult:
    with session_factory() as s:
        batch = adapter_for(source, clock).parse(payload, user_id)
        result = ingest_batch(s, user_id, batch, categoriser, clock)
        seq = next_ingest_seq(UserRepo(s, user_id)) - 1
        s.commit()
    result.event_id = bus.publish({"type": "data.ingested", "user_id": user_id, "source": source, "ingest_seq": seq,
                                   "as_of": clock.today().isoformat(), "trigger": trigger})
    return result
