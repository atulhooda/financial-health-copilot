from __future__ import annotations

import os

os.environ.setdefault("LLM_PROVIDER", "none")
os.environ.setdefault("EVENT_BUS", "inprocess")

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from app.core.clock import FixedClock
from app.core.config import BACKEND_DIR
from app.db.session import make_sessionmaker
from app.demo.personas import T0


def migrate(engine) -> None:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "app/db/migrations"))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")


@pytest.fixture
def engine(tmp_path):
    """A fresh SQLite database built by the real Alembic migrations."""
    eng = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    migrate(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    s = make_sessionmaker(engine)()
    yield s
    s.close()


@pytest.fixture
def clock():
    return FixedClock(T0)


@pytest.fixture(scope="session")
def categoriser():
    from app.pipeline.categorise.train import get_categoriser

    return get_categoriser()


@pytest.fixture(scope="session")
def copilot_world(tmp_path_factory, categoriser):
    """SQLite with demo-a replayed T0 -> +3 (one snapshot per step) and B and C seeded, one snapshot each."""
    from types import SimpleNamespace

    from sqlalchemy import create_engine

    from app.core.clock import get_clock, set_clock
    from app.demo.personas import BC_AS_OF
    from app.demo.replay import replay
    from app.demo.scenario import seed_payloads
    from app.events.bus import InProcessBus
    from app.events.snapshots import take_snapshot
    from app.ingest.registry import adapter_for
    from app.ingest.service import ingest_batch

    previous = get_clock()
    eng = create_engine(f"sqlite:///{tmp_path_factory.mktemp('copilot') / 'copilot.db'}", future=True)
    migrate(eng)
    sf = make_sessionmaker(eng)
    steps = replay(sf, InProcessBus(), categoriser)
    others = {}
    for pid in ("demo-b", "demo-c"):
        clock = FixedClock(BC_AS_OF)
        set_clock(clock)
        for source, payload in seed_payloads(pid):
            with sf() as s:
                ingest_batch(s, pid, adapter_for(source, clock).parse(payload, pid), categoriser, clock)
                s.commit()
        with sf() as s:
            snap, _, _ = take_snapshot(s, pid, BC_AS_OF, "seed", categoriser, clock)
            s.commit()
            others[pid] = snap
    set_clock(previous)
    return SimpleNamespace(sf=sf, steps=steps, others=others)
