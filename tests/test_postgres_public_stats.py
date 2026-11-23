"""The home page's live totals against the database (api.public_stats, migration 0058).

policy.public_usage_totals() adds up the last 30 days of the website's day totals and the credential gate's
requests, withholds any total under 10, counts organisations over the last 7 days only, and is all the serving
role may do with it: the tables stay closed. /site/v1/stats serves it beside the catalogue's own counts."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_v1_text import UNITS, publish_act

from api import public_stats
from api.service import create_service
from api.store import Store
from rights.database import PolicyNamespace, provision_roles

pytestmark = pytest.mark.postgres


@pytest.fixture
def namespace(pg_engine):
    migrate(pg_engine)
    with pg_engine.connect() as connection:
        return PolicyNamespace(connection.scalar(text("SELECT current_schema()")))


def today(connection):
    return connection.scalar(text("SELECT (statement_timestamp() AT TIME ZONE 'UTC')::date"))


def count(connection, namespace, days_ago: int, metric: str, label: str, n: int) -> None:
    table = namespace.qualified(connection, "accounts", "traffic_day")
    connection.execute(
        text(f"INSERT INTO {table} (day, metric, label, count) VALUES (:d, :m, :l, :n)"),
        {"d": today(connection) - timedelta(days=days_ago), "m": metric, "l": label, "n": n},
    )


def used(connection, namespace, organisation: str, days_ago: int, kind: str, n: int) -> None:
    # usage_day names an organisation without a key to it, so a total needs no account.
    table = namespace.qualified(connection, "accounts", "usage_day")
    connection.execute(
        text(
            f"INSERT INTO {table} (organisation_id, day, credential, class, requests)"
            " VALUES (:o, :d, :c, :k, :n)"
        ),
        {
            "o": organisation,
            "d": today(connection) - timedelta(days=days_ago),
            "c": "key_" + organisation.encode().hex().ljust(32, "0")[:32],
            "k": kind,
            "n": n,
        },
    )


def totals(connection, namespace) -> dict:
    return connection.scalar(
        text(f"SELECT {namespace.qualified(connection, 'policy', 'public_usage_totals')}()")
    )


def test_an_empty_database_gives_every_total_as_withheld(pg_engine, namespace):
    with pg_engine.begin() as connection:
        assert totals(connection, namespace) == {
            "days": 30,
            "texts_read": None,
            "searches": None,
            "searches_answered": None,
            "api_requests": None,
            "organisations_week": None,
        }


def test_totals_cover_thirty_days_and_any_under_ten_is_withheld(pg_engine, namespace):
    with pg_engine.begin() as connection:
        count(connection, namespace, 0, "records", "texts", 7)
        count(connection, namespace, 29, "records", "texts", 5)
        count(connection, namespace, 30, "records", "texts", 1000)  # a day past the window
        count(connection, namespace, 0, "records", "views", 500)  # a record opened, not its text read
        count(connection, namespace, 1, "searches", "all", 40)
        count(connection, namespace, 1, "searches", "none_found", 33)  # 7 found something: withheld
        count(connection, namespace, 2, "bots", "other", 900)
        found = totals(connection, namespace)
    assert found["texts_read"] == 12
    assert found["searches"] == 40
    assert found["searches_answered"] is None


def test_requests_are_rest_and_mcp_and_organisations_are_this_weeks(pg_engine, namespace):
    with pg_engine.begin() as connection:
        for number in range(12):
            organisation = f"org-{number:02d}"
            # Ten active in the last 7 days, two only before.
            used(connection, namespace, organisation, 2 if number < 10 else 10, "rest", 3)
        used(connection, namespace, "org-00", 3, "mcp", 4)
        used(connection, namespace, "org-01", 4, "bulk", 1000)  # downloads are not requests
        used(connection, namespace, "org-02", 31, "rest", 1000)  # past the window
        found = totals(connection, namespace)
    assert found["api_requests"] == 12 * 3 + 4
    assert found["organisations_week"] == 10


def test_the_serving_role_can_ask_for_the_totals_and_cannot_read_the_tables(pg_engine, namespace):
    with pg_engine.begin() as connection:
        roles = provision_roles(connection, namespace)
    try:
        with pg_engine.connect() as connection:
            quoted = connection.dialect.identifier_preparer.quote_identifier(roles["serving"])
            connection.execute(text(f"SET ROLE {quoted}"))
            assert totals(connection, namespace)["days"] == 30
            for table in ("traffic_day", "usage_day"):
                savepoint = connection.begin_nested()
                with pytest.raises(DBAPIError):
                    connection.execute(
                        text(f"SELECT count(*) FROM {namespace.qualified(connection, 'accounts', table)}")
                    )
                savepoint.rollback()
            connection.rollback()
    finally:
        with pg_engine.begin() as connection:
            quote = connection.dialect.identifier_preparer.quote_identifier
            for kind in ("policy", "staging", "corpus"):
                connection.execute(text(f"DROP SCHEMA {quote(namespace.schema(kind))} CASCADE"))
            for role in roles.values():
                connection.execute(text(f"DROP OWNED BY {quote(role)}"))
                connection.execute(text(f"DROP ROLE {quote(role)}"))


def test_the_site_serves_the_catalogue_and_the_usage_totals_together(
    pg_engine, namespace, monkeypatch, tmp_path
):
    for name in ("ALVARY_ACCOUNTS", "ALVARY_ADMISSION_CONFIG", "ALVARY_SITE_ADMISSION_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALVARY_POLICY_SCHEMA", namespace.base)
    store = Store(pg_engine)
    publish_act(store, UNITS)
    with pg_engine.begin() as connection:
        count(connection, namespace, 0, "searches", "all", 25)
        count(connection, namespace, 0, "searches", "none_found", 5)
    frontend = tmp_path / "site"
    frontend.mkdir()
    (frontend / "index.html").write_text("<html></html>")
    with TestClient(create_service(store=store, frontend=frontend)) as client:
        response = client.get("/site/v1/stats")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        stats = response.json()
        assert stats["catalogue"]["verified_texts"] == 1
        assert stats["catalogue"]["official_sources"] >= 1
        assert stats["catalogue"]["rights_basis_share"] == 1.0
        assert stats["catalogue"]["jurisdictions"] and stats["catalogue"]["kinds"]
        assert stats["usage"]["searches"] == 25 and stats["usage"]["searches_answered"] == 20
        assert stats["usage"]["texts_read"] is None
        assert datetime.fromisoformat(stats["as_of"]) <= datetime.now(UTC)
        assert stats["window_days"] == public_stats.WINDOW_DAYS
