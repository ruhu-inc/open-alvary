"""Reviewed pages to canonical text: profile oa-text-1/workbench-units-1 (TXP-02).

The bytes a published text version holds, built from the units a reviewer approved: each unit with
words is one leaf (its printed number, a space, its words), margin notes are leaves just before the
unit they stand beside, a table is one block of TAB-separated cells, and leaves are separated by a
blank line. The document ends with one line feed. Formatting, alignment and indent levels are not
in the bytes. Nothing is reordered, repaired or joined unless the reviewer marked the join.

Offsets are UTF-8 bytes over the result (DAT-06). Which note is a section's heading, and which
leaves belong to which provision, is the structure's business (workbench.structure).
"""

import hashlib
import unicodedata
from dataclasses import dataclass, field

PROFILE_ID = "oa-text-1/workbench-units-1"
SEPARATOR = "\n\n"
_EDGE = " \t"  # outer horizontal whitespace is trimmed from every leaf; interior whitespace stays


@dataclass(frozen=True)
class Issue:
    """Something the reviewer must see before approving. Errors block approval; warnings do not."""

    level: str  # error | warning
    code: str
    page: int | None
    unit: int | None
    message: str


@dataclass
class Leaf:
    """One block of the canonical text and the reviewed unit it came from."""

    role: str  # text | note | caption | table
    page: int
    unit: int  # the unit's position on its page
    text: str
    note: int | None = None  # which of the unit's notes, for role "note"
    page_end: int | None = None  # the last page a joined paragraph reaches
    joined: list[tuple[int, int]] = field(default_factory=list)  # (page, unit) of continuations
    start: int = 0  # byte offsets of the leaf in the canonical text
    end: int = 0

    @property
    def last_page(self) -> int:
        return self.page_end or self.page


@dataclass
class Serialized:
    text: str
    leaves: list[Leaf]
    issues: list[Issue]
    profile_id: str = PROFILE_ID

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.level == "error"]


def clean(value: str) -> str:
    """NFC, line feeds only, outer spaces and tabs trimmed."""
    value = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    return value.strip(_EDGE)


def unit_words(unit: dict) -> str:
    """A unit's leaf: its printed number, one space and its words (or either alone)."""
    marker, text = clean(unit.get("marker") or ""), clean(unit.get("text") or "")
    return f"{marker} {text}" if marker and text else marker or text


def table_block(unit: dict, page: int, index: int, issues: list[Issue]) -> str:
    """Row-major cells, TAB between columns, LF between rows (OA-text-1). A merged cell's words are
    written once, at its top-left position; the positions it covers are empty fields."""
    rows = [[clean(cell) for cell in row] for row in unit.get("rows") or []]
    width = max((len(row) for row in rows), default=0)
    rows = [row + [""] * (width - len(row)) for row in rows]
    for r, c, height, span in unit.get("spans") or []:
        for dr in range(height):
            for dc in range(span):
                if (dr or dc) and r + dr < len(rows) and c + dc < width and rows[r + dr][c + dc]:
                    issues.append(
                        Issue(
                            "warning",
                            "covered_cell_text",
                            page,
                            index,
                            f"Words in a merged cell's covered position (row {r + dr + 1}, column "
                            f"{c + dc + 1}) are left out",
                        )
                    )
                    rows[r + dr][c + dc] = ""
    if not any(cell for row in rows for cell in row):
        issues.append(Issue("warning", "empty_table", page, index, "The table has no words"))
    return "\n".join("\t".join(row) for row in rows)


def serialize(pages: list[dict]) -> Serialized:
    """Canonical text of a document's final pages ({"page", "units"}, in page order)."""
    leaves: list[Leaf] = []
    issues: list[Issue] = []
    last_text: Leaf | None = None  # the paragraph a continuation on the next page joins
    for page in sorted(pages, key=lambda p: p["page"]):
        number = page["page"]
        for index, unit in enumerate(page["units"]):
            if unit.get("kind") == "table":
                caption = clean(unit.get("caption") or "")
                if caption:
                    leaves.append(Leaf("caption", number, index, caption))
                leaves.append(Leaf("table", number, index, table_block(unit, number, index, issues)))
                last_text = None
                continue
            words = unit_words(unit)
            notes = [clean(note) for note in unit.get("notes") or [] if clean(note)]
            if unit.get("continues_previous"):
                problem = _join_problem(unit, words, notes, last_text, number)
                if problem:
                    issues.append(Issue("error", "join", number, index, problem))
                else:
                    _join(last_text, words, bool(unit.get("dehyphenate")), number, index)
                    continue
            if not words:
                if notes:
                    issues.append(
                        Issue("error", "note_without_words", number, index, "A margin note beside no words")
                    )
                else:
                    issues.append(Issue("warning", "empty_unit", number, index, "An empty unit is left out"))
                continue
            for n, note in enumerate(notes):
                leaves.append(Leaf("note", number, index, note, note=n))
            last_text = Leaf("text", number, index, words)
            leaves.append(last_text)
    text, position = [], 0
    for i, leaf in enumerate(leaves):
        if i:
            position += len(SEPARATOR.encode())
        leaf.start = position
        position += len(leaf.text.encode())
        leaf.end = position
        text.append(leaf.text)
    body = SEPARATOR.join(text)
    return Serialized(body + "\n" if leaves else "", leaves, issues)


def _join_problem(unit: dict, words: str, notes: list[str], previous: Leaf | None, page: int) -> str:
    if previous is None or previous.last_page >= page:
        return "Marked as continuing the previous page, but the previous page does not end in a paragraph"
    if unit.get("marker"):
        return "A paragraph continuing from the previous page cannot have its own number"
    if notes:
        return "A paragraph continuing from the previous page cannot carry margin notes; move them"
    if not words:
        return "Marked as continuing the previous page, but it has no words"
    if unit.get("dehyphenate") and not previous.text.endswith("-"):
        return "Marked to remove a hyphen, but the previous page does not end in one"
    return ""


def _join(previous: Leaf, words: str, dehyphenate: bool, page: int, index: int) -> None:
    """Profile operations join_page_break (one space) and dehyphenate (the hyphen removed, no space)."""
    previous.text = previous.text[:-1] + words if dehyphenate else f"{previous.text} {words}"
    previous.page_end = page
    previous.joined.append((page, index))
