"""Reading the public change log (SUB C1, changes-contracts.md): filters, encrypted cursors and resume
tokens, replay pages, single events, Atom and RSS windows and coverage.

Every read rechecks that a work is public now before naming it; a withdrawal of a record that is no
longer public is shown without identifying it, except to a keyed caller when the withdrawal's reason may
be told (owner decision, 2026-10-22; Withdrawals): then it names the record, and the text, by their IDs
only. Positions are (publication revision, event ID, event revision); a window freezes its upper
revision H, so nothing committed later appears inside it.
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

from changes.record import DISCLOSED_REASONS, WITH_RIGHTS_REVISION, current, digest, namespace
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
    "membership_recorded",
    "suspension_recorded",
    "rights_changed",
)
SUPPORTED = (
    "catalogue_added",
    "metadata_corrected",
    "text_version_published",
    "legal_effect_recorded",
    "treatment_recorded",
    "withdrawal",
    "coverage_changed",
    "membership_recorded",
    "suspension_recorded",
    "rights_changed",
)
# Events about a record shown only while it is public.
NAMED_ONLY = ("catalogue_added", "metadata_corrected", "legal_effect_recorded", "treatment_recorded",
              "rights_changed")  # fmt: skip
# Events about a jurisdiction rather than a record: never subject to a record's visibility.
PLACE_EVENTS = ("coverage_changed", "membership_recorded", "suspension_recorded")
# Events about a member and a body, each in the payload's object of that name.
BODY_EVENTS = {"membership_recorded": "membership", "suspension_recorded": "suspension"}
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
                # A membership, or a suspension from participation, concerns its body as much as its member.
                alternatives += [
                    f"(e.event_type = '{kind}' AND e.payload #>> '{{{name},body,id}}' = :j{i})"
                    for kind, name in BODY_EVENTS.items()
                ]
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

    def selects(self, row: dict, shown: dict) -> bool:
        """keyword() for an event as served: a withdrawal that shows no title of its record (unnamed, or
        named by its IDs only) is never selected by words it no longer shows."""
        if self.q and shown["event_type"] == "withdrawal" and not (shown["work"] or {}).get("title"):
            return True
        return self.keyword(row)

    def matches(self, event: dict) -> list[str] | None:
        """The same test as sql() and keyword(), for one event: the dimensions it matched, or None."""
        if self.type and event["event_type"] not in self.type:
            return None
        if _notice(event):
            return ["notice"]
        matched = []
        if self.jurisdiction:
            place = event["jurisdiction_id"]
            told = event["payload"].get(BODY_EVENTS.get(event["event_type"], "membership")) or {}
            body = told.get("body", {}).get("id")
            if body not in self.jurisdiction and not any(
                place == j or ("-" not in j and place.startswith(j + "-")) for j in self.jurisdiction
            ):
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
    # A caller with a key or token (api.credentials admitted it): told, by IDs, of the records and texts
    # withdrawn for a reason that may be told (Withdrawals).
    keyed: bool = False
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
            if shown is not None and filters.selects(dict(row), shown):
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
            if shown is not None and filters.selects(dict(row), shown):
                found.append(shown)
                if len(found) == FEED_ENTRIES:
                    break
        return found

    def project(self, row: dict, visible: "_Visibility") -> dict | None:
        """The public form of one event, or None while what it names is not public."""
        payload = row["payload"]
        kind = row["event_type"]
        work = visible.work(row["work_id"]) if kind not in PLACE_EVENTS else None
        named = work is not None
        membership = suspension = None
        if kind in BODY_EVENTS:
            # A reviewed membership or suspension is a public fact about two jurisdictions; their names are
            # current ones.
            told = dict(payload.get(BODY_EVENTS[kind]) or {})
            for side in ("member", "body"):
                place = (told.get(side) or {}).get("id")
                told[side] = {"id": place, "name": visible.jurisdiction(place)}
            membership, suspension = (told, None) if kind == "membership_recorded" else (None, told)
        if kind in NAMED_ONLY and not named:
            return None
        legal_effect = treatment = None
        if kind in ("legal_effect_recorded", "treatment_recorded"):
            told = payload.get("legal_effect" if kind == "legal_effect_recorded" else "treatment") or {}
            shown = dict(told)
            # The other record: `by` the one that had the effect (a provision event, CPR-02, may have none),
            # or `of` the instrument a law gives effect to (changes.record.BOTH_SIDES); for a treatment, `of`
            # the judgment treated, or `by` the judgment that overruled this one (changes.record.TREATED).
            for role in ("by", "of"):
                if role not in told:
                    continue
                other = visible.work((told.get(role) or {}).get("id"))
                if other is None:  # the other record is not public: say nothing of it
                    return None
                shown[role] = {
                    "id": told[role]["id"],
                    "title": other["projection"].get("title"),
                    "citation": other["projection"].get("citation"),
                }
            legal_effect, treatment = (shown, None) if kind == "legal_effect_recorded" else (None, shown)
        if kind == "text_version_published" and (not named or row["version_id"] not in work["versions"]):
            return None
        summary = payload.get("summary")
        withdrawal = None
        # A withdrawal from a record no longer public, told to a keyed caller by IDs only (Withdrawals).
        by_id = kind == "withdrawal" and not named and self.keyed and visible.told(row) is not None
        if kind == "withdrawal":
            # A text version withdrawn from a record still public is named, with what to do and why; so,
            # to a keyed caller, is a record or text withdrawn for a reason that may be told. Anything else
            # says only to revalidate, never that a hidden record exists.
            told = (payload.get("withdrawal") or {}) if named or by_id else {}
            withdrawal = {
                "scope_disclosure": "named" if named or by_id else "revalidate_releases",
                "action": told.get("action", "revalidate"),
                "reason_code": told.get("reason_code"),
            }
            if by_id:
                summary = (
                    "A text of a record that is no longer public was withdrawn. Stop serving copies of it."
                    if row["version_id"]
                    else "A previously public record was withdrawn. Stop serving it and its texts."
                )
            elif not named:
                summary = "A previously public record was withdrawn. Revalidate any copies you keep."
        jurisdiction = None
        if named or by_id or kind in PLACE_EVENTS:
            jurisdiction = {
                "id": row["jurisdiction_id"],
                "name": visible.jurisdiction(row["jurisdiction_id"]),
            }
        version = None
        if kind == "text_version_published" or (
            kind == "withdrawal" and (named or by_id) and row["version_id"]
        ):
            facts = (payload.get("version") or {}) if named or kind == "text_version_published" else {}
            version = {
                "id": row["version_id"],
                "content_hash": facts.get("content_hash"),
                "language": facts.get("language"),
            }
        return {
            "schema_version": payload.get("schema_version", "change-event-1"),
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
            "work": self._work(row, payload, work) if named else (self._ids(row) if by_id else None),
            "version": version,
            # The rights record revision in force after the change, where it was recorded (OA-R1).
            "rights_revision": payload.get("rights_revision")
            if named and kind in WITH_RIGHTS_REVISION
            else None,
            "changed_fields": payload.get("changed_fields", []) if named or kind in BODY_EVENTS else [],
            "change_reason": row["change_reason"],
            "supersedes": {
                "event_id": str(row["supersedes_event_id"]),
                "event_revision": row["supersedes_revision"],
            }
            if row["supersedes_event_id"]
            else None,
            "summary": summary,
            "sources": payload.get("sources", []) if named or kind in BODY_EVENTS else [],
            "withdrawal": withdrawal,
            "legal_effect": legal_effect,
            # A reviewer's direct treatment of one judgment by another, never an inferred risk (EDT-02).
            "treatment": treatment,
            "membership": membership,
            # Never a change of membership: a suspended member remains one.
            "suspension": suspension,
            # A correction's previous values, only for fields that can never have been withheld.
            "previous": payload.get("previous", {}) if named and kind == "metadata_corrected" else {},
            "version_diff": payload.get("version_diff") if kind == "text_version_published" else None,
            # A new revision of the record's rights record: {revision, decision} (G15).
            "rights": payload.get("rights") if kind == "rights_changed" else None,
        }

    @staticmethod
    def _ids(row):
        """A record that is no longer public, named by its IDs only: every other field is null."""
        return {
            "id": row["work_id"],
            "title": None,
            "citation": None,
            "legal_status": None,
            "jurisdiction_id": row["jurisdiction_id"],
            "document_class": None,
            "language": None,
            "url": None,
            "api_url": None,
        }

    def _work(self, row, payload, current=None):
        return {
            "id": row["work_id"],
            "title": payload.get("title"),
            "citation": payload.get("citation"),
            # Whether it is in force now, as the catalogue has it (not as it was when the change was made).
            "legal_status": ((current or {}).get("projection") or {}).get("legal_status"),
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


class Withdrawals:
    """What a keyed caller may be told of records and texts that are no longer public (owner decision,
    2026-10-22). A record or text that was public and was withdrawn for a reason that may be told
    (changes.record.DISCLOSED_REASONS: its rights record changed or lapsed, or its approval was withdrawn)
    is named by its IDs in the feed, and reads answer `withdrawn`. One withdrawn for a confidential reason
    (privacy, a legal or court order, another confidential reason, or none recorded), one never public and
    one that never existed are the same: unnamed, and `unavailable`. Nothing announced before a record's
    last confidential withdrawal is told again (changes.record.withhold appends one when a reason turns
    out to be confidential later). Read from the change log: what the last comparison recorded, so a
    record hidden by a write not yet compared is told nothing."""

    def __init__(self, conn: Connection):
        ns = namespace(conn)
        self.conn = conn
        self.t = {n: ns.qualified(conn, "changes", n) for n in ("change_event", "published_state")}
        self.records: dict[str, dict | None] = {}
        self.withheld: dict[str, int] = {}  # work -> publication revision of its last confidential withdrawal

    def record(self, work_id: str | None) -> dict | None:
        """The withdrawal of a record that is no longer public, when it may be told: what its latest
        whole-record withdrawal recorded ({action, reason_code, versions}). None while the record is
        public as last recorded, when its latest withdrawal was confidential, and when none is recorded
        (it was never public, or never existed)."""
        if work_id not in self.records:
            latest = self.conn.execute(
                text(
                    f"SELECT e.payload FROM {self.t['change_event']} e JOIN {self.t['published_state']} p"
                    " ON p.work_id = e.work_id AND NOT p.visible WHERE e.work_id = :w AND e.version_id IS NULL"
                    " AND e.event_type IN ('withdrawal', 'catalogue_added')"
                    " ORDER BY e.publication_revision DESC, e.event_revision DESC LIMIT 1"
                ),
                {"w": work_id},
            ).scalar()
            told = (latest or {}).get("withdrawal") or {}
            self.records[work_id] = told if told.get("reason_code") in DISCLOSED_REASONS else None
        return self.records[work_id]

    def told(self, row: dict) -> dict | None:
        """For a withdrawal event of a record that is no longer public: what may be told of it, when its
        own reason may be told, so may the record's latest withdrawal, and no confidential withdrawal of
        the record came after it (one withheld later stays unnamed, whatever follows)."""
        told = row["payload"].get("withdrawal") or {}
        work_id = row["work_id"]
        if told.get("reason_code") not in DISCLOSED_REASONS or self.record(work_id) is None:
            return None
        return told if row["publication_revision"] > self._withheld(work_id) else None

    def _withheld(self, work_id: str) -> int:
        """The publication revision of the record's last confidential withdrawal; 0 when it has none."""
        if work_id not in self.withheld:
            self.withheld[work_id] = self.conn.scalar(
                text(
                    f"SELECT coalesce(max(publication_revision), 0) FROM {self.t['change_event']}"
                    " WHERE work_id = :w AND version_id IS NULL AND event_type = 'withdrawal'"
                    " AND coalesce(payload #>> '{withdrawal,reason_code}', '') <> ALL(:told)"
                ),
                {"w": work_id, "told": list(DISCLOSED_REASONS)},
            )
        return self.withheld[work_id]

    def version(self, version_id: str | None) -> bool:
        """Whether a keyed caller may be told this text was withdrawn: it was public, it is not readable
        as last recorded, and its withdrawal, or its record's, may be told."""
        from api.store import tables

        versions = tables["versions"]
        owner = self.conn.scalar(select(versions.c.source_id).where(versions.c.id == version_id))
        if owner is None:
            return False
        state = self.conn.execute(
            text(f"SELECT visible, readable_versions FROM {self.t['published_state']} WHERE work_id = :w"),
            {"w": owner},
        ).first()
        if state is None:
            return False
        if not state.visible:  # the whole record was withdrawn: was this one of its public texts?
            told = self.record(owner)
            return told is not None and (
                version_id in told.get("versions", []) or self._announced(version_id, self._withheld(owner))
            )
        if version_id in state.readable_versions:
            return False
        latest = self.conn.execute(
            text(
                f"SELECT event_type, payload, publication_revision FROM {self.t['change_event']}"
                " WHERE version_id = :v AND event_type IN ('text_version_published', 'withdrawal')"
                " ORDER BY publication_revision DESC, event_revision DESC LIMIT 1"
            ),
            {"v": version_id},
        ).first()
        return (
            latest is not None
            and latest.event_type == "withdrawal"
            and (latest.payload.get("withdrawal") or {}).get("reason_code") in DISCLOSED_REASONS
            and latest.publication_revision > self._withheld(owner)
        )

    def item(self, item) -> bool:
        """For a cited part to check (schema.public.CheckItem) that is not public: whether its text may be
        told withdrawn."""
        return self.anchor(item.anchor_id) if item.anchor_id is not None else self.version(item.version_id)

    def anchor(self, anchor_id: str | None) -> bool:
        """Whether a keyed caller may be told the text this anchor is in was withdrawn."""
        from api.store import tables

        structure = tables["structure"]
        version_id = self.conn.scalar(select(structure.c.version_id).where(structure.c.id == anchor_id))
        return version_id is not None and self.version(version_id)

    def _announced(self, version_id: str, since: int) -> bool:
        """Whether the change log announced this text as public, or its withdrawal, after a publication
        revision (its record's last confidential withdrawal: what came before it is never told)."""
        return bool(
            self.conn.scalar(
                text(
                    f"SELECT EXISTS(SELECT 1 FROM {self.t['change_event']} WHERE version_id = :v"
                    " AND event_type IN ('text_version_published', 'withdrawal') AND publication_revision > :r)"
                ),
                {"v": version_id, "r": since},
            )
        )


class _Visibility:
    """Current public state, looked up once per work per request."""

    def __init__(self, conn):
        self.conn, self.works, self.names = conn, {}, {}
        self._withdrawals: Withdrawals | None = None

    def told(self, row: dict) -> dict | None:
        """Withdrawals.told, for a withdrawal event of a record that is no longer public."""
        if self._withdrawals is None:
            self._withdrawals = Withdrawals(self.conn)
        return self._withdrawals.told(row)

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
    "membership_recorded": "Membership",
    "suspension_recorded": "Suspension",
    "treatment_recorded": "Treatment",
    "rights_changed": "Rights",
}


def _title(event):
    name = (event["work"] or {}).get("title") or (event["jurisdiction"] or {}).get("name") or "A record"
    return f"{LABELS.get(event['event_type'], event['event_type'])}: {name}"


def _link(event, origin):
    return (event["work"] or {}).get("url") or f"{origin}/changes"


def _rfc822(stamp: str) -> str:
    moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return moment.strftime("%a, %d %b %Y %H:%M:%S +0000")
