"""The website's daily counts (api.traffic, TRF-01–05): what a request adds to, and what never gets in.

Totals only: the words of a search, an address and a user agent have no way in. A request never waits for
the database and never fails because of it; memory is bounded; the counts are off unless switched on.
tests/test_postgres_traffic.py runs them through the service against the database."""

import logging
import random
import threading
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.exc import IntegrityError, OperationalError

from api import traffic as T
from api.service import create_service
from api.store import Store, engine_for
from api.traffic import Traffic
from rights.database import PolicyNamespace

CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"
CUBOT = "Mozilla/5.0 (Linux; Android 11; CUBOT X50) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"
OWN = frozenset({"open.alvary.ai"})
DEPLOYMENT = Path(__file__).resolve().parents[1] / "deployment"


class Refused:
    """An engine that fails every write the way a database does."""

    def __init__(self, error):
        self.error = error
        self.calls = 0

    def begin(self):
        self.calls += 1
        raise self.error


def counter(engine=None, **options) -> Traffic:
    return Traffic(engine, PolicyNamespace(), flush_seconds=3600, own_hosts=OWN, **options)


def scope(agent=CHROME, method="GET", client=("198.51.100.7", 1), **headers):
    pairs = [(b"user-agent", agent.encode())] + [
        (name.replace("_", "-").encode(), value.encode()) for name, value in headers.items()
    ]
    return {"method": method, "headers": pairs, "client": client}


def from_page(agent=CHROME):
    return scope(agent, sec_fetch_site="same-origin")


def held(traffic) -> dict:
    return {(metric, label): n for (_, metric, label), n in traffic._counts.items()}


def test_who_is_a_person_and_who_a_program():
    assert T.automated(CHROME) is None and T.automated(IPHONE) is None
    assert T.automated(CUBOT) is None  # a make of phone, whatever its name holds
    assert (
        T.automated("Mozilla/5.0 AppleWebKit/537.36 (compatible; GPTBot/1.2; +https://openai.com/gptbot)")
        == "ai:gptbot"
    )
    assert T.automated("Mozilla/5.0 (compatible; ClaudeBot/1.0; +claudebot@anthropic.com)") == "ai:claudebot"
    assert (
        T.automated("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)")
        == "search:googlebot"
    )
    assert T.automated("GoogleStackdriverMonitoring-UptimeChecks(https://cloud.google.com/monitoring)") == (
        "monitor:googlestackdrivermonitoring"
    )
    for program in ("curl/8.7.1", "python-httpx/0.27", "", "Mozilla/5.0 HeadlessChrome/141.0"):
        assert T.automated(program) == "other"


def test_a_referrer_is_a_sites_name_and_nothing_else_of_its_address():
    assert T.referrer("", OWN) == "direct"
    assert T.referrer("https://www.google.com/search?q=land+dispute+kigali", OWN) == "google.com"
    assert T.referrer("https://nigerianlawyers.example.org/forum/thread/123?user=amina", OWN) == (
        "nigerianlawyers.example.org"
    )
    assert (
        T.referrer("https://open.alvary.ai/browse?q=secret", OWN) is None
    )  # this site's own, its search too
    for junk in (
        "https://10.0.0.8/intranet",
        "http://localhost:3000/",
        "not a url",
        "https://a b/",
        "http://[bad",
    ):
        assert T.referrer(junk, OWN) == "other"
    assert T.referrer("https://" + "a" * 120 + ".example.org/", OWN) == "other"


def test_a_visit_adds_to_daily_totals_and_keeps_nothing_about_who():
    traffic = counter()
    traffic.arrival(
        scope(
            IPHONE,
            x_client_country="NG",
            referer="https://www.google.com/search?q=Qzv7731kq",
            sec_fetch_dest="document",
        ),
        "sources/ng-act-1",
    )
    assert held(traffic) == {
        ("arrivals", "record"): 1,
        ("people.country", "NG"): 1,
        ("people.device", "phone"): 1,
        ("people.referrer", "google.com"): 1,
    }
    assert "qzv7731kq" not in repr(vars(traffic)).lower() and "198.51" not in repr(vars(traffic))


def test_what_is_not_a_person_arriving_does_not_count_as_one():
    traffic = counter()
    traffic.arrival(scope(method="HEAD"), "")
    traffic.arrival(scope(sec_purpose="prefetch"), "")
    traffic.arrival(scope(sec_fetch_dest="empty"), "")
    assert held(traffic) == {}
    traffic.arrival(scope("curl/8.7.1"), "")
    traffic.arrival(scope("Mozilla/5.0 (compatible; GPTBot/1.2)"), "browse")
    assert held(traffic) == {("bots", "other"): 1, ("bots", "ai:gptbot"): 1}


def test_a_country_header_that_is_not_a_country_is_not_written():
    traffic = counter()
    traffic.arrival(scope(x_client_country="<script>"), "")
    traffic.arrival(scope(x_client_country="ng"), "")
    assert held(traffic)[("people.country", "unknown")] == 2


def test_a_search_is_counted_without_its_words_and_a_data_request_must_come_from_the_page():
    traffic = counter()
    traffic.searched(from_page(), "rw", True)
    traffic.searched(from_page(), None, False)
    traffic.searched(scope(), "rw", True)  # a browser's agent, but not the website asking
    assert held(traffic) == {
        ("searches", "all"): 2,
        ("searches", "none_found"): 1,
        ("searches.jurisdiction", "rw"): 1,
        ("searches.jurisdiction", "all"): 1,
        ("bots", "other"): 1,
    }


def test_a_record_is_counted_with_the_position_it_was_opened_from():
    traffic = counter()
    traffic.record_opened(from_page(), "rw-law-1", "rw", "2")
    traffic.record_opened(from_page(), "rw-law-1", "rw", "7")
    traffic.record_opened(from_page(), "rw-law-1", "rw", "41")
    traffic.record_opened(from_page(), "rw-law-1", "rw", "Qzv7731kq")
    traffic.text_read(from_page(), "rw-law-1")
    assert held(traffic) == {
        ("records", "views"): 4,
        ("records", "texts"): 1,
        ("records.jurisdiction", "rw"): 4,
        ("searches.opened", "2"): 1,
        ("searches.opened", "4-10"): 1,
        ("searches.opened", "11+"): 1,
    }
    assert list(traffic._records.values()) == [[4, 1]]


def test_downloads_count_programs_but_not_crawlers():
    traffic = counter()
    traffic.downloaded(scope("curl/8.7.1"), "rw", "metadata")
    traffic.downloaded(scope(), "rw", "metadata")
    traffic.downloaded(scope("Mozilla/5.0 (compatible; bingbot/2.0)"), "rw", "metadata")
    assert held(traffic) == {("downloads", "rw/metadata"): 2, ("bots", "search:bingbot"): 1}


def test_memory_is_bounded_however_many_records_or_referrers_a_script_makes_up():
    traffic = counter(max_keys=50)
    for i in range(200):
        traffic.record_opened(from_page(), f"rw-law-{i}", "rw")
    assert len(traffic._records) + len(traffic._counts) <= 51
    assert held(traffic)[("lost", "full")] > 0
    traffic = counter()
    for i in range(T.MAX_REFERRERS + 50):
        traffic.arrival(scope(referer=f"https://site{i}.example.org/"), "")
    labels = [label for (metric, label) in held(traffic) if metric == "people.referrer"]
    assert len(labels) == T.MAX_REFERRERS + 1 and "other" in labels


def test_a_database_away_loses_nothing_and_logs_no_value(caplog):
    error = OperationalError("INSERT Qzv7731kq rw-law-1", {}, Exception("gone"))
    traffic = counter(Refused(error))
    traffic.record_opened(from_page(), "rw-law-1", "rw")
    before = held(traffic), dict(traffic._records)
    with caplog.at_level(logging.DEBUG):
        traffic.flush()  # does not raise
    assert (held(traffic), dict(traffic._records)) == before
    assert caplog.records and "rw-law-1" not in caplog.text and "Qzv7731kq" not in caplog.text


def test_a_batch_the_database_refuses_is_dropped_not_retried_for_ever():
    engine = Refused(IntegrityError("x", {}, Exception("check")))
    traffic = counter(engine)
    traffic.searched(from_page(), None, True)
    traffic.flush()
    assert held(traffic) == {("lost", "refused"): 1}


def test_a_request_never_waits_for_the_write():
    traffic = counter()
    started = []
    traffic.flush = lambda: started.append(True)
    traffic._next = 0.0
    traffic.searched(from_page(), None, True)
    traffic.searched(from_page(), None, True)
    for thread in threading.enumerate():
        if thread.name == "traffic-counts":
            thread.join()
    assert started == [True]  # one round, on its own thread, then not again until the next is due


def test_the_estimate_of_different_visitors_stays_close_at_any_size():
    rng = random.Random(7)
    salt = rng.randbytes(32)
    for size in (50, 2_000, 200_000):
        registers = [0] * T.REGISTERS
        for i in range(size):
            T.add_to_sketch(registers, salt, f"visitor {i}")
            if i % 3 == 0:
                T.add_to_sketch(registers, salt, f"visitor {i}")  # seen twice, counted once
        assert abs(T.estimate(registers) - size) / size < 0.05


def test_the_salt_decides_the_sketch():
    one, other = [0] * T.REGISTERS, [0] * T.REGISTERS
    T.add_to_sketch(one, b"a" * 32, "198.51.100.7 Chrome")
    T.add_to_sketch(other, b"b" * 32, "198.51.100.7 Chrome")
    assert one != other


def test_visitors_are_seen_only_when_switched_on_and_only_with_the_days_salt():
    today = datetime.now(UTC).date()
    off = counter()
    off._salt = (today, b"s" * 32)
    off.arrival(scope(), "")
    assert off._sketches == {}
    on = counter(visitors=True)
    on.arrival(scope(), "")
    assert on._sketches == {}  # no salt yet: unseen, and the next write asks for it
    on._salt = (today, b"s" * 32)
    on.arrival(scope(), "")
    assert sum(map(bool, on._sketches[today])) == 1


def test_counting_is_off_unless_switched_on(monkeypatch, tmp_path):
    monkeypatch.delenv("ALVARY_TRAFFIC_COUNTS", raising=False)
    engine = engine_for(f"sqlite:///{tmp_path / 'off.db'}")
    assert Traffic.from_environment(engine, None) is None
    monkeypatch.setenv("ALVARY_TRAFFIC_COUNTS", "1")
    assert Traffic.from_environment(engine, None) is None  # PostgreSQL only
    create_service(store=Store(engine))  # and the service starts as ever


def test_every_metric_written_is_a_listed_one():
    traffic = counter()
    traffic.arrival(scope(x_client_country="NG"), "")
    traffic.searched(from_page(), "rw", False)
    traffic.record_opened(from_page(), "rw-law-1", "rw", "1")
    traffic.text_read(from_page(), "rw-law-1")
    traffic.downloaded(scope(), "rw", "metadata")
    traffic.arrival(scope("curl/8"), "")
    assert {metric for metric, _ in held(traffic)} <= set(T.METRICS)


def test_each_deployment_gives_the_country_and_runs_the_daily_upkeep():
    assert "X-Client-Country:{client_region}" in (DEPLOYMENT / "gcp-terraform/env/edge.tf").read_text()
    front_door = (DEPLOYMENT / "azure/env/frontdoor.tf").read_text()
    assert '"X-Client-Country"' in front_door and "{geo_country}" in front_door
    for daily in ("gcp-terraform/env/jobs.tf", "azure/env/jobs.tf", "gcp/public_service.sh"):
        assert "python -m api.traffic maintain" in (DEPLOYMENT / daily).read_text()
