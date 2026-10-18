"""Reading the public change log (SUB C1, changes-contracts.md): filters, encrypted cursors and resume
tokens, replay pages, single events, Atom and RSS windows and coverage.

Every read rechecks that a work is public now before naming it; a withdrawal of a record that is no
longer public is shown without identifying it. Positions are (publication revision, event ID,
event revision); a window freezes its upper revision H, so nothing committed later appears inside it.
"""

import base64
import json
import logging
import os
import re
import secrets
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import get_args
from xml.sax.saxutils import escape

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select, text
from sqlalchemy.engine import Connection

from changes.record import current, digest, namespace
from schema.models import Source

TYPES = (
    "catalogue_added",
    "source_publication_observed",
    "text_version_published",
    "metadata_corrected",
    "legal_effect_recorded",
    "proposal_recorded",
    "treatment_recorded",
    "withdrawal",
    "coverage_changed",
)
SUPPORTED = (
    "catalogue_added",
    "metadata_corrected",
    "text_version_published",
    "withdrawal",
    "coverage_changed",
)
DIMENSIONS = {
    "jurisdiction": "jurisdiction_id",
    "work": "work_id",
    "class": "document_class",
    "language": "language",
}
UNSUPPORTED = ("issuer", "collection", "topic")
DOCUMENT_CLASSES = frozenset(get_args(Source.model_fields["document_type"].annotation))
CAP = 50  # values per filter dimension (D19)
SCAN_BUDGET = 2000  # positions one request may scan (D19)
CURSOR_SECONDS = 15 * 60
FEED_ENTRIES, FEED_DAYS = 50, 90  # Atom/RSS window (D19)
ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,159}$")
LOWEST = uuid.UUID(int=0)
HIGHEST = uuid.UUID(int=2**128 - 1)
log = logging.getLogger("open_alvary.changes")


class FeedError(Exception):
    def __init__(self, status: int, code: str, detail: str, retry_after: int | None = None):
        super().__init__(detail)
        self.status, self.code, self.detail, self.retry_after = status, code, detail, retry_after


# ---------- filters ----------
@dataclass(frozen=True)
class Filters:
    jurisdiction: tuple[str, ...] = ()
    work: tuple[str, ...] = ()
    type: tuple[str, ...] = ()
    cls: tuple[str, ...] = ()
    language: tuple[str, ...] = ()
    q: str | None = None

    @classmethod
    def parse(cls, values: dict[str, list[str]]) -> "Filters":
        for name in UNSUPPORTED:
            if values.get(name):
                raise FeedError(400, "unsupported_filter", f"Filtering by {name} is not available yet.")
        found = {}
        for name in ("jurisdiction", "work", "type", "class", "language"):
            given = tuple(dict.fromkeys(v.strip() for v in values.get(name, []) if v.strip()))
            if len(given) > CAP:
                raise FeedError(400, "invalid_filter", f"At most {CAP} values for {name}.")
            if name == "type" and any(t not in TYPES for t in given):
                raise FeedError(400, "invalid_filter", "Unknown change type.")
            if name != "type" and any(not ID.match(v) for v in given):
                raise FeedError(400, "invalid_filter", "The filter is not valid.")
            found["cls" if name == "class" else name] = tuple(
                v.lower() if name in ("jurisdiction", "language") else v for v in given
            )
        q = values.get("q") or []
        if len(q) > 1:
            raise FeedError(400, "invalid_filter", "One keyword query at most.")
        query = unicodedata.normalize("NFC", q[0]).strip() if q else ""
        if len(query) > 200:
            raise FeedError(400, "invalid_filter", "The keyword query is limited to 200 characters.")
        return cls(**found, q=query or None)

    def canonical(self) -> dict:
        return {
            "jurisdiction": sorted(self.jurisdiction),
            "work": sorted(self.work),
            "type": sorted(self.type),
            "class": sorted(self.cls),
            "language": sorted(self.language),
            "q": self.q,
        }

    def hash(self) -> str:
        return digest(self.canonical())[:32]

    def sql(self) -> tuple[str, dict]:
        """WHERE conditions. A withdrawal that names nothing is a notice for everyone: only the type
        filter applies to it (SUB: mandatory notices are independent of routine filters)."""
        params: dict = {}
        dims = []
        if self.jurisdiction:
            alternatives = []
            for i, value in enumerate(self.jurisdiction):
                params[f"j{i}"] = value
                alternatives.append(f"e.jurisdiction_id = :j{i}")
                if "-" not in value:  # a country includes its sub-national units
                    params[f"jp{i}"] = value + "-"
                    alternatives.append(f"left(e.jurisdiction_id, length(:jp{i})) = :jp{i}")
            dims.append("(" + " OR ".join(alternatives) + ")")
        for name, column in (("work", "work_id"), ("cls", "document_class"), ("language", "language")):
            values = getattr(self, name)
            if values:
                params[name] = list(values)
                dims.append(f"e.{column} = ANY(:{name})")
        clauses = []
        if self.type:
            params["types"] = list(self.type)
            clauses.append("e.event_type = ANY(:types)")
        if dims:
            clauses.append(
                "((" + " AND ".join(dims) + ") OR (e.event_type = 'withdrawal' AND e.version_id IS NULL))"
            )
        return (" AND ".join(clauses) or "true"), params

    def keyword(self, event: dict) -> bool:
        # A notice that names nothing is never selected by what it no longer names.
        if not self.q or _notice(event):
            return True
        haystack = " ".join(
            str(event["payload"].get(k) or "") for k in ("title", "citation", "summary")
        ).casefold()
        return all(re.search(r"\b" + re.escape(word) + r"\b", haystack) for word in self.q.casefold().split())

    def matches(self, event: dict) -> list[str] | None:
        """The same test as sql() and keyword(), for one event: the dimensions it matched, or None."""
        if self.type and event["event_type"] not in self.type:
            return None
        if _notice(event):
            return ["notice"]
        matched = []
        if self.jurisdiction:
            place = event["jurisdiction_id"]
            if not any(place == j or ("-" not in j and place.startswith(j + "-")) for j in self.jurisdiction):
                return None
            matched.append("jurisdiction")
        for name, column in (("work", "work_id"), ("cls", "document_class"), ("language", "language")):
            values = getattr(self, name)
            if values:
                if event[column] not in values:
                    return None
                matched.append("class" if name == "cls" else name)
        if self.q:
            if not self.keyword(event):
                return None
            matched.append("q")
        if self.type:
            matched.append("type")
        return matched


def _notice(event: dict) -> bool:
    return event["event_type"] == "withdrawal" and event.get("version_id") is None


# ---------- tokens ----------
class Tokens:
    """AES-256-GCM sealed positions: clients cannot read or forge them (SUB C1 replay contract)."""

    def __init__(self, keys: dict[str, bytes], current_kid: str):
        if current_kid not in keys or any(len(k) != 32 for k in keys.values()):
            raise ValueError("Feed keys must be 32 bytes and include the current key")
        self.keys, self.kid = keys, current_kid

    @classmethod
    def from_environment(cls) -> "Tokens | None":
        """Shared keys from ALVARY_FEED_KEYS. Without them a development process makes its own; a
        persistent deployment (several instances) has none, and replay answers 503 until set."""
        raw = os.getenv("ALVARY_FEED_KEYS")
        if raw:
            keys = {kid: base64.b64decode(value) for kid, value in json.loads(raw).items()}
            return cls(keys, os.environ["ALVARY_FEED_KEY_ID"])
        if os.getenv("ALVARY_RUNTIME_MODE") == "persistent":
            log.warning("ALVARY_FEED_KEYS is not set: change-feed replay is off")
            return None
        log.warning("ALVARY_FEED_KEYS is not set: change-feed tokens last only as long as this process")
        return cls({"ephemeral": secrets.token_bytes(32)}, "ephemeral")

    def seal(self, kind: str, body: dict) -> str:
        nonce = secrets.token_bytes(12)
        kid = self.kid.encode()
        sealed = AESGCM(self.keys[self.kid]).encrypt(nonce, json.dumps(body).encode(), kind.encode())
        return f"{kind}1." + base64.urlsafe_b64encode(
            bytes([len(kid)]) + kid + nonce + sealed
        ).decode().rstrip("=")

    def open(self, kind: str, token: str) -> dict:
        try:
            if not token.startswith(f"{kind}1.") or len(token) > 2000:
                raise ValueError
            raw = base64.urlsafe_b64decode(token[3:] + "=" * (-len(token[3:]) % 4))
            size = raw[0]
            kid, nonce, sealed = raw[1 : 1 + size].decode(), raw[1 + size : 13 + size], raw[13 + size :]
            return json.loads(AESGCM(self.keys[kid]).decrypt(nonce, sealed, kind.encode()))
        except (ValueError, KeyError, IndexError, InvalidTag, UnicodeDecodeError):
            raise FeedError(400, "invalid_cursor", "This cursor or resume token is not valid.") from None


# ---------- reading ----------
@dataclass
class Feed:
    conn: Connection
    tokens: "Tokens | None"
    origin: str = "https://open.alvary.ai"
    t: dict = field(init=False)

    def __post_init__(self):
        ns = namespace(self.conn)
        self.t = {
            n: ns.qualified(self.conn, "changes", n)
            for n in ("change_event", "feed_state", "coverage_state", "published_state")
        }

    def state(self) -> dict:
        return dict(self.conn.execute(text(f"SELECT * FROM {self.t['feed_state']}")).mappings().one())

    def high(self) -> int:
        return self.conn.scalar(
            text(f"SELECT coalesce(max(publication_revision), 0) FROM {self.t['change_event']}")
        )

    def page(self, filters: Filters, limit: int = 25, cursor=None, resume=None, start_now=False) -> dict:
        if self.tokens is None:
            raise FeedError(
                503, "temporarily_unavailable", "Change replay is not configured on this server yet.", 3600
            )
        if sum(bool(x) for x in (cursor, resume, start_now)) > 1:
            raise FeedError(400, "invalid_input", "Send at most one of cursor, resume_token and start=now.")
        state = self.state()
        epoch = str(state["epoch"])
        if not (cursor or resume):
            self.validate(filters)  # a token's filters were checked when it was first issued (SUB)
        if start_now:
            high = self.high()
            return self._answer([], False, None, (high, HIGHEST, 2**31 - 1), filters, epoch, high, limit)
        if cursor:
            body = self.tokens.open("c", cursor)
            self._check(body, filters, epoch, state)
            if body["x"] < time.time():
                raise FeedError(
                    409, "stale_cursor", "This cursor expired. Continue from your last resume token."
                )
            if body["l"] != limit:
                raise FeedError(400, "invalid_cursor", "A cursor keeps the page size it was issued with.")
            position, high = _position(body["p"]), body["h"]
        else:
            if resume:
                body = self.tokens.open("r", resume)
                self._check(body, filters, epoch, state)
                position = _position(body["p"])
            else:
                position = (state["purged_through_revision"], HIGHEST, 2**31 - 1)
            high = self.high()
        return self._scan(filters, limit, position, high, epoch)

    def _check(self, body, filters, epoch, state):
        if body.get("f") != filters.hash():
            raise FeedError(
                400, "invalid_cursor", "The filters differ from the ones this token was issued for."
            )
        if body.get("e") != epoch:
            raise FeedError(
                409,
                "feed_reset",
                "The change feed was reset. Capture a new start=now token, reconcile from a current release,"
                " then replay from that token.",
            )
        if body["p"][0] < state["purged_through_revision"]:
            raise FeedError(
                410,
                "replay_window_expired",
                "Changes after this position are no longer kept. Reconcile from a current release.",
            )

    def _scan(self, filters, limit, position, high, epoch):
        where, params = filters.sql()
        rows = (
            self.conn.execute(
                text(
                    f"SELECT * FROM {self.t['change_event']} e WHERE (e.publication_revision, e.event_id, e.event_revision)"
                    f" > (:r, :e, :v) AND e.publication_revision <= :h AND {where}"
                    " ORDER BY e.publication_revision, e.event_id, e.event_revision LIMIT :budget"
                ),
                params
                | {"r": position[0], "e": position[1], "v": position[2], "h": high, "budget": SCAN_BUDGET},
            )
            .mappings()
            .all()
        )
        visible = _Visibility(self.conn)
        events, last = [], position
        for row in rows:
            last = (row["publication_revision"], row["event_id"], row["event_revision"])
            shown = self.project(dict(row), visible)
            if shown is not None and filters.keyword(dict(row)):
                events.append(shown)
                if len(events) == limit:
                    break
        complete = len(events) < limit and len(rows) < SCAN_BUDGET
        if complete:
            last = (high, HIGHEST, 2**31 - 1)
        return self._answer(
            events, not complete, None if complete else last, last, filters, epoch, high, limit
        )

    def _answer(self, events, more, cursor_at, resume_at, filters, epoch, high, limit):
        now = int(time.time())
        answer = {
            "events": events,
            "has_more": more,
            "feed_epoch": epoch,
            "resume_token": self.tokens.seal(
                "r", {"e": epoch, "p": _wire(resume_at), "f": filters.hash(), "i": now}
            ),
        }
        if more and cursor_at is not None:
            answer["next_cursor"] = self.tokens.seal(
                "c",
                {
                    "e": epoch,
                    "p": _wire(cursor_at),
                    "h": high,
                    "f": filters.hash(),
                    "l": limit,
                    "i": now,
                    "x": now + CURSOR_SECONDS,
                },
            )
        return answer

    def event(self, event_id: str) -> dict:
        try:
            wanted = uuid.UUID(event_id)
        except ValueError:
            raise FeedError(404, "unavailable", "This change is not available.") from None
        row = (
            self.conn.execute(
                text(
                    f"SELECT * FROM {self.t['change_event']} WHERE event_id = :e ORDER BY event_revision DESC LIMIT 1"
                ),
                {"e": wanted},
            )
            .mappings()
            .first()
        )
        shown = self.project(dict(row), _Visibility(self.conn)) if row else None
        if shown is None:
            raise FeedError(404, "unavailable", "This change is not available.")
        return shown

    def validate(self, filters: Filters) -> None:
        """Unknown or no-longer-public IDs answer one generic refusal, saying neither which value nor
        whether it exists (SUB-03)."""
        from api.store import tables

        def known(table, values):
            if not values:
                return True
            column = tables[table].c.id
            found = self.conn.execute(select(column).where(column.in_(values))).scalars().all()
            return set(found) == set(values)

        if (
            not known("jurisdictions", filters.jurisdiction)
            or not known("languages", filters.language)
            or any(c not in DOCUMENT_CLASSES for c in filters.cls)
            or (filters.work and set(current(self.conn, filters.work)) != set(filters.work))
        ):
            raise FeedError(400, "invalid_filter", "The filter is not valid.")

    def recent(self, filters: Filters) -> list[dict]:
        """Newest first: the Atom and RSS window. Not a replay API."""
        self.validate(filters)
        where, params = filters.sql()
        rows = (
            self.conn.execute(
                text(
                    f"SELECT * FROM {self.t['change_event']} e WHERE e.committed_at > :since AND {where}"
                    " ORDER BY e.publication_revision DESC, e.event_id DESC, e.event_revision DESC LIMIT :budget"
                ),
                params | {"since": datetime.now(UTC) - timedelta(days=FEED_DAYS), "budget": SCAN_BUDGET},
            )
            .mappings()
            .all()
        )
        visible, found = _Visibility(self.conn), []
        for row in rows:
            shown = self.project(dict(row), visible)
            if shown is not None and filters.keyword(dict(row)):
                found.append(shown)
                if len(found) == FEED_ENTRIES:
                    break
        return found

    def project(self, row: dict, visible: "_Visibility") -> dict | None:
        """The public form of one event, or None while what it names is not public."""
        payload = row["payload"]
        kind = row["event_type"]
        work = visible.work(row["work_id"]) if kind != "coverage_changed" else None
        named = work is not None
        if kind in ("catalogue_added", "metadata_corrected") and not named:
            return None
        if kind == "text_version_published" and (not named or row["version_id"] not in work["versions"]):
            return None
        summary = payload.get("summary")
        withdrawal = None
        if kind == "withdrawal":
            # A text version withdrawn from a record still public is named, with what to do and why;
            # anything else says only to revalidate, never that a hidden record exists.
            told = (payload.get("withdrawal") or {}) if named else {}
            withdrawal = {
                "scope_disclosure": "named" if named else "revalidate_releases",
                "action": told.get("action", "revalidate"),
                "reason_code": told.get("reason_code"),
            }
            if not named:
                summary = "A previously public record was withdrawn. Revalidate any copies you keep."
        jurisdiction = None
        if named or kind == "coverage_changed":
            jurisdiction = {
                "id": row["jurisdiction_id"],
                "name": visible.jurisdiction(row["jurisdiction_id"]),
            }
        version = None
        if kind == "text_version_published" or (kind == "withdrawal" and named and row["version_id"]):
            facts = payload.get("version") or {}
            version = {
                "id": row["version_id"],
                "content_hash": facts.get("content_hash"),
                "language": facts.get("language"),
            }
        return {
            "event_id": str(row["event_id"]),
            "event_revision": row["event_revision"],
            "event_type": kind,
            "feed_epoch": str(row["feed_epoch"]),
            "publication_revision": row["publication_revision"],
            # In UTC whatever the database session's time zone.
            "committed_at": _time(row["committed_at"]),
            "observed_at": row["observed_at"] and _time(row["observed_at"]),
            "publisher_published_at": row["publisher_published_at"],
            "legal_effective": row["legal_effective"],
            "jurisdiction": jurisdiction,
            "work": self._work(row, payload) if named else None,
            "version": version,
            # The rights record revision in force for a text published or withdrawn (OA-R1).
            "rights_revision": payload.get("rights_revision") if version is not None else None,
            "changed_fields": payload.get("changed_fields", []) if named else [],
            "change_reason": row["change_reason"],
            "supersedes": {
                "event_id": str(row["supersedes_event_id"]),
                "event_revision": row["supersedes_revision"],
            }
            if row["supersedes_event_id"]
            else None,
            "summary": summary,
            "sources": payload.get("sources", []) if named else [],
            "withdrawal": withdrawal,
        }

    def _work(self, row, payload):
        return {
            "id": row["work_id"],
            "title": payload.get("title"),
            "citation": payload.get("citation"),
            "jurisdiction_id": row["jurisdiction_id"],
            "document_class": row["document_class"],
            "language": row["language"],
            "url": f"{self.origin}/sources/{row['work_id']}",
            "api_url": api_url(self.origin, row["work_id"]),
        }

    def coverage(self) -> dict:
        state = self.state()
        earliest = self.conn.scalar(text(f"SELECT min(committed_at) FROM {self.t['change_event']}"))
        rows = self.conn.execute(
            text(
                f"SELECT p.jurisdiction_id, count(*) FILTER (WHERE p.visible) AS works,"
                f" count(*) FILTER (WHERE p.visible AND cardinality(p.readable_versions) > 0) AS readable,"
                f" (SELECT max(committed_at) FROM {self.t['change_event']} e WHERE e.jurisdiction_id = p.jurisdiction_id)"
                f" AS last_change FROM {self.t['published_state']} p GROUP BY p.jurisdiction_id ORDER BY 1"
            )
        ).mappings()
        visible = _Visibility(self.conn)
        return {
            "feed_started_at": state["baseline_at"] and _time(state["baseline_at"]),
            "feed_epoch": str(state["epoch"]),
            "earliest_replayable_at": earliest and _time(earliest),
            "supported_event_types": list(SUPPORTED),
            "supported_filters": ["jurisdiction", "work", "type", "class", "language", "q"],
            "jurisdictions": [
                {
                    "id": r["jurisdiction_id"],
                    "name": visible.jurisdiction(r["jurisdiction_id"]),
                    "works": r["works"],
                    "readable_works": r["readable"],
                    "last_change_at": r["last_change"] and _time(r["last_change"]),
                    "last_checked_at": None,
                    "expected_cadence": None,
                    "known_gaps": None,
                }
                for r in rows
                if r["works"]
            ],
            "notice": "Changes to what Open Alvary holds, not every change in a country's law.",
        }


class _Visibility:
    """Current public state, looked up once per work per request."""

    def __init__(self, conn):
        self.conn, self.works, self.names = conn, {}, {}

    def work(self, work_id):
        if work_id not in self.works:
            self.works[work_id] = current(self.conn, [work_id]).get(work_id)
        return self.works[work_id]

    def jurisdiction(self, jurisdiction_id):
        if jurisdiction_id not in self.names:
            from api.store import tables

            table = tables["jurisdictions"]
            payload = self.conn.scalar(
                text(f"SELECT payload FROM {table.name} WHERE id = :j"), {"j": jurisdiction_id}
            )
            self.names[jurisdiction_id] = (payload or {}).get("name", jurisdiction_id)
        return self.names[jurisdiction_id]


def api_url(origin: str, work_id: str) -> str:
    """The record in the API. With accounts on (activation B) the anonymous API answers 410, so
    the link is the keyed one on the API's own domain."""
    if os.getenv("ALVARY_ACCOUNTS") == "1":
        api = os.getenv("ALVARY_API_ORIGIN", "https://api.open.alvary.ai").rstrip("/")
        return f"{api}/api/v1/sources/{work_id}"
    return f"{origin}/api/sources/{work_id}"


def _position(wire):
    return (int(wire[0]), uuid.UUID(wire[1]), int(wire[2]))


def _wire(position):
    return [position[0], str(position[1]), position[2]]


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


# ---------- feeds ----------
def atom(events: list[dict], self_url: str, origin: str) -> str:
    updated = events[0]["committed_at"] if events else _time(datetime.now(UTC))
    entries = "".join(
        "<entry>"
        f"<id>urn:uuid:{e['event_id']}</id>"
        f"<title>{escape(_title(e))}</title>"
        f"<updated>{e['committed_at']}</updated>"
        f'<link rel="alternate" href="{escape(_link(e, origin))}"/>'
        f'<category term="{e["event_type"]}"/>'
        f"<summary>{escape(e['summary'] or '')}</summary>"
        "</entry>"
        for e in events
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        f"<id>{escape(self_url)}</id><title>Open Alvary changes</title><updated>{updated}</updated>"
        f'<link rel="self" href="{escape(self_url)}"/><link rel="alternate" href="{escape(origin)}/changes"/>'
        "<author><name>Open Alvary</name></author>"
        f"{entries}</feed>"
    )


def rss(events: list[dict], self_url: str, origin: str) -> str:
    items = "".join(
        "<item>"
        f"<title>{escape(_title(e))}</title>"
        f"<link>{escape(_link(e, origin))}</link>"
        f'<guid isPermaLink="false">urn:uuid:{e["event_id"]}</guid>'
        f"<pubDate>{_rfc822(e['committed_at'])}</pubDate>"
        f"<category>{e['event_type']}</category>"
        f"<description>{escape(e['summary'] or '')}</description>"
        "</item>"
        for e in events
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>'
        f"<title>Open Alvary changes</title><link>{escape(origin)}/changes</link>"
        "<description>Changes to the Open Alvary catalogue of African law.</description>"
        f'<atom:link href="{escape(self_url)}" rel="self" type="application/rss+xml"/>'
        f"{items}</channel></rss>"
    )


LABELS = {
    "catalogue_added": "Added",
    "metadata_corrected": "Corrected",
    "text_version_published": "Text published",
    "withdrawal": "Withdrawn",
    "coverage_changed": "Coverage",
}


def _title(event):
    name = (event["work"] or {}).get("title") or (event["jurisdiction"] or {}).get("name") or "A record"
    return f"{LABELS.get(event['event_type'], event['event_type'])}: {name}"


def _link(event, origin):
    return (event["work"] or {}).get("url") or f"{origin}/changes"


def _rfc822(stamp: str) -> str:
    moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return moment.strftime("%a, %d %b %Y %H:%M:%S +0000")
