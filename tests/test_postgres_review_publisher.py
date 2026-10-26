"""The publisher's own login to the review database (workbench.store.provision_publisher): it reads what
publishing needs, writes only the publishing tables, and can read no session, passkey or enrolment code
and change no review."""

import secrets
import string
from uuid import uuid4

import pytest
from postgres_support import validated_test_url
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.pool import NullPool

from operations import publish
from workbench import store as review

pytestmark = pytest.mark.postgres
FORBIDDEN = (
    "SELECT * FROM session",
    "SELECT * FROM operator_passkey",
    "SELECT * FROM passkey_enrolment",
    "SELECT * FROM legal_event_revision",
    "SELECT * FROM auth_flow",
    "SELECT * FROM audit_event",
    "UPDATE operator SET name = 'x'",
    "UPDATE approval SET decision = 'approved'",
    "DELETE FROM approval",
    "INSERT INTO page_revision (act_id) VALUES ('x')",
    "DELETE FROM publication_status",
    "CREATE TABLE intruder (id int)",
)


def test_the_publisher_reads_what_publishing_needs_and_writes_only_its_own_tables(monkeypatch):
    url = validated_test_url()
    name = "oa_" + uuid4().hex + "_test"
    admin = create_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    password = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))
    engines = []
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        owner_url = url.set(database=name).render_as_string(hide_password=False)
        publisher_url = url.set(database=name, username=review.PUBLISHER, password=password)
        monkeypatch.setenv(
            "ALVARY_REVIEW_PUBLISHER_DATABASE_URL", publisher_url.render_as_string(hide_password=False)
        )
        owner = review.connect(owner_url)
        engines.append(owner)
        store = review.Store(owner)
        store.add_operator("app@example.org", "App", {"approver"})
        publisher = create_engine(publisher_url, poolclass=NullPool)
        engines.append(publisher)
        with publisher.connect() as db:
            assert db.execute(text("SELECT count(*) FROM approval")).scalar() == 0
            assert db.execute(text("SELECT name FROM operator")).scalar() == "App"
            assert db.execute(text("SELECT count(*) FROM detail_revision")).scalar() == 0
        for statement in FORBIDDEN:
            with publisher.connect() as db, pytest.raises(ProgrammingError, match="permission denied"):
                db.execute(text(statement))
        # Its own record of a run, which the workbench reads.
        log = publish.PublicationLog(publisher)
        run, trigger, _ = log.start()
        row = {
            "document_id": "rw-x",
            "work": "rw-x",
            "jurisdiction": "rw",
            "details": "public",
            "text": "no_decision",
            "reason": "",
        }
        log.record(run, [row], {})
        log.record(run, [row], log.statuses())  # an update, not a second row
        log.finish(run, "succeeded", {"countries": {}})
        assert trigger == "schedule" and store.publication_statuses()["rw-x"]["details"] == "public"
        assert [r["status"] for r in store.publication_runs()] == ["succeeded"]
        # The workbench starting again brings the login up to date; a weak password is refused.
        engines.append(review.connect(owner_url))
        with publisher.connect() as db:
            db.execute(text("SELECT 1 FROM approval LIMIT 1"))
        with pytest.raises(SystemExit, match="generated"):
            review.provision_publisher(owner, "short'; DROP TABLE approval; --")
    finally:
        for engine in engines:
            engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
            conn.execute(text(f"DROP ROLE IF EXISTS {review.PUBLISHER}"))
        admin.dispose()
