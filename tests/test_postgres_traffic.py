"""The website's daily counts against the database (api.traffic, accounts.traffic, migration 0044).

A marker searched through the real service reaches no table. Batches add up across instances at once;
instances can write only through the function, and only what its checks allow; the daily job drops old
months whole, turns a finished day's sketch into one number and leaves no salt; the workbench's role reads
the totals, and not the sketch."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_request_logging import MARKER
from test_traffic import CHROME, from_page, scope
from test_v1_text import UNITS, WORK, publish_act

from accounts.access_admin import provision_role
from accounts.traffic import report
from api import traffic as T
from api.service import create_service
from api.store import Store
from api.traffic import Traffic, maintain
from rights.database import PolicyNamespace, provision_roles

pytestmark = pytest.mark.postgres
TODAY = datetime.now(UTC).date()
BROWSER = {"user-agent": CHROME, "sec-fetch-site": "same-origin"}


@pytest.fixture
def namespace(pg_engine):
    migrate(pg_engine)
    with pg_engine.connect() as connection:
        return PolicyNamespace(connection.scalar(text("SELECT current_schema()")))


@pytest.fixture
def capability_roles(pg_engine, namespace):
    with pg_engine.begin() as connection:
        roles = provision_roles(connection, namespace)
    yield roles
    with pg_engine.begin() as connection:
        quote = connection.dialect.identifier_preparer.quote_identifier
        for kind in ("policy", "staging", "corpus"):
            connection.execute(text(f"DROP SCHEMA {quote(namespace.schema(kind))} CASCADE"))
        for role in roles.values():
            connection.execute(text(f"DROP OWNED BY {quote(role)}"))
            connection.execute(text(f"DROP ROLE {quote(role)}"))


def rows(engine, namespace, table: str, columns: str = "*", where: str = "TRUE") -> list[tuple]:
    with engine.connect() as connection:
        name = namespace.qualified(connection, "accounts", table)
        return [tuple(r) for r in connection.execute(text(f"SELECT {columns} FROM {name} WHERE {where}"))]


def total(engine, namespace, metric: str, label: str) -> int:
    found = rows(engine, namespace, "traffic_day", "sum(count)", f"metric = '{metric}' AND label = '{label}'")
    return int(found[0][0] or 0)


def send(engine, namespace, batch: str) -> None:
    with engine.begin() as connection:
        fn = namespace.qualified(connection, "policy", "record_traffic")
        connection.execute(text(f"SELECT {fn}(CAST(:b AS jsonb))"), {"b": batch})


def test_batches_add_up_across_instances_at_once(pg_engine, namespace):
    def instance(number: int) -> None:
        traffic = Traffic(pg_engine, namespace, flush_seconds=3600)
        for i in range(30):
            traffic.searched(from_page(), None, True)
            traffic.record_opened(from_page(), f"rw-law-{i % 5}", "rw")
        traffic.flush()

    with ThreadPoolExecutor(6) as pool:
        list(pool.map(instance, range(12)))
    assert total(pg_engine, namespace, "searches", "all") == 360
    assert total(pg_engine, namespace, "records", "views") == 360
    assert rows(pg_engine, namespace, "traffic_record_day", "sum(views)") == [(360,)]
    assert rows(pg_engine, namespace, "traffic_record_month", "count(*), sum(views)") == [(5, 360)]


def test_a_flush_splits_a_large_round_into_batches(pg_engine, namespace, monkeypatch):
    monkeypatch.setattr(T, "BATCH_ROWS", 10)
    traffic = Traffic(pg_engine, namespace, flush_seconds=3600)
    for i in range(45):
        traffic.record_opened(from_page(), f"rw-law-{i}", "rw")
    traffic.flush()
    assert rows(pg_engine, namespace, "traffic_record_day", "count(*)") == [(45,)]
    assert total(pg_engine, namespace, "records", "views") == 45


@pytest.mark.parametrize(
    "batch",
    [
        '{"counts": [{"day": "%s", "metric": "searches", "label": "land dispute in kigali", "n": 1}]}',
        '{"counts": [{"day": "%s", "metric": "Searches Now", "label": "all", "n": 1}]}',
        '{"records": [{"day": "%s", "source_id": "not an id", "views": 1, "texts": 0}]}',
    ],
)
def test_the_database_itself_refuses_a_label_that_could_hold_words(pg_engine, namespace, batch):
    with pytest.raises(IntegrityError):
        send(pg_engine, namespace, batch % TODAY)


def test_a_count_for_a_day_long_past_is_dropped(pg_engine, namespace):
    old = TODAY - timedelta(days=40)
    send(
        pg_engine,
        namespace,
        f'{{"counts": [{{"day": "{old}", "metric": "searches", "label": "all", "n": 5}}]}}',
    )
    assert total(pg_engine, namespace, "searches", "all") == 0


def test_the_serving_role_can_only_call_the_functions(pg_engine, namespace, capability_roles):
    role = capability_roles["serving"]
    with pg_engine.connect() as connection:
        table = namespace.qualified(connection, "accounts", "traffic_day")
        fn = namespace.qualified(connection, "policy", "record_traffic")
        salt = namespace.qualified(connection, "policy", "traffic_salt")
        quoted = connection.dialect.identifier_preparer.quote_identifier(role)
        connection.execute(text(f"SET ROLE {quoted}"))
        one_count = f'{{"counts": [{{"day": "{TODAY}", "metric": "searches", "label": "all", "n": 1}}]}}'
        connection.execute(text(f"SELECT {fn}(CAST(:b AS jsonb))"), {"b": one_count})
        assert len(bytes(connection.scalar(text(f"SELECT {salt}(:s)"), {"s": b"s" * 32}))) == 32
        for attempt in (f"SELECT count(*) FROM {table}", f"DELETE FROM {table}"):
            savepoint = connection.begin_nested()
            with pytest.raises(DBAPIError):
                connection.execute(text(attempt))
            savepoint.rollback()
        connection.commit()
    assert total(pg_engine, namespace, "searches", "all") == 1


def test_the_day_keeps_one_salt_and_yesterdays_is_cleared_when_today_starts(pg_engine, namespace):
    one = Traffic(pg_engine, namespace, flush_seconds=3600, visitors=True)
    other = Traffic(pg_engine, namespace, flush_seconds=3600, visitors=True)
    with pg_engine.begin() as connection:
        visitors = namespace.qualified(connection, "accounts", "traffic_visitors")
        connection.execute(
            text(f"INSERT INTO {visitors} (day, salt) VALUES (:d, :s)"),
            {"d": TODAY - timedelta(days=1), "s": b"y" * 32},
        )
    one._refresh_salt()
    other._refresh_salt()
    assert one._salt == other._salt and len(one._salt[1]) == 32
    assert rows(pg_engine, namespace, "traffic_visitors", "salt", f"day < '{TODAY}'") == [(None,)]


def test_a_finished_days_sketch_becomes_one_number_and_nothing_else_stays(pg_engine, namespace):
    instances = [Traffic(pg_engine, namespace, flush_seconds=3600, visitors=True) for _ in range(3)]
    for traffic in instances:
        traffic._refresh_salt()
    # The same 900 people, on all three instances at random; the sketches merge in the database.
    for i in range(900):
        for traffic in instances:
            traffic.arrival(scope(client=(f"10.0.{i // 250}.{i % 250}", 1)), "")
    for traffic in instances:
        traffic.flush()
    held = rows(pg_engine, namespace, "traffic_visitors", "registers")[0][0]
    assert abs(T.estimate(list(held)) - 900) / 900 < 0.05
    maintain(pg_engine, namespace, today=TODAY + timedelta(days=1))
    assert rows(pg_engine, namespace, "traffic_visitors") == []
    (number,) = rows(pg_engine, namespace, "traffic_day", "count", "metric = 'visitors' AND label = 'all'")[0]
    assert abs(number - 900) / 900 < 0.05


def partitions(engine, namespace) -> set[str]:
    with engine.connect() as connection:
        parent = namespace.qualified(connection, "accounts", "traffic_record_day")
        return {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
                    " WHERE i.inhparent = CAST(:p AS regclass)"
                ),
                {"p": parent},
            )
        }


def test_months_are_kept_ahead_and_old_ones_dropped_whole_with_their_monthly_totals_kept(
    pg_engine, namespace
):
    traffic = Traffic(pg_engine, namespace, flush_seconds=3600)
    traffic.record_opened(from_page(), "rw-law-1", "rw")
    traffic.flush()
    this_month = f"traffic_record_day_{TODAY:%Y_%m}"
    assert this_month in partitions(pg_engine, namespace)
    assert len(partitions(pg_engine, namespace)) == 4

    # Soon after: the next months are there, this month's rows too.
    soon = maintain(pg_engine, namespace, today=TODAY + timedelta(days=35))
    assert soon["months_dropped"] == 0 and this_month in partitions(pg_engine, namespace)
    assert len(partitions(pg_engine, namespace)) >= 5

    # Months later: this month has gone whole, its monthly total stays.
    later = maintain(pg_engine, namespace, today=TODAY + timedelta(days=130))
    assert later["months_dropped"] >= 1 and this_month not in partitions(pg_engine, namespace)
    assert rows(pg_engine, namespace, "traffic_record_day", "count(*)") == [(0,)]
    assert rows(pg_engine, namespace, "traffic_record_month", "source_id, views") == [("rw-law-1", 1)]


def test_maintain_can_run_again(pg_engine, namespace):
    assert maintain(pg_engine, namespace)["months_dropped"] == 0
    assert maintain(pg_engine, namespace)["months_dropped"] == 0


def test_rare_referrers_join_others_once_the_day_has_passed(pg_engine, namespace):
    traffic = Traffic(pg_engine, namespace, flush_seconds=3600)
    for _ in range(5):
        traffic.arrival(scope(referer="https://www.google.com/"), "")
    traffic.arrival(scope(referer="https://one-person.example.org/"), "")
    traffic.arrival(scope(referer="https://made-up.example.net/"), "")
    traffic.flush()
    maintain(pg_engine, namespace, today=TODAY)
    assert total(pg_engine, namespace, "people.referrer", "one-person.example.org") == 1  # today is not done
    maintain(pg_engine, namespace, today=TODAY + timedelta(days=1))
    assert total(pg_engine, namespace, "people.referrer", "google.com") == 5
    assert total(pg_engine, namespace, "people.referrer", "one-person.example.org") == 0
    assert total(pg_engine, namespace, "people.referrer", "other") == 2


def test_the_most_read_records_are_worked_out_once_a_day(pg_engine, namespace, monkeypatch):
    monkeypatch.setattr(T, "TOP_RECORDS", 2)
    traffic = Traffic(pg_engine, namespace, flush_seconds=3600)
    for source, views in (("rw-law-1", 1), ("rw-law-2", 5), ("rw-law-3", 3)):
        for _ in range(views):
            traffic.record_opened(from_page(), source, "rw")
    traffic.text_read(from_page(), "rw-law-1")
    traffic.flush()
    maintain(pg_engine, namespace, today=TODAY)
    assert (
        rows(pg_engine, namespace, "traffic_top", "source_id, views", "period = '30d'") == []
    )  # none finished
    maintain(pg_engine, namespace, today=TODAY + timedelta(days=1))
    assert rows(
        pg_engine, namespace, "traffic_top", "rank, source_id, views", "period = '30d' ORDER BY rank"
    ) == [
        (1, "rw-law-2", 5),
        (2, "rw-law-3", 3),
    ]


def test_the_report_reads_totals_through_the_workbenchs_role_and_not_the_sketch(pg_engine, namespace):
    traffic = Traffic(pg_engine, namespace, flush_seconds=3600)
    traffic.arrival(scope(x_client_country="NG", referer="https://www.google.com/"), "browse")
    traffic.searched(from_page(), "rw", False)
    traffic.searched(from_page(), None, True)
    traffic.record_opened(from_page(), "rw-law-1", "rw", "1")
    traffic.flush()
    maintain(pg_engine, namespace, today=TODAY + timedelta(days=1))

    role = "traffic_reader_" + namespace.base[-12:]
    provision_role(pg_engine, role)
    options = f"-crole={role} -csearch_path={namespace.base}"
    reader = create_engine(pg_engine.url.update_query_dict({"options": options}))
    try:
        with reader.connect() as connection:
            with pytest.raises(DBAPIError):
                sketch = namespace.qualified(connection, "accounts", "traffic_visitors")
                connection.execute(text(f"SELECT count(*) FROM {sketch}"))
        found = report(reader, namespace, days=7, today=TODAY + timedelta(days=1))
    finally:
        reader.dispose()
        with pg_engine.begin() as connection:
            connection.execute(text(f'DROP OWNED BY "{role}"'))
            connection.execute(text(f'DROP ROLE "{role}"'))
    assert found["totals"]["arrivals"] == 1 and found["totals"]["searches"] == 2
    assert found["totals"]["none_found"] == 1 and found["totals"]["records"] == 1
    assert found["lists"]["country"] == [{"label": "NG", "count": 1}]
    assert found["lists"]["opened_position"] == [{"label": "1", "count": 1}]
    assert found["top_records"]["items"] == [{"source_id": "rw-law-1", "views": 1}]
    assert [line["day"] for line in found["daily"]][-1] == str(TODAY + timedelta(days=1))
    assert found["developers"]["people"] == 0 and len(found["daily"]) == 7
    assert report(pg_engine, namespace, days=12345, today=TODAY)["days"] == 30


def test_a_search_through_the_service_counts_and_its_words_reach_no_table(
    pg_engine, namespace, monkeypatch, tmp_path
):
    for name in ("ALVARY_ACCOUNTS", "ALVARY_ADMISSION_CONFIG", "ALVARY_SITE_ADMISSION_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALVARY_TRAFFIC_COUNTS", "1")
    monkeypatch.setenv("PUBLIC_DOMAIN", "open.example.test")
    store = Store(pg_engine)
    publish_act(store, UNITS)
    frontend = tmp_path / "site"
    frontend.mkdir()
    (frontend / "index.html").write_text("<html></html>")
    (frontend / "app.js").write_text("")
    with TestClient(create_service(store=store, frontend=frontend)) as client:  # leaving it flushes
        search = f"/site/v1/search?q={MARKER}"
        assert client.get(search, headers=BROWSER).status_code == 200
        assert client.get(f"{search}&page=2", headers=BROWSER).status_code in (200, 400)  # not a new search
        assert client.get(search).status_code == 200  # a program, not the website
        assert (
            client.get(f"/site/v1/sources/{WORK}", headers={**BROWSER, "x-search-result": "1"}).status_code
            == 200
        )
        assert client.get(f"/site/v1/sources/{WORK}/versions", headers=BROWSER).status_code == 200
        assert client.get("/site/v1/sources/no-such-record", headers=BROWSER).status_code == 404
        document = {"user-agent": CHROME, "sec-fetch-dest": "document", "x-client-country": "RW"}
        page = {**document, "referer": f"https://open.example.test/browse?q={MARKER}"}
        assert client.get("/", headers=document).status_code == 200
        assert client.get(f"/browse?q={MARKER}", headers=page).status_code == 200
        assert client.get("/app.js", headers=document).status_code == 200  # a file, not a page
        assert client.get("/.git/config", headers=document).status_code == 200  # the site's page, at a guess
    assert total(pg_engine, namespace, "searches", "all") == 1
    assert total(pg_engine, namespace, "searches", "none_found") == 1
    assert total(pg_engine, namespace, "bots", "other") == 2
    assert total(pg_engine, namespace, "arrivals", "other") == 0
    assert total(pg_engine, namespace, "records", "views") == 1
    assert total(pg_engine, namespace, "records", "texts") == 1
    assert total(pg_engine, namespace, "searches.opened", "1") == 1
    assert total(pg_engine, namespace, "arrivals", "home") == 1
    assert total(pg_engine, namespace, "arrivals", "browse") == 1
    assert total(pg_engine, namespace, "people.country", "RW") == 2
    assert total(pg_engine, namespace, "people.referrer", "direct") == 1  # its own page is no referrer
    assert rows(pg_engine, namespace, "traffic_record_day", "source_id, views, texts") == [(WORK, 1, 1)]
    assert not any(
        "no-such-record" in str(row) or MARKER.lower() in str(row).lower()
        for table in ("traffic_day", "traffic_record_day", "traffic_record_month", "traffic_top")
        for row in rows(pg_engine, namespace, table)
    )


def test_with_counting_off_nothing_is_written(pg_engine, namespace, monkeypatch):
    for name in (
        "ALVARY_TRAFFIC_COUNTS",
        "ALVARY_ACCOUNTS",
        "ALVARY_ADMISSION_CONFIG",
        "ALVARY_SITE_ADMISSION_CONFIG",
    ):
        monkeypatch.delenv(name, raising=False)
    store = Store(pg_engine)
    publish_act(store, UNITS)
    with TestClient(create_service(store=store)) as client:
        assert client.get(f"/site/v1/sources/{WORK}", headers=BROWSER).status_code == 200
    assert (
        rows(pg_engine, namespace, "traffic_day") == []
        and rows(pg_engine, namespace, "traffic_record_day") == []
    )


def test_months_for_a_date_arithmetic():
    assert T._month(date(2026, 12, 31), 1) == date(2027, 1, 1)
    assert T._month(date(2027, 1, 15), -1) == date(2026, 12, 1)
