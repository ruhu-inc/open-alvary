"""The reference client for Open Alvary's retrieval service (RSV-09).

It asks for evidence (``GET /retrieve``), checks cited parts (``POST /currentness``) and follows the
change feed (``GET /changes``) from its saved position, keeping its cache honest:

- a withdrawal purges every cached unit, pack and answer citing that version, at once;
- a later text of an expression marks that expression's units stale, and a legal effect (or its
  correction) marks the affected work's currentness stale: either is checked before reuse;
- a feed reset marks every cached unit as needing the check operation before reuse.

Nothing is cached before the client has a feed position, so no change can fall between the two.
When Open Alvary cannot be reached it answers from the cache only: every cited unit is labelled
``currentness_not_checked`` with the time it was cached, nothing whose withdrawal it has seen is
served, and after seven days without a successful feed read nothing is served at all. A request is
sent at most three times and never sooner than Retry-After says. Query text and legal text are never
logged, by this client or by httpx on its behalf.
"""

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx

from .cache import Cache, MemoryCache, unit_key
from .errors import CacheExpired, OpenAlvaryError, RateLimited, Refused, Unavailable

API = "https://api.open.alvary.ai/api/v1"
ATTEMPTS = 3  # a request is sent at most three times
MAX_WAIT = 60.0  # a Retry-After longer than this is not waited for: the request fails at once
OFFLINE_LIMIT = timedelta(days=7)  # without a successful feed read for longer, the cache stops answering
FEED_INTERVAL = timedelta(minutes=15)  # a cached pack is reused online only with the feed read this recently
FEED_PAGE, FEED_PAGES = 100, 50  # events a page, and pages a sync, before it stops and saves its place
CHECK_BATCH = 100
NOT_CHECKED = "currentness_not_checked"
# Answers that leave the saved position unusable: every cached unit needs a check, and the feed is
# taken again from its present. A stale cursor keeps the position (it is still valid).
RESETS = {"feed_reset", "replay_window_expired", "stale_cursor", "invalid_cursor"}
# The parameters of /retrieve: those that may repeat, those that take one value, and the defaults.
DEFAULTS = {"max_units": "10", "max_evidence_bytes": "49152", "include": ["context", "expansion", "ranking"]}
LISTS = {"jurisdiction", "type", "status", "include"}
SINGLE = {"language", "as_of", "year_from", "year_to", "source_id", "version_id"} | (DEFAULTS.keys() - LISTS)

log = logging.getLogger("open_alvary_client")


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass(frozen=True)
class Cited:
    """One cited unit as served. ``label`` is ``checked`` when Open Alvary assessed its currentness in
    this call (a fresh pack, the check operation or a 304), otherwise ``currentness_not_checked``.
    ``flags`` say why it would need a check: ``newer_text``, ``currentness``, ``needs_check`` or
    ``newer_version``."""

    unit: dict
    label: str
    cached_at: datetime | None = None
    checked_at: datetime | None = None
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Answer:
    """An evidence pack and how it was served: from the cache or not, checked or not, and whether
    Open Alvary was out of reach (``offline``). A cached answer is ``checked`` only when every unit
    was checked in this call; an offline answer never is."""

    pack: dict
    units: tuple[Cited, ...]
    from_cache: bool
    checked: bool
    offline: bool = False
    cached_at: datetime | None = None

    @property
    def dataset(self) -> str:
        return self.pack["revisions"]["dataset"]


@dataclass(frozen=True)
class SavedAnswer:
    """An answer the application kept with ``save_answer``, with the units it cites as they are cached
    now. Its units are never labelled checked: check them before showing it."""

    body: object
    units: tuple[Cited, ...]
    saved_at: datetime


@dataclass
class SyncResult:
    """What one read of the change feed did: events applied, versions whose entries were purged, cached
    units newly needing a check, why every unit needs one (``reset``), and whether it stopped early."""

    events: int = 0
    purged: list[str] = field(default_factory=list)
    marked: int = 0
    reset: str | None = None
    more: bool = False


@dataclass(frozen=True)
class _Request:
    params: list[tuple[str, str]]
    key: str


def normalise(query: str, filters: dict) -> _Request:
    """The request as sent, and its cache key: words separated by single spaces, list filters sorted,
    defaults left out, so requests that ask the same thing share an entry."""
    words = " ".join(query.split())
    if not words:
        raise ValueError("The query must not be blank.")
    shown: dict[str, object] = {}
    for name, value in filters.items():
        if name not in LISTS | SINGLE:
            raise TypeError(f"Unknown filter: {name}")
        if value is None:
            continue
        if name in LISTS:
            values = sorted({str(v) for v in ([value] if isinstance(value, str) else value)})
            if values:
                shown[name] = values
        else:
            shown[name] = value.isoformat() if isinstance(value, date) else str(value)
    for name, default in DEFAULTS.items():
        if shown.get(name) == default:
            del shown[name]
    params = [("q", words)]
    for name in sorted(shown):
        value = shown[name]
        params += [(name, v) for v in value] if isinstance(value, list) else [(name, value)]
    key = hashlib.sha256(json.dumps({"q": words, **shown}, sort_keys=True).encode()).hexdigest()
    return _Request(params, key)


def _item(given, as_of: str | None = None) -> dict:
    """A check item from an evidence unit, a Cited, or an item: the anchor (or version and locator),
    the span hash and the date to assess it at."""
    unit = given.unit if isinstance(given, Cited) else given
    item = (
        {"anchor_id": unit["anchor_id"]}
        if unit.get("anchor_id")
        else {"version_id": unit["version_id"], "locator": unit["locator"]}
    )
    for name in ("span_hash", "as_of"):
        if unit.get(name) is not None:
            item[name] = unit[name]
    if as_of and "as_of" not in item:
        item["as_of"] = as_of
    return item


def _when(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _retry_after(response: httpx.Response, now: datetime) -> float | None:
    """Retry-After in seconds, given as seconds or as an HTTP date."""
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when if when.tzinfo else when.replace(tzinfo=UTC)).timestamp() - now.timestamp())


def _problem(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


class _WithoutQuery(logging.Filter):
    """httpx logs every request's URL at INFO, and the question is in its query string: it goes."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                a.copy_with(query=None) if isinstance(a, httpx.URL) else a for a in record.args
            )
        return True


def _quiet_httpx() -> None:
    logger = logging.getLogger("httpx")
    if not any(isinstance(f, _WithoutQuery) for f in logger.filters):
        logger.addFilter(_WithoutQuery())


class Client:
    """``Client(API, key)`` talks to Open Alvary with an API key, keeping evidence in ``cache`` (in
    memory unless given). ``transport`` (an ``httpx.BaseTransport``) and ``clock`` (``now()`` and
    ``sleep(seconds)``) are for tests and other runtimes."""

    def __init__(
        self,
        base_url: str,
        key: str,
        *,
        cache: Cache | None = None,
        transport: httpx.BaseTransport | None = None,
        clock=None,
        timeout: float = 30.0,
        feed_interval: timedelta = FEED_INTERVAL,
    ):
        self.cache = cache if cache is not None else MemoryCache()
        self.clock = clock or SystemClock()
        self.feed_interval = feed_interval
        self.http = httpx.Client(
            base_url=base_url,
            transport=transport,
            timeout=timeout,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )
        _quiet_httpx()

    def close(self) -> None:
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # --- requests ----------------------------------------------------------------------------------

    def _send(self, method: str, path: str, **options) -> httpx.Response:
        """One request, sent at most ATTEMPTS times: again only after a network error, a timeout, a 429
        or a 5xx, and never sooner than Retry-After says. A longer wait than MAX_WAIT ends the
        attempts. Only the method, path and status are logged."""
        response = None
        for attempt in range(1, ATTEMPTS + 1):
            try:
                response = self.http.request(method, path, **options)
            except httpx.TransportError as error:  # timeouts included
                response, wait = None, float(2 ** (attempt - 1))
                log.warning("%s %s failed on attempt %d: %s", method, path, attempt, type(error).__name__)
            else:
                if response.status_code != 429 and response.status_code < 500:
                    return response
                after = _retry_after(response, self.clock.now())
                wait = float(2 ** (attempt - 1)) if after is None else after
                log.warning("%s %s answered %d on attempt %d", method, path, response.status_code, attempt)
            if attempt == ATTEMPTS or wait > MAX_WAIT:
                break
            self.clock.sleep(wait)
        if response is not None and response.status_code == 429:
            problem = _problem(response)
            raise RateLimited(
                problem.get("code"), problem.get("detail"), _retry_after(response, self.clock.now())
            )
        raise Unavailable(f"Open Alvary could not be reached ({method} {path}).")

    @staticmethod
    def _json(response: httpx.Response) -> dict:
        if response.is_success:
            return response.json()
        problem = _problem(response)
        raise Refused(response.status_code, problem.get("code"), problem.get("detail"))

    # --- the change feed ---------------------------------------------------------------------------

    def sync_changes(self) -> SyncResult:
        """Apply every change since the saved position, a page at a time, saving the position after
        each page. Raises Unavailable when the feed cannot be read."""
        result = SyncResult()
        feed = self.cache.feed()
        if not feed.get("resume_token"):
            # Without a position nothing cached can be trusted to be current (normally nothing is cached).
            result.marked += self.cache.mark("needs_check")
            self._start()
            return result
        epoch = feed.get("epoch")
        params = {"resume_token": feed["resume_token"], "limit": FEED_PAGE}
        for _ in range(FEED_PAGES):
            response = self._send("GET", "/changes", params=params)
            code = _problem(response).get("code") if response.status_code in (400, 409, 410) else None
            if code in RESETS:
                self._reset(result, code)
                return result
            page = self._json(response)
            with self.cache.transaction():
                if epoch is not None and page.get("feed_epoch") != epoch:
                    result.reset = "new_epoch"
                    result.marked += self.cache.mark("needs_check")
                for event in page.get("events", []):
                    self._apply(event, result)
                epoch = page.get("feed_epoch")
                self.cache.save_feed(resume_token=page["resume_token"], epoch=epoch)
            if not page.get("has_more"):
                break
            params = {"cursor": page["next_cursor"], "limit": FEED_PAGE}
        else:
            result.more = True
        self.cache.save_feed(read_at=self.clock.now().isoformat())
        return result

    def _start(self) -> None:
        """Note where the feed is now: changes from here on are applied to the cache."""
        page = self._json(self._send("GET", "/changes", params={"start": "now"}))
        self.cache.save_feed(
            resume_token=page["resume_token"],
            epoch=page.get("feed_epoch"),
            read_at=self.clock.now().isoformat(),
        )

    def _reset(self, result: SyncResult, code: str) -> None:
        result.reset = code
        result.marked += self.cache.mark("needs_check")
        if code == "stale_cursor":  # the last saved resume token still holds: the next sync continues it
            self.cache.save_feed(read_at=self.clock.now().isoformat())
            return
        self.cache.save_feed(resume_token=None)
        self._start()

    def _apply(self, event: dict, result: SyncResult) -> None:
        kind = event.get("event_type")
        work = (event.get("work") or {}).get("id")
        version = event.get("version") or {}
        result.events += 1
        if kind == "withdrawal":
            named = (event.get("withdrawal") or {}).get("scope_disclosure") == "named"
            if named and version.get("id"):
                self.cache.withdraw(version["id"], self.clock.now())
                result.purged.append(version["id"])
            elif named and work:
                self.cache.purge(source=work)
            else:  # which record was withdrawn is not said: every cached unit is checked before reuse
                result.marked += self.cache.mark("needs_check")
        elif kind == "text_version_published" and work and event.get("change_reason") != "first_text":
            # A first text has no earlier one of its edition, so nothing of it can be cached.
            language = version.get("language")
            scope = {"expression": f"{work}:{language}"} if language else {"source": work}
            result.marked += self.cache.mark("newer_text", **scope)
        elif work and (
            kind == "legal_effect_recorded"
            or (kind == "metadata_corrected" and "legal_status" in (event.get("changed_fields") or []))
        ):
            result.marked += self.cache.mark("currentness", source=work)

    def _feed_current(self) -> bool:
        feed = self.cache.feed()
        read_at = _when(feed.get("read_at"))
        return (
            bool(feed.get("resume_token"))
            and read_at is not None
            and self.clock.now() - read_at <= self.feed_interval
        )

    def _follow_feed(self) -> bool:
        """Whether the feed has been read within feed_interval, reading it first if not."""
        if self._feed_current():
            return True
        try:
            self.sync_changes()
        except OpenAlvaryError as error:
            log.warning("The change feed could not be read: %s", type(error).__name__)
            return False
        return True

    # --- evidence ----------------------------------------------------------------------------------

    def retrieve(self, query: str, **filters) -> Answer:
        """Evidence for a question. ``filters`` are the parameters of /retrieve: jurisdiction, language,
        type, status, as_of, year_from, year_to, source_id, version_id, max_units, max_evidence_bytes
        and include. A cached pack is reused while the feed is followed: as it is when nothing has
        flagged its units, after the check operation when something has, and fetched again when the
        check finds a part changed or gone. When Open Alvary cannot be reached the cached pack answers,
        labelled; raises Unavailable (or CacheExpired) when nothing may answer."""
        request = normalise(query, filters)
        current = self._follow_feed()
        entry = self.cache.pack(request.key)
        try:
            if entry is not None and current and entry["profiles"] == self.cache.profiles():
                answer = self._reuse(entry)
                if answer is not None:
                    return answer
            return self._fetch(request, self.cache.pack(request.key))
        except Unavailable as outage:
            return self._offline(request, outage)

    def _reuse(self, entry: dict) -> Answer | None:
        units = self.cache.units_of(entry)
        if any(u is None or "newer_version" in u["flags"] for u in units):
            return None
        if not any(u["flags"] for u in units):
            return self._served(entry, units, checked=False)
        as_of = entry["pack"]["query"].get("as_of")
        page = self._check([_item(u["unit"], as_of) for u in units])
        if any(r["result"] != "unchanged" for r in page["results"]):
            return None
        return self._served(entry, self.cache.units_of(entry), checked=True)

    def _fetch(self, request: _Request, entry: dict | None) -> Answer:
        tag = entry and entry.get("etag")
        response = self._send(
            "GET", "/retrieve", params=request.params, headers={"If-None-Match": tag} if tag else {}
        )
        now = self.clock.now()
        if response.status_code == 304 and entry is not None:
            # The same pack Open Alvary would send now, rechecked by it before answering.
            with self.cache.transaction():
                for unit in entry["pack"]["evidence"]:
                    self.cache.confirm(
                        unit_key(unit["anchor_id"], unit["content_hash"]), unit["currentness"], now
                    )
            return self._served(entry, self.cache.units_of(entry), checked=True)
        pack = self._json(response)
        if self.cache.feed().get("resume_token") and pack.get("status") == "results":
            self.cache.store_pack(request.key, pack, response.headers.get("ETag"), now)
        units = tuple(Cited(unit, "checked", checked_at=now) for unit in pack["evidence"])
        return Answer(pack=pack, units=units, from_cache=False, checked=True)

    def _offline(self, request: _Request, outage: Unavailable) -> Answer:
        entry = self.cache.pack(request.key)
        units = self.cache.units_of(entry) if entry is not None else []
        if entry is None or any(u is None or self.cache.withdrawn(u["version_id"]) for u in units):
            raise Unavailable("Open Alvary could not be reached and nothing cached answers this.") from outage
        self._within_offline_limit(outage)
        return self._served(entry, units, checked=False, offline=True)

    def _within_offline_limit(self, cause: Exception | None = None) -> None:
        read_at = _when(self.cache.feed().get("read_at"))
        if read_at is None or self.clock.now() - read_at > OFFLINE_LIMIT:
            raise CacheExpired(
                "The change feed has not been read for seven days: the cache no longer answers."
            ) from cause

    @staticmethod
    def _served(entry: dict, units: list[dict], *, checked: bool, offline: bool = False) -> Answer:
        """A cached pack as served now: each unit with its latest known currentness and its label."""
        evidence, cited = [], []
        for unit, stored in zip(entry["pack"]["evidence"], units, strict=True):
            shown = unit | {"currentness": stored["currentness"]}
            evidence.append(shown)
            cited.append(
                Cited(
                    shown,
                    "checked" if checked else NOT_CHECKED,
                    cached_at=_when(stored["cached_at"]),
                    checked_at=_when(stored["checked_at"]),
                    flags=tuple(stored["flags"]),
                )
            )
        pack = entry["pack"] | {"evidence": evidence}
        cached_at = _when(entry["cached_at"])
        return Answer(
            pack, tuple(cited), from_cache=True, checked=checked, offline=offline, cached_at=cached_at
        )

    # --- the check operation -----------------------------------------------------------------------

    def check(self, items) -> dict:
        """The check operation for cited parts: evidence units (or an Answer's units), or items with
        ``anchor_id`` (or ``version_id`` and ``locator``), ``span_hash`` and ``as_of``. Sent 100 at a
        time and answered as one page, indexed as given. What it finds updates the cache: ``unchanged``
        confirms a unit, ``newer_version`` has its packs fetched again, ``span_mismatch`` and
        ``unavailable`` purge it. Raises Unavailable when Open Alvary cannot be reached: a check is
        never answered from the cache."""
        return self._check([_item(i) for i in items])

    def _check(self, items: list[dict]) -> dict:
        results, dataset = [], None
        for start in range(0, len(items), CHECK_BATCH):
            batch = items[start : start + CHECK_BATCH]
            page = self._json(self._send("POST", "/currentness", json={"items": batch}))
            dataset = page.get("dataset")
            results += [r | {"index": r["index"] + start} for r in page["results"]]
        self._learn(items, results)
        return {"schema_version": "oa-check-1", "dataset": dataset, "results": results}

    def _learn(self, items: list[dict], results: list[dict]) -> None:
        anchors = {r["index"]: items[r["index"]].get("anchor_id") or r.get("anchor_id") for r in results}
        cached = self.cache.units_with_anchor({a for a in anchors.values() if a})
        now = self.clock.now()
        with self.cache.transaction():
            for result in results:
                for key in cached.get(anchors[result["index"]], ()):
                    if result["result"] == "unchanged":
                        self.cache.confirm(key, result.get("currentness"), now)
                    elif result["result"] == "newer_version":
                        record = self.cache.get("unit", key)
                        if record is not None and "newer_version" not in record["flags"]:
                            self.cache.put(
                                "unit", key, record | {"flags": [*record["flags"], "newer_version"]}
                            )
                    else:  # span_mismatch or unavailable: the cached words are not Open Alvary's now
                        self.cache.purge(unit=key)

    # --- the application's own answers -------------------------------------------------------------

    def save_answer(self, key: str, body, answer: Answer) -> None:
        """Keep an answer the application wrote from ``answer`` (``body`` is any JSON value), so that a
        withdrawal of anything it cites purges it with the evidence."""
        units = [c.unit for c in answer.units]
        if any(self.cache.withdrawn(u["version_id"]) for u in units):
            raise ValueError("The answer cites a withdrawn text.")
        self.cache.put_answer(key, body, units, self.clock.now())

    def saved_answer(self, key: str) -> SavedAnswer | None:
        """A kept answer, or None once anything it cites has been purged. Raises CacheExpired after seven
        days without a successful feed read."""
        record = self.cache.answer(key)
        if record is None:
            return None
        self._within_offline_limit()
        cited = []
        for unit, (ref, *_) in zip(record["units"], record["cites"], strict=True):
            stored = self.cache.get("unit", ref)
            flags = tuple(stored["flags"]) if stored else ()
            when = _when(stored["cached_at"]) if stored else None
            checked_at = _when(stored["checked_at"]) if stored else None
            shown = unit | {"currentness": stored["currentness"]} if stored else unit
            cited.append(Cited(shown, NOT_CHECKED, cached_at=when, checked_at=checked_at, flags=flags))
        return SavedAnswer(record["body"], tuple(cited), _when(record["saved_at"]))
