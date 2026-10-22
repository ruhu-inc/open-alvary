"""Operator commands for the public change log (changes-contracts.md C1).

sweep               compare every work with its last recorded projection (expiries); after each
                    seed and daily
new-epoch --reason  start a new feed epoch: required after any database restore or loss of a
                    cursor key (SYS-07); clients then reconcile (409 feed_reset)
purge               remove events past retention (400 days) and raise purged_through
status              epoch, baseline, events and the highest publication revision
withdraw-confidentially --work ID
                    take a public record out of public view for a reason that must not be told
                    (privacy, a legal or court order, or another confidential reason): its rights
                    record stops allowing its metadata, and its withdrawal names nothing, to anyone

Any other write that takes a record out of public view by changing its rights record is announced as a
withdrawal with reason_code rights_changed, which names the record by its IDs to keyed callers (owner
decision, 2026-10-22). Use withdraw-confidentially, not a catalogue edit, when the reason must not be
told: the withdrawal is recorded with the write that makes it, so it cannot be made confidential later.
"""

import argparse
import json

from sqlalchemy import select, text, update

from api.store import check_legacy_authority, engine_for, tables
from changes.record import current, namespace, publish, sweep


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sweep")
    epoch = commands.add_parser("new-epoch")
    epoch.add_argument("--reason", required=True)
    commands.add_parser("purge")
    commands.add_parser("status")
    confidential = commands.add_parser("withdraw-confidentially")
    confidential.add_argument("--work", required=True)
    args = parser.parse_args(argv)
    engine = engine_for()
    if engine.dialect.name != "postgresql":
        raise SystemExit("The change log lives in PostgreSQL")
    with engine.begin() as conn:
        ns = namespace(conn)
        state, events = (ns.qualified(conn, "changes", n) for n in ("feed_state", "change_event"))
        if args.command == "sweep":
            check_legacy_authority(conn, write=False)
            print(f"Recorded {sweep(conn)} changes")
        elif args.command == "withdraw-confidentially":
            check_legacy_authority(conn, write=True)
            if not current(conn, [args.work]):
                raise SystemExit("No public record has that ID")
            rights = tables["rights"]
            record = conn.scalar(select(rights.c.payload).where(rights.c.source_id == args.work))
            conn.execute(
                update(rights)
                .where(rights.c.source_id == args.work)
                .values(payload=record | {"metadata_public": False})
            )
            recorded = publish(conn, [args.work], confidential=[args.work])
            print(f"Withdrawn; recorded {recorded} changes, none naming the record")
        elif args.command == "new-epoch":
            conn.execute(
                text(
                    f"UPDATE {state} SET epoch = gen_random_uuid(), epoch_started_at = statement_timestamp(),"
                    " epoch_reason = :r"
                ),
                {"r": args.reason.strip()[:500]},
            )
            print("New feed epoch started; clients will be told to reconcile")
        elif args.command == "purge":
            conn.execute(text("SELECT set_config('open_alvary.retention_purge', 'on', true)"))
            through = conn.scalar(
                text(
                    f"SELECT max(publication_revision) FROM {events} WHERE publication_revision < ALL("
                    f"SELECT publication_revision FROM {events} WHERE committed_at >= statement_timestamp()"
                    " - interval '400 days')"
                )
            )
            if through:
                removed = conn.execute(
                    text(f"DELETE FROM {events} WHERE publication_revision <= :r"), {"r": through}
                ).rowcount
                conn.execute(
                    text(
                        f"UPDATE {state} SET purged_through_revision = greatest(purged_through_revision, :r)"
                    ),
                    {"r": through},
                )
                print(f"Purged {removed} events through revision {through}")
            else:
                print("Nothing past retention")
        else:
            row = conn.execute(text(f"SELECT * FROM {state}")).mappings().one()
            count, high = conn.execute(
                text(f"SELECT count(*), coalesce(max(publication_revision), 0) FROM {events}")
            ).one()
            print(json.dumps(dict(row) | {"events": count, "highest_revision": high}, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
