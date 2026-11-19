"""The multi-agent claim on PostgreSQL, where its guarantees have to hold (the design's sections 3, 4
and 14; docs are not in the repository). The other multi-agent tests run on SQLite, which takes one
writer at a time and so cannot show a race. Here: a review database made before the change is upgraded
in place, however many copies of the workbench start together; one claim per label is held by the
database; and many agents claiming at the same moment never share a work."""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from workbench import claims
from workbench.store import Store, connect

pytestmark = pytest.mark.postgres

# What the change adds to tables that exist already, and the tables it brings.
ADDED_COLUMNS = {
    "operator": ("agent", "may_change_approved"),
    "page_revision": ("label", "task"),
    "detail_revision": ("label", "task"),
}
NEW_TABLES = ("work_claim", "blocked_label", "review_audit", "work_release")
ACCOUNT = "reviewer-claude@example.org"


@pytest.fixture
def scratch_url():
    """A database of its own on the test server, dropped afterwards."""
    base = make_url(os.environ["TEST_DATABASE_URL"])
    name = f"oa_ma_{uuid.uuid4().hex[:12]}"
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as db:
            db.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as error:  # noqa: BLE001 - a login that may not create databases cannot run these
        admin.dispose()
        pytest.skip(f"the test login cannot create a scratch database: {error}")
    yield base.set(database=name).render_as_string(hide_password=False)
    with admin.connect() as db:
        db.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def together(count: int, action) -> list:
    """Run ``action(n)`` on ``count`` threads at once; each result, or the exception it raised."""

    def run(n: int):
        try:
            return action(n)
        except Exception as error:  # noqa: BLE001 - the caller sorts results from refusals
            return error

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(run, range(count)))


def live_claims(engine) -> list:
    with engine.connect() as db:
        return db.execute(
            text("SELECT work_id, account, label FROM work_claim WHERE expires_at > CURRENT_TIMESTAMP")
        ).all()


def test_copies_of_the_workbench_starting_together_make_the_database_once(scratch_url):
    # Six copies start at the same moment on an empty database, as after a release with several instances.
    started = together(6, lambda n: connect(scratch_url))
    failed = [error for error in started if isinstance(error, Exception)]
    assert not failed, failed
    engine = started[0]
    found = inspect(engine)
    assert set(NEW_TABLES) <= set(found.get_table_names())
    index = next(i for i in found.get_indexes("work_claim") if i["name"] == "work_claim_account_label_uq")
    assert index["unique"] and index["column_names"] == ["account", "label"]
    assert "unlabelled" in str(index["dialect_options"]["postgresql_where"])
    for made in started:
        made.dispose()


def test_a_database_from_before_the_change_is_upgraded_in_place_with_its_rows_kept(scratch_url):
    engine = connect(scratch_url)
    # Put it back as the release before left it: none of the new tables, columns or indexes.
    with engine.begin() as db:
        for table in NEW_TABLES:
            db.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        for table, columns in ADDED_COLUMNS.items():
            for column in columns:
                db.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}"))
        for name in ("audit_event_actor_at_idx", "audit_event_act_id_id_idx"):
            db.execute(text(f"DROP INDEX IF EXISTS {name}"))
        now = datetime.now(UTC)
        db.execute(
            text(
                "INSERT INTO operator (email, name, roles, active, created_at) "
                "VALUES ('owner@example.org', 'The Owner', 'admin,approver,reviewer', true, :now)"
            ),
            {"now": now},
        )
        db.execute(
            text(
                "INSERT INTO page_revision (act_id, page, revision, reading_sha256, units, status, resolutions,"
                " comment, author, created_at) VALUES ('rw-law-1-en', 1, 1, :sha, '[]', 'checked', '{}', '',"
                " 'owner@example.org', :now)"
            ),
            {"sha": "0" * 64, "now": now},
        )
    engine.dispose()

    # Four copies of the new workbench start on it together.
    started = together(4, lambda n: connect(scratch_url))
    failed = [error for error in started if isinstance(error, Exception)]
    assert not failed, failed
    engine = started[0]
    found = inspect(engine)
    assert set(NEW_TABLES) <= set(found.get_table_names())
    for table, columns in ADDED_COLUMNS.items():
        present = {column["name"]: column for column in found.get_columns(table)}
        assert set(columns) <= set(present), table
    operator = {column["name"]: column for column in found.get_columns("operator")}
    assert not operator["agent"]["nullable"] and not operator["may_change_approved"]["nullable"]
    with engine.connect() as db:
        # The owner's row is kept, and is no agent; the old page revision is kept, with no label.
        assert db.execute(text("SELECT agent, may_change_approved, roles FROM operator")).one() == (
            False,
            False,
            "admin,approver,reviewer",
        )
        assert db.execute(text("SELECT revision, label, task FROM page_revision")).one() == (1, None, None)
    # Starting again changes nothing and refuses nothing.
    connect(scratch_url).dispose()
    for made in started:
        made.dispose()


def test_fifty_agents_claiming_at_once_each_get_a_different_work(scratch_url):
    store = Store(connect(scratch_url))
    works = [f"rw-law-{n:03d}" for n in range(200)]
    got = together(50, lambda n: claims.claim_next(store, works, ACCOUNT, f"claude-m1-{n:02d}"))
    refused = [g for g in got if isinstance(g, Exception)]
    assert not refused, refused[:3]
    assert len({g["work_id"] for g in got}) == 50  # no work handed to two agents
    assert len({g["token"] for g in got}) == 50
    held = live_claims(store.engine)
    assert len(held) == 50 and len({row.work_id for row in held}) == 50
    store.engine.dispose()


def test_one_work_wanted_by_many_goes_to_exactly_one(scratch_url):
    store = Store(connect(scratch_url))
    got = together(30, lambda n: claims.claim_next(store, ["rw-law-only"], ACCOUNT, f"claude-m2-{n:02d}"))
    won = [g for g in got if isinstance(g, dict)]
    lost = [g for g in got if isinstance(g, claims.ClaimError)]
    assert len(won) == 1 and len(lost) == 29, got
    assert {error.status_code for error in lost} == {404}
    assert [row.work_id for row in live_claims(store.engine)] == ["rw-law-only"]
    store.engine.dispose()


def test_one_label_asking_for_many_works_at_once_holds_only_one(scratch_url):
    store = Store(connect(scratch_url))
    got = together(
        20,
        lambda n: claims.claim_work(store, f"rw-law-{n:03d}", ACCOUNT, "claude-m3-01", check_limit=False),
    )
    won = [g for g in got if isinstance(g, dict)]
    lost = [g for g in got if isinstance(g, claims.ClaimError)]
    assert len(won) == 1, got
    assert len(lost) == 19 and {error.status_code for error in lost} == {409}
    held = live_claims(store.engine)
    assert len(held) == 1 and held[0].label == "claude-m3-01"  # the unique index, not a check, holds this
    store.engine.dispose()


def test_a_lapsed_claim_is_taken_over_by_exactly_one_agent(scratch_url):
    store = Store(connect(scratch_url))
    first = claims.claim_work(store, "rw-law-lapsed", ACCOUNT, "claude-m4-00", check_limit=False)
    with store.engine.begin() as db:
        db.execute(
            text("UPDATE work_claim SET expires_at = :past WHERE work_id = 'rw-law-lapsed'"),
            {"past": datetime.now(UTC) - timedelta(minutes=5)},
        )
    got = together(
        20, lambda n: claims.claim_next(store, ["rw-law-lapsed"], ACCOUNT, f"claude-m4-{n + 1:02d}")
    )
    won = [g for g in got if isinstance(g, dict)]
    assert len(won) == 1, got
    assert won[0]["token"] != first["token"]
    held = live_claims(store.engine)
    assert len(held) == 1 and held[0].label != "claude-m4-00"
    store.engine.dispose()
