"""Currentness of a unit at a date (RSV-08, IDT-03, CPR-02).

Two bases, never mixed up:

- ``provision``: the work has reviewed legal events (corpus.legal_event, published by
  operations.events). currentness-v1 decides each provision from the events whose scope covers it
  (the whole work, the provision, or a provision containing it) and the jurisdiction's reviewed rule
  profile for its document class:
  commenced (or entered into force, or revived) on or before the date and not ceased since: in_force;
  proven not yet commenced, or ceased (repealed, expired, declared invalid, disapplied), or within a
  suspension: not_in_force; no commencement evidence, an uncertain date or a condition: unknown;
  conflicting evidence (two commencements on different dates, a revival after a repeal without an
  explicit supersession): contested. A superseded event no longer counts. A unit holding parts with
  their own events aggregates them: all alike, that state; a proven mixture, partly_in_force.
- ``record``: the work has no reviewed events. The record's legal status and the text version's
  effective dates are the only evidence; the state speaks for the whole text, never for one provision.
  With no date, the record's status now decides; with a date, the version's effective interval does,
  and a text with no effective date is ``unknown`` then.
"""

import calendar
from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Connection

from schema.public import Currentness

RECORD_ALGORITHM = "record-status-1"
ALGORITHM = "currentness-v1"
_BY_STATUS = {"current": "in_force", "repealed": "not_in_force", "superseded": "not_in_force",
              "historical": "not_in_force"}  # fmt: skip
STARTS = frozenset({"commenced", "entered_into_force", "revived"})
STOPS = frozenset({"repealed", "expired", "declared_invalid", "disapplied"})


@dataclass(frozen=True)
class Event:
    event_id: str
    event_type: str
    scope: str
    locators: tuple[str, ...]
    date_from: date | None
    date_to: date | None
    precision: str
    conditions: str | None
    supersedes: str | None


@dataclass(frozen=True)
class Profile:
    profile_id: str
    silent_commencement: str  # assent or unknown


def record_currentness(legal_status: str, version, as_of: date | None, today: date) -> Currentness:
    reasons: list[str] = []
    if as_of is None:
        state = _BY_STATUS.get(legal_status, "unknown")
        if state == "unknown":
            reasons.append("The record's legal status is unknown.")
        if version.effective_until is not None and today >= version.effective_until and state == "in_force":
            state = "not_in_force"
            reasons.append("The text's effective period has ended.")
    elif version.effective_from is None:
        state = "unknown"
        reasons.append("The text has no effective date to compare with the date asked.")
    elif as_of < version.effective_from:
        state = "not_in_force"
        reasons.append("The date asked is before the text's effective date.")
    elif version.effective_until is not None and as_of >= version.effective_until:
        state = "not_in_force"
        reasons.append("The date asked is after the text's effective period.")
    else:
        state = "in_force"
    reasons.append("Assessed for the whole text from the record; no provision-level events are recorded.")
    return Currentness(
        state=state,
        basis="record",
        assessed_at=as_of or today,
        algorithm=RECORD_ALGORITHM,
        reasons=tuple(reasons),
    )


# --- currentness-v1 -----------------------------------------------------------------------------------


def _period_end(event: Event) -> date:
    """The last day the event's date could be, given its precision."""
    day = event.date_from
    if event.precision == "year":
        return date(day.year, 12, 31)
    if event.precision == "month":
        return date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])
    return day


def _before(event: Event, at: date) -> bool:
    """Certainly on or before ``at``."""
    return event.date_from is not None and event.precision != "unknown" and _period_end(event) <= at


def _after(event: Event, at: date) -> bool:
    """Certainly after ``at``."""
    return event.date_from is not None and event.precision != "unknown" and event.date_from > at


def covers(event: Event, locator: str) -> bool:
    return event.scope == "whole" or any(locator == x or locator.startswith(x + "__") for x in event.locators)


def provision_state(
    events: list[Event], locator: str, at: date, profile: Profile | None
) -> tuple[str, list[str], list[str]]:
    """One provision's state at ``at``: (state, reasons, the events relied on)."""
    superseded = {e.supersedes for e in events if e.supersedes}
    relevant = [e for e in events if e.event_id not in superseded and covers(e, locator)]
    used = [e.event_id for e in relevant]
    if any(e.conditions for e in relevant if e.event_type in STARTS | STOPS):
        return "unknown", ["A relevant event has a condition that is not assessed."], used
    starts = [e for e in relevant if e.event_type in STARTS]
    stops = [e for e in relevant if e.event_type in STOPS]
    if not starts and profile is not None and profile.silent_commencement == "assent":
        assent = [e for e in relevant if e.event_type == "assented" and e.scope == "whole"]
        starts = assent  # the reviewed profile: an instrument silent on commencement commences on assent
    if not starts:
        return "unknown", ["No reviewed commencement evidence covers this provision."], used
    commenced = [e for e in starts if e.event_type != "revived" and _before(e, at)]
    if len({e.date_from for e in commenced}) > 1:
        return "contested", ["Reviewed events give different commencement dates."], used
    ceased = [e for e in stops if _before(e, at)]
    revived = [e for e in starts if e.event_type == "revived" and _before(e, at)]
    if ceased and revived and any(r.date_from >= s.date_from for r in revived for s in ceased):
        return "contested", ["A revival follows a repeal without a reviewed supersession."], used
    if ceased:
        return (
            "not_in_force",
            [f"Ceased: {ceased[0].event_type} from {ceased[0].date_from.isoformat()}."],
            used,
        )
    for event in relevant:
        if (
            event.event_type == "suspended"
            and _before(event, at)
            and (event.date_to is None or at < event.date_to)
        ):
            return "not_in_force", ["Suspended on the date asked."], used
    if commenced or revived:
        return "in_force", [], used
    if all(_after(e, at) for e in starts):
        return "not_in_force", ["Commences after the date asked."], used
    return "unknown", ["The commencement date is not certain relative to the date asked."], used


def aggregate(states: list[str]) -> str:
    unique = set(states)
    if len(unique) == 1:
        return states[0]
    if "contested" in unique:
        return "contested"
    if unique <= {"in_force", "not_in_force", "partly_in_force"}:
        return "partly_in_force"
    return "unknown"


def provision_currentness(
    events: list[Event],
    profile: Profile | None,
    locator: str,
    inner: list[str],
    as_of: date | None,
    today: date,
) -> Currentness:
    """A unit's currentness: its own provision and every part inside it (``inner``) that an event names."""
    at = as_of or today
    named = {x for e in events for x in e.locators}
    assessed = [locator] + [i for i in inner if any(i == x or i.startswith(x + "__") for x in named)]
    results = [provision_state(events, loc, at, profile) for loc in dict.fromkeys(assessed)]
    state = aggregate([r[0] for r in results])
    reasons = list(dict.fromkeys(reason for r in results for reason in r[1]))
    evidence = tuple(dict.fromkeys(e for r in results for e in r[2]))
    return Currentness(
        state=state,
        basis="provision",
        assessed_at=at,
        algorithm=ALGORITHM,
        profile=profile.profile_id if profile else None,
        evidence=evidence,
        reasons=tuple(reasons),
    )


# --- reading events ---------------------------------------------------------------------------------


def _table(conn: Connection, name: str) -> str | None:
    if conn.dialect.name != "postgresql":
        return None
    from changes.record import namespace

    table = namespace(conn).qualified(conn, "corpus", name)
    return table if conn.scalar(text("SELECT to_regclass(:t)"), {"t": table}) is not None else None


class Events:
    """Reviewed events and profiles, read once per work and class for one request."""

    def __init__(self, conn: Connection):
        self.conn = conn
        self.events_table = _table(conn, "legal_event")
        self.profiles_table = _table(conn, "currentness_profile")
        self._events: dict[str, list[Event]] = {}
        self._profiles: dict[tuple[str, str], Profile | None] = {}

    def of(self, work_id: str) -> list[Event]:
        if work_id not in self._events:
            rows = []
            if self.events_table is not None:
                rows = self.conn.execute(
                    text(
                        f"SELECT event_id, event_type, scope, locators, date_from, date_to, precision, conditions,"
                        f" supersedes_event_id FROM {self.events_table}"
                        " WHERE affected_work = :w AND review_state = 'reviewed' ORDER BY event_id"
                    ),
                    {"w": work_id},
                ).all()
            self._events[work_id] = [
                Event(r[0], r[1], r[2], tuple(r[3] or ()), r[4], r[5], r[6], r[7], r[8]) for r in rows
            ]
        return self._events[work_id]

    def profile(self, jurisdiction_id: str, document_type: str) -> Profile | None:
        key = (jurisdiction_id, document_type)
        if key not in self._profiles:
            row = None
            if self.profiles_table is not None:
                row = self.conn.execute(
                    text(
                        f"SELECT profile_id, silent_commencement FROM {self.profiles_table}"
                        " WHERE jurisdiction_id = :j AND document_type = :d"
                    ),
                    {"j": jurisdiction_id, "d": document_type},
                ).first()
            self._profiles[key] = Profile(row[0], row[1]) if row else None
        return self._profiles[key]

    def currentness(self, source, version, locator: str, inner: list[str], as_of: date | None, today: date):
        """Provision-level when the work has reviewed events; otherwise record-level."""
        events = self.of(source.id)
        if not events:
            return record_currentness(source.legal_status, version, as_of, today)
        profile = self.profile(source.jurisdiction_id, source.document_type)
        return provision_currentness(events, profile, locator, inner, as_of, today)
