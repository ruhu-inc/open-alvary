"""Production denial-journal backend in a separate PostgreSQL database: conformance drills."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from journal_conformance import CHECKS, deny
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from operations.journal import JournalCorrupt, JournalError
from operations.journal_postgres import PostgresJournal, install, open_journal

pytestmark = pytest.mark.postgres


@pytest.fixture
def journal_db():
    from postgres_support import validated_test_url

    admin_url = validated_test_url()
    name = f"oa_journal_{uuid4().hex[:12]}_test"
    admin = create_engine(admin_url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(admin_url.set(database=name), poolclass=NullPool)
    try:
        install(engine)
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
def test_postgres_journal_conformance(journal_db, check):
    check(PostgresJournal(journal_db))


@pytest.mark.parametrize(
    "statement",
    ["UPDATE journal_event SET sequence=99", "DELETE FROM journal_event", "TRUNCATE journal_event"],
)
def test_journal_is_append_only_even_for_the_owner(journal_db, statement):
    PostgresJournal(journal_db).append(deny())
    with pytest.raises(DBAPIError) as denied, journal_db.begin() as conn:
        conn.execute(text(statement))
    assert denied.value.orig.sqlstate == "42501"


def test_tampering_by_a_superuser_is_detected(journal_db):
    journal = PostgresJournal(journal_db)
    for revision in range(1, 4):
        journal.append(deny(revision=revision))
    with journal_db.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role=replica"))
        conn.execute(text("ALTER TABLE journal_event DISABLE TRIGGER journal_event_append_only"))
        conn.execute(
            text("UPDATE journal_event SET payload=jsonb_set(payload,'{revision}','9') WHERE sequence=2")
        )
        conn.execute(text("ALTER TABLE journal_event ENABLE ALWAYS TRIGGER journal_event_append_only"))
    with pytest.raises(JournalCorrupt):
        journal.high_watermark()


def test_concurrent_appends_get_unique_contiguous_sequences(journal_db):
    journal = PostgresJournal(journal_db)
    with ThreadPoolExecutor(8) as pool:
        receipts = list(pool.map(lambda _: journal.append(deny()), range(40)))
    assert sorted(r.sequence for r in receipts) == list(range(1, 41))
    assert journal.high_watermark()[0] == 40


def test_writer_role_can_append_but_not_rewrite(journal_db):
    role = "journal_writer_" + uuid4().hex[:8]
    with journal_db.begin() as conn:
        conn.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
    install(journal_db, writer_role=role)
    try:
        with journal_db.begin() as conn:
            conn.execute(text(f'SET LOCAL ROLE "{role}"'))
            conn.execute(
                text(
                    "INSERT INTO journal_event(sequence,event_id,payload,payload_sha256,chain_sha256)"
                    " VALUES (1,:id,'{}',repeat('a',64),repeat('b',64))"
                ),
                {"id": uuid4()},
            )
        with pytest.raises(DBAPIError) as denied, journal_db.begin() as conn:
            conn.execute(text(f'SET LOCAL ROLE "{role}"'))
            conn.execute(text("ALTER TABLE journal_event DISABLE TRIGGER journal_event_append_only"))
        assert denied.value.orig.sqlstate == "42501"
    finally:
        with journal_db.begin() as conn:
            conn.execute(text(f'DROP OWNED BY "{role}"'))
            conn.execute(text(f'DROP ROLE "{role}"'))


def test_journal_outage_is_a_journal_error(monkeypatch):
    engine = create_engine("postgresql+psycopg://nobody@127.0.0.1:1/none_test", poolclass=NullPool)
    with pytest.raises(JournalError):
        PostgresJournal(engine).append(deny())
    monkeypatch.setenv("ALVARY_JOURNAL_DATABASE_URL", "sqlite:///x.db")
    with pytest.raises(JournalError):
        open_journal()
