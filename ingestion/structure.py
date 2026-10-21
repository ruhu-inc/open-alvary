"""Restore the printed legal structure of a page reading without changing any word.

Two things the primary reading flattens are put back:

- Margin notes. Gazettes print a short note beside each section ("Promotion of generation from
  renewable energy"); the primary often glues it into the body, sometimes into the following
  section, or emits it as a heading. A check reader that reports positions identifies text set
  in the outer margin and the body paragraph printed beside it. The note's words are moved out
  of the body, exactly as the primary read them, and attached to the unit that opens with that
  paragraph's words; where no unit does, to the unit they were cut from.
- Units run together. Subsections "(2)", paragraphs "(a)" and new sections "81." that the
  primary merged into one block are split onto their own nodes at those boundaries. Where the
  primary flattened a list with no punctuation to split on (an arrangement of sections, a run
  of definitions), the block is split wherever the positional check reader starts a paragraph
  on the same words.

depth() gives each unit's nesting (0 section, 1 subsection, 2 paragraph, 3 sub-paragraph).
"""

import difflib
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
    # An article of civil-law and treaty drafting, by the separator printed after its number, so a
    # sentence citing one ("Article 5 of the Charter") is never cut: "SECTION 7: Whoever …",
    # "ARTICLE 4.- Le …", "Article 5 : …", "Article premier. — …", "Art. 2. — …", "ARTIGO 6.º …".
    r"|(?:SECTION|ARTICLE|Article|ARTIGO|Artigo)\s+(?:\d{1,4}(?:-\d{1,3})*|premier|1er)"
    r"(?:\s*[:]|\.\s*[-–—]|\s+[-–—]\s|\.?\s*º)"
    r"|Art\.\s*\d{1,4}(?:-\d{1,3})*\.\s*[—–]"
    r")"
)
LEVEL = [
    (re.compile(r"^\d{1,3}[A-Z]?\."), 0),
    (re.compile(r"^\(\d{1,3}[A-Z]?\)"), 1),
    (re.compile(r"^\([ivxl]{2,6}\)"), 3),
    (re.compile(r"^\([a-z]{1,2}\)"), 2),
]
MARKERISH = re.compile(r"^[\"“]?(?:\d{1,3}[A-Z]?\.|\(\w{1,6}\))[\"”]?$")
PART = re.compile(r"^PART\s+[IVXLC]+\b")


@dataclass
class Note:
    text: str
    anchor: tuple[str, ...] | None  # opening words of the body paragraph printed beside it
    numbered: tuple[str, ...] | None = None  # the same opening with its numbers ("4. (1) the …")


def opening(text: str, numbers: bool = False) -> tuple[str, ...]:
    """A unit's first words for matching; leading numbers and markers kept only when asked."""
    words = [key(w).lower() for w in TOKEN.findall(text)]
    while not numbers and words and MARKERISH.match(words[0]):
        words.pop(0)
    return tuple(w for w in words if w)[:ANCHOR_WORDS]


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
            paragraph = max(beside, key=lambda b: b["box"]["y0"])["text"] if beside else ""
            anchor, numbered = opening(paragraph), opening(paragraph, numbers=True)
            notes.append(
                Note(
                    block["text"],
                    anchor if len(anchor) >= 3 else None,
                    numbered if numbered != anchor else None,
                )
            )
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


def split_at_boundaries(nodes: list[dict], check_pages: dict[str, dict]) -> list[dict]:
    """Split a primary unit where the positional reader starts a main-column paragraph inside it.

    The boundary is taken only where both of the paragraph's first two words align with the
    primary's words, and not where the paragraph opens in lower case (a wrapped line).
    """
    blocks = [
        b
        for page in check_pages.values()
        for b in page.get("blocks") or []
        if b.get("box") and b["box"]["x0"] < MARGIN and not b.get("role")
    ]
    reference, starts = [], []
    for block in blocks:
        words = TOKEN.findall(block["text"])
        if not words:
            continue
        if not (words[0][0].islower() and not MARKERISH.match(words[0])):
            starts.append(len(reference))
        reference.extend(key(w).lower() for w in words)
    if not starts:
        return nodes
    positions = [
        (index, match)
        for index, node in enumerate(nodes)
        if node["kind"] in {"paragraph", "list_item", "heading"} and node.get("text")
        for match in TOKEN.finditer(node["text"])
    ]
    primary = [key(match.group()).lower() for _, match in positions]
    cuts: dict[int, set[int]] = {}
    for a, b, size in difflib.SequenceMatcher(a=reference, b=primary, autojunk=False).get_matching_blocks():
        for start in starts:
            if a <= start < a + size - 1:
                index, match = positions[b + start - a]
                if match.start() > 0:
                    cuts.setdefault(index, set()).add(match.start())
    out = []
    for index, node in enumerate(nodes):
        if index not in cuts:
            out.append(node)
            continue
        edges = [0, *sorted(cuts[index]), len(node["text"])]
        for number, (left, right) in enumerate(zip(edges, edges[1:], strict=False)):
            part = node["text"][left:right].strip()
            if not part:
                continue
            if number == 0:
                out.append({**node, "text": part})
                continue
            unit = {
                k: v for k, v in node.items() if k not in {"marker", "marker_source", "notes", "_pending"}
            }
            unit.update(text=part, split=True)
            if PART.match(part):
                unit["kind"] = "heading"
            out.append(unit)
    return out


def attach_notes(nodes: list[dict]) -> list[dict]:
    """Give each note to the unit opening with the paragraph printed beside it, if one does.

    The opening is matched with its numbers first ("4. (1) The National …"), since provisions
    often open with the same words; without numbers only when the main reading dropped them, and
    then, if several units match, the one nearest to where the note was found.
    """
    texts = [
        (f"{n['marker']} {n.get('text', '')}" if n.get("marker") else n.get("text", ""))
        if n["kind"] in {"paragraph", "list_item"}
        else ""
        for n in nodes
    ]
    plain = [opening(t) for t in texts]
    numbered = [opening(t, numbers=True) for t in texts]

    def starts(openings, anchor):
        width = len(anchor)
        return [i for i, o in enumerate(openings) if len(o) >= width and o[:width] == anchor]

    for index, node in enumerate(nodes):
        for note in node.pop("_pending", []):
            owner = index
            matches = starts(numbered, note.numbered) if note.numbered else []
            if not matches and note.anchor:
                matches = starts(plain, note.anchor)
            if matches:
                owner = min(matches, key=lambda i: (abs(i - index), i))
            nodes[owner].setdefault("notes", []).append(note.text)
    return nodes


def depth(node: dict) -> int | None:
    lead = (node.get("marker") or node.get("text") or "").lstrip()
    for pattern, level in LEVEL:
        if pattern.match(lead):
            return level
    return None


def restructure(nodes: list[dict], check_pages: dict[str, dict]) -> list[dict]:
    nodes = split_units(place_notes(nodes, margin_notes(check_pages)))
    return attach_notes(split_at_boundaries(nodes, check_pages))
