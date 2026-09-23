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
