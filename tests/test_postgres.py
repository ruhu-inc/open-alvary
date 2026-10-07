"""Real migrated database contracts; never use the application or production database."""

import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool

from api.store import Store, seed
from rights.policy import public_corpus

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg_engine():
    raw = os.getenv("TEST_DATABASE_URL")
    if not raw:
        pytest.fail("--run-postgres requires TEST_DATABASE_URL; integration tests cannot be skipped")
    try:
        url = make_url(raw)
        valid = url.drivername == "postgresql+psycopg" and url.database and url.database.endswith("_test")
    except Exception:
        valid = False
    if not valid:
        pytest.fail("TEST_DATABASE_URL must use postgresql+psycopg and a database ending in _test")
    namespace = "test_" + uuid4().hex
    admin = create_engine(url, poolclass=NullPool)
    engine = None
    created = False
    try:
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{namespace}"'))
        created = True
        engine = create_engine(
            url, poolclass=NullPool, connect_args={"options": f"-csearch_path={namespace}"}
        )
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            with admin.begin() as conn:
                for suffix in ("policy", "staging", "corpus"):
                    conn.execute(text(f'DROP SCHEMA IF EXISTS "{namespace}_{suffix}" CASCADE'))
                conn.execute(text(f'DROP SCHEMA "{namespace}" CASCADE'))
        admin.dispose()


def migrate(engine, revision="head"):
    config = Config("alembic.ini")
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def test_fresh_migrations_and_idempotent_seed(pg_engine):
    migrate(pg_engine)
    store = Store(pg_engine)
    seed(store)
    before = store.load().model_dump(mode="json")
    seed(store)
    assert store.load().model_dump(mode="json") == before
    assert len(public_corpus(store.load()).sources) == 4
    assert not public_corpus(store.load()).versions


def test_populated_0001_upgrades_without_data_loss(pg_engine):
    migrate(pg_engine, "0001")
    store = Store(pg_engine)
    seed(store)
    before = store.load().model_dump(mode="json")
    migrate(pg_engine)
    assert store.load().model_dump(mode="json") == before


def test_database_rejects_orphan_and_rolls_back_transaction(pg_engine):
    migrate(pg_engine)
    with pytest.raises(IntegrityError):
        with pg_engine.begin() as conn:
            conn.execute(text("INSERT INTO languages (id,payload) VALUES ('rollback-test','{}')"))
            conn.execute(
                text("INSERT INTO authorities (id,jurisdiction_id,payload) VALUES ('bad','absent','{}')")
            )
    with pg_engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM languages WHERE id='rollback-test'")) == 0
        assert conn.scalar(text("SELECT count(*) FROM authorities")) == 0


def test_database_rejects_duplicate_rights_owner(pg_engine):
    migrate(pg_engine)
    seed(Store(pg_engine))
    with pytest.raises(IntegrityError):
        with pg_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO rights (id,source_id,payload) SELECT 'duplicate',source_id,payload FROM rights LIMIT 1"
                )
            )
