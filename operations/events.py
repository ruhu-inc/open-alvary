"""Reviewed legal events and currentness profiles (CPR-02): proposed from published text, reviewed by an
editor in a file, published by the operator.

  python -m operations.events propose --work ng-data-protection-act-2023   proposals to review (no writes)
  python -m operations.events publish ingestion/events/ng.json [--dry-run]

An events file holds ``profiles`` and ``events``. An event names the work it affects, its scope (the whole
work, or provisions by locator), its type, its date and precision, any condition, its evidence and
who reviewed it when. Events are never edited: a correction is a new event that ``supersedes`` the old,
and the only change allowed to a published event is withdrawing it. Publishing writes them and
announces each new, corrected or withdrawn event in the change log, in one transaction.
"""

import argparse
import json
import re
import sys
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator
from sqlalchemy import text

EVENT_TYPES = Literal[
    "adopted", "assented", "published", "commenced", "amended", "repealed", "suspended", "revived", "expired",
    "corrected", "consolidated", "declared_invalid", "disapplied", "entered_into_force",
]  # fmt: skip
EVENT_ID = r"^evt_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Evidence(Record):
    url: HttpUrl
    locator: str | None = None  # the provision of the asserting text that says so
    quote: str | None = Field(None, max_length=1000)


class EventIn(Record):
    id: str = Field(pattern=EVENT_ID)
    type: EVENT_TYPES
    affected_work: str = Field(min_length=1, max_length=160)
    asserting_work: str | None = Field(None, min_length=1, max_length=160)
    scope: Literal["whole", "explicit_nodes"]
    locators: tuple[str, ...] = ()
    date_from: date | None = None
    date_to: date | None = None
    precision: Literal["day", "month", "year", "unknown"] = "day"
    conditions: str | None = Field(None, max_length=2000)
    evidence: Evidence
    review_state: Literal["reviewed", "withdrawn"] = "reviewed"
    reviewed_by: str = Field(min_length=1, max_length=200)
    reviewed_at: datetime
    supersedes: str | None = Field(None, pattern=EVENT_ID)

    @model_validator(mode="after")
    def consistent(self):
        if self.scope == "explicit_nodes" and not self.locators:
            raise ValueError("an explicit scope names its provisions")
        if self.scope == "whole" and self.locators:
            raise ValueError("a whole-work event names no provisions")
        if self.date_from is None and self.precision != "unknown":
            raise ValueError("an event without a date has precision unknown")
        if self.date_to and self.date_from and self.date_to < self.date_from:
            raise ValueError("date_to is before date_from")
        if self.reviewed_at.tzinfo is None:
            raise ValueError("reviewed_at needs a time zone")
        return self


class ProfileIn(Record):
    id: str = Field(min_length=1, max_length=160)
    jurisdiction_id: str
    document_type: str
    silent_commencement: Literal["assent", "unknown"]
    basis: str = Field(min_length=1, max_length=2000)  # the law or practice the rule rests on
    reviewed_by: str = Field(min_length=1, max_length=200)
    reviewed_at: datetime


class EventFile(Record):
    profiles: tuple[ProfileIn, ...] = ()
    events: tuple[EventIn, ...] = ()


class Refused(ValueError):
    pass


def _tables(conn) -> dict[str, str]:
    from changes.record import namespace

    ns = namespace(conn)
    return {
        "event": ns.qualified(conn, "corpus", "legal_event"),
        "profile": ns.qualified(conn, "corpus", "currentness_profile"),
    }


def _locators(conn, work_id: str) -> set[str]:
    from api.catalogue import LegacyCatalogue
    from api.repositories import LegacyRepository

    service = LegacyCatalogue(LegacyRepository(conn))
    source, rights = service._require(work_id)
    found = set()
    for version in service._approved(source, rights):
        found |= {n.locator for n in service._ordered_nodes(version)}
    return found


def publish(engine, events: EventFile, *, dry_run: bool = False) -> dict:
    """Write the file's profiles and events and announce them, in one transaction; refuse the whole file
    on any problem. Returns what was (or, in a dry run, would be) written."""
    from changes.record import current
    from changes.record import publish as record

    report = {"profiles": 0, "events_added": 0, "events_withdrawn": 0, "unchanged": 0, "announced": 0}
    with engine.connect() as conn, conn.begin() as transaction:
        t = _tables(conn)
        works = {e.affected_work for e in events.events}
        public = current(conn, works) if works else {}
        problems = [
            f"{e.id}: {e.affected_work} is not a public record"
            for e in events.events
            if e.affected_work not in public
        ]
        ids = {e.id for e in events.events}
        for event in events.events:
            if event.supersedes and event.supersedes not in ids:
                held = conn.scalar(
                    text(f"SELECT 1 FROM {t['event']} WHERE event_id = :e"), {"e": event.supersedes}
                )
                if held is None:
                    problems.append(f"{event.id}: supersedes an unknown event {event.supersedes}")
        known: dict[str, set[str]] = {}
        for event in events.events:
            if event.scope == "explicit_nodes" and event.affected_work in public:
                known.setdefault(event.affected_work, _locators(conn, event.affected_work))
                missing = [x for x in event.locators if x not in known[event.affected_work]]
                if missing:
                    problems.append(
                        f"{event.id}: no public text of {event.affected_work} has {', '.join(missing)}"
                    )
        if problems:
            raise Refused("; ".join(problems))
        for profile in events.profiles:
            conn.execute(
                text(
                    f"INSERT INTO {t['profile']} (profile_id, jurisdiction_id, document_type, silent_commencement,"
                    " basis, reviewed_by, reviewed_at) VALUES (:id, :j, :d, :s, :b, :r, :at)"
                    " ON CONFLICT (profile_id) DO UPDATE SET silent_commencement = EXCLUDED.silent_commencement,"
                    " basis = EXCLUDED.basis, reviewed_by = EXCLUDED.reviewed_by, reviewed_at = EXCLUDED.reviewed_at"
                ),
                {"id": profile.id, "j": profile.jurisdiction_id, "d": profile.document_type,
                 "s": profile.silent_commencement, "b": profile.basis, "r": profile.reviewed_by,
                 "at": profile.reviewed_at},
            )  # fmt: skip
            report["profiles"] += 1
        # An event that supersedes another in the same file is written after it.
        ordered = sorted(events.events, key=lambda e: (e.supersedes in ids, e.id))
        for event in ordered:
            values = {
                "id": event.id, "type": event.type, "work": event.affected_work, "by": event.asserting_work,
                "scope": event.scope, "locators": list(event.locators), "from": event.date_from,
                "to": event.date_to, "precision": event.precision, "conditions": event.conditions,
                "evidence": event.evidence.model_dump_json(), "state": event.review_state,
                "reviewer": event.reviewed_by, "at": event.reviewed_at, "supersedes": event.supersedes,
            }  # fmt: skip
            held = conn.execute(
                text(
                    f"SELECT event_type, affected_work, asserting_work, scope, locators, date_from, date_to,"
                    f" precision, conditions, evidence, review_state, supersedes_event_id FROM {t['event']}"
                    " WHERE event_id = :id"
                ),
                {"id": event.id},
            ).first()
            if held is None:
                conn.execute(
                    text(
                        f"INSERT INTO {t['event']} (event_id, event_type, affected_work, asserting_work, scope,"
                        " locators, date_from, date_to, precision, conditions, evidence, review_state, reviewed_by,"
                        " reviewed_at, supersedes_event_id) VALUES (:id, :type, :work, :by, :scope, :locators,"
                        " :from, :to, :precision, :conditions, CAST(:evidence AS jsonb), :state, :reviewer, :at,"
                        " :supersedes)"
                    ),
                    values,
                )
                report["events_added"] += 1
                continue
            same = (
                held.event_type == event.type
                and held.affected_work == event.affected_work
                and held.asserting_work == event.asserting_work
                and held.scope == event.scope
                and tuple(held.locators or ()) == event.locators
                and held.date_from == event.date_from
                and held.date_to == event.date_to
                and held.precision == event.precision
                and held.conditions == event.conditions
                and held.supersedes_event_id == event.supersedes
            )
            if not same:
                raise Refused(
                    f"{event.id}: published events are not edited; record a new event that supersedes it"
                )
            if held.review_state == event.review_state:
                report["unchanged"] += 1
            elif event.review_state == "withdrawn":
                conn.execute(
                    text(f"UPDATE {t['event']} SET review_state = 'withdrawn' WHERE event_id = :id"), values
                )
                report["events_withdrawn"] += 1
            else:
                raise Refused(f"{event.id}: a withdrawn event is not reinstated; record a new event")
        report["announced"] = record(conn, works) if works else 0
        if dry_run:
            transaction.rollback()
    return report


# --- proposals from published text ----------------------------------------------------------------

MONTHS = "january february march april may june july august september october november december".split()
_DATE = r"(\d{1,2})(?:st|nd|rd|th)?\s+(?:day\s+of\s+)?(" + "|".join(MONTHS) + r"),?\s+(\d{4})"
_COMMENCES = re.compile(
    r"(?:come|comes|came)\s+into\s+(?:force|operation|effect)|(?:shall\s+)?commence|takes?\s+effect", re.I
)
_ON_DATE = re.compile(r"\bon\s+(?:the\s+)?" + _DATE, re.I)
_APPOINTED = re.compile(r"(?:such\s+)?date\s+as\s+the\s+[\w\s]+?\s+may[\w\s,]*?\s+appoint", re.I)
_REPEAL = re.compile(r"\b[Tt]he\s+([A-Z][\w\s,()’'-]{3,120}?)\s+is\s+(?:hereby\s+)?repealed\b")


def propose_from(text: bytes, nodes, work_id: str, url: str) -> list[dict]:
    """Events a work's text states, for an editor to check: its commencement provision (a date, or a date
    to be appointed: a condition), and the laws it repeals. ``text`` is the canonical text's UTF-8
    bytes and ``nodes`` its structure, each with ``kind``, ``locator``, ``start_byte`` and ``end_byte``
    (as the catalogue and the workbench's built text both hold them); ``url`` is where the text is
    published, the evidence's address. No database is needed. Never counted until reviewed and
    published."""
    proposals = []
    for node in nodes:
        if node["kind"] not in ("section", "article"):
            continue
        words = " ".join(text[node["start_byte"] : node["end_byte"]].decode().split())
        evidence = {"url": url, "locator": node["locator"], "quote": words[:300]}
        if _COMMENCES.search(words):
            found = _ON_DATE.search(words)
            appointed = _APPOINTED.search(words)
            if found or appointed:
                day, condition = None, None
                if found:
                    try:
                        day = date(
                            int(found.group(3)), MONTHS.index(found.group(2).lower()) + 1, int(found.group(1))
                        )
                    except ValueError:  # "the 31st day of June": printed, but no such day
                        condition = f"The text gives a date that does not exist: {found.group(0)}"
                if appointed and not day:
                    condition = "On a date to be appointed: " + appointed.group(0)
                proposals.append(
                    {
                        "id": f"evt_{uuid.uuid4()}", "type": "commenced", "affected_work": work_id,
                        "scope": "whole", "date_from": day.isoformat() if day else None,
                        "precision": "day" if day else "unknown", "conditions": condition,
                        "evidence": evidence, "review_state": "reviewed", "reviewed_by": "", "reviewed_at": "",
                    }
                )  # fmt: skip
        for repealed in _REPEAL.finditer(words):
            proposals.append(
                {
                    "id": f"evt_{uuid.uuid4()}", "type": "repealed", "affected_work": None,
                    "affected_title": repealed.group(1).strip(), "asserting_work": work_id, "scope": "whole",
                    "date_from": None, "precision": "unknown", "evidence": evidence,
                    "review_state": "reviewed", "reviewed_by": "", "reviewed_at": "",
                }
            )  # fmt: skip
    return proposals


def propose(conn, work_id: str) -> list[dict]:
    """Events the published text of a work states (propose_from), read from its first public text;
    editors review other texts separately."""
    from api.catalogue import LegacyCatalogue
    from api.repositories import LegacyRepository

    service = LegacyCatalogue(LegacyRepository(conn))
    source, rights = service._require(work_id)
    for version in service._approved(source, rights):
        nodes = [
            {"kind": n.kind, "locator": n.locator, "start_byte": n.start_byte, "end_byte": n.end_byte}
            for n in service._ordered_nodes(version)
        ]
        return propose_from(version.canonical_text.encode(), nodes, work_id, str(version.canonical_url))
    return []


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    proposing = commands.add_parser("propose")
    proposing.add_argument("--work", required=True)
    publishing = commands.add_parser("publish")
    publishing.add_argument("file", type=Path)
    publishing.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    from api.store import engine_for

    engine = engine_for()
    if args.command == "propose":
        with engine.begin() as conn:
            print(json.dumps({"events": propose(conn, args.work)}, indent=2, ensure_ascii=False))
        print(
            "Proposals only: an editor checks each against the text, fills affected_work, reviewed_by and "
            "reviewed_at (and dates or conditions), then publishes the file.",
            file=sys.stderr,
        )
        return 0
    try:
        report = publish(engine, EventFile.model_validate_json(args.file.read_text()), dry_run=args.dry_run)
    except Refused as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report | {"dry_run": args.dry_run}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
