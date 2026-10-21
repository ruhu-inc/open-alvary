"""Unofficial consolidations (CPR-03): a law's reviewed units as amended at a date, by a deterministic
applier of reviewed amendment instructions.

An instruction comes from an amending provision and names its target by locator (TXP-03):
  substitute   replace the target's units with new units
  insert       put new units before or after the target
  repeal       replace the target with its number and a note of the repeal
  renumber     change the target's printed number
  amend_words  delete words in the target (exactly as many times as stated) and insert others

Instructions effective on or before the date apply in effective-date order. After each one the
structure is derived again, so locators are always those of the TXP-03 rules. The applier refuses,
and the consolidation stops until an editor resolves it, when:
- a target does not resolve;
- the words to delete do not occur exactly as many times as stated;
- a target shares a unit with words outside it ("5. (1)" opening a section and its subsection: edit the
  units apart first).

Every resulting unit records where it came from: the original, or which instruction of which amending
law made or changed it. The result is a candidate for the ordinary pipeline (TXP-01): serialized,
derived, reviewed by an editor and a second editor (EDT-04), and published only as an unofficial
consolidation expression (D32). It is never the official text.
"""

import json
import re
import sys
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from workbench import content, publication

APPLIER = "oa-consolidate-1"
LEADING_NUMBER = re.compile(r"^\s*((?:\d{1,3}[A-Z]{0,2}\.)?(?:\s*\(\w{1,5}\))*)\s*")


class Amending(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    work: str
    locator: str
    citation: str  # how the amending provision is cited, for the repeal note and provenance


class Instruction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1, max_length=80)
    operation: Literal["substitute", "insert", "repeal", "renumber", "amend_words"]
    target: str = Field(min_length=1, max_length=1000)
    effective_date: date
    amending: Amending
    position: Literal["before", "after"] | None = None
    units: tuple[dict, ...] = ()
    words_delete: str | None = None
    words_insert: str = ""
    occurrences: int = Field(1, ge=1, le=100)
    label_from: str | None = None
    label_to: str | None = None

    @model_validator(mode="after")
    def complete(self):
        needs = {
            "substitute": bool(self.units),
            "insert": bool(self.units) and self.position is not None,
            "repeal": True,
            "renumber": bool(self.label_from and self.label_to),
            "amend_words": bool(self.words_delete),
        }
        if not needs[self.operation]:
            raise ValueError(f"{self.operation} is missing what it needs")
        if self.units:
            content.validate_units(list(self.units))
        return self


class Refused(ValueError):
    def __init__(self, instruction: str, reason: str):
        super().__init__(f"{instruction}: {reason}")
        self.instruction, self.reason = instruction, reason


@dataclass
class Entry:
    page: int
    unit: dict
    origin: dict = field(default_factory=lambda: {"origin": "original"})


@dataclass
class Result:
    pages: list[dict]
    provenance: list[dict]
    applied: list[str]
    pending: list[str]  # instructions effective after the date


def _pages(entries: list[Entry]) -> list[dict]:
    pages: dict[int, list[dict]] = {}
    for entry in entries:
        pages.setdefault(entry.page, []).append(entry.unit)
    return [{"page": number, "units": units} for number, units in sorted(pages.items())]


def _positions(entries: list[Entry]) -> dict[tuple[int, int], int]:
    """(page, unit index on the page) → index in the flat list."""
    counters: dict[int, int] = {}
    found = {}
    for n, entry in enumerate(entries):
        index = counters.get(entry.page, 0)
        found[(entry.page, index)] = n
        counters[entry.page] = index + 1
    return found


def _target(document: dict, entries: list[Entry], instruction: Instruction) -> list[int]:
    """The flat indices of the units the target holds, refused unless it holds them whole."""
    text = publication.build(document, _pages(entries))
    nodes = {node.locator: node for node in text.structure.nodes}
    node = nodes.get(instruction.target)
    if node is None:
        raise Refused(instruction.id, f"no provision {instruction.target} in the text as it stands")
    inside = {id(n) for n in node.walk()}
    leaves = sorted({i for n in node.walk() for i in n.leaves})
    positions = _positions(entries)
    held = set()
    for index in leaves:
        leaf = text.serialized.leaves[index]
        for page, unit in [(leaf.page, leaf.unit), *leaf.joined]:
            held.add(positions[(page, unit)])
    if node.trim or node.opens_at is not None:
        raise Refused(
            instruction.id, f"{instruction.target} starts inside a unit it shares: edit the units apart first"
        )
    for other in text.structure.nodes:
        if id(other) in inside:
            continue
        for index in other.leaves:
            leaf = text.serialized.leaves[index]
            if positions[(leaf.page, leaf.unit)] in held:
                raise Refused(
                    instruction.id,
                    f"{instruction.target} shares a unit with {other.locator}: edit the units apart first",
                )
    if not held:
        raise Refused(instruction.id, f"{instruction.target} holds no units")
    return sorted(held)


def _origin(instruction: Instruction, kind: str) -> dict:
    return {"origin": kind, "instruction": instruction.id, "by": instruction.amending.model_dump()}


def apply(document: dict, final: list[dict], instructions: list[Instruction], as_at: date) -> Result:
    """The consolidation of ``final`` (a document's reviewed pages) at ``as_at``."""
    entries = [Entry(page["page"], deepcopy(unit)) for page in final for unit in page["units"]]
    ordered = sorted(instructions, key=lambda i: (i.effective_date, i.id))
    applied, pending = [], [i.id for i in ordered if i.effective_date > as_at]
    for instruction in (i for i in ordered if i.effective_date <= as_at):
        held = _target(document, entries, instruction)
        first, last = held[0], held[-1]
        if held != list(range(first, last + 1)):
            raise Refused(instruction.id, f"{instruction.target} is not one run of units")
        page = entries[first].page
        if instruction.operation == "substitute":
            new = [Entry(page, deepcopy(u), _origin(instruction, "substituted")) for u in instruction.units]
            entries[first : last + 1] = new
        elif instruction.operation == "insert":
            new = [Entry(page if instruction.position == "before" else entries[last].page, deepcopy(u),
                         _origin(instruction, "inserted")) for u in instruction.units]  # fmt: skip
            at = first if instruction.position == "before" else last + 1
            entries[at:at] = new
        elif instruction.operation == "repeal":
            head = entries[first].unit
            marker = head.get("marker") or LEADING_NUMBER.match(head.get("text", "")).group(1).strip()
            note = {
                "kind": head.get("kind", "paragraph"),
                "text": f"[Repealed by {instruction.amending.citation}.]",
            }
            if marker:
                note["marker"] = marker
            entries[first : last + 1] = [Entry(page, note, _origin(instruction, "repealed"))]
        elif instruction.operation == "renumber":
            head = entries[first]
            unit = dict(head.unit)
            if unit.get("marker", "").startswith(instruction.label_from):
                unit["marker"] = instruction.label_to + unit["marker"][len(instruction.label_from) :]
            elif unit.get("text", "").lstrip().startswith(instruction.label_from):
                words = unit["text"].lstrip()
                unit["text"] = instruction.label_to + words[len(instruction.label_from) :]
            else:
                raise Refused(
                    instruction.id, f"{instruction.target} is not numbered {instruction.label_from}"
                )
            entries[first] = Entry(head.page, unit, _origin(instruction, "renumbered"))
        else:  # amend_words
            pattern = re.compile(rf"(?<!\w){re.escape(instruction.words_delete)}(?!\w)")
            count = sum(len(pattern.findall(entries[n].unit.get("text", ""))) for n in held)
            if count != instruction.occurrences:
                raise Refused(
                    instruction.id,
                    f"“{instruction.words_delete}” occurs {count} times in {instruction.target}, "
                    f"not {instruction.occurrences}",
                )
            for n in held:
                unit = dict(entries[n].unit)
                changed = pattern.sub(instruction.words_insert, unit.get("text", ""))
                if changed != unit.get("text", ""):
                    unit["text"] = " ".join(changed.split()) if not instruction.words_insert else changed
                    entries[n] = Entry(entries[n].page, unit, _origin(instruction, "amended"))
        applied.append(instruction.id)
    pages = _pages(entries)
    positions = _positions(entries)
    provenance = [
        {"page": key[0], "unit": key[1], **entries[n].origin}
        for key, n in sorted(positions.items(), key=lambda x: x[1])
    ]
    return Result(pages, provenance, applied, pending)


def main(argv=None) -> int:
    """python -m workbench.consolidation build --database URL --document ID --instructions FILE --as-at DATE"""
    import argparse

    from workbench.store import Store, connect

    parser = argparse.ArgumentParser(description=main.__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("build")
    run.add_argument("--database", required=True)
    run.add_argument("--document", required=True)
    run.add_argument("--instructions", required=True)
    run.add_argument("--as-at", required=True, type=date.fromisoformat)
    args = parser.parse_args(argv)
    library, store = content.Library(), Store(connect(args.database))
    reading, latest = library.reading(args.document), store.latest_pages(args.document)
    final = content.final_pages(reading, latest)
    with open(args.instructions) as handle:
        instructions = [Instruction.model_validate(i) for i in json.load(handle)["instructions"]]
    document = library.act(args.document)  # its type decides the structure rules, as in the workbench
    try:
        result = apply(document, final, instructions, args.as_at)
    except Refused as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1
    built = publication.build(document, result.pages)
    json.dump(
        {
            "applier": APPLIER, "base": args.document, "as_at": args.as_at.isoformat(), "label": "unofficial consolidation",
            "applied": result.applied, "pending": result.pending, "pages": result.pages, "provenance": result.provenance,
            "text_sha256": built.text_sha256, "structure_sha256": built.structure_sha256,
            "issues": [{"level": i.level, "code": i.code, "page": i.page, "message": i.message} for i in built.issues],
        },
        sys.stdout,
        indent=2,
        ensure_ascii=False,
    )  # fmt: skip
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
