"""Restore the printed legal structure of a page reading without changing any word.

Two things the primary reading flattens are put back:

- Margin notes. Gazettes print a short note beside each section ("Promotion of generation from
  renewable energy"); the primary often glues it into the body, sometimes into the following
  section, or emits it as a heading. A check reader that reports positions identifies text set
  in the outer margin and the body paragraph printed beside it. The note's words are moved out
  of the body, exactly as the primary read them, and attached to the unit that opens with that
  paragraph's words; where no unit does, to the unit they were cut from.
- Units run together. Subsections "(2)", paragraphs "(a)" and new sections "81." that the
  primary merged into one block are split onto their own nodes at those boundaries.

depth() gives each unit's nesting (0 section, 1 subsection, 2 paragraph, 3 sub-paragraph).
"""

import re
from dataclasses import dataclass

from ingestion.compare import TOKEN, key

MARGIN = 0.70  # a block starting beyond 70% of the page width sits in the outer margin
NOTE_WORDS = 20
ALIGN = 0.012  # a note's top sits within about a line of its paragraph's first line
ANCHOR_WORDS = 6

# Boundaries inside a block, each after closing punctuation (or a closing quotation mark):
# "… Minister. (3) The office …", "… means — (a) any …", "… two years." 10. Section 41 …".
SPLIT = re.compile(
    # never between a section number and its first subsection ("80. (1)"); a year ("2015.") may end
    r"(?<!\b\d\.)(?<!\b\d\d\.)(?<!\b\d\d\d\.)(?<!\b\d[A-Z]\.)(?<!\b\d\d[A-Z]\.)"
    r"(?<=[.;:—\"”’])\s+(?="
    r"\(\d{1,3}[A-Z]?\)\s+[A-Z\"“]"  # subsection
    r"|\([a-z]{1,2}\)\s+\S"  # paragraph
    r"|\([ivxl]{1,6}\)\s+\S"  # sub-paragraph
    r"|\d{1,3}[A-Z]?\.\s*(?:[—–-]\s*)?\(\d{1,3}[A-Z]?\)\s+[A-Z]"  # section with first subsection
    r"|\d{1,3}[A-Z]?\.\s+[A-Z]"  # section without subsections
    r")"
)
LEVEL = [
    (re.compile(r"^\d{1,3}[A-Z]?\."), 0),
    (re.compile(r"^\(\d{1,3}[A-Z]?\)"), 1),
    (re.compile(r"^\([ivxl]{2,6}\)"), 3),
    (re.compile(r"^\([a-z]{1,2}\)"), 2),
]
MARKERISH = re.compile(r"^[\"“]?(?:\d{1,3}[A-Z]?\.|\(\w{1,6}\))[\"”]?$")


@dataclass
class Note:
    text: str
    anchor: tuple[str, ...] | None  # opening words of the body paragraph printed beside it


def opening(text: str) -> tuple[str, ...]:
    """A unit's first words for matching, ignoring leading numbers and markers."""
    words = [key(w).lower() for w in TOKEN.findall(text)]
    while words and MARKERISH.match(words[0]):
        words.pop(0)
    return tuple(words[:ANCHOR_WORDS])


def margin_notes(check_pages: dict[str, dict]) -> list[Note]:
    """Short blocks a positional reader saw in the outer margin, with the paragraph beside each."""
    notes = []
    for page in check_pages.values():
        blocks = [b for b in page.get("blocks") or [] if b.get("box") and not b.get("role")]
        body = [b for b in blocks if b["box"]["x0"] < MARGIN]
        for block in blocks:
            words = block.get("text", "").split()
            if block["box"]["x0"] < MARGIN or not 0 < len(words) <= NOTE_WORDS:
                continue
            beside = [b for b in body if b["box"]["y0"] <= block["box"]["y0"] + ALIGN]
            anchor = opening(max(beside, key=lambda b: b["box"]["y0"])["text"]) if beside else ()
            notes.append(Note(block["text"], anchor if len(anchor) >= 3 else None))
    return notes


def _remove(text: str, note: str) -> tuple[str, str, int] | None:
    """Cut the note's words out, matching tolerantly; return (rest, words as read, cut offset)."""
    tokens = list(TOKEN.finditer(text))
    keys = [key(t.group()).lower() for t in tokens]
    want = [key(w).lower() for w in note.split()]
    size = len(want)
    for start in range(len(keys) - size + 1):
        if keys[start : start + size] == want:
            first, last = tokens[start].start(), tokens[start + size - 1].end()
            head = text[:first].rstrip()
            rest = (head + " " + text[last:].lstrip()).strip()
            return rest, text[first:last], len(head)
    return None


def place_notes(nodes: list[dict], notes: list[Note]) -> list[dict]:
    """Cut margin-note words out of body text; they are attached after splitting."""
    for note in sorted(notes, key=lambda n: len(n.text), reverse=True):
        for index, node in enumerate(nodes):
            if node["kind"] not in {"heading", "paragraph", "list_item"} or not node.get("text"):
                continue
            found = _remove(node["text"], note.text)
            if not found:
                continue
            rest, as_read, cut = found
            if rest:
                shrink = len(node["text"]) - len(rest)
                cuts = [(at - shrink if at > cut else at, n) for at, n in node.get("_cuts", [])]
                node["text"], node["_cuts"] = rest, [*cuts, (cut, Note(as_read, note.anchor))]
            else:
                # The whole block is the note (emitted as a heading): it belongs to a following unit.
                target = next(
                    (n for n in nodes[index + 1 :] if n["kind"] in {"paragraph", "list_item"}), None
                )
                if target is None:
                    continue
                node["kind"], node["text"] = "spent", ""
                target.setdefault("_pending", []).append(Note(as_read, note.anchor))
            break
    return [node for node in nodes if node["kind"] != "spent"]


def split_units(nodes: list[dict]) -> list[dict]:
    """Split merged units; a note cut from a merged block falls to the unit it was cut from."""
    out = []
    for node in nodes:
        cuts = node.pop("_cuts", [])
        if node["kind"] not in {"paragraph", "list_item"} or not node.get("text"):
            node.setdefault("_pending", []).extend(note for _, note in cuts)
            out.append(node)
            continue
        text, spans, position = node["text"], [], 0
        for part in SPLIT.split(text):
            if part.strip():
                start = text.index(part, position)
                spans.append((start, part))
                position = start + len(part)
        units = []
        for index, (_, part) in enumerate(spans):
            unit = (
                dict(node)
                if index == 0
                else {
                    **{
                        k: v
                        for k, v in node.items()
                        if k not in {"marker", "marker_source", "notes", "_pending"}
                    },
                    "split": True,
                }
            )
            unit["text"] = part
            units.append(unit)
        for at, note in cuts:
            # Cut inside a unit: that unit. Cut on the space between two units: the one after it.
            owner = next(
                (i for i, (start, part) in enumerate(spans) if at < start + len(part)), len(units) - 1
            )
            units[owner].setdefault("_pending", []).append(note)
        out.extend(units)
    return out


def attach_notes(nodes: list[dict]) -> list[dict]:
    """Give each note to the unit opening with the paragraph printed beside it, if one does."""
    openings = [opening(n.get("text", "")) if n["kind"] in {"paragraph", "list_item"} else () for n in nodes]
    for node in nodes:
        for note in node.pop("_pending", []):
            owner = node
            if note.anchor:
                width = len(note.anchor)
                match = next(
                    (i for i, o in enumerate(openings) if len(o) >= width and o[:width] == note.anchor), None
                )
                if match is not None:
                    owner = nodes[match]
            owner.setdefault("notes", []).append(note.text)
    return nodes


def depth(node: dict) -> int | None:
    lead = (node.get("marker") or node.get("text") or "").lstrip()
    for pattern, level in LEVEL:
        if pattern.match(lead):
            return level
    return None


def restructure(nodes: list[dict], check_pages: dict[str, dict]) -> list[dict]:
    return attach_notes(split_units(place_notes(nodes, margin_notes(check_pages))))
