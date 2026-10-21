"""Court hierarchy registers (CPR-04): a reviewer checks an entry of a register against the sources it
cites, then records the review here, entry by entry. Until then the entry is served as unreviewed, and a
relation answers ``unknown``.

  python -m operations.courts show ingestion/courts/ke.json              each entry, its review and sources
  python -m operations.courts review ingestion/courts/ke.json ke-sc-on-courts --by "A. Advocate" \\
      --role "Kenyan advocate" --level doctrinal --qualification "Advocate of the High Court of Kenya"
  python -m operations.courts review-structural ingestion/courts/ke.json --by "A. Owner" --role owner
  python -m operations.courts withdraw ingestion/courts/ke.json [ke-sc-on-courts]     back to unreviewed

Each takes ``--on 2026-10-21`` for the day of the review (default today). Courts, matters, members, routes
from member states' courts and declarations are structural entries: which courts there are, from when,
and where an appeal or a reference lies; the owner may review them, and ``review-structural`` reviews
every one not yet reviewed. Relations and the precedent rule are doctrinal: how decisions bind, prevail
or persuade. A doctrinal review states the reviewer's legal qualification for the jurisdiction, whatever
their role, and is refused without one.

A review records who reviewed the entry, in what role and at what level, on which day, and the SHA-256 of
the entry as reviewed, with the sources it cites and the matter it is limited to (api.courts.digest). An
entry changed after its review is served as unreviewed, on its own, so nothing unreviewed is ever
answered as reviewed; the tests refuse a register that keeps such a review: review the entry again, or
withdraw its review. The file is rewritten with its keys in the same order.
"""

import argparse
import copy
import json
import sys
from datetime import date
from pathlib import Path

from api.courts import CourtRegister, Entry, digest


class Refused(Exception):
    pass


def located(data: dict, entry_id: str) -> dict:
    """The written entry ``entry_id``: the precedent rule, or an entry of one of the register's lists."""
    if entry_id == "precedent" and data.get("precedent"):
        return data["precedent"]
    for key in ("courts", "relations", "matters", "members", "member_routes", "declarations"):
        for item in data.get(key) or ():
            if item.get("id") == entry_id:
                return item
    raise Refused(f"{data.get('jurisdiction')}: the register has no entry {entry_id}")


def recorded(
    register: CourtRegister, entry: Entry, by: str, role: str, on: date, qualification: str | None
) -> dict:
    """The review of ``entry`` as written: who, in what role, on which day, and what they reviewed."""
    if not by.strip():
        raise Refused("name the reviewer")
    if not role.strip():
        raise Refused("name the reviewer's role, such as owner or Kenyan advocate")
    if entry.LEVEL == "doctrinal" and not (qualification or "").strip():
        raise Refused(
            f"{entry.id} is doctrinal: state the reviewer's legal qualification for {register.jurisdiction} "
            "(--qualification)"
        )
    review = {"review_level": entry.LEVEL, "reviewed_by": by.strip(), "reviewer_role": role.strip()}
    if (qualification or "").strip():
        review["reviewer_qualification"] = qualification.strip()
    return review | {"reviewed_on": on.isoformat(), "reviewed_sha256": digest(register, entry)}


def review(
    data: dict, entry_id: str, by: str, role: str, level: str, on: date, qualification: str | None = None
) -> dict:
    """The register's data with ``entry_id`` reviewed by ``by``, as it is now, at ``level``: the entry's own
    level, which the reviewer names so that a doctrinal entry is never reviewed by mistake."""
    register = CourtRegister.model_validate(data)  # every entry cites its sources
    entry = next((e for e in register.entries() if e.id == entry_id), None)
    if entry is None:
        raise Refused(f"{register.jurisdiction}: the register has no entry {entry_id}")
    if level != entry.LEVEL:
        raise Refused(f"{entry_id} is a {entry.LEVEL} entry: review it at that level")
    written = copy.deepcopy(data)
    located(written, entry_id)["review"] = recorded(register, entry, by, role, on, qualification)
    return written


def review_structural(data: dict, by: str, role: str, on: date, qualification: str | None = None) -> dict:
    """The register's data with every structural entry not yet reviewed as it is now reviewed by ``by``."""
    register = CourtRegister.model_validate(data)
    due = [e for e in register.entries() if e.LEVEL == "structural" and not register.reviewed(e)]
    if not due:
        raise Refused(f"{register.jurisdiction}: no structural entry awaits review")
    written = copy.deepcopy(data)
    for entry in due:
        located(written, entry.id)["review"] = recorded(register, entry, by, role, on, qualification)
    return written


def withdrawn(data: dict, entry_id: str | None = None) -> dict:
    """The register's data with the review of ``entry_id``, or of every entry, taken off."""
    written = copy.deepcopy(data)
    ids = [e.id for e in CourtRegister.model_validate(data).entries()]
    for found in ids if entry_id is None else [entry_id]:
        located(written, found).pop("review", None)  # an entry the register lacks is refused
    return written


def sheet(register: CourtRegister) -> list[str]:
    """Each entry with its review and the sources it cites, for the reviewer to check one by one."""
    sources = {s.id: s for s in register.sources}

    def cited(ids) -> list[str]:
        return [f"      {sources[i].title}{', ' + sources[i].pinpoint if sources[i].pinpoint else ''}: "
                f"{sources[i].url}" for i in ids]  # fmt: skip

    def dates(entry) -> str:
        if not (entry.valid_from or entry.valid_until):
            return ""
        return f" [{entry.valid_from or '…'} to {entry.valid_until or 'now'}]"

    def state(entry) -> str:
        if register.reviewed(entry):
            r = entry.review
            qualified = f"; {r.reviewer_qualification}" if r.reviewer_qualification else ""
            return f"{entry.LEVEL}, reviewed by {r.reviewed_by} ({r.reviewer_role}{qualified}) on {r.reviewed_on}"
        if entry.review is not None:
            return f"{entry.LEVEL}, reviewed, then changed: review it again"
        return f"{entry.LEVEL}, unreviewed"

    reviewed = sum(register.reviewed(e) for e in register.entries())
    lines = [f"{register.jurisdiction}: {reviewed} of {len(register.entries())} entries reviewed"]
    if register.precedent:
        rule = register.precedent
        lines += [f"  precedent [{state(rule)}] binds: {rule.binds}. {rule.note}"] + cited(rule.sources)
    else:
        lines += ["  precedent: not recorded (binding and persuasive answer unknown)"]
    for court in register.courts:
        appeals = f" → {', '.join(court.appeals_to)}" if court.appeals_to else ""
        lines += [f"  court {court.id} [{state(court)}]: {court.name} ({court.kind}, {court.level}){appeals}"
                  f"{dates(court)}"]  # fmt: skip
        lines += cited(court.sources)
    for r in register.relations:
        bound = r.bound or ("member states' courts" if r.members else "its courts generally")
        matter = f" on {r.matter}" if r.matter else ""
        lines += [f"  relation {r.id} [{state(r)}]: {r.court} → {bound}: {r.relation}{matter}{dates(r)}"]
        lines += [f"      “{q.quote}” ({sources[q.source].title}, {q.provision})" for q in r.rests_on]
        lines += cited(r.sources)
    for m in register.matters:
        lines += [f"  matter {m.id} [{state(m)}]: {m.name}"] + cited(m.sources)
    for m in register.members:
        to = f" ({m.court})" if m.court else ""
        lines += [f"  member {m.id} [{state(m)}]: {m.state}{to}{dates(m)}"] + cited(m.sources)
    for r in register.member_routes:
        matter = f" on {r.matter}" if r.matter else ""
        lines += [f"  route {r.id} [{state(r)}]: {r.kind} to {r.court}{matter}"] + cited(r.sources)
    for d in register.declarations:
        lines += [f"  declaration {d.id} [{state(d)}]: {d.state} → {d.court}{dates(d)}"] + cited(d.sources)
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("show", "review", "review-structural", "withdraw"):
        command = commands.add_parser(name)
        command.add_argument("file", type=Path)
        if name in ("review", "withdraw"):
            command.add_argument("entry", nargs=None if name == "review" else "?")
        if name in ("review", "review-structural"):
            command.add_argument("--by", required=True)
            command.add_argument("--role", required=True)
            command.add_argument("--qualification")
            command.add_argument("--on", type=date.fromisoformat, default=date.today())
        if name == "review":
            command.add_argument("--level", required=True, choices=("structural", "doctrinal"))
    args = parser.parse_args(argv)
    data = json.loads(args.file.read_text())
    try:
        if args.command == "show":
            print("\n".join(sheet(CourtRegister.model_validate(data))))
            return 0
        if args.command == "review":
            written = review(data, args.entry, args.by, args.role, args.level, args.on, args.qualification)
        elif args.command == "review-structural":
            written = review_structural(data, args.by, args.role, args.on, args.qualification)
        else:
            written = withdrawn(data, args.entry)
    except Refused as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1
    args.file.write_text(json.dumps(written, indent=2, ensure_ascii=False) + "\n")
    register = CourtRegister.model_validate(written)
    done = sum(register.reviewed(e) for e in register.entries())
    print(f"{register.jurisdiction}: {done} of {len(register.entries())} entries reviewed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
