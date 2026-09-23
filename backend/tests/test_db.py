import os

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, func, select, text

from app.db.models import USER_SCOPED_MODELS, Base, GlobalCounter, Transaction, User
from app.db.repo import UserRepo
from app.demo.scenario import seed_payloads
from app.ingest.registry import adapter_for
from app.ingest.service import ingest_batch


def test_migrations_match_models(engine):
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_user_id_on_every_table_but_the_anonymous_counter():
    tables = {m.__tablename__ for m in USER_SCOPED_MODELS}
    for table in Base.metadata.sorted_tables:
        if table.name == GlobalCounter.__tablename__:
            assert "user_id" not in table.c
        else:
            assert table.name in tables and "user_id" in table.c and table.c.user_id.nullable is False


def test_repo_refuses_rows_for_other_users(session):
    with pytest.raises(ValueError):
        UserRepo(session, "u1").add(User(user_id="u2"))


def test_erasure_leaves_nothing_for_the_user(session, clock, categoriser):
    for source, payload in seed_payloads("demo-b"):
        ingest_batch(session, "demo-b", adapter_for(source, clock).parse(payload, "demo-b"), categoriser, clock)
    for source, payload in seed_payloads("demo-a"):
        ingest_batch(session, "demo-a", adapter_for(source, clock).parse(payload, "demo-a"), categoriser, clock)
    session.commit()
    counts = UserRepo(session, "demo-b").erase_all()
    session.commit()
    assert counts["transactions"] > 0
    for m in USER_SCOPED_MODELS:
        assert session.scalar(select(func.count()).select_from(m).where(m.user_id == "demo-b")) == 0
    assert session.scalar(select(func.count()).select_from(Transaction).where(Transaction.user_id == "demo-a")) > 0


PG_URL = os.environ.get("TEST_POSTGRES_URL", "postgresql+psycopg://hisaab:hisaab@localhost:5433/hisaab_test")


@pytest.mark.postgres
def test_postgres_migration_and_snapshot_immutability():
    from tests.conftest import migrate

    eng = create_engine(PG_URL)
    try:
        eng.connect().close()
    except Exception:
        pytest.skip("PostgreSQL not reachable (run `make up`)")
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    migrate(eng)
    with eng.begin() as c:
        c.execute(text("INSERT INTO snapshots VALUES ('u1','s1',1,'baseline','2026-09-20',now(),'h','0.1','{}')"))
    with pytest.raises(Exception, match="immutable"), eng.begin() as c:
        c.execute(text("UPDATE snapshots SET trigger='x' WHERE snapshot_id='s1'"))
    with eng.begin() as c:
        assert c.execute(text("DELETE FROM snapshots WHERE user_id='u1'")).rowcount == 1
