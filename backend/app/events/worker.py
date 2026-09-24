"""The recompute worker (SPEC §7): events -> one immutable snapshot per user, acked after commit."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy.orm import sessionmaker

from app.core.clock import Clock, get_clock
from app.db.models import Snapshot, SnapshotDiff
from app.events.bus import EventBus
from app.events.snapshots import take_snapshot
from app.pipeline.categorise.model import Categoriser


@dataclass
class Processed:
    user_id: str
    snapshot: Snapshot
    diff: SnapshotDiff | None
    created: bool
    event_ids: list[str]


def process_available(bus: EventBus, session_factory: sessionmaker, categoriser: Categoriser | None,
                      clock: Clock | None = None, block_ms: int = 0) -> list[Processed]:
    """Drain what's available; events for the same user coalesce into ONE snapshot (debounce)."""
    clock = clock or get_clock()
    events = bus.read(block_ms=block_ms)
    by_user: dict[str, list[tuple[str, dict]]] = {}
    for eid, ev in events:
        if ev.get("type") in ("data.ingested", "user.rule_changed", "user.preference_changed"):
            by_user.setdefault(ev["user_id"], []).append((eid, ev))
    out = []
    for user_id in sorted(by_user):
        evs = by_user[user_id]
        as_of = max(dt.date.fromisoformat(ev["as_of"]) for _, ev in evs)
        trigger = "+".join(dict.fromkeys(ev.get("trigger") or f"{ev['type']}:{ev.get('source', '')}" for _, ev in evs))
        with session_factory() as s:
            snap, diff, created = take_snapshot(s, user_id, as_of, trigger[:64], categoriser, clock)
            s.commit()
        ids = [eid for eid, _ in evs]
        bus.ack(ids)  # only after the snapshot is committed
        out.append(Processed(user_id, snap, diff, created, ids))
    skipped = [eid for eid, ev in events if ev.get("user_id") not in by_user]
    bus.ack(skipped)
    return out


def run_forever(bus: EventBus, session_factory: sessionmaker, categoriser: Categoriser | None) -> None:  # pragma: no cover
    while True:
        process_available(bus, session_factory, categoriser, block_ms=5000)
