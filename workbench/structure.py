"""Legal structure of reviewed text: parts, sections, articles and their spans (TXP-03).

Built from the canonical text's leaves (workbench.serialize) and the units they came from, by the
structure rules of the document's kind:

  ng-federal-act-1   Nigerian federal Acts and constitutional alterations: contents, preamble,
                     parts and chapters, sections, subsections, paragraphs, sub-paragraphs,
                     definitions, schedules and the Clerk's certificates after them
  rw-law-1           Rwandan laws, one language at a time: titles, chapters, divisions (a civil-law
                     "section" groups articles) and articles

Where the reviewer set an indent level it decides a unit's depth; numbering patterns decide only
where no level was set. Locators follow the Akoma Ntoso eId convention of the workbench export
(sec_23__subsec_1__para_a): parts and chapters do not qualify the sections inside them, so a
section keeps its locator if parts are renumbered. Spans are UTF-8 byte offsets over the canonical
text, half-open, covering a node's own leaves and its descendants (DAT-06).
"""

import hashlib
import re
from dataclasses import dataclass, field

from workbench.serialize import Issue, Leaf, Serialized, clean
from workbench.store import canonical_json

NG_FEDERAL_ACT = "ng-federal-act-1"
RW_LAW = "rw-law-1"

# --- Nigerian federal Acts ---------------------------------------------------------------------------

SECTION = re.compile(r"^(\d{1,3}[A-Z]{0,2})\.(?=\s|$)\s*(?:[—–-]\s*)?(?:(\((\d{1,3}[A-Z]?)\))(?=\s|$))?")
SUBSECTION = re.compile(r"^\((\d{1,3}[A-Z]?)\)(?=\s|$)")
LETTER = re.compile(r"^\(([a-z]{1,2})\)(?=\s|$)")
ROMAN = re.compile(r"^\((x{0,3}(?:ix|iv|v?i{0,3}))\)(?=\s|$)")
PART = re.compile(r"^PART\s+([IVXLC]+|\d+)\b\s*[—–:.-]?\s*(.*)$", re.S)
CHAPTER = re.compile(r"^CHAPTER\s+([IVXLC]+|\d+)\b\s*[—–:.-]?\s*(.*)$", re.S)
SCHEDULES = re.compile(r"^SCHEDULES\s*$", re.I)
ORDINALS = ("first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth").split()
SCHEDULE = re.compile(
    r"^(?:(" + "|".join(ORDINALS) + r")\s+schedule|schedule(?:\s+([ivxlc]+|\d+))?)\b(?!\s+to\s+the\b)", re.I
)
CONTENTS = re.compile(
    r"^(?:arrangement\s+of\s+(?:sections|clauses|articles|paragraphs)|table\s+of\s+contents)\b", re.I
)
PREAMBLE = re.compile(r"^(?:a\s+bill\b|an?\s+act\s+(?:to|for)\b|(?:be\s+it\s+)?enacted\b)", re.I)
MEMORANDUM = re.compile(r"^explanatory\s+memorandum\b", re.I)
CERTIFICATE = re.compile(r"^I,?\s+certify\b", re.I)
# A quoted term, then (perhaps after a few words: "for the time being") what it means.
DEFINITION = re.compile(
    r"^[“\"‘']([^”\"’']{1,120})[”\"’']\s+(?:[^\s.;:]+\s+){0,5}?(?:means|includes|has|shall\s+have|refers)\b"
)
NUMBERED = re.compile(r"^(?:\d{1,3}[A-Z]{0,2}\.|\(\w{1,5}\))(?=\s|$)")

SECTION_DEPTH, SUBSECTION_DEPTH, DEFINITION_DEPTH, PARAGRAPH_DEPTH, SUBPARAGRAPH_DEPTH = 0, 1, 1.5, 2, 3
BODY_KINDS = {0: "section", 1: "subsection", 2: "paragraph", 3: "subparagraph"}
BODY_TOKENS = {"section": "sec", "subsection": "subsec", "paragraph": "para", "subparagraph": "subpara"}


@dataclass
class Node:
    kind: str
    locator: str
    label: str = ""
    heading: str = ""
    parent: "Node | None" = None
    depth: float = -1  # provisions only: 0 section … 3 sub-paragraph
    children: list["Node"] = field(default_factory=list)
    leaves: list[int] = field(default_factory=list)  # indices of the leaves this node holds itself
    trim: dict[int, int] = field(default_factory=dict)  # leaf index -> bytes of the leaf before this node
    opens_at: int | None = None  # a byte offset where the node starts without holding the leaf there
    target: str | None = None  # contents entries: the locator the entry points to
    order: int = 0
    start: int = 0
    end: int = 0
    page_start: int | None = None
    page_end: int | None = None

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass
class Structure:
    root: Node
    issues: list[Issue]
    rules_id: str

    @property
    def nodes(self) -> list[Node]:
        return list(self.root.walk())

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.level == "error"]

    def canonical(self) -> list[dict]:
        """The nodes without identifiers, in document order: what an approval binds."""
        return [
            {
                "kind": n.kind,
                "locator": n.locator,
                "label": n.label,
                "heading": n.heading,
                "parent": n.parent.locator if n.parent else None,
                "order": n.order,
                "start_byte": n.start,
                "end_byte": n.end,
                "page_start": n.page_start,
                "page_end": n.page_end,
                **({"target": n.target} if n.kind == "contents_entry" else {}),
            }
            for n in self.nodes
        ]

    @property
    def structure_hash(self) -> str:
        return hashlib.sha256(canonical_json(self.canonical()).encode()).hexdigest()


def rules_for(document: dict) -> str:
    return RW_LAW if (document.get("jurisdiction") or document.get("id", "")[:2]) == "rw" else NG_FEDERAL_ACT


def derive(
    serialized: Serialized, pages: list[dict], rules_id: str, language: str | None = None
) -> Structure:
    units = {(p["page"], i): unit for p in pages for i, unit in enumerate(p["units"])}
    if rules_id == RW_LAW:
        builder: Builder = RwandaBuilder(language or "en", serialized, units)
    else:
        builder = ActBuilder(serialized, units)
    builder.build()
    finish(builder.root, serialized.leaves)
    return Structure(builder.root, builder.issues, rules_id)


# --- shared machinery --------------------------------------------------------------------------------


class Builder:
    def __init__(self, serialized: Serialized, units: dict):
        self.leaves, self.units = serialized.leaves, units
        self.root = Node("document", "document")
        self.issues: list[Issue] = []
        self.locators: dict[str, Node] = {}
        self.pending_notes: list[int] = []  # margin notes waiting for the words they stand beside

    def unit(self, leaf: Leaf) -> dict:
        return self.units[(leaf.page, leaf.unit)]

    def issue(self, level: str, code: str, leaf: Leaf, message: str) -> None:
        self.issues.append(Issue(level, code, leaf.page, leaf.unit, message))

    def add(self, parent: Node, kind: str, locator: str, leaf: Leaf, **values) -> Node:
        """A new child node with a unique locator; a repeated one is suffixed and reported."""
        unique, count = locator, 1
        while unique in self.locators:
            count += 1
            unique = f"{locator}_{count}"
        if count > 1:
            why = "is defined more than once" if kind == "definition" else "occurs more than once"
            hint = "" if kind == "definition" else "; a misread number?"
            self.issue("warning", "repeated_locator", leaf, f"{locator} {why} (published as {unique}){hint}")
        node = Node(kind, unique, parent=parent, order=len(parent.children), **values)
        parent.children.append(node)
        self.locators[unique] = node
        return node

    def hold(self, node: Node, index: int) -> None:
        node.leaves.append(index)

    def notes_to(self, node: Node, heading: bool) -> None:
        """Margin notes go to the node that holds their words; a section's first note is its heading."""
        for n, index in enumerate(self.pending_notes):
            self.hold(node, index)
            if heading and n == 0 and not node.heading:
                node.heading = self.leaves[index].text
        self.pending_notes = []

    def build(self) -> None:
        for index, leaf in enumerate(self.leaves):
            if leaf.role == "note":
                self.pending_notes.append(index)
            else:
                self.place(index, leaf)
        if self.pending_notes:  # notes whose unit had no words were refused by the serializer
            self.notes_to(self.root, heading=False)
        self.check_running_heads()

    def check_running_heads(self) -> None:
        seen: dict[str, Leaf] = {}
        for leaf in self.leaves:
            if leaf.role != "text" or self.unit(leaf).get("kind") != "heading":
                continue
            first = seen.setdefault(leaf.text.casefold(), leaf)
            if first is not leaf and first.page != leaf.page:
                self.issue(
                    "warning",
                    "running_head",
                    leaf,
                    f"“{leaf.text[:80]}” repeats the heading on page {first.page}: a running head to remove?",
                )


def finish(root: Node, leaves: list[Leaf]) -> None:
    """Spans and pages from the leaves each node holds and its descendants."""

    def visit(node: Node) -> None:
        starts = [leaves[i].start + node.trim.get(i, 0) for i in node.leaves]
        ends = [leaves[i].end for i in node.leaves]
        pages = [p for i in node.leaves for p in (leaves[i].page, leaves[i].last_page)]
        if node.opens_at is not None:
            starts.append(node.opens_at)
        for child in node.children:
            visit(child)
            starts.append(child.start)
            ends.append(child.end)
            pages += [p for p in (child.page_start, child.page_end) if p is not None]
        node.start, node.end = (min(starts), max(ends)) if ends else (0, 0)
        node.page_start, node.page_end = (min(pages), max(pages)) if pages else (None, None)

    visit(root)


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")[:60] or "x"


def number_of(label: str) -> str:
    return label.strip(".()").strip()


# --- ng-federal-act-1 --------------------------------------------------------------------------------


class ActBuilder(Builder):
    def __init__(self, serialized: Serialized, units: dict):
        super().__init__(serialized, units)
        self.region = "front"  # front, contents, preamble, body, schedules, endmatter
        self.container: Node = self.root  # where provisions and cross-headings go
        self.chapter: Node | None = None
        self.schedule: Node | None = None
        self.region_node: Node | None = None  # the open contents, preamble or notice
        self.stack: list[Node] = []  # open provisions, shallowest first
        self.awaiting_heading: Node | None = None  # a part or schedule whose heading may follow
        self.last_section: tuple[int, str] | None = None
        self.counts: dict[str, int] = {}

    def next_number(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def place(self, index: int, leaf: Leaf) -> None:
        unit, text = self.unit(leaf), leaf.text
        heading_unit = unit.get("kind") == "heading"
        awaiting, self.awaiting_heading = self.awaiting_heading, None
        if leaf.role in ("caption", "table"):
            self.table(index, leaf)
            return
        if CERTIFICATE.match(text) and self.region in ("body", "schedules", "endmatter"):
            self.notes_to(
                self.open_region("endmatter", "notice", f"notice_{self.next_number('notice')}", leaf), False
            )
            self.hold(self.region_node, index)
            return
        if self.region == "endmatter":
            self.notes_to(self.region_node, False)
            self.hold(self.region_node, index)
            return
        if self.region == "front":
            opens = MEMORANDUM, CONTENTS, PREAMBLE, SECTION, PART, CHAPTER
            first = not self.root.leaves and not self.root.children
            if first and heading_unit and not any(pattern.match(text) for pattern in opens):
                self.root.heading = text  # the document's title
                self.notes_to(self.root, False)
                self.hold(self.root, index)
                return
            if MEMORANDUM.match(text):
                node = self.open_region("front", "notice", f"notice_{self.next_number('notice')}", leaf)
                node.heading = text
                self.notes_to(node, False)
                self.hold(node, index)
                return
        if self.region in ("front", "contents") and PREAMBLE.match(text):
            node = self.open_region("preamble", "preamble", "preamble", leaf)
            self.notes_to(node, False)
            self.hold(node, index)
            return
        if self.region == "front" and CONTENTS.match(text):
            node = self.open_region("contents", "contents", "contents", leaf)
            node.heading = text
            self.notes_to(node, False)
            self.hold(node, index)
            return
        if self.region == "contents" and not self.contents_ends(leaf, unit):
            self.contents_entry(index, leaf, unit)
            return
        if self.region in ("front", "contents", "preamble") and not self.body_starts(text):
            self.notes_to(self.region_node or self.root, False)
            self.hold(self.region_node or self.root, index)
            return
        if self.region in ("front", "contents", "preamble"):
            if self.region == "contents":
                self.issue(
                    "warning", "contents_unclosed", leaf, "The contents end without the enacting words"
                )
            self.region, self.region_node = "body", None
        self.body(index, leaf, unit, heading_unit, awaiting)

    def open_region(self, region: str, kind: str, locator: str, leaf: Leaf) -> Node:
        self.region, self.stack, self.container = region, [], self.root
        self.chapter = self.schedule = None
        self.region_node = self.add(self.root, kind, locator, leaf)
        return self.region_node

    def contents_ends(self, leaf: Leaf, unit: dict) -> bool:
        """A long numbered paragraph with a margin note is the body, not a contents entry."""
        return bool(SECTION.match(leaf.text) and (unit.get("notes") or len(leaf.text) > 150))

    def body_starts(self, text: str) -> bool:
        return bool(SECTION.match(text) or PART.match(text) or CHAPTER.match(text))

    def contents_entry(self, index: int, leaf: Leaf, unit: dict) -> None:
        text, contents = leaf.text, self.region_node
        found = SECTION.match(text) or PART.match(text) or CHAPTER.match(text) or SCHEDULE.match(text)
        self.notes_to(contents, False)
        if not found:
            self.hold(contents, index)
            return
        if found.re is SECTION:
            label, target = f"{found.group(1)}.", f"sec_{found.group(1)}"
        elif found.re is PART:
            label, target = f"PART {found.group(1)}", f"part_{found.group(1)}"
        elif found.re is CHAPTER:
            label, target = f"CHAPTER {found.group(1)}", f"chap_{found.group(1)}"
        else:
            label, target = found.group(0), f"sch_{self.schedule_number(found)}"
        entry = self.add(contents, "contents_entry", f"contents__entry_{len(contents.children) + 1}", leaf)
        entry.label, entry.target = label, target
        self.hold(entry, index)

    def schedule_number(self, found: re.Match) -> str:
        if found.group(1):
            return str(ORDINALS.index(found.group(1).casefold()) + 1)
        return number_of(found.group(2)) if found.group(2) else "1"

    def table(self, index: int, leaf: Leaf) -> None:
        owner = self.stack[-1] if self.stack else self.region_node or self.container
        if leaf.role == "caption":
            self.notes_to(owner, False)
            caption = SCHEDULE.match(leaf.text) if self.region != "endmatter" else None
            if caption:
                owner = self.open_schedule(leaf, caption)
            node = self.add(
                owner, "table", f"{owner.locator}__table_{self.next_number('table:' + owner.locator)}", leaf
            )
            node.heading = leaf.text
            self.hold(node, index)
            self._caption_for = node
            return
        node = getattr(self, "_caption_for", None)
        if (
            node is None
            or self.leaves[node.leaves[0]].unit != leaf.unit
            or self.leaves[node.leaves[0]].page != leaf.page
        ):
            self.notes_to(owner, False)
            node = self.add(
                owner, "table", f"{owner.locator}__table_{self.next_number('table:' + owner.locator)}", leaf
            )
        self._caption_for = None
        self.hold(node, index)

    def open_schedule(self, leaf: Leaf, found: re.Match) -> Node:
        self.region, self.stack, self.chapter, self.region_node = "schedules", [], None, None
        self.schedule = self.add(self.root, "schedule", f"sch_{self.schedule_number(found)}", leaf)
        self.schedule.label = found.group(0)
        self.container = self.schedule
        return self.schedule

    def body(self, index: int, leaf: Leaf, unit: dict, heading_unit: bool, awaiting: Node | None) -> None:
        text = leaf.text
        if SCHEDULES.match(text):
            self.region, self.stack, self.container, self.chapter = "schedules", [], self.root, None
            self.notes_to(self.root, False)
            self.hold(self.root, index)
            return
        found = SCHEDULE.match(text)
        if found and (heading_unit or unit.get("align") == "center" or self.region == "schedules"):
            node = self.open_schedule(leaf, found)
            self.notes_to(node, False)
            self.hold(node, index)
            self.awaiting_heading = node
            return
        if self.region == "schedules" and self.schedule is None:
            self.issue(
                "warning",
                "outside_schedule",
                leaf,
                "Text after the schedules heading but before any schedule",
            )
            self.notes_to(self.root, False)
            self.hold(self.root, index)
            return
        part, chapter = PART.match(text), CHAPTER.match(text)
        if chapter and self.region == "body":
            self.stack = []
            self.chapter = self.container = self.add(self.root, "chapter", f"chap_{chapter.group(1)}", leaf)
            self.chapter.label, self.chapter.heading = f"CHAPTER {chapter.group(1)}", chapter.group(2).strip()
            self.notes_to(self.chapter, False)
            self.hold(self.chapter, index)
            self.awaiting_heading = None if self.chapter.heading else self.chapter
            return
        if part:
            self.stack = []
            owner = self.schedule if self.region == "schedules" else self.chapter or self.root
            prefix = f"{self.schedule.locator}__" if self.region == "schedules" else ""
            self.container = self.add(owner, "part", f"{prefix}part_{part.group(1)}", leaf)
            self.container.label, self.container.heading = f"PART {part.group(1)}", part.group(2).strip()
            self.notes_to(self.container, False)
            self.hold(self.container, index)
            self.awaiting_heading = None if self.container.heading else self.container
            return
        numbered = SECTION.match(text) or SUBSECTION.match(text) or LETTER.match(text)
        if awaiting is not None and not awaiting.heading and not numbered:
            if heading_unit or unit.get("align") == "center":
                awaiting.heading = text  # "PART III" then "LICENSED INDEPENDENT POWER PRODUCERS"
                self.notes_to(awaiting, False)
                self.hold(awaiting, index)
                return
        if heading_unit:
            following = self.depth_of(self.next_text(index))
            if self.stack and following is not None and following > SECTION_DEPTH:
                # A heading inside a provision ("Committees" before "(7)"): the provision goes on.
                while self.stack and self.stack[-1].depth >= following:
                    self.stack.pop()
                owner = self.stack[-1] if self.stack else self.container
            else:
                self.stack, owner = [], self.container  # a cross-heading groups the provisions after it
            self.notes_to(owner, False)
            self.hold(owner, index)
            return
        self.provision(index, leaf, unit)

    def next_text(self, index: int) -> Leaf | None:
        for leaf in self.leaves[index + 1 :]:
            if leaf.role == "text":
                return leaf
            if leaf.role != "note":
                return None
        return None

    def depth_of(self, leaf: Leaf | None) -> float | None:
        if leaf is None:
            return None
        level = self.unit(leaf).get("level")
        if SECTION.match(leaf.text):
            return SECTION_DEPTH if level is None else level
        if SUBSECTION.match(leaf.text) or LETTER.match(leaf.text) or ROMAN.match(leaf.text):
            return (
                (SUBSECTION_DEPTH if SUBSECTION.match(leaf.text) else PARAGRAPH_DEPTH)
                if level is None
                else level
            )
        return None

    def build(self) -> None:
        super().build()
        for node in self.root.walk():
            if node.kind == "contents_entry" and node.target not in self.locators:
                self.issue(
                    "warning",
                    "contents_unmatched",
                    self.leaves[node.leaves[0]],
                    f"The contents list has {node.label}, but the text has no {node.target}",
                )

    def provision(self, index: int, leaf: Leaf, unit: dict) -> None:
        text, level = leaf.text, unit.get("level")
        in_schedule = self.region == "schedules"
        if unit.get("marker") and NUMBERED.match(clean(unit.get("text") or "")):
            self.issue(
                "warning",
                "two_numbers",
                leaf,
                f"“{text[:24]}…” has two numbers in a row: the restored number may be in the wrong place",
            )
        section = SECTION.match(text)
        definition = DEFINITION.match(text) if not in_schedule else None
        if section:
            depth, label = SECTION_DEPTH, f"{section.group(1)}."
        elif SUBSECTION.match(text):
            depth, label = SUBSECTION_DEPTH, SUBSECTION.match(text).group(0)
        elif LETTER.match(text) or (ROMAN.match(text) and ROMAN.match(text).group(1)):
            depth, label = self.letter_or_roman(text)
        else:
            depth, label = None, ""
        if level is not None and isinstance(level, int):
            if label and depth != level:
                section = None  # the reviewer's level decides: "(b)" at level 3 is a sub-paragraph
            depth = level if label else depth
        if not label:
            if definition:
                self.open_definition(index, leaf, definition)
                return
            self.unnumbered(index, leaf, level)
            return
        node = self.open_provision(depth, label, leaf, in_schedule)
        self.notes_to(node, heading=depth == SECTION_DEPTH and not in_schedule)
        if section and section.group(2) and level in (None, 0):  # "5. (1) There is established…"
            child = self.open_provision(SUBSECTION_DEPTH, section.group(2), leaf, in_schedule)
            child.trim[index] = len(text[: section.start(2)].encode())
            node.opens_at = leaf.start
            node = child
        self.hold(node, index)
        if depth == SECTION_DEPTH and not in_schedule:
            self.check_order(leaf, label)

    def letter_or_roman(self, text: str) -> tuple[int, str]:
        """(i), (v) and (x) are letters only when they continue a run of letters: (h) then (i)."""
        letter, roman = LETTER.match(text), ROMAN.match(text)
        if not (roman and roman.group(1)):
            return PARAGRAPH_DEPTH, letter.group(0)
        if letter:
            previous = next((n for n in reversed(self.stack) if n.depth == PARAGRAPH_DEPTH), None)
            if previous is not None:
                before = number_of(previous.label)
                if (
                    len(before) == len(letter.group(1))
                    and before[-1:]
                    and chr(ord(before[-1]) + 1) == letter.group(1)[-1]
                ):
                    return PARAGRAPH_DEPTH, letter.group(0)
        return SUBPARAGRAPH_DEPTH, roman.group(0)

    def open_provision(self, depth: float, label: str, leaf: Leaf, in_schedule: bool) -> Node:
        while self.stack and self.stack[-1].depth >= depth:
            self.stack.pop()
        parent = self.stack[-1] if self.stack else self.container
        if in_schedule:
            kind = "paragraph" if depth == SECTION_DEPTH else "subparagraph"
            token = "para" if depth == SECTION_DEPTH else "subpara"
        else:
            kind = BODY_KINDS[int(depth)]
            token = BODY_TOKENS[kind]
        own = f"{token}_{slug(number_of(label))}"
        qualified = kind == "section" or parent.kind in ("document", "part", "chapter")
        locator = own if qualified and not in_schedule else f"{parent.locator}__{own}"
        if not qualified and parent.kind in ("document", "part", "chapter") and not in_schedule:
            self.issue("warning", "outside_section", leaf, f"{label} is outside any section")
        node = self.add(parent, kind, locator, leaf, label=label, depth=depth)
        self.stack.append(node)
        return node

    def open_definition(self, index: int, leaf: Leaf, found: re.Match) -> None:
        while self.stack and self.stack[-1].depth >= DEFINITION_DEPTH:
            self.stack.pop()
        parent = self.stack[-1] if self.stack else self.container
        node = self.add(
            parent,
            "definition",
            f"{parent.locator}__def_{slug(found.group(1))}",
            leaf,
            label=found.group(1),
            depth=DEFINITION_DEPTH,
        )
        self.stack.append(node)
        self.notes_to(node, False)
        self.hold(node, index)

    def unnumbered(self, index: int, leaf: Leaf, level) -> None:
        """Provisos and closing words: within the open provision at the reviewer's level, else the
        innermost one."""
        if isinstance(level, int):
            while self.stack and self.stack[-1].depth > level:
                self.stack.pop()
        owner = self.stack[-1] if self.stack else self.container
        self.notes_to(owner, False)
        self.hold(owner, index)

    def check_order(self, leaf: Leaf, label: str) -> None:
        digits = re.match(r"\d+", label)
        if not digits:
            return
        current = (int(digits.group(0)), label)
        if self.last_section and current[0] < self.last_section[0]:
            self.issue(
                "warning", "section_order", leaf, f"Section {label} follows section {self.last_section[1]}"
            )
        self.last_section = current


# --- rw-law-1 ----------------------------------------------------------------------------------------


class RwandaBuilder(Builder):
    """One language column of a Rwandan law. Article and division headings are recognised in
    heading units only, as in the side-by-side view, so the contents list is not mistaken for the
    body. Text within an article is held by the article."""

    DIVISION_KINDS = {"title": "title", "chapter": "chapter", "section": "division", "subsection": "division"}
    DIVISION_TOKENS = {"title": "title", "chapter": "chap", "section": "dvs", "subsection": "subdvs"}
    RANK = {"title": 0, "chapter": 1, "section": 2, "subsection": 3}
    CONTENTS = re.compile(r"^(?:ishakiro|table\s+of\s+contents|table\s+des\s+mati[eè]res)\b", re.I)
    SIGNATURE = re.compile(r"^(?:bikorewe\s+i|done\s+at|fait\s+[àa])\b", re.I)

    def __init__(self, language: str, *args):
        super().__init__(*args)
        from ingestion import rw_gazette

        self.language, self.rw = language, rw_gazette
        self.article_pattern = rw_gazette.ARTICLE.get(language)
        self.divisions = rw_gazette.DIVISIONS.get(language, [])
        self.open: list[tuple[int, Node]] = []  # open divisions with their rank
        self.article: Node | None = None
        self.region_node: Node | None = None
        self.counts: dict[str, int] = {}

    def owner(self) -> Node:
        return self.article or (self.open[-1][1] if self.open else self.region_node or self.root)

    def place(self, index: int, leaf: Leaf) -> None:
        unit, text = self.unit(leaf), clean(leaf.text)
        heading_unit = unit.get("kind") == "heading"
        if self.CONTENTS.match(text):
            self.region_node, self.article, self.open = (
                self.add(self.root, "contents", "contents", leaf),
                None,
                [],
            )
            self.region_node.heading = text
            self.notes_to(self.region_node, False)
            self.hold(self.region_node, index)
            return
        if self.SIGNATURE.match(text):
            self.counts["notice"] = self.counts.get("notice", 0) + 1
            self.region_node = self.add(self.root, "notice", f"notice_{self.counts['notice']}", leaf)
            self.article, self.open = None, []
            self.notes_to(self.region_node, False)
            self.hold(self.region_node, index)
            return
        if heading_unit:
            for name, pattern in self.divisions:
                found = pattern.match(text)
                if found:
                    self.division(index, leaf, name, found)
                    return
            found = self.article_pattern.match(text) if self.article_pattern else None
            if found:
                if self.region_node is not None and self.region_node.kind == "contents":
                    self.region_node = None  # the first article heading ends the contents list
                number = self.rw._number(found.group(1))
                parent = self.open[-1][1] if self.open else self.root
                self.article = self.add(
                    parent, "article", f"art_{slug(str(number))}", leaf, label=text[: found.end(1)]
                )
                self.article.heading = (found.group(2) or found.group(3) or "").strip()
                self.notes_to(self.article, True)
                self.hold(self.article, index)
                return
        owner = self.owner()
        self.notes_to(owner, False)
        self.hold(owner, index)

    def division(self, index: int, leaf: Leaf, name: str, found: re.Match) -> None:
        rank = self.RANK[name]
        while self.open and self.open[-1][0] >= rank:
            self.open.pop()
        parent = self.open[-1][1] if self.open else self.root
        token = f"{self.DIVISION_TOKENS[name]}_{slug(found.group(1))}"
        locator = token if parent is self.root else f"{parent.locator}__{token}"
        node = self.add(parent, self.DIVISION_KINDS[name], locator, leaf, label=leaf.text[: found.end(1)])
        node.heading = (found.group(2) or "").strip()
        self.open.append((rank, node))
        self.article, self.region_node = None, None
        self.notes_to(node, False)
        self.hold(node, index)
