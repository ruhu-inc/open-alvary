"""Membership of the African Union and the regional bodies (JUR-01, JUR-04): researched as proposals in
the membership register (schema/memberships.json), approved by the reviewer, published by the operator.

  python -m operations.memberships review ml:aa-ecowas:1 --by "Full Name" [--valid-from YYYY-MM-DD]
      [--valid-to YYYY-MM-DD | --ongoing] [--evidence URL] [--note TEXT]
  python -m operations.memberships publish [--dry-run]

``review`` approves one row of the register: it records who reviewed it and when, with the start date
(a reviewed membership is dated) and any end, evidence or note the reviewer sets. Run it again on a
reviewed row to record its end or a correction; it is then reviewed again. One person may review
(single-reviewer rule). Commit the register: it is the public record of what was reviewed, by whom.

``publish`` writes the reviewed memberships to the catalogue (corpus.membership, PostgreSQL), adds the
catalogue rows of the members and bodies it lacks, removes memberships no longer reviewed and announces
each change in the change log (membership_recorded), in one transaction. Proposals are never published.
"""

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select, text

from operations.publish import Refused, decider, evidence_links
from schema.jurisdictions import catalogue_entry, registry
from schema.memberships import Membership, register, write

COLUMNS = (
    "member_id",
    "body_id",
    "valid_from",
    "valid_to",
    "evidence_url",
    "note",
    "reviewed_by",
    "reviewed_at",
)


def review(
    membership_id: str,
    by: str,
    *,
    valid_from: date | None = None,
    valid_to: date | None = None,
    ongoing: bool = False,
    evidence: str | None = None,
    note: str | None = None,
    source: Path | None = None,
    now: datetime | None = None,
) -> Membership:
    """Approve one membership of the register into a reviewed fact, and write the register."""
    by = decider(by)
    held = register(source)
    rows = list(held.memberships)
    index = next((i for i, row in enumerate(rows) if row.id == membership_id), None)
    if index is None:
        raise Refused(f"There is no membership {membership_id} in the register.")
    if ongoing and valid_to is not None:
        raise Refused("Give an end date or --ongoing, not both.")
    update = {
        "status": "reviewed",
        "reviewed_by": by,
        "reviewed_at": now or datetime.now(UTC).replace(microsecond=0),
        **{key: value for key, value in (("valid_from", valid_from), ("valid_to", valid_to),
                                          ("evidence_url", evidence), ("note", note)) if value is not None},
        **({"valid_to": None} if ongoing else {}),
    }  # fmt: skip
    try:
        reviewed = Membership.model_validate(rows[index].model_dump() | update)
        evidence_links([reviewed.evidence_url])
        rows[index] = reviewed
        write(held.model_copy(update={"memberships": tuple(rows)}), source)
    except ValueError as error:
        raise Refused(str(error)) from None
    return reviewed


def publish(engine, source: Path | None = None, *, dry_run: bool = False) -> dict:
    """Make the catalogue's memberships the register's reviewed ones and announce what changed, in one
    transaction. Returns what was (or, in a dry run, would be) done."""
    from api.store import check_legacy_authority, tables
    from changes.record import namespace
    from changes.record import publish as record

    held_register = register(source)
    reviewed = {m.id: m for m in held_register.reviewed()}
    report = {"published": 0, "updated": 0, "removed": 0, "unchanged": 0, "jurisdictions_added": []}
    report["proposals_not_published"] = len(held_register.memberships) - len(reviewed)
    with engine.connect() as conn, conn.begin() as transaction:
        if conn.dialect.name != "postgresql":
            raise Refused("Memberships are published to the PostgreSQL catalogue.")
        check_legacy_authority(conn, write=True)
        table = namespace(conn).qualified(conn, "corpus", "membership")
        if conn.scalar(text("SELECT to_regclass(:t)"), {"t": table}) is None:
            raise Refused("The catalogue has no membership table yet: migrate it first.")
        places = tables["jurisdictions"]
        catalogued = set(conn.scalars(select(places.c.id)))
        profiles = registry()
        for code in sorted({c for m in reviewed.values() for c in (m.subject_id, m.target_id)} - catalogued):
            entry = catalogue_entry(code, profiles=profiles)
            conn.execute(places.insert().values(id=code, payload=entry.model_dump(mode="json")))
            report["jurisdictions_added"].append(code)
        held = {
            row["membership_id"]: row
            for row in conn.execute(
                text(f"SELECT membership_id, {', '.join(COLUMNS)} FROM {table}")
            ).mappings()
        }
        for membership_id in sorted(held.keys() - reviewed.keys()):
            conn.execute(text(f"DELETE FROM {table} WHERE membership_id = :id"), {"id": membership_id})
            report["removed"] += 1
        for membership_id, m in sorted(reviewed.items()):
            values = {
                "id": membership_id, "member_id": m.subject_id, "body_id": m.target_id,
                "valid_from": m.valid_from, "valid_to": m.valid_to, "evidence_url": m.evidence_url,
                "note": m.note, "reviewed_by": m.reviewed_by, "reviewed_at": m.reviewed_at,
            }  # fmt: skip
            was = held.get(membership_id)
            if was is not None and all(was[k] == values[k] for k in COLUMNS):
                report["unchanged"] += 1
                continue
            if was is None:
                conn.execute(
                    text(
                        f"INSERT INTO {table} (membership_id, {', '.join(COLUMNS)}) VALUES (:id, :member_id,"
                        " :body_id, :valid_from, :valid_to, :evidence_url, :note, :reviewed_by, :reviewed_at)"
                    ),
                    values,
                )
                report["published"] += 1
                continue
            conn.execute(
                text(
                    f"UPDATE {table} SET member_id = :member_id, body_id = :body_id, valid_from = :valid_from,"
                    " valid_to = :valid_to, evidence_url = :evidence_url, note = :note,"
                    " reviewed_by = :reviewed_by, reviewed_at = :reviewed_at,"
                    " published_at = statement_timestamp() WHERE membership_id = :id"
                ),
                values,
            )
            report["updated"] += 1
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
    reviewing.add_argument("membership_id")
    reviewing.add_argument("--by", required=True, help="Who reviews: a full name")
    reviewing.add_argument("--valid-from", type=date.fromisoformat)
    reviewing.add_argument("--valid-to", type=date.fromisoformat, help="The membership's last day")
    reviewing.add_argument("--ongoing", action="store_true", help="The membership has not ended")
    reviewing.add_argument("--evidence")
    reviewing.add_argument("--note")
    publishing = commands.add_parser("publish")
    publishing.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "review":
            reviewed = review(
                args.membership_id,
                args.by,
                valid_from=args.valid_from,
                valid_to=args.valid_to,
                ongoing=args.ongoing,
                evidence=args.evidence,
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
