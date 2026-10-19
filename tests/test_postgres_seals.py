"""MAC-13.7: the audit log's daily seals verify, and any change to a sealed day shows (accounts.seals)."""

import io
import json
from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine

from accounts import privacy, seals
from accounts.ids import new_id
from accounts.store import Accounts
from rights.database import PolicyNamespace

pytestmark = pytest.mark.postgres
TODAY = datetime.now(UTC).date()
LATER = datetime.combine(TODAY + timedelta(days=1), time(2), tzinfo=UTC)  # today has ended, plus the grace


@pytest.fixture
def accounts(pg_engine):
    migrate(pg_engine)
    with pg_engine.connect() as conn:
        return Accounts(
            pg_engine, PolicyNamespace(conn.scalar(text("SELECT current_schema()"))), key_pepper=b"k" * 32
        )


def entry(accounts, at, action="key.created"):
    with accounts.engine.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO {accounts._t(conn, 'audit_event')} (id, at, actor_type, actor_ref, action, summary)"
                " VALUES (:id, :at, 'person', 'act_x', :action, '{\"name\": \"laptop\"}')"
            ),
            {"id": new_id("evt"), "at": at, "action": action},
        )


def tamper(accounts, sql, table="audit_event"):
    """What only someone bypassing the service could do: switch the append-only trigger off."""
    with accounts.engine.begin() as conn:
        name = accounts._t(conn, table)
        conn.execute(text(f"ALTER TABLE {name} DISABLE TRIGGER USER"))
        conn.execute(text(sql.format(t=name)))
        conn.execute(text(f"ALTER TABLE {name} ENABLE TRIGGER USER"))


def problems(accounts, now=LATER):
    return [(p["day"], p["problem"]) for p in seals.verify(accounts, now=now)["problems"]]


def days_ago(n, hour=12):
    return datetime.combine(TODAY - timedelta(days=n), time(hour), tzinfo=UTC)


def test_every_ended_day_is_sealed_once_in_a_chain_and_verifies(accounts):
    assert seals.seal(accounts, now=LATER) == []  # nothing logged yet, nothing to seal
    entry(accounts, days_ago(3))
    entry(accounts, days_ago(3, hour=18))
    entry(accounts, days_ago(1))
    out = io.StringIO()
    sealed = seals.seal(accounts, now=LATER, out=out)
    # Every day from the first entry's to today, the empty one included, chained in order.
    assert [s["day"] for s in sealed] == [(TODAY - timedelta(days=n)).isoformat() for n in (3, 2, 1, 0)]
    assert [s["entries"] for s in sealed] == [2, 0, 1, 0]
    assert sealed[0]["previous_hash"] is None and sealed[1]["previous_hash"] == sealed[0]["seal_hash"]
    logged = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [line["audit_seal"]["seal_hash"] for line in logged] == [s["seal_hash"] for s in sealed]
    assert seals.seal(accounts, now=LATER) == []  # sealed once
    report = seals.verify(accounts, now=LATER)
    assert report == {"days": 4, "past_retention": 0, "unsealed_days": 0, "ok": True, "problems": []}
    # A day that has not been over for an hour waits.
    entry(accounts, datetime.combine(TODAY + timedelta(days=1), time(0, 30), tzinfo=UTC))
    assert seals.seal(accounts, now=LATER + timedelta(days=1) - timedelta(hours=1, minutes=30)) == []


class WriteOnceStore:
    """A container with an immutability policy: blobs are added, never replaced (AzureBlobStore.put)."""

    def __init__(self):
        self.blobs = {}

    def list(self, prefix):
        return [name for name in self.blobs if name.startswith(prefix)]

    def put(self, name, data, content_type, metadata=None, replace=False):
        if name in self.blobs:
            return False
        self.blobs[name] = data
        return True


def test_each_seal_is_copied_once_to_the_container_and_a_missed_one_later(accounts):
    store = WriteOnceStore()
    assert seals.copy_out(accounts, store) == 0  # nothing sealed yet
    entry(accounts, days_ago(2))
    out = io.StringIO()
    sealed = seals.seal(accounts, now=LATER, out=out)
    assert seals.copy_out(accounts, store) == 3
    # Each copy holds the same fields as the seal's log line.
    logged = {
        line["audit_seal"]["day"]: line["audit_seal"] for line in map(json.loads, out.getvalue().splitlines())
    }
    assert {name: json.loads(data) for name, data in store.blobs.items()} == {
        f"{s['day']}.json": logged[s["day"]] for s in sealed
    }
    assert seals.copy_out(accounts, store) == 0  # copied once
    # A day whose copy a failed run never wrote is written by the next run; the others are kept.
    missed = f"{sealed[1]['day']}.json"
    kept = store.blobs.pop(missed)
    entry(accounts, datetime.combine(TODAY + timedelta(days=1), time(9), tzinfo=UTC))
    assert len(seals.seal(accounts, now=LATER + timedelta(days=1), out=io.StringIO())) == 1
    assert seals.copy_out(accounts, store) == 2 and store.blobs[missed] == kept
    assert len(store.blobs) == 4


def test_changing_removing_or_back_dating_an_entry_breaks_that_days_seal(accounts):
    entry(accounts, days_ago(2))
    entry(accounts, days_ago(1))
    entry(accounts, days_ago(1, hour=15))
    seals.seal(accounts, now=LATER)
    day2, day1 = ((TODAY - timedelta(days=n)).isoformat() for n in (2, 1))
    tamper(accounts, f"UPDATE {{t}} SET summary = '{{{{}}}}' WHERE at = '{days_ago(2).isoformat()}'")
    assert problems(accounts) == [(day2, "entries_changed")]
    tamper(accounts, f"DELETE FROM {{t}} WHERE at = '{days_ago(1, hour=15).isoformat()}'")
    assert problems(accounts) == [(day2, "entries_changed"), (day1, "entries_changed")]


def test_a_back_dated_entry_or_an_altered_seal_shows(accounts):
    entry(accounts, days_ago(2))
    entry(accounts, days_ago(1))
    seals.seal(accounts, now=LATER)
    day2, day1 = ((TODAY - timedelta(days=n)).isoformat() for n in (2, 1))
    entry(accounts, days_ago(4))  # before the chain began
    entry(accounts, days_ago(1, hour=20))  # into a sealed day
    assert problems(accounts) == [(day1, "entries_changed"), (day2, "entries_before_first_seal")]
    with pytest.raises(DBAPIError, match="append_only"), accounts.engine.begin() as conn:
        conn.execute(text(f"UPDATE {accounts._t(conn, 'audit_seal')} SET entries = 9"))
    tamper(accounts, f"UPDATE {{t}} SET seal_hash = '{'0' * 64}' WHERE day = '{day2}'", "audit_seal")
    found = problems(accounts)
    assert (day2, "seal_altered") in found and (day1, "chain_broken") in found


def test_days_past_retention_verify_through_the_chain_once_purged(accounts):
    entry(accounts, days_ago(800))
    entry(accounts, days_ago(1))
    seals.seal(accounts, out=io.StringIO())
    assert privacy.purge(accounts)["audit_entries"] == 1  # the two-year-old entry; whole days only
    report = seals.verify(accounts)
    # The days more than two years old (about 70 of the 800) are past retention.
    assert report["ok"] and 66 <= report["past_retention"] <= 71 and report["days"] == 800  # to yesterday


def test_sealing_that_falls_behind_is_a_problem(accounts):
    entry(accounts, days_ago(1))
    seals.seal(accounts, now=LATER)
    assert seals.verify(accounts, now=LATER + timedelta(days=1))["ok"]  # one day may wait for the job
    report = seals.verify(accounts, now=LATER + timedelta(days=3))
    assert not report["ok"] and report["unsealed_days"] == 3
    assert report["problems"] == [
        {"day": (TODAY + timedelta(days=1)).isoformat(), "problem": "sealing_behind"}
    ]
