"""Membership of the African Union and the regional bodies (JUR-01, JUR-04): researched as proposals in
the membership register (schema/memberships.json), approved by the reviewer, published by the operator.

  python -m operations.memberships review ml:aa-ecowas:1 --by "Full Name" [--valid-from DATE]
      [--valid-to DATE | --ongoing] [--basis KIND --instrument TEXT --pinpoint ART --basis-evidence URL
      [--basis-quote TEXT]] [--evidence URL] [--note TEXT]
  python -m operations.memberships review ml:aa-au:1:s1 --by "Full Name" [--valid-from DATE]
      [--valid-to DATE --lifted-evidence URL | --ongoing] [--evidence URL] [--note TEXT]
  python -m operations.memberships publish [--dry-run]

A DATE is a day (2001-05-26), a month (2000-12), a year (1976) or a window holding the day
([2016-08-15..2016-09-05]), as schema.dates reads it; FIRST/LAST, an EDTF interval, is refused.
``review`` approves one row of the register, a membership or a suspension from participation: it records
who reviewed it and when, with the start date (a reviewed row is dated) and any end, evidence or note the
reviewer sets. A reviewed membership states its basis, why it starts when it does, with the article that
says so: from the proposal, or given here. A reviewed suspension states its legal basis (from the
proposal), each article with its role, the one its decision cites (decision_basis) among them, and, once
lifted, the decision that lifted it. Run it again on a reviewed row to record its end
or a correction; it is then reviewed again. One person may review (single-reviewer rule). Commit the
register: it is the public record of what was reviewed, by whom.

``publish`` writes the reviewed memberships and suspensions to the catalogue (corpus.membership and
corpus.membership_suspension, PostgreSQL), adds the catalogue rows of the members and bodies it lacks,
removes rows no longer reviewed and announces each change in the change log (membership_recorded,
suspension_recorded), in one transaction. Proposals are never published.
"""

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text

from operations.publish import Refused, decider, evidence_links
from schema.dates import UncertainDate
from schema.jurisdictions import catalogue_entry, registry
from schema.memberships import BASES, Membership, Suspension, register, write

DATED = ("valid_from", "valid_from_earliest", "valid_from_latest", "valid_to", "valid_to_earliest",
         "valid_to_latest")  # fmt: skip
COLUMNS = ("member_id", "body_id", *DATED, "basis", "evidence_url", "note", "reviewed_by", "reviewed_at")
SUSPENSION_COLUMNS = ("membership_id", *DATED, "legal_basis", "evidence_url", "lifted_evidence_url", "note",
                      "reviewed_by", "reviewed_at")  # fmt: skip
JSONB = ("basis", "legal_basis")
SUSPENSION = re.compile(r":s[1-9][0-9]{0,2}$")


def review(
    record_id: str,
    by: str,
    *,
    valid_from: str | None = None,
    valid_to: str | None = None,
    ongoing: bool = False,
    basis: dict | None = None,
    evidence: str | None = None,
    lifted_evidence: str | None = None,
    note: str | None = None,
    source: Path | None = None,
    now: datetime | None = None,
) -> Membership | Suspension:
    """Approve one membership or suspension of the register into a reviewed fact, and write the register."""
    by = decider(by)
    held = register(source)
    suspension = bool(SUSPENSION.search(record_id))
    rows = list(held.suspensions if suspension else held.memberships)
    index = next((i for i, row in enumerate(rows) if row.id == record_id), None)
    if index is None:
        raise Refused(
            f"There is no {'suspension' if suspension else 'membership'} {record_id} in the register."
        )
    if ongoing and valid_to is not None:
        raise Refused("Give an end date or --ongoing, not both.")
    if suspension and basis is not None:
        raise Refused("A suspension's legal basis is recorded in its proposal, not with --basis.")
    if not suspension and lifted_evidence is not None:
        raise Refused("Only a suspension is lifted.")
    update = {
        "status": "reviewed",
        "reviewed_by": by,
        "reviewed_at": now or datetime.now(UTC).replace(microsecond=0),
        **{key: value for key, value in (("valid_from", valid_from), ("valid_to", valid_to), ("basis", basis),
                                          ("evidence_url", evidence), ("lifted_evidence_url", lifted_evidence),
                                          ("note", note)) if value is not None},
        **({"valid_to": None} if ongoing else {}),
    }  # fmt: skip
    kind = Suspension if suspension else Membership
    try:
        reviewed = kind.model_validate(rows[index].model_dump() | update)
        if suspension:
            evidence_links(
                [reviewed.evidence_url, *([reviewed.lifted_evidence_url] if reviewed.lifted_evidence_url else []),
                 *(p.evidence_url for p in reviewed.legal_basis)]
            )  # fmt: skip
        else:
            evidence_links([reviewed.evidence_url, reviewed.basis.evidence_url])
        rows[index] = reviewed
        field = "suspensions" if suspension else "memberships"
        write(held.model_copy(update={field: tuple(rows)}), source)
    except ValueError as error:
        raise Refused(str(error)) from None
    return reviewed


def _dates(row) -> dict:
    """A row's dates as stored: each EDTF string and its inclusive bounds (schema.dates)."""
    found = {}
    for name in ("valid_from", "valid_to"):
        value: UncertainDate | None = getattr(row, name)
        found |= {
            name: value and str(value),
            f"{name}_earliest": value and value.earliest,
            f"{name}_latest": value and value.latest,
        }
    return found


def _sync(conn, table: str, key: str, wanted: dict[str, dict], columns: tuple[str, ...]) -> dict:
    """Make ``table``'s rows the wanted ones: delete those not wanted, insert the new, update the changed.
    Returns the counts."""
    counts = {"published": 0, "updated": 0, "removed": 0, "unchanged": 0}
    held = {
        row[key]: row
        for row in conn.execute(text(f"SELECT {key}, {', '.join(columns)} FROM {table}")).mappings()
    }
    for gone in sorted(held.keys() - wanted.keys()):
        conn.execute(text(f"DELETE FROM {table} WHERE {key} = :id"), {"id": gone})
        counts["removed"] += 1
    placeholders = [f"CAST(:{c} AS jsonb)" if c in JSONB else f":{c}" for c in columns]
    for row_id, values in sorted(wanted.items()):
        was = held.get(row_id)
        if was is not None and all(was[c] == values[c] for c in columns):
            counts["unchanged"] += 1
            continue
        params = {"id": row_id} | {c: json.dumps(values[c]) if c in JSONB else values[c] for c in columns}
        if was is None:
            conn.execute(
                text(
                    f"INSERT INTO {table} ({key}, {', '.join(columns)}) VALUES (:id, {', '.join(placeholders)})"
                ),
                params,
            )
            counts["published"] += 1
            continue
        assignments = ", ".join(f"{c} = {p}" for c, p in zip(columns, placeholders, strict=True))
        conn.execute(
            text(f"UPDATE {table} SET {assignments}, published_at = statement_timestamp() WHERE {key} = :id"),
            params,
        )
        counts["updated"] += 1
    return counts


def publish(engine, source: Path | None = None, *, dry_run: bool = False) -> dict:
    """Make the catalogue's memberships and suspensions the register's reviewed ones and announce what
    changed, in one transaction. Returns what was (or, in a dry run, would be) done."""
    from api.store import check_legacy_authority, tables
    from changes.record import namespace
    from changes.record import publish as record

    held_register = register(source)
    reviewed = {m.id: m for m in held_register.reviewed()}
    suspensions = {s.id: s for s in held_register.reviewed_suspensions()}
    report = {"jurisdictions_added": []}
    with engine.connect() as conn, conn.begin() as transaction:
        if conn.dialect.name != "postgresql":
            raise Refused("Memberships are published to the PostgreSQL catalogue.")
        check_legacy_authority(conn, write=True)
        ns = namespace(conn)
        table = ns.qualified(conn, "corpus", "membership")
        suspended = ns.qualified(conn, "corpus", "membership_suspension")
        if conn.scalar(text("SELECT to_regclass(:t)"), {"t": suspended}) is None:
            raise Refused("The catalogue has no membership tables of this version yet: migrate it first.")
        places = tables["jurisdictions"]
        catalogued = set(conn.scalars(select(places.c.id)))
        profiles = registry()
        for code in sorted({c for m in reviewed.values() for c in (m.subject_id, m.target_id)} - catalogued):
            entry = catalogue_entry(code, profiles=profiles)
            conn.execute(places.insert().values(id=code, payload=entry.model_dump(mode="json")))
            report["jurisdictions_added"].append(code)
        # A suspension refers to its membership: those no longer reviewed go first.
        lifted = conn.execute(
            text(f"DELETE FROM {suspended} WHERE NOT (suspension_id = ANY(:keep))"),
            {"keep": sorted(suspensions)},
        ).rowcount
        report |= _sync(
            conn,
            table,
            "membership_id",
            {
                m.id: {
                    "member_id": m.subject_id,
                    "body_id": m.target_id,
                    **_dates(m),
                    "basis": m.basis.model_dump(mode="json", exclude_none=True),
                    "evidence_url": m.evidence_url,
                    "note": m.note,
                    "reviewed_by": m.reviewed_by,
                    "reviewed_at": m.reviewed_at,
                }
                for m in reviewed.values()
            },
            COLUMNS,
        )
        report["proposals_not_published"] = len(held_register.memberships) - len(reviewed)
        report["suspensions"] = _sync(
            conn,
            suspended,
            "suspension_id",
            {
                s.id: {
                    "membership_id": s.membership_id,
                    **_dates(s),
                    "legal_basis": [p.model_dump(mode="json", exclude_none=True) for p in s.legal_basis],
                    "evidence_url": s.evidence_url,
                    "lifted_evidence_url": s.lifted_evidence_url,
                    "note": s.note,
                    "reviewed_by": s.reviewed_by,
                    "reviewed_at": s.reviewed_at,
                }
                for s in suspensions.values()
            },
            SUSPENSION_COLUMNS,
        )
        report["suspensions"]["removed"] += lifted
        report["suspension_proposals_not_published"] = len(held_register.suspensions) - len(suspensions)
        # The change log records what this write changed, before it commits (SUB C1).
        report["announced"] = record(conn, ())
        if dry_run:
            transaction.rollback()
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    reviewing = commands.add_parser("review")
    reviewing.add_argument("record_id", help="A membership (ml:aa-ecowas:1) or a suspension (ml:aa-au:1:s1)")
    reviewing.add_argument("--by", required=True, help="Who reviews: a full name")
    reviewing.add_argument("--valid-from", help="The first day: YYYY-MM-DD, YYYY-MM, YYYY or [FIRST..LAST]")
    reviewing.add_argument("--valid-to", help="The last day, in the same forms")
    reviewing.add_argument("--ongoing", action="store_true", help="It has not ended (or not been lifted)")
    reviewing.add_argument("--basis", choices=BASES, help="Why the membership starts when it does")
    reviewing.add_argument("--instrument", help="The instrument whose article sets the start")
    reviewing.add_argument("--pinpoint", help="Its article, such as 'art. 28'")
    reviewing.add_argument("--basis-evidence", help="Where the article was read")
    reviewing.add_argument("--basis-quote", help="The article's words, up to 400 characters")
    reviewing.add_argument("--evidence")
    reviewing.add_argument("--lifted-evidence", help="The decision that lifted a suspension")
    reviewing.add_argument("--note")
    publishing = commands.add_parser("publish")
    publishing.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "review":
            parts = (args.instrument, args.pinpoint, args.basis_evidence, args.basis_quote)
            if args.basis is None and any(parts):
                raise Refused("Give the basis with --basis KIND, its instrument, article and evidence.")
            basis = args.basis and {
                "kind": args.basis, "instrument": args.instrument, "pinpoint": args.pinpoint,
                "evidence_url": args.basis_evidence, "quote": args.basis_quote,
            }  # fmt: skip
            reviewed = review(
                args.record_id,
                args.by,
                valid_from=args.valid_from,
                valid_to=args.valid_to,
                ongoing=args.ongoing,
                basis=basis,
                evidence=args.evidence,
                lifted_evidence=args.lifted_evidence,
                note=args.note,
            )
            print(
                f"Reviewed {reviewed.id}: {reviewed.valid_from} to {reviewed.valid_to or 'now'}. Commit "
                "schema/memberships.json, then publish."
            )
            return 0
        from api.store import engine_for

        report = publish(engine_for(), dry_run=args.dry_run)
    except Refused as refused:
        print(f"Refused: {refused}", file=sys.stderr)
        return 1
    print(json.dumps(report | {"dry_run": args.dry_run}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
