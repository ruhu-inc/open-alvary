"""Real migrated database contracts; never use the application or production database."""

from uuid import uuid4

import pytest
from postgres_support import database, validated_test_url
from postgres_support import migrate as migrate
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from api.store import Store, seed
from rights.policy import public_corpus

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg_engine(request):
    fresh = request.config.getoption("--fresh-postgres") or request.node.get_closest_marker(
        "fresh_migrations"
    )
    if fresh:
        with database(validated_test_url(), "test_" + uuid4().hex) as (engine, _):
            yield engine
    else:
        url, namespace, template = request.getfixturevalue("pg_template")
        with database(url, namespace, template) as (engine, _):
            yield engine


@pytest.mark.fresh_migrations
def test_fresh_migrations_and_idempotent_seed(pg_engine):
    migrate(pg_engine)
    store = Store(pg_engine)
    seed(store)
    before = store.load().model_dump(mode="json")
    seed(store)
    assert store.load().model_dump(mode="json") == before
    assert len(public_corpus(store.load()).sources) == 4
    assert not public_corpus(store.load()).versions


@pytest.mark.fresh_migrations
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
