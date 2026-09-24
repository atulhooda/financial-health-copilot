"""Persona A replay T0 -> +3 through the real bus and worker (docs/DEMO.md)."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import sessionmaker

from app.core.clock import FixedClock, set_clock
from app.db.models import Snapshot, SnapshotDiff
from app.db.repo import UserRepo
from app.demo.scenario import Step, persona_a_steps
from app.events.bus import EventBus
from app.events.worker import process_available
from app.pipeline.categorise.model import Categoriser
from app.services import ingest_and_publish

USER = "demo-a"


@dataclass
class StepResult:
    step: Step
    snapshot: Snapshot
    diff: SnapshotDiff | None


def reset(session_factory: sessionmaker, user_id: str = USER) -> None:
    with session_factory() as s:
        UserRepo(s, user_id).erase_all()
        s.commit()


def run_step(session_factory: sessionmaker, bus: EventBus, categoriser: Categoriser | None, step: Step) -> StepResult:
    clock = FixedClock(step.as_of)
    set_clock(clock)
    for source, payload in step.payloads:
        ingest_and_publish(session_factory, bus, USER, source, payload, categoriser, clock, f"demo:{step.name}")
    done = [p for p in process_available(bus, session_factory, categoriser, clock) if p.user_id == USER]
    return StepResult(step, done[-1].snapshot, done[-1].diff)


def replay(session_factory: sessionmaker, bus: EventBus, categoriser: Categoriser | None,
           upto: str = "3") -> list[StepResult]:
    reset(session_factory)
    out = []
    for step in persona_a_steps():
        out.append(run_step(session_factory, bus, categoriser, step))
        if step.name == upto:
            break
    return out
