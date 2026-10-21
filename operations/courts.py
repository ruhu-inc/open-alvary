"""Court hierarchy registers (CPR-04): a reviewer checks every entry of a register against the sources it
cites, then records the review here. Until then the register answers ``unknown`` everywhere.

  python -m operations.courts show ingestion/courts/ke.json                    each entry and its sources
  python -m operations.courts review ingestion/courts/ke.json --by "A. Reviewer" [--on 2026-10-21]
  python -m operations.courts withdraw ingestion/courts/ke.json                 back to unreviewed

A review records who reviewed the register, on which day, and the SHA-256 of what they reviewed (every
entry and source; api.courts.digest). It covers the register as a whole: one person may record it. A
register changed after its review no longer loads, so nothing unreviewed is ever answered as reviewed:
review it again, or withdraw the review first. The file is rewritten with its keys in the same order.
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from api.courts import CourtRegister, digest


class Refused(Exception):
    pass


def unreviewed(data: dict) -> dict:
    """The register's data with any review taken off."""
    return data | {
        "review_state": "unreviewed",
        "reviewed_by": None,
        "reviewed_on": None,
        "reviewed_sha256": None,
    }


def review(data: dict, by: str, on: date) -> dict:
    """The register's data, reviewed by ``by`` on ``on``: its entries as they are now, checked first."""
    if not by.strip():
        raise Refused("name the reviewer")
    register = CourtRegister.model_validate(unreviewed(data))  # every entry cites its sources
    if not (register.courts or register.relations or register.precedent):
        raise Refused(f"{register.jurisdiction}: the register records nothing to review")
    return data | {
        "review_state": "reviewed",
        "reviewed_by": by.strip(),
        "reviewed_on": on.isoformat(),
        "reviewed_sha256": digest(register),
    }


def sheet(register: CourtRegister, state: str) -> list[str]:
    """Each entry with the sources it cites, for the reviewer to check one by one."""
    sources = {s.id: s for s in register.sources}

    def cited(ids) -> list[str]:
        return [f"      {sources[i].title}{', ' + sources[i].pinpoint if sources[i].pinpoint else ''}: "
                f"{sources[i].url}" for i in ids]  # fmt: skip

    def dates(entry) -> str:
        if not (entry.valid_from or entry.valid_until):
            return ""
        return f" [{entry.valid_from or '…'} to {entry.valid_until or 'now'}]"

    lines = [f"{register.jurisdiction}: {state}"]
    if register.precedent:
        lines += [f"  precedent binds: {register.precedent.binds}. {register.precedent.note}"]
        lines += cited(register.precedent.sources)
    else:
        lines += ["  precedent: not recorded (every relation answers unknown)"]
    for court in register.courts:
        appeals = f" → {', '.join(court.appeals_to)}" if court.appeals_to else ""
        lines += [f"  court {court.id}: {court.name} ({court.kind}, {court.level}){appeals}{dates(court)}"]
        lines += cited(court.sources)
    for r in register.relations:
        bound = r.bound or ("member states' courts" if r.members else "its courts generally")
        matter = f" on {r.matter}" if r.matter else ""
        lines += [f"  relation {r.court} → {bound}: {r.relation}{matter}{dates(r)}"]
        lines += cited(r.sources)
    for m in register.matters:
        lines += [f"  matter {m.id}: {m.name}"] + cited(m.sources)
    for m in register.members:
        to = f" ({m.court})" if m.court else ""
        lines += [f"  member {m.state}{to}{dates(m)}"] + cited(m.sources)
    for r in register.member_routes:
        lines += [f"  route {r.kind} to {r.court}{' on ' + r.matter if r.matter else ''}"] + cited(r.sources)
    for d in register.declarations:
        lines += [f"  declaration {d.state} → {d.court}{dates(d)}"] + cited(d.sources)
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("show", "review", "withdraw"):
        command = commands.add_parser(name)
        command.add_argument("file", type=Path)
        if name == "review":
            command.add_argument("--by", required=True)
            command.add_argument("--on", type=date.fromisoformat, default=date.today())
    args = parser.parse_args(argv)
    data = json.loads(args.file.read_text())
    try:
        if args.command == "show":
            register = CourtRegister.model_validate(unreviewed(data))
            state = data.get("review_state", "unreviewed")
            if state == "reviewed" and data.get("reviewed_sha256") != digest(register):
                state = "reviewed, then changed: review it again"
            print("\n".join(sheet(register, state)))
            return 0
        written = review(data, args.by, args.on) if args.command == "review" else unreviewed(data)
    except Refused as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1
    args.file.write_text(json.dumps(written, indent=2, ensure_ascii=False) + "\n")
    register = CourtRegister.model_validate(written)
    print(f"{register.jurisdiction}: {register.review_state}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
