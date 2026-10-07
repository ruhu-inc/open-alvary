"""Migrated identity constraints, maintenance authority races and safe compatibility."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine

from api.identity import (
    IdentityConflict,
    IdentityRepository,
    backfill,
    end_rehearsal,
    freeze,
    reconcile,
    rehearse_cutover,
    resume_legacy,
)
from api.main import create_app
from api.store import RELATIONS, Store, check_legacy_authority, seed
from rights.policy import public_corpus
from schema.identity import mint

pytestmark = pytest.mark.postgres


@pytest.fixture
def identity_db(pg_engine):
    migrate(pg_engine)
    seed(Store(pg_engine))
    return pg_engine


def copy_shadow(engine):
    with engine.begin() as conn:
        return backfill(conn, expected_epoch=0)


def test_upgrade_preserves_public_ids_and_backfill_is_repeatable(pg_engine):
    migrate(pg_engine, "0001")
    store = Store(pg_engine)
    seed(store)
    before = store.load().model_dump(mode="json")
    client = TestClient(create_app(store))
    routes = ["/sources", "/search?q=constitution", "/coverage"] + [
        f"/sources/{s['id']}" for s in before["sources"]
    ]
    responses = {route: client.get(route).json() for route in routes}
    migrate(pg_engine)
    first = copy_shadow(pg_engine)
    with pg_engine.connect() as conn:
        mappings = list(
            conn.execute(text("SELECT * FROM identity_legacy_sources ORDER BY source_id")).mappings()
        )
    assert copy_shadow(pg_engine) == first
    with pg_engine.connect() as conn:
        assert (
            list(conn.execute(text("SELECT * FROM identity_legacy_sources ORDER BY source_id")).mappings())
            == mappings
        )
        assert len({row["work_id"] for row in mappings}) == 4
        for table in (
            "identity_expressions",
            "identity_manifestations",
            "identity_observations",
            "identity_collections",
        ):
            assert conn.scalar(text(f"SELECT count(*) FROM {table}")) == 0
    assert store.load().model_dump(mode="json") == before
    assert {route: client.get(route).json() for route in routes} == responses
    assert not public_corpus(store.load()).versions


def test_synthetic_legacy_bytes_and_approvals_are_preserved_not_qualified(pg_engine, cleared):
    migrate(pg_engine, "0001")
    Store(pg_engine).save(cleared)
    original = Store(pg_engine).load().model_dump(mode="json")
    migrate(pg_engine)
    copy_shadow(pg_engine)
    with pg_engine.begin() as conn:
        row = conn.execute(text("SELECT * FROM identity_legacy_versions")).mappings().one()
        assert row["content_hash"] == cleared.versions[0].content_hash
        assert row["raw_hash"] == cleared.versions[0].raw_hash
        assert row["normalization_status"] == "legacy_unqualified"
        freeze(conn, expected_epoch=0)
    with pg_engine.begin() as conn:
        reconcile(conn, expected_epoch=1)
    assert Store(pg_engine).load().model_dump(mode="json") == original


@pytest.mark.parametrize(
    "field,value",
    [
        ("authority_id", "absent"),
        ("jurisdiction_id", "absent"),
        ("catalogue_language_id", "absent"),
        ("document_class", "invented"),
        ("id", "wrk_not-a-uuid"),
        ("collection_unknown_reason", None),
    ],
)
def test_typed_work_constraints(identity_db, field, value):
    copy_shadow(identity_db)
    with pytest.raises(IntegrityError), identity_db.begin() as conn:
        conn.execute(text(f"UPDATE identity_works SET {field}=:value"), {"value": value})


def test_authority_and_collection_scope_cannot_cross_ownership(identity_db):
    copy_shadow(identity_db)
    with identity_db.begin() as conn:
        conn.execute(text("INSERT INTO jurisdictions (id,payload) VALUES ('synthetic','{}')"))
    with pytest.raises(IntegrityError), identity_db.begin() as conn:
        conn.execute(text("UPDATE identity_works SET jurisdiction_id='synthetic'"))
    collection = mint("scp")
    with identity_db.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO identity_collections(id,jurisdiction_id,document_class,provider_key)
            VALUES (:id,'ng','judgment','synthetic-provider')
        """),
            {"id": collection},
        )
    with pytest.raises(IntegrityError), identity_db.begin() as conn:
        conn.execute(
            text("""
            UPDATE identity_works SET collection_id=:id,collection_unknown_reason=NULL
            WHERE document_class='act'
        """),
            {"id": collection},
        )


def test_expression_ownership_unknowns_and_logical_uniqueness(identity_db):
    copy_shadow(identity_db)
    with identity_db.connect() as conn:
        work = conn.scalar(text("SELECT id FROM identity_works LIMIT 1"))
    insert = text("""
        INSERT INTO identity_expressions(id,work_id,language_id,edition_kind,edition_key,
            edition_date_unknown_reason,authenticity_unknown_reason)
        VALUES (:id,:work,'en','original','synthetic-edition',:reason,'Not assessed')
    """)
    with identity_db.begin() as conn:
        conn.execute(insert, {"id": mint("exp"), "work": work, "reason": "Not dated"})
    for bad in (
        {"work": mint("wrk"), "reason": "Not dated"},
        {"work": work, "reason": None},
        {"work": work, "reason": "Not dated"},
    ):
        with pytest.raises(IntegrityError), identity_db.begin() as conn:
            conn.execute(insert, {"id": mint("exp"), **bad})


def test_raw_hash_does_not_merge_works_and_observation_retry_key_is_scoped(identity_db):
    copy_shadow(identity_db)
    man = mint("man")
    same_bytes = mint("man")
    with identity_db.begin() as conn:
        for key in (man, same_bytes):
            conn.execute(
                text("""
                INSERT INTO identity_manifestations(id,raw_hash,media_type,size_bytes,provenance_reference)
                VALUES (:id,:hash,'text/plain',0,'Synthetic fixture')
            """),
                {"id": key, "hash": "a" * 64},
            )
        works = list(conn.scalars(text("SELECT id FROM identity_works ORDER BY id LIMIT 2")))
        for work in works:
            conn.execute(
                text("INSERT INTO identity_work_manifestations VALUES (:work,:man)"),
                {"work": work, "man": man},
            )
        assert conn.scalar(text("SELECT count(*) FROM identity_work_manifestations")) == 2
    insert = text("""
        INSERT INTO identity_observations(id,manifestation_id,provider_key,retrieval_key,
            requested_url,final_url,observed_at,response_status,processing_config_hash,acquisition_reference)
        VALUES (:id,:man,:provider,:key,'https://example.org/synthetic','https://example.org/synthetic',
            '2026-01-01T00:00:00Z',200,:hash,'Synthetic permission fixture')
    """)
    with identity_db.begin() as conn:
        for provider, key in (("p1", "r1"), ("p1", "r2"), ("p2", "r1")):
            conn.execute(
                insert, {"id": mint("obs"), "man": man, "provider": provider, "key": key, "hash": "b" * 64}
            )
    for bad_man, provider, key in ((man, "p1", "r1"), (mint("man"), "p1", "r3")):
        with pytest.raises(IntegrityError), identity_db.begin() as conn:
            conn.execute(
                insert,
                {"id": mint("obs"), "man": bad_man, "provider": provider, "key": key, "hash": "b" * 64},
            )


def test_interrupted_backfill_rolls_back_and_retry_mints_only_once(identity_db):
    with pytest.raises(RuntimeError, match="interrupted"), identity_db.begin() as conn:
        backfill(conn, expected_epoch=0)
        raise RuntimeError("interrupted")
    with identity_db.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM identity_works")) == 0
        assert conn.scalar(text("SELECT source_checkpoint FROM identity_migration_state")) is None
    copy_shadow(identity_db)
    assert len(Store(identity_db).load().sources) == 4


def test_uuid_collision_retry_does_not_overwrite_another_work(identity_db, monkeypatch):
    copy_shadow(identity_db)
    store = Store(identity_db)
    corpus = store.load()
    extra = corpus.sources[0].model_copy(update={"id": "synthetic-extra", "title": "Synthetic extra"})
    corpus.sources.append(extra)
    store.save(corpus)
    with identity_db.connect() as conn:
        collision = conn.scalar(text("SELECT id FROM identity_works ORDER BY id LIMIT 1"))
        before = (
            conn.execute(text("SELECT * FROM identity_works WHERE id=:id"), {"id": collision})
            .mappings()
            .one()
        )
    sequence = iter((collision, mint("wrk")))
    monkeypatch.setattr("api.identity.mint", lambda _: next(sequence))
    copy_shadow(identity_db)
    with identity_db.connect() as conn:
        # Backfill refreshes update timestamp but never substitutes the extra source's fields.
        after = (
            conn.execute(text("SELECT * FROM identity_works WHERE id=:id"), {"id": collision})
            .mappings()
            .one()
        )
        assert after["title"] == before["title"]
        assert conn.scalar(text("SELECT count(*) FROM identity_works")) == 5


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE rights SET payload=jsonb_set(payload::jsonb,'{metadata_public}','false')::json",
        "UPDATE rights SET payload=jsonb_set(payload::jsonb,'{approved_content_hashes}','[]')::json",
        "UPDATE rights SET payload=jsonb_set(payload::jsonb,'{expires_at}','\"2026-01-01T00:00:00Z\"')::json",
        "UPDATE rights SET payload=jsonb_set(payload::jsonb,'{reviewer}','\"changed-reviewer\"')::json",
        "UPDATE rights SET payload=jsonb_set(payload::jsonb,'{evidence}','[]')::json",
    ],
)
def test_changed_policy_blocks_rehearsal_even_with_equal_counts(pg_engine, cleared, mutation):
    migrate(pg_engine)
    Store(pg_engine).save(cleared)
    copy_shadow(pg_engine)
    with pg_engine.begin() as conn:
        conn.execute(text(mutation))
        freeze(conn, expected_epoch=0)
    with pytest.raises(IdentityConflict, match="snapshot changed"), pg_engine.begin() as conn:
        rehearse_cutover(conn, expected_epoch=1, evidence="Synthetic rehearsal")
    with pg_engine.begin() as conn:
        backfill(conn, expected_epoch=1)
        rehearse_cutover(conn, expected_epoch=1, evidence="Synthetic rehearsal after final policy copy")
    with pytest.raises(DBAPIError), pg_engine.begin() as conn:
        check_legacy_authority(conn, write=False)


def test_typed_field_corruption_blocks_reconciliation(identity_db):
    copy_shadow(identity_db)
    with identity_db.begin() as conn:
        conn.execute(text("UPDATE identity_works SET title='Wrong synthetic title'"))
        freeze(conn, expected_epoch=0)
    with pytest.raises(IdentityConflict, match="typed identity"), identity_db.begin() as conn:
        reconcile(conn, expected_epoch=1)


def test_stale_epoch_and_rehearsal_never_fall_back_to_legacy(identity_db):
    copy_shadow(identity_db)
    with identity_db.begin() as conn:
        freeze(conn, expected_epoch=0)
    with pytest.raises(IdentityConflict, match="Stale"), identity_db.begin() as conn:
        backfill(conn, expected_epoch=0)
    with identity_db.begin() as conn:
        assert rehearse_cutover(conn, expected_epoch=1, evidence="Synthetic rehearsal") == 2
    with pytest.raises(DBAPIError):
        Store(identity_db).load()
    with pytest.raises(DBAPIError):
        seed(Store(identity_db))
    with identity_db.begin() as conn:
        assert end_rehearsal(conn, expected_epoch=2) == 3
        assert resume_legacy(conn, expected_epoch=3) == 4
    seed(Store(identity_db))
    assert len(Store(identity_db).load().sources) == 4


@pytest.fixture
def legacy_app_role(identity_db):
    role = "app_" + uuid4().hex
    with identity_db.begin() as conn:
        namespace = conn.scalar(text("SELECT current_schema()"))
        conn.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
        conn.execute(text(f'GRANT USAGE ON SCHEMA "{namespace}" TO "{role}"'))
        for table in RELATIONS:
            conn.execute(text(f'GRANT SELECT,INSERT,UPDATE,DELETE,TRUNCATE ON {table} TO "{role}"'))
    try:
        yield role
    finally:
        with identity_db.begin() as conn:
            conn.execute(text(f'DROP OWNED BY "{role}"'))
            conn.execute(text(f'DROP ROLE "{role}"'))


@pytest.mark.parametrize("table", list(RELATIONS))
def test_direct_application_sql_is_fenced_for_every_legacy_table(identity_db, legacy_app_role, table):
    with identity_db.begin() as conn:
        conn.execute(text(f'SET LOCAL ROLE "{legacy_app_role}"'))
        conn.execute(text(f"UPDATE {table} SET id=id WHERE false"))
    with identity_db.begin() as conn:
        freeze(conn, expected_epoch=0)
    with pytest.raises(DBAPIError) as denied, identity_db.begin() as conn:
        conn.execute(text(f'SET LOCAL ROLE "{legacy_app_role}"'))
        conn.execute(text(f"UPDATE {table} SET id=id WHERE false"))
    assert denied.value.orig.sqlstate == "55000"


def test_application_role_cannot_edit_fence_or_shadow_and_truncate_is_fenced(identity_db, legacy_app_role):
    for query in (
        "UPDATE identity_migration_state SET phase='legacy'",
        "DELETE FROM identity_legacy_snapshots",
        "SELECT payload FROM identity_legacy_snapshots",
        "ALTER TABLE sources DISABLE TRIGGER ALL",
    ):
        with pytest.raises(DBAPIError) as denied, identity_db.begin() as conn:
            conn.execute(text(f'SET LOCAL ROLE "{legacy_app_role}"'))
            conn.execute(text(query))
        assert denied.value.orig.sqlstate == "42501"
    with identity_db.begin() as conn:
        freeze(conn, expected_epoch=0)
    with pytest.raises(DBAPIError) as denied, identity_db.begin() as conn:
        conn.execute(text(f'SET LOCAL ROLE "{legacy_app_role}"'))
        conn.execute(text("TRUNCATE citations"))
    assert denied.value.orig.sqlstate == "55000"


def wait_for_lock(engine, ready, pid):
    assert ready.wait(2)
    deadline = monotonic() + 2
    while monotonic() < deadline:
        with engine.connect() as conn:
            if (
                conn.scalar(
                    text("SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"), {"pid": pid[0]}
                )
                == "Lock"
            ):
                return
        Event().wait(0.02)
    pytest.fail("Concurrent connection did not reach the expected database lock")


def test_freeze_waits_for_existing_direct_sql_writer(identity_db, legacy_app_role):
    copy_shadow(identity_db)
    writer = identity_db.connect()
    transaction = writer.begin()
    ready, pid = Event(), []

    def freezing():
        with identity_db.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout='3s'"))
            pid.append(conn.scalar(text("SELECT pg_backend_pid()")))
            ready.set()
            freeze(conn, expected_epoch=0)

    try:
        writer.execute(text(f'SET LOCAL ROLE "{legacy_app_role}"'))
        writer.execute(
            text("UPDATE rights SET payload=jsonb_set(payload::jsonb,'{metadata_public}','false')::json")
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(freezing)
            wait_for_lock(identity_db, ready, pid)
            transaction.commit()
            future.result(timeout=4)
        with pytest.raises(IdentityConflict), identity_db.begin() as conn:
            reconcile(conn, expected_epoch=1)
    finally:
        if transaction.is_active:
            transaction.rollback()
        writer.close()


def test_queued_writer_rechecks_phase_after_freeze_commits(identity_db, legacy_app_role):
    ready, pid = Event(), []

    def stale_writer():
        with pytest.raises(DBAPIError) as denied, identity_db.begin() as conn:
            conn.execute(text(f'SET LOCAL ROLE "{legacy_app_role}"'))
            conn.execute(text("SET LOCAL lock_timeout='3s'"))
            pid.append(conn.scalar(text("SELECT pg_backend_pid()")))
            ready.set()
            conn.execute(text("UPDATE rights SET payload=payload"))
        assert denied.value.orig.sqlstate == "55000"

    with ThreadPoolExecutor(max_workers=1) as pool:
        with identity_db.begin() as conn:
            freeze(conn, expected_epoch=0)
            future = pool.submit(stale_writer)
            wait_for_lock(identity_db, ready, pid)
        future.result(timeout=4)


def test_bounded_identity_lookup_checks_current_policy_without_whole_corpus(identity_db):
    copy_shadow(identity_db)
    source_id = Store(identity_db).load().sources[0].id
    queries = []

    def track(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    event.listen(identity_db, "before_cursor_execute", track)
    try:
        with identity_db.begin() as conn:
            result = IdentityRepository(conn).public_identity(source_id)
            assert result.legacy_source_id == source_id
            assert set(result.model_dump()) == {
                "legacy_source_id",
                "work_id",
                "title",
                "citation",
                "reference_url",
            }
        assert len(queries) == 4
        assert all(
            "WHERE" in query or "identity_check_legacy_authority" in query or "to_regclass" in query
            for query in queries
        )
    finally:
        event.remove(identity_db, "before_cursor_execute", track)
    with identity_db.begin() as conn:
        conn.execute(
            text("UPDATE rights SET payload=jsonb_set(payload::jsonb,'{metadata_public}','false')::json")
        )
    with identity_db.begin() as conn:
        assert IdentityRepository(conn).public_identity(source_id) is None


def test_expired_metadata_decision_denies_new_identity_projection(identity_db):
    copy_shadow(identity_db)
    source_id = Store(identity_db).load().sources[0].id
    with identity_db.begin() as conn:
        conn.execute(
            text("""
            UPDATE rights SET payload=jsonb_set(payload::jsonb,'{expires_at}',
                '\"2026-01-01T00:00:00Z\"')::json
        """)
        )
    with identity_db.begin() as conn:
        assert IdentityRepository(conn).public_identity(source_id) is None


def test_state_check_and_source_version_ownership_are_database_enforced(identity_db, cleared):
    Store(identity_db).save(cleared)
    copy_shadow(identity_db)
    source_id = cleared.sources[1].id
    with pytest.raises(IntegrityError), identity_db.begin() as conn:
        conn.execute(text("UPDATE identity_legacy_versions SET source_id=:id"), {"id": source_id})
    for query in (
        "INSERT INTO identity_migration_state(singleton) VALUES(false)",
        "UPDATE identity_migration_state SET phase='rehearsal'",
        "UPDATE identity_migration_state SET phase='target'",
    ):
        with pytest.raises(IntegrityError), identity_db.begin() as conn:
            conn.execute(text(query))


def test_identical_synthetic_text_keeps_distinct_work_and_version_identities(pg_engine, cleared):
    migrate(pg_engine)
    duplicate = cleared.versions[0].model_copy(
        update={"id": "synthetic-same-bytes", "source_id": cleared.sources[1].id}
    )
    cleared.versions.append(duplicate)
    Store(pg_engine).save(cleared)
    copy_shadow(pg_engine)
    with pg_engine.connect() as conn:
        rows = list(
            conn.execute(
                text("""
            SELECT v.version_id,v.content_hash,s.work_id FROM identity_legacy_versions v
            JOIN identity_legacy_sources s ON s.source_id=v.source_id
        """)
            ).mappings()
        )
    assert len(rows) == 2
    assert len({r["content_hash"] for r in rows}) == 1
    assert len({r["work_id"] for r in rows}) == 2


def test_shadow_downgrade_requires_legacy_phase_and_preserves_pilot(identity_db):
    before = Store(identity_db).load().model_dump(mode="json")
    copy_shadow(identity_db)
    with identity_db.begin() as conn:
        freeze(conn, expected_epoch=0)
    with pytest.raises(RuntimeError, match="write.*fenced|authority is fenced"), identity_db.begin() as conn:
        config = Config("alembic.ini")
        config.attributes["connection"] = conn
        command.downgrade(config, "0001")
    with identity_db.begin() as conn:
        resume_legacy(conn, expected_epoch=1)
        config = Config("alembic.ini")
        config.attributes["connection"] = conn
        command.downgrade(config, "0001")
    assert Store(identity_db).load().model_dump(mode="json") == before
    migrate(identity_db)
    assert copy_shadow(identity_db)
