"""LIF-01 audited metadata publication: command-only writes, receipts, revisions and audit."""

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_postgres import pg_engine as pg_engine
from test_postgres_policy import foundation as foundation

pytestmark = pytest.mark.postgres


def audit(f):
    with f.engine.connect() as conn:
        return conn.execute(
            text(
                f"SELECT kind,scope_id,revision FROM {f.name(conn, 'policy', 'audit_event')}"
                " WHERE kind LIKE 'metadata_%' ORDER BY revision"
            )
        ).all()


@pytest.mark.parametrize("statement", ["INSERT", "UPDATE"])
def test_release_role_cannot_write_projection_directly(foundation, statement):
    f = foundation
    f.release()
    sql = (
        "INSERT INTO {t} VALUES (:w,:c,'x','y','https://example.org/x',false)"
        if statement == "INSERT"
        else "UPDATE {t} SET suspended=false WHERE work_id=:w"
    )
    with pytest.raises(DBAPIError) as denied, f.as_role("release") as conn:
        conn.execute(
            text(sql.format(t=f.name(conn, "corpus", "metadata_projection"))),
            {"w": f.work, "c": f.collection},
        )
    assert denied.value.orig.sqlstate == "42501"


def test_project_and_suspend_are_audited_revisions(foundation):
    f = foundation
    assert tuple(f.release()) == (1, False)
    assert tuple(f.release("suspend_metadata", expected=1)) == (2, False)
    assert audit(f) == [("metadata_project_metadata", f.work, 1), ("metadata_suspend_metadata", f.work, 2)]


def test_stale_revision_and_changed_retry_conflict(foundation):
    f = foundation
    f.release()
    with pytest.raises(DBAPIError) as stale:
        f.release("suspend_metadata", expected=0)
    assert stale.value.orig.sqlstate == "40001"


def test_identical_retry_replays_and_records_once(foundation):
    f = foundation
    envelope_id = uuid4()
    import json

    body = {
        "command_id": str(envelope_id),
        "collection_id": f.collection,
        "work_id": f.work,
        "action": "project_metadata",
        "expected_revision": 0,
        "evidence_id": str(f.evidence),
        "reason": "Synthetic release decision",
        "payload": {"title": "T", "citation": "C", "reference_url": "https://example.org/t"},
    }
    for expected in [(1, False), (1, True)]:
        with f.as_role("release") as conn:
            fn = f.name(conn, "policy", "apply_release_command")
            row = conn.execute(text(f"SELECT * FROM {fn}(CAST(:e AS jsonb))"), {"e": json.dumps(body)}).one()
            assert tuple(row) == expected
    assert len(audit(f)) == 1


@pytest.mark.parametrize("role", ["serving", "review", "acquisition"])
def test_only_release_role_may_publish(foundation, role):
    f = foundation
    with pytest.raises(DBAPIError) as denied, f.as_role(role) as conn:
        f.release(conn=conn)
    assert denied.value.orig.sqlstate == "42501"


def test_release_cannot_move_a_work_between_collections(foundation):
    f = foundation
    f.release()
    with f.engine.begin() as conn:
        conn.execute(
            text(f"INSERT INTO {f.name(conn, 'policy', 'assignment')} VALUES (:a,:c,'release')"),
            {"a": f.actors["release"], "c": f.other_collection},
        )
        conn.execute(
            text(f"INSERT INTO {f.name(conn, 'policy', 'evidence')} VALUES (:id,:c,'private://x',:h,:now)"),
            {"id": uuid4(), "c": f.other_collection, "h": "e" * 64, "now": f.now},
        )
    other = type(f)(**{**f.__dict__, "collection": f.other_collection})
    with pytest.raises(DBAPIError):
        other.release(expected=1)
