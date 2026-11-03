"""Daily counts of how the public website is used (TRF-01–05): totals, never visits.

A request adds 1 to a few daily totals and is forgotten. Nothing is stored per visit or per visitor, no
cookie is set and the browser is asked for nothing beyond the requests the site makes anyway. What can be
counted is the closed list below; the words of a search are not among it, nor an address, nor a user agent.
Each metric stands alone, never crossed with another, so no total narrows down to one person.

Built to stay put as the corpus and the traffic grow. Counts gather in the instance's memory, which is
bounded, and reach the database as one batch a minute from each instance (and at shutdown), adding to
what is there. A request never waits for the database and never fails because of this; if the database is
away the batch waits for the next round. The table that grows with the corpus is split by month, and its
old months are dropped whole by the daily job, `python -m api.traffic maintain`.

Off unless ALVARY_TRAFFIC_COUNTS=1. ALVARY_TRAFFIC_VISITORS=1 adds the day's estimate of different visitors:
a keyed hash of the network and browser goes into a sketch of 4096 small numbers (HyperLogLog) and is not
kept, the key changes each day and is cleared at the next, and so one visitor cannot be followed from one
day to the next, or found in the sketch once the day has passed.
"""

import argparse
import hashlib
import ipaddress
import json
import logging
import math
import os
import random
import re
import secrets
import threading
import time
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError, IntegrityError

from api.admission import AdmissionConfig, client_address, network_group
from rights.database import PolicyNamespace

log = logging.getLogger("open_alvary.traffic")

FLUSH_SECONDS = 60.0
SALT_RETRY_SECONDS = 5.0
MAX_KEYS = 50_000  # about 10 MB; a minute's traffic on one instance stays well under it
BATCH_ROWS = 2_000
MAX_REFERRERS = 500  # different referring sites an instance keeps per day; the rest are "other"
REFERRER_FLOOR = 3  # a site that sent fewer on a day joins "other" when the day has passed
RECORD_DAYS_KEPT = 90
TOP_RECORDS = 100
PARTITIONS_AHEAD = 3
REGISTERS = 4096

COUNTRY = re.compile(r"[A-Z]{2}")
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,159}")
HOST = re.compile(r"(?=.{4,100}$)([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z][a-z0-9-]*[a-z0-9]")
POSITION = re.compile(r"[1-9][0-9]{0,3}")

# Named crawlers each get a line of their own (robots.txt tokens): who reads the corpus by machine is
# worth knowing. Most specific first: an AI crawler's agent can also name the search engine behind it.
AI_CRAWLERS = (
    "gptbot", "chatgpt-user", "oai-searchbot", "claudebot", "claude-user", "claude-searchbot", "claude-web",
    "anthropic-ai", "perplexitybot", "perplexity-user", "googleother", "google-cloudvertexbot", "bytespider",
    "ccbot", "amazonbot", "meta-externalagent", "meta-externalfetcher", "cohere-ai", "duckassistbot",
    "mistralai-user", "applebot-extended", "diffbot", "youbot", "timpibot", "imagesiftbot",
)  # fmt: skip
SEARCH_CRAWLERS = (
    "googlebot", "bingbot", "yandexbot", "baiduspider", "duckduckbot", "applebot", "slurp", "seznambot",
    "sogou", "yeti", "petalbot",
)  # fmt: skip
MONITORS = ("googlestackdrivermonitoring", "uptimerobot", "pingdom", "statuscake", "betteruptime")
AUTOMATED = re.compile(
    r"bot|crawl|spider|scrape|fetch|curl|wget|python|httpx|axios|node-fetch|okhttp|java/|go-http|ruby|php"
    r"|headless|lighthouse|phantomjs|puppeteer|playwright|selenium|preview|monitor|check|scan",
    re.IGNORECASE,
)

PAGES = {
    "": "home",
    "browse": "browse",
    "sources": "record",
    "coverage": "coverage",
    "changes": "changes",
    "governance": "governance",
    "releases": "releases",
    "developers": "developers",
    "about": "about",
    "privacy": "privacy",
    "security": "security",
    "terms": "terms",
    "acceptable-use": "terms",
    "docs": "docs",
}

# Every metric that can be written, and what its labels are.
METRICS = {
    "arrivals": "a full load of a website page by a person, by the kind of page",
    "people.country": "the same arrivals by country, from the edge's own lookup",
    "people.device": "the same arrivals: phone, tablet or desktop",
    "people.referrer": "the same arrivals by the site they came from: its name alone, or direct",
    "searches": "first pages of search results a person asked for: all, and none_found among them",
    "searches.jurisdiction": "the same searches by the country filter chosen, or all",
    "searches.opened": "records opened from search results, by position: 1, 2, 3, 4-10 or 11+",
    "records": "a person opening a record (views) or its text (texts)",
    "records.jurisdiction": "records opened, by the record's country",
    "downloads": "public release files, by country and kind",
    "bots": "requests by automated clients: ai:<name>, search:<name>, monitor:<name> or other; other also takes "
    "a page load of an address the website has no page for",
    "visitors": "the day's estimate of different visitors (when switched on), written once the day is over",
    "lost": "counts dropped because the instance's bounded memory was full, or refused by the database",
}


def _header(scope, name: bytes) -> str:
    for key, value in scope.get("headers", ()):
        if key == name:
            return value.decode("latin-1")
    return ""


@lru_cache(maxsize=4096)
def automated(agent: str) -> str | None:
    """The bots line a user agent belongs on, or None where it looks like a person's browser."""
    lowered = agent.lower().replace("cubot", "")  # a make of phone, not a robot
    for kind, names in (("ai", AI_CRAWLERS), ("search", SEARCH_CRAWLERS), ("monitor", MONITORS)):
        for name in names:
            if name in lowered:
                return f"{kind}:{name}"
    if not lowered.startswith("mozilla/") or AUTOMATED.search(lowered):
        return "other"
    return None


def device(agent: str) -> str:
    if "iPad" in agent or "Tablet" in agent or ("Android" in agent and "Mobi" not in agent):
        return "tablet"
    return "phone" if "Mobi" in agent else "desktop"


def referrer(value: str, own_hosts: frozenset[str]) -> str | None:
    """The name of the site a person came from, and nothing else of its address: "direct" with none, and
    None when it is this site's own. A value that is not a site's name (an address, a local name) is
    "other", so what a visitor's browser sends can never fill the table with text of its choosing."""
    if not value:
        return "direct"
    try:
        host = (urlsplit(value).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return "other"
    if host in own_hosts:
        return None
    try:
        ipaddress.ip_address(host)
        return "other"
    except ValueError:
        pass
    return host if HOST.fullmatch(host) else "other"


def position_label(value: str) -> str | None:
    if not POSITION.fullmatch(value):
        return None
    number = int(value)
    return value if number <= 3 else "4-10" if number <= 10 else "11+"


def page_kind(path: str) -> str | None:
    """The kind of page an address is, or None where the website has none (web/src/main.tsx): the site answers
    any address with its own page, so a scanner's /.git/config gets as far as this."""
    first, *below = path.strip("/").split("/")
    if any(part.startswith(".") for part in below):
        return None
    kind = PAGES.get(first)
    if kind == "record":
        return kind if len(below) == 1 else None
    return kind if not below or kind == "docs" else None


def add_to_sketch(registers: list[int], salt: bytes, who: str) -> None:
    hashed = int.from_bytes(hashlib.blake2b(who.encode(), key=salt, digest_size=8).digest())
    index, rest = hashed & (REGISTERS - 1), hashed >> 12
    registers[index] = max(registers[index], 52 - rest.bit_length() + 1)


def estimate(registers: list[int]) -> int:
    """HyperLogLog's estimate, with its correction for small counts; within about 2% at any size."""
    size = len(registers)
    raw = 0.7213 / (1 + 1.079 / size) * size * size / sum(2.0**-r for r in registers)
    empty = registers.count(0)
    return round(size * math.log(size / empty) if raw <= 2.5 * size and empty else raw)


class Traffic:
    def __init__(
        self,
        engine: Engine,
        namespace: PolicyNamespace,
        *,
        trusted_proxies: int = 0,
        visitors: bool = False,
        own_hosts: frozenset[str] = frozenset(),
        flush_seconds: float = FLUSH_SECONDS,
        max_keys: int = MAX_KEYS,
    ):
        self.engine, self.namespace = engine, namespace
        self.trusted_proxies, self.visitors, self.own_hosts = trusted_proxies, visitors, own_hosts
        self.flush_seconds, self.max_keys = flush_seconds, max_keys
        self._lock, self._writing = threading.Lock(), threading.Lock()
        self._counts: Counter = Counter()
        self._records: dict[tuple[date, str], list[int]] = {}
        self._sketches: dict[date, list[int]] = {}
        self._referrers: dict[date, set[str]] = {}
        self._salt: tuple[date, bytes] | None = None
        self._salt_asked = -SALT_RETRY_SECONDS
        self._next = time.monotonic() + self._pause()

    @classmethod
    def from_environment(cls, engine: Engine, admission: AdmissionConfig | None) -> "Traffic | None":
        if os.getenv("ALVARY_TRAFFIC_COUNTS") != "1" or engine.dialect.name != "postgresql":
            return None
        with engine.connect() as connection:
            namespace = PolicyNamespace(connection.scalar(text("SELECT current_schema()")))
        hosts = [os.getenv("PUBLIC_DOMAIN"), urlsplit(os.getenv("ALVARY_API_ORIGIN", "")).hostname]
        return cls(
            engine,
            namespace,
            trusted_proxies=admission.trusted_proxies if admission else 0,
            visitors=os.getenv("ALVARY_TRAFFIC_VISITORS") == "1",
            own_hosts=frozenset(h.lower().removeprefix("www.") for h in hosts if h),
        )

    # ---------- what the website's requests add to ----------
    def arrival(self, scope, path: str) -> None:
        """A full load of a website page. The page's own data requests are counted where they are served."""
        if scope.get("method") != "GET":
            return
        if _header(scope, b"sec-purpose") or "prefetch" in _header(scope, b"purpose"):
            return
        if _header(scope, b"sec-fetch-dest") not in ("", "document"):
            return
        agent = _header(scope, b"user-agent")
        day = self._today()
        kind = page_kind(path)
        bot = automated(agent) or (None if kind else "other")
        if bot:
            return self._count(day, "bots", bot)
        origin = referrer(_header(scope, b"referer"), self.own_hosts)
        country = _header(scope, b"x-client-country")
        rows = [
            ("arrivals", kind),
            ("people.country", country if COUNTRY.fullmatch(country) else "unknown"),
            ("people.device", device(agent)),
        ]
        with self._lock:
            if origin is not None:
                known = self._referrers.setdefault(day, set())
                if origin not in known and len(known) >= MAX_REFERRERS:
                    origin = "other"
                else:
                    known.add(origin)
                rows.append(("people.referrer", origin))
            for metric, label in rows:
                self._add(day, metric, label)
        self._see(scope, agent, day)
        self._maybe_write()

    def searched(self, scope, jurisdiction: str | None, found: bool) -> None:
        """A first page of search results. The words searched are not passed here, and never kept."""
        day = self._today()
        if not self._person(scope, day):
            return
        rows = [("searches", "all"), ("searches.jurisdiction", self._label(jurisdiction, "all"))]
        rows += [] if found else [("searches", "none_found")]
        with self._lock:
            for metric, label in rows:
                self._add(day, metric, label)
        self._maybe_write()

    def record_opened(self, scope, source_id: str, jurisdiction: str | None, result: str = "") -> None:
        """`result` is the position in the search results it was opened from, when it was."""
        day = self._today()
        if not self._person(scope, day):
            return
        position = position_label(result)
        with self._lock:
            self._add(day, "records", "views")
            self._add(day, "records.jurisdiction", self._label(jurisdiction, "unknown"))
            if position:
                self._add(day, "searches.opened", position)
            self._add_record(day, source_id, 0)
        self._maybe_write()

    def text_read(self, scope, source_id: str) -> None:
        day = self._today()
        if not self._person(scope, day):
            return
        with self._lock:
            self._add(day, "records", "texts")
            self._add_record(day, source_id, 1)
        self._maybe_write()

    def downloaded(self, scope, jurisdiction: str, mode: str) -> None:
        """A release file. Programs count too, unless they are crawlers: the files are meant for them."""
        day = self._today()
        bot = automated(_header(scope, b"user-agent"))
        if bot and bot != "other":
            return self._count(day, "bots", bot)
        self._count(day, "downloads", self._label(f"{jurisdiction}/{mode}", "other"))

    # ---------- memory ----------
    def _today(self) -> date:
        return datetime.now(UTC).date()

    def _label(self, value: str | None, otherwise: str) -> str:
        return value if value and LABEL.fullmatch(value) else otherwise

    def _person(self, scope, day: date) -> bool:
        """Whether a data request comes from the website in a person's browser: browsers say so
        themselves (Sec-Fetch-Site) on what a page of this site asks for; anything else is on the bots line."""
        agent = _header(scope, b"user-agent")
        bot = automated(agent)
        if bot or _header(scope, b"sec-fetch-site") != "same-origin":
            self._count(day, "bots", bot or "other")
            return False
        self._see(scope, agent, day)
        return True

    def _count(self, day: date, metric: str, label: str) -> None:
        with self._lock:
            self._add(day, metric, label)
        self._maybe_write()

    def _add(self, day: date, metric: str, label: str) -> None:
        key = (day, metric, label)
        if key not in self._counts and self._full():
            key = (day, "lost", "full")
        self._counts[key] += 1

    def _add_record(self, day: date, source_id: str, column: int) -> None:
        key = (day, source_id)
        if key not in self._records and self._full():
            self._counts[(day, "lost", "full")] += 1
            return
        self._records.setdefault(key, [0, 0])[column] += 1

    def _full(self) -> bool:
        return len(self._counts) + len(self._records) >= self.max_keys

    def _see(self, scope, agent: str, day: date) -> None:
        if not self.visitors:
            return
        salt = self._salt
        if salt is None or salt[0] != day:
            # The next write asks for the day's salt; a few visitors go unseen until it comes.
            now = time.monotonic()
            if now >= self._salt_asked + SALT_RETRY_SECONDS:
                self._salt_asked, self._next = now, 0.0
            return
        peer = scope.get("client")[0] if scope.get("client") else None
        address = client_address(scope.get("headers", []), peer, self.trusted_proxies)
        try:
            group = network_group(address) if address else None
        except ValueError:
            group = None
        if group:
            with self._lock:
                add_to_sketch(self._sketches.setdefault(day, [0] * REGISTERS), salt[1], f"{group} {agent}")

    # ---------- the database ----------
    def _pause(self) -> float:
        return self.flush_seconds * random.uniform(0.8, 1.2)  # instances started together write apart

    def _maybe_write(self) -> None:
        # Started while this request is being served, when a platform that idles an instance's CPU between
        # requests (Cloud Run) still gives it some; and the request does not wait for it.
        now = time.monotonic()
        if now < self._next:
            return
        with self._lock:
            if now < self._next:
                return
            self._next = now + self._pause()
        threading.Thread(target=self.flush, name="traffic-counts", daemon=True).start()

    def _take(self) -> list[dict]:
        with self._lock:
            counts, self._counts = self._counts, Counter()
            records, self._records = self._records, {}
            sketches, self._sketches = self._sketches, {}
            today = self._today()
            self._referrers = {d: known for d, known in self._referrers.items() if d >= today}
        rows = [
            {"day": str(day), "metric": metric, "label": label, "n": n}
            for (day, metric, label), n in sorted(counts.items())
        ]
        people = [
            {"day": str(day), "source_id": source_id, "views": views, "texts": texts}
            for (day, source_id), (views, texts) in sorted(records.items())
        ]
        batches = [{"counts": rows[i : i + BATCH_ROWS]} for i in range(0, len(rows), BATCH_ROWS)]
        batches += [{"records": people[i : i + BATCH_ROWS]} for i in range(0, len(people), BATCH_ROWS)]
        batches += [
            {"visitors": [{"day": str(day), "registers": registers}]} for day, registers in sketches.items()
        ]
        return batches

    def _restore(self, batch: dict) -> None:
        with self._lock:
            for row in batch.get("counts", ()):
                key = (date.fromisoformat(row["day"]), row["metric"], row["label"])
                if key in self._counts or not self._full():
                    self._counts[key] += row["n"]
                else:
                    self._counts[(key[0], "lost", "full")] += row["n"]
            for row in batch.get("records", ()):
                key = (date.fromisoformat(row["day"]), row["source_id"])
                if key in self._records or not self._full():
                    held = self._records.setdefault(key, [0, 0])
                    held[0] += row["views"]
                    held[1] += row["texts"]
                else:
                    self._counts[(key[0], "lost", "full")] += row["views"] + row["texts"]
            for sketch in batch.get("visitors", ()):
                day = date.fromisoformat(sketch["day"])
                held = self._sketches.setdefault(day, [0] * REGISTERS)
                self._sketches[day] = [max(a, b) for a, b in zip(held, sketch["registers"], strict=True)]

    def flush(self) -> None:
        """Write what has gathered. Never raises; never logs a value: only that a round failed, and of what
        kind, since a database error's text would carry the batch."""
        with self._writing:
            if self.visitors:
                self._refresh_salt()
            for batch in self._take():
                try:
                    with self.engine.begin() as connection:
                        connection.execute(text("SET LOCAL statement_timeout = '5s'"))
                        fn = self.namespace.qualified(connection, "policy", "record_traffic")
                        connection.execute(text(f"SELECT {fn}(CAST(:b AS jsonb))"), {"b": json.dumps(batch)})
                except (DataError, IntegrityError) as error:
                    log.warning("traffic counts refused by the database: %s", type(error).__name__)
                    self._count(self._today(), "lost", "refused")
                except Exception as error:
                    log.warning("traffic counts kept for the next round: %s", type(error).__name__)
                    self._restore(batch)

    def _refresh_salt(self) -> None:
        today = self._today()
        if self._salt is not None and self._salt[0] == today:
            return
        try:
            with self.engine.begin() as connection:
                fn = self.namespace.qualified(connection, "policy", "traffic_salt")
                salt = connection.scalar(text(f"SELECT {fn}(:s)"), {"s": secrets.token_bytes(32)})
            self._salt = (today, bytes(salt))
        except Exception as error:
            log.warning("no salt for today's visitors: %s", type(error).__name__)


# ---------- the daily job ----------
def _month(day: date, ahead: int = 0) -> date:
    months = day.year * 12 + day.month - 1 + ahead
    return date(months // 12, months % 12 + 1, 1)


def maintain(engine: Engine, namespace: PolicyNamespace, today: date | None = None) -> dict:
    """The daily upkeep; safe to run again. Each step has its own transaction, so one failing leaves the
    others done, and the job's failure is what raises the operator's alert."""
    today = today or datetime.now(UTC).date()
    done: dict = {}

    def t(connection, name: str) -> str:
        return namespace.qualified(connection, "accounts", name)

    with engine.begin() as connection:
        quote = connection.dialect.identifier_preparer.quote_identifier
        parent = t(connection, "traffic_record_day")
        schema = quote(namespace.schema("accounts"))
        # Months ahead, so a job failing for weeks loses nothing: counts need their month to exist.
        for ahead in range(PARTITIONS_AHEAD + 1):
            start, end = _month(today, ahead), _month(today, ahead + 1)
            name = quote(f"traffic_record_day_{start:%Y_%m}")
            connection.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {schema}.{name} PARTITION OF {parent}"
                    f" FOR VALUES FROM ('{start}') TO ('{end}') WITH (fillfactor = 80)"
                )
            )
        # A month goes whole when its last day is past what is kept; monthly totals stay.
        done["months_dropped"] = 0
        keep_from = today - timedelta(days=RECORD_DAYS_KEPT)
        for (name,) in connection.execute(
            text(
                "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
                " WHERE i.inhparent = CAST(:parent AS regclass)"
            ),
            {"parent": parent},
        ).all():
            year, month = name.removeprefix("traffic_record_day_").split("_")
            if _month(date(int(year), int(month), 1), 1) <= keep_from:
                connection.execute(text(f"DROP TABLE {schema}.{quote(name)}"))
                done["months_dropped"] += 1

    # A finished day's sketch becomes its one number, and the sketch and its salt are gone.
    with engine.begin() as connection:
        visitors = t(connection, "traffic_visitors")
        rows = connection.execute(
            text(f"DELETE FROM {visitors} WHERE day < :today RETURNING day, registers"), {"today": today}
        ).all()
        for day, registers in rows:
            connection.execute(
                text(
                    f"INSERT INTO {t(connection, 'traffic_day')} (day, metric, label, count)"
                    " VALUES (:day, 'visitors', 'all', :n) ON CONFLICT (day, metric, label) DO NOTHING"
                ),
                {"day": day, "n": estimate(list(registers))},
            )
        done["visitor_days_closed"] = len(rows)

    # A site that sent only a few people on a finished day joins "other": it keeps the table's size to
    # what real traffic makes, whatever referrers a script invents, and leaves no rare name to point at
    # one person's visit. The last week only: older days are done.
    with engine.begin() as connection:
        days = t(connection, "traffic_day")
        merged = connection.execute(
            text(
                f"WITH rare AS (DELETE FROM {days} WHERE metric = 'people.referrer'"
                " AND day >= :since AND day < :today AND count < :floor AND label NOT IN ('direct', 'other')"
                " RETURNING day, count),"
                " summed AS (SELECT day, sum(count) AS n FROM rare GROUP BY day)"
                f" INSERT INTO {days} AS t (day, metric, label, count)"
                " SELECT day, 'people.referrer', 'other', n FROM summed"
                " ON CONFLICT (day, metric, label) DO UPDATE SET count = t.count + EXCLUDED.count"
            ),
            {"since": today - timedelta(days=7), "today": today, "floor": REFERRER_FLOOR},
        )
        done["referrer_days_merged"] = merged.rowcount

    # The most read records, once a day, so that a report need not add up the whole table.
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL statement_timeout = '300s'"))
        connection.execute(text("SET LOCAL work_mem = '64MB'"))
        top = t(connection, "traffic_top")
        record_days, record_months = (
            t(connection, "traffic_record_day"),
            t(connection, "traffic_record_month"),
        )
        previous = _month(today, -1)
        for period, source, parameters in (
            *(
                (
                    f"{days_back}d",
                    f"SELECT source_id, sum(views) AS views FROM {record_days}"
                    " WHERE day >= :since AND day < :today GROUP BY source_id",
                    {"since": today - timedelta(days=days_back), "today": today},
                )
                for days_back in (7, 30)
            ),
            (
                f"{previous:%Y-%m}",
                f"SELECT source_id, views FROM {record_months} WHERE month = :month",
                {"month": previous},
            ),
        ):
            connection.execute(text(f"DELETE FROM {top} WHERE period = :period"), {"period": period})
            connection.execute(
                text(
                    f"INSERT INTO {top} (period, rank, source_id, views)"
                    " SELECT :period, row_number() OVER (ORDER BY views DESC, source_id), source_id, views"
                    f" FROM ({source}) AS read WHERE views > 0 ORDER BY views DESC, source_id LIMIT :limit"
                ),
                {"period": period, "limit": TOP_RECORDS, **parameters},
            )
    return done


def main(argv=None) -> int:
    from api.store import engine_for

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("maintain", help="months ahead and past, finished days, the most read records")
    parser.parse_args(argv)
    engine = engine_for()
    with engine.connect() as connection:
        namespace = PolicyNamespace(connection.scalar(text("SELECT current_schema()")))
    print(json.dumps({"traffic": maintain(engine, namespace)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
