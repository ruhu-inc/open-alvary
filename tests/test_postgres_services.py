"""Bounded services against actual migrated PostgreSQL and restricted target readers."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_postgres_policy import foundation as foundation

from api.catalogue import LegacyCatalogue, TargetCatalogue
from api.main import create_app
from api.repositories import LegacyRepository
from api.store import Store, seed
from rights.database import PolicyRepository

pytestmark = pytest.mark.postgres


def test_pg_legacy_page_is_bounded_and_never_loads_whole_corpus(pg_engine, monkeypatch):
    migrate(pg_engine)
    store = Store(pg_engine)
    seed(store)

    def forbidden():
        raise AssertionError("No whole-corpus load")

    monkeypatch.setattr(store, "load", forbidden)
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(pg_engine, "before_cursor_execute", capture)
    try:
        result = TestClient(create_app(store)).get("/sources?limit=2&offset=1")
        assert result.status_code == 200
        assert result.json()["total"] == 4 and len(result.json()["items"]) == 2
        assert len(statements) == 4  # authority lookup/check, count, bounded joined page
        assert "LIMIT" in statements[-1] and "OFFSET" in statements[-1]
        assert not any("versions" in statement or "staging" in statement for statement in statements)
    finally:
        event.remove(pg_engine, "before_cursor_execute", capture)


def test_pg_gate_checks_precede_approved_body_and_anchor_validation(pg_engine, cleared):
    migrate(pg_engine)
    Store(pg_engine).save(cleared)
    with pg_engine.begin() as conn:
        service = LegacyCatalogue(LegacyRepository(conn))
        result = service.versions(cleared.sources[0].id)
        assert result.items[0].canonical_text == cleared.versions[0].canonical_text
        assert result.structure[0].source_hash == result.items[0].content_hash


def test_target_service_reads_only_eligible_view_and_keys_its_page(foundation):
    f = foundation
    f.decision(redistribute_metadata="allow")
    with f.engine.begin() as conn:
        works = list(
            conn.scalars(text("SELECT id FROM identity_works WHERE document_class='act' ORDER BY id"))
        )
        conn.execute(
            text(
                "UPDATE identity_works SET collection_id=:id,collection_unknown_reason=NULL WHERE document_class='act'"
            ),
            {"id": f.collection},
        )
    with f.as_role("release") as conn:
        for work in works:
            conn.execute(
                text(
                    f"INSERT INTO {f.name(conn, 'corpus', 'metadata_projection')} VALUES (:work,:cid,'Synthetic','Fixture','https://example.org/synthetic',false)"
                ),
                {"work": work, "cid": f.collection},
            )
    with f.as_role("serving") as conn:
        service = TargetCatalogue(PolicyRepository(conn, f.ns))
        first = service.page(limit=2)
        assert [item.work_id for item in first.items] == works[:2]
        assert first.next_after == works[1]
        second = service.page(limit=2, after=first.next_after)
        assert [item.work_id for item in second.items] == works[2:]
        assert second.next_after is None
        assert service.metadata(works[0]).work_id == works[0]
    f.decision(revision=2, redistribute_metadata="deny")
    with f.as_role("serving") as conn:
        assert TargetCatalogue(PolicyRepository(conn, f.ns)).page().items == ()


def test_services_never_commit_the_callers_transaction(foundation):
    f = foundation
    f.decision(redistribute_metadata="allow")
    with f.engine.connect() as conn:
        transaction = conn.begin()
        role = conn.dialect.identifier_preparer.quote_identifier(f.roles["release"])
        conn.execute(text(f"SET LOCAL SESSION AUTHORIZATION {role}"))
        conn.execute(
            text(
                f"INSERT INTO {f.name(conn, 'corpus', 'metadata_projection')} VALUES (:work,:cid,'Synthetic','Fixture','https://example.org/synthetic',false)"
            ),
            {"work": f.work, "cid": f.collection},
        )
        repository = PolicyRepository(conn, f.ns)
        assert TargetCatalogue(repository).metadata(f.work) is not None
        assert repository.lock_collection(f.collection, expected_revision=1) == 1
        assert transaction.is_active
        transaction.rollback()
    with f.as_role("serving") as conn:
        assert TargetCatalogue(PolicyRepository(conn, f.ns)).metadata(f.work) is None
