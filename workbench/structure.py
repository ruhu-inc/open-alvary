"""Legal structure of reviewed text: parts, sections, articles and their spans (TXP-03).

Built from the canonical text's leaves (workbench.serialize) and the units they came from, by the
structure rules for the document's jurisdiction and type (RULES):

  ng-federal-act-1   Nigerian federal Acts and constitutional alterations: contents, preamble,
                     parts and chapters, sections, subsections, paragraphs, sub-paragraphs,
                     definitions, schedules and the Clerk's certificates after them
  eac-act-1          Acts of the East African Community: Nigeria's rules, with the Community's
                     long title ("An Act of the Community to …"), the Act's title on its cover,
                     lettered parts in schedules ("PART A—PROHIBITED GOODS") and a schedule's line
                     of the sections it serves ("(ss. 18, 19, and 20.)")
  ke-act-1           Kenyan Acts as the Kenya Gazette Supplement prints them: Nigeria's rules, with
                     Kenya's long title ("AN ACT of Parliament to …") and the Act's title after the
                     gazette's cover
  za-act-1           South African Acts as the Government Gazette prints them: the gazette's notices,
                     the long title and enacting words, then the contents; chapters with parts A, B …
                     inside them; a section's heading printed above it; definitions in ‘‘ ’’
  gh-act-1           Ghanaian Acts: unnumbered group headings, a section's heading printed above it,
                     and the first group heading after "PASSED by Parliament …" opening the body
  ohada-uniform-act-1
                     OHADA Uniform Acts in French, as the Journal officiel prints them: the
                     SOMMAIRE, the Council of Ministers' preamble, PARTIE, LIVRE, TITRE, SOUS-TITRE,
                     CHAPITRE and Section, articles numbered through the Act ("Article 2-1"
                     inserted) and their alinéas, and the signatures
  au-treaty-1        The AU's charters and protocols, in English, French and Portuguese: the
                     preamble to the enacting words, parts ("PREMIERE PARTIE") and chapters, articles
                     with their heading after the number, numbered paragraphs and lettered items
  ecowas-act-1       ECOWAS's Supplementary Acts in English: as au-treaty-1, with a heading inside an
                     article (a defined term) kept in it, the logo text on every page reported, and
                     the signatures ("H. E. …") a notice
  ci-loi-1           Ivorian laws as the Journal officiel prints them: the issue's front matter, the
                     title with its enacting words, TITRE, CHAPITRE and Section, articles opening with
                     their first alinéa ("Art. 2. — …"), alinéas as OHADA's, and "Fait à …" a notice
                     holding the rest of the issue
  cm-law-1           Cameroonian laws in English ("SECTION 7:", subsections "(1)") and French
                     ("ARTICLE 7.-", numbered paragraphs), the stamp printed on every page reported
  ao-lei-1           Angolan laws as the Diário da República prints them: CAPÍTULO, SECÇÃO, articles
                     ("ARTIGO 3.º") with their heading in brackets, números and alíneas, and "Vista e
                     aprovada …" a notice
  rw-law-1           Rwandan laws, orders and the other instruments of the Official Gazette, one
                     language at a time: titles, chapters, divisions (a civil-law "section" groups
                     articles) and articles
  judgment-paragraphs-1
                     Nigerian judgments and rulings: the header, each opinion with its numbered
                     paragraphs (each one a retrieval unit, cited "[2019] NGSC 4 [12]"), and the orders

The Commonwealth families are one builder (ActBuilder) with each family's Drafting: what it prints
otherwise than Nigeria's federal Acts. The article families after OHADA's are one builder
(ArticleBuilder) with each family's ArticleStyle, calibrated on the cloud readings of official texts
(ingestion.calibrate) as reviewers receive them. Each was written from, and calibrated on, whole official texts
of its family; a family whose Acts read exactly as Nigeria's would be mapped to ng-federal-act-1. All
four read a definition however long the scope before its meaning ("‘‘de-identify’’, in relation to …,
means").

There is no default. A document of any other jurisdiction or type has no structure but one node
holding its whole text, and an error that blocks its approval and publication: its layout is
unsupported until rules are written for it from real texts (DAT-05), never read by another's.

Where the reviewer set an indent level it decides a unit's depth; numbering patterns decide only
where no level was set. Locators follow the Akoma Ntoso eId convention of the workbench export
(sec_23__subsec_1__para_a): parts and chapters do not qualify the sections inside them, so a
section keeps its locator if parts are renumbered. Spans are UTF-8 byte offsets over the canonical
text, half-open, covering a node's own leaves and its descendants (DAT-06).
"""

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

from workbench.serialize import Issue, Leaf, Serialized, clean
from workbench.store import canonical_json

NG_FEDERAL_ACT = "ng-federal-act-1"
EAC_ACT = "eac-act-1"
KE_ACT = "ke-act-1"
ZA_ACT = "za-act-1"
GH_ACT = "gh-act-1"
OHADA_UNIFORM_ACT = "ohada-uniform-act-1"
RW_LAW = "rw-law-1"
AU_TREATY = "au-treaty-1"
ECOWAS_ACT = "ecowas-act-1"
CI_LAW = "ci-loi-1"
CM_LAW = "cm-law-1"
AO_LAW = "ao-lei-1"
JUDGMENT = "judgment-paragraphs-1"
JUDGMENT_TYPES = {"judgment", "ruling"}
# The kinds of instrument the Rwandan Gazette reader names from the heading that opens each one
# (ingestion.rw_gazette.KIND, CONSTITUTION), whose article and division headings rw-law-1 reads. An
# agreement, and a text whose kind was not recognised ("other"), are not among them.
RW_INSTRUMENTS = (
    "constitution",
    "organic_law",
    "law",
    "decree_law",
    "presidential_order",
    "prime_ministers_order",
    "ministerial_order",
    "regulations",
    "instructions",
    "decree",
    "ordinance",
    "royal_decree",
)
# The rules for each (jurisdiction, document type) written so far (TXP-03), and nothing else.
RULES = {
    ("ng", "act"): NG_FEDERAL_ACT,
    ("aa-eac", "act"): EAC_ACT,
    ("ke", "act"): KE_ACT,
    ("za", "act"): ZA_ACT,
    ("gh", "act"): GH_ACT,
    ("aa-ohada", "uniform_act"): OHADA_UNIFORM_ACT,
    ("aa-au", "charter"): AU_TREATY,
    ("aa-au", "protocol"): AU_TREATY,
    ("aa-ecowas", "supplementary_act"): ECOWAS_ACT,
    ("ci", "law"): CI_LAW,
    ("cm", "law"): CM_LAW,
    ("ao", "law"): AO_LAW,
    **{("ng", kind): JUDGMENT for kind in JUDGMENT_TYPES},
    **{("rw", kind): RW_LAW for kind in RW_INSTRUMENTS},
}
NO_RULES = "no_structure_rules"

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


@dataclass(frozen=True)
class Drafting:
    """What a family of Commonwealth Acts prints otherwise than Nigeria's federal Acts; the defaults
    are ng-federal-act-1's own."""

    preamble: re.Pattern = PREAMBLE  # the long title or enacting words, which open the preamble
    contents: re.Pattern = CONTENTS
    part: re.Pattern = PART
    definition: re.Pattern = DEFINITION
    notices: tuple[re.Pattern, ...] = (MEMORANDUM,)  # front matter that opens a notice
    certificate: re.Pattern | None = CERTIFICATE  # after the body, opens a notice
    # The Act's title, wherever it is printed before the body ("THE DATA PROTECTION ACT", on a
    # gazette's cover or again before the long title); without it, the first heading is the title.
    title: re.Pattern | None = None
    # A heading just before a section is that section's heading, as a margin note is (South Africa,
    # Ghana); otherwise it is a cross-heading.
    headings_above: bool = False
    # Parts restart in each chapter, so the chapter qualifies them: chap_3__part_A (South Africa).
    parts_in_chapters: bool = False
    # The contents follow the enacting words, and end where a chapter or part they list is printed
    # again (South Africa).
    contents_after_preamble: bool = False
    # Enacting words after which the next heading opens the body, though it is no part or section
    # (Ghana: "PASSED by Parliament …", then the first group heading).
    enacting: re.Pattern | None = None
    # A schedule's line of the sections it serves, printed under its name ("SECOND SCHEDULE" then
    # "(ss. 18, 19, and 20.)"): held by the schedule, whose heading is the line after it.
    reference: re.Pattern | None = None


# An Act's title as printed above its long title or on the gazette's cover: "THE DATA PROTECTION ACT",
# "THE EAST AFRICAN COMMUNITY ONE STOP BORDER POSTS ACT, 2016"; never "The Data Protection Act, 2019 901"
# (a cover's list of contents).
ACT_TITLE = re.compile(r"^the\s+.+\bact(?:,?\s+\d{4})?$", re.I | re.S)
# A definition as the East African, Kenyan, South African and Ghanaian Acts print them: Nigeria's, with
# the term in ‘‘ ’’ too, "mean" after two terms ("“approved place of loading” and “approved place of
# unloading” mean"), and a term's scope before its meaning however long ("‘‘de-identify’’, in relation to
# personal information of a data subject, means", "“owner” in respect of—" before its paragraphs).
DEFINITIONS = re.compile(
    r"^(?:‘‘|''|[“\"‘'])([^”\"’']{1,120})(?:’’|''|[”\"’'])"
    r"(?:\s+(?:[^\s.;:]+\s+){0,5}?(?:means?|includes?|has|shall\s+have|refers)\b"
    r"|[^;]{0,120}?\bin\s+(?:relation\s+to|respect\s+of)\b[^;]{0,200}?(?:\b(?:means?|includes?)\b|[—–]$))"
)
NG = Drafting()
EAC = Drafting(
    preamble=re.compile(
        r"^(?:a\s+bill\b|an?\s+act\s+(?:of\s+the\s+community\s+)?(?:to|for)\b|(?:be\s+it\s+)?enacted\b)", re.I
    ),
    # "PART A—PROHIBITED GOODS" in a schedule
    part=re.compile(r"^PART\s+([IVXLC]+|\d+|[A-Z])\b\s*[—–:.-]?\s*(.*)$", re.S),
    definition=DEFINITIONS,
    title=ACT_TITLE,
    reference=re.compile(r"^\(ss?\.\s.*\)$"),
)
KE = Drafting(
    preamble=re.compile(
        r"^(?:a\s+bill\b|an?\s+act\s+(?:of\s+parliament\s+)?(?:to|for)\b|(?:be\s+it\s+)?enacted\b)", re.I
    ),
    definition=DEFINITIONS,
    title=ACT_TITLE,
)
ZA = Drafting(
    # "ACT" above the long title ("To promote …"), or older Acts' "BE IT ENACTED by the Parliament …".
    preamble=re.compile(r"^(?:ACT$|(?i:be\s+it\s+enacted)\b)"),
    contents=re.compile(
        r"^(?:contents\s+of\s+act|arrangement\s+of\s+sections|table\s+of\s+contents)\b", re.I
    ),
    part=re.compile(r"^(?:PART|Part)\s+([IVXLC]+|\d+|[A-Z])\b\s*[—–:.-]?\s*(.*)$", re.S),
    definition=DEFINITIONS,
    # gov.za's note on the languages it publishes; the Government Gazette's front page, with the
    # Presidency's notice of assent; the gazette's explanatory note on brackets and underlining.
    notices=(
        re.compile(r"^please\s+note\s+that\s+most\s+acts\b", re.I),
        re.compile(r"^government\s+gazette$", re.I),
        re.compile(r"^general\s+explanatory\s+note\b", re.I),
    ),
    certificate=None,
    headings_above=True,
    parts_in_chapters=True,
    contents_after_preamble=True,
)
GH = Drafting(
    definition=DEFINITIONS,
    certificate=re.compile(r"^date\s+of\s+gazette\s+notification\b", re.I),
    headings_above=True,
    enacting=re.compile(r"^passed\s+by\s+parliament\b", re.I),
)
DRAFTING = {NG_FEDERAL_ACT: NG, EAC_ACT: EAC, KE_ACT: KE, ZA_ACT: ZA, GH_ACT: GH}


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
    author: str = ""  # opinions: the judge who wrote it, when printed
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
    rules_id: str | None  # None: no rules for the document's jurisdiction and type

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
                **({"author": n.author} if n.kind == "opinion" else {}),
            }
            for n in self.nodes
        ]

    @property
    def structure_hash(self) -> str:
        return hashlib.sha256(canonical_json(self.canonical()).encode()).hexdigest()


def rules_key(document: dict) -> tuple[str, str]:
    """A document's jurisdiction (else its id's) and type (its details' document type, else its
    collection type)."""
    kind = document.get("document_type") or document.get("type") or document.get("kind") or ""
    return str(document.get("jurisdiction") or document.get("id", "")[:2]), str(kind).casefold()


def rules_for(document: dict) -> str | None:
    """The rules for a document's jurisdiction and type, or None where none are written."""
    return RULES.get(rules_key(document))


def for_document(document: dict, serialized: Serialized, pages: list[dict]) -> Structure:
    """A document's structure by the rules for its jurisdiction and type. Without rules, one node
    holds the whole text and an error says why, so the document can be neither approved nor
    published (TXP-01, DAT-05)."""
    rules_id = rules_for(document)
    if rules_id is not None:
        return derive(serialized, pages, rules_id, document.get("language"))
    jurisdiction, kind = rules_key(document)
    root = Node("document", "document", leaves=list(range(len(serialized.leaves))))
    finish(root, serialized.leaves)
    message = (
        f"No structure rules for {jurisdiction or '(no jurisdiction)'}/{kind or '(no type)'}: "
        "this layout is not supported yet"
    )
    return Structure(root, [Issue("error", NO_RULES, None, None, message)], None)


def derive(
    serialized: Serialized, pages: list[dict], rules_id: str, language: str | None = None
) -> Structure:
    units = {(p["page"], i): unit for p in pages for i, unit in enumerate(p["units"])}
    if rules_id == RW_LAW:
        builder: Builder = RwandaBuilder(language or "en", serialized, units)
    elif rules_id == JUDGMENT:
        builder = JudgmentBuilder(serialized, units)
    elif rules_id in DRAFTING:
        builder = ActBuilder(serialized, units, DRAFTING[rules_id])
    elif rules_id == OHADA_UNIFORM_ACT:
        builder = UniformActBuilder(language, serialized, units)
    elif rules_id in ARTICLE_STYLES:
        builder = ArticleBuilder(ARTICLE_STYLES[rules_id], language, serialized, units)
    else:
        raise ValueError(f"No structure rules {rules_id!r}")
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

    def repeatable(self) -> set[int]:
        """Leaves that are neither running heads nor the first occurrence a later heading repeats: a
        contents list repeats the headings it lists (a Part's own heading is not a running head)."""
        return {
            i for node in self.root.walk() if node.kind in ("contents", "contents_entry") for i in node.leaves
        }

    def check_running_heads(self) -> None:
        listed = self.repeatable()
        seen: dict[str, Leaf] = {}
        for index, leaf in enumerate(self.leaves):
            if leaf.role != "text" or self.unit(leaf).get("kind") != "heading" or index in listed:
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


# --- ng-federal-act-1 and the other Commonwealth families ---------------------------------------------


class ActBuilder(Builder):
    def __init__(self, serialized: Serialized, units: dict, drafting: Drafting = NG):
        super().__init__(serialized, units)
        self.drafting = drafting
        self.region = "front"  # front, contents, preamble, body, schedules, endmatter
        self.container: Node = self.root  # where provisions and cross-headings go
        self.chapter: Node | None = None
        self.schedule: Node | None = None
        self.region_node: Node | None = None  # the open contents, preamble or notice
        self.stack: list[Node] = []  # open provisions, shallowest first
        self.awaiting_heading: Node | None = None  # a part or schedule whose heading may follow
        self.last_section: tuple[int, str] | None = None
        self.counts: dict[str, int] = {}
        self.enacted = False  # the preamble holds the enacting words (Drafting.enacting)
        self.listed_chapter: str | None = None  # the chapter the contents list parts under
        self.titles: set[int] = set()  # leaves that print the Act's title (Drafting.title)

    def next_number(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def repeatable(self) -> set[int]:
        listed = super().repeatable() | self.titles  # the title, on the cover and again before the long title
        if self.drafting.parts_in_chapters:  # each chapter has its own "Part A"
            listed |= {node.leaves[0] for node in self.root.walk() if node.kind == "part" and node.leaves}
        return listed

    def place(self, index: int, leaf: Leaf) -> None:
        unit, text, drafting = self.unit(leaf), leaf.text, self.drafting
        heading_unit = unit.get("kind") == "heading"
        awaiting, self.awaiting_heading = self.awaiting_heading, None
        if leaf.role in ("caption", "table"):
            self.table(index, leaf)
            return
        certificate = drafting.certificate.match(text) if drafting.certificate else None
        if certificate and self.region in ("body", "schedules", "endmatter"):
            self.notes_to(
                self.open_region("endmatter", "notice", f"notice_{self.next_number('notice')}", leaf), False
            )
            self.hold(self.region_node, index)
            return
        if self.region == "endmatter":
            self.notes_to(self.region_node, False)
            self.hold(self.region_node, index)
            return
        if drafting.title and self.region in ("front", "contents") and drafting.title.match(text):
            self.root.heading = self.root.heading or text  # the document's title, which ends the contents
            self.titles.add(index)
            self.region, self.region_node = "front", None
            self.notes_to(self.root, False)
            self.hold(self.root, index)
            return
        if self.region == "front":
            opens = *drafting.notices, drafting.contents, drafting.preamble, SECTION, drafting.part, CHAPTER
            first = not self.root.leaves and not self.root.children and not drafting.title
            if first and heading_unit and not any(pattern.match(text) for pattern in opens):
                self.root.heading = text  # the document's title
                self.notes_to(self.root, False)
                self.hold(self.root, index)
                return
            if any(pattern.match(text) for pattern in drafting.notices):
                node = self.open_region("front", "notice", f"notice_{self.next_number('notice')}", leaf)
                node.heading = text
                self.notes_to(node, False)
                self.hold(node, index)
                return
        if self.region in ("front", "contents") and drafting.preamble.match(text):
            node = self.open_region("preamble", "preamble", "preamble", leaf)
            self.notes_to(node, False)
            self.hold(node, index)
            return
        opening = ("front", "preamble") if drafting.contents_after_preamble else ("front",)
        if self.region in opening and drafting.contents.match(text):
            node = self.open_region("contents", "contents", "contents", leaf)
            node.heading = text
            self.notes_to(node, False)
            self.hold(node, index)
            return
        if self.region == "contents" and not self.contents_ends(leaf, unit):
            self.contents_entry(index, leaf, unit)
            return
        if self.region in ("front", "contents", "preamble") and not self.body_starts(text, heading_unit):
            self.notes_to(self.region_node or self.root, False)
            self.hold(self.region_node or self.root, index)
            if self.region == "preamble" and drafting.enacting and drafting.enacting.match(text):
                self.enacted = True
            return
        if self.region in ("front", "contents", "preamble"):
            if self.region == "contents" and not drafting.contents_after_preamble:
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
        """A long numbered paragraph with a margin note is the body, not a contents entry; so is a
        chapter or part the contents already list, where they follow the enacting words."""
        if SECTION.match(leaf.text) and (unit.get("notes") or len(leaf.text) > 150):
            return True
        if not self.drafting.contents_after_preamble:
            return False
        chapter, part = CHAPTER.match(leaf.text), self.drafting.part.match(leaf.text)
        if chapter:
            target = f"chap_{chapter.group(1)}"
        elif part:
            target = self.part_locator(part.group(1), self.listed_chapter)
        else:
            return False
        return any(entry.target == target for entry in self.region_node.children)

    def body_starts(self, text: str, heading_unit: bool) -> bool:
        if heading_unit and self.enacted:  # the first group heading after "PASSED by Parliament …"
            return True
        return bool(SECTION.match(text) or self.drafting.part.match(text) or CHAPTER.match(text))

    def part_locator(self, number: str, chapter: str | None) -> str:
        """part_II; within a chapter where parts restart in each, chap_3__part_A."""
        qualified = self.drafting.parts_in_chapters and chapter
        return f"{chapter}__part_{number}" if qualified else f"part_{number}"

    def contents_entry(self, index: int, leaf: Leaf, unit: dict) -> None:
        text, contents, part = leaf.text, self.region_node, self.drafting.part
        found = SECTION.match(text) or part.match(text) or CHAPTER.match(text) or SCHEDULE.match(text)
        self.notes_to(contents, False)
        if not found:
            self.hold(contents, index)
            return
        if found.re is SECTION:
            label, target = f"{found.group(1)}.", f"sec_{found.group(1)}"
        elif found.re is part:
            label, target = f"PART {found.group(1)}", self.part_locator(found.group(1), self.listed_chapter)
        elif found.re is CHAPTER:
            label, target = f"CHAPTER {found.group(1)}", f"chap_{found.group(1)}"
            self.listed_chapter = target
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
        part, chapter = self.drafting.part.match(text), CHAPTER.match(text)
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
            if self.region == "schedules":
                locator = f"{self.schedule.locator}__part_{part.group(1)}"
            else:
                locator = self.part_locator(part.group(1), self.chapter.locator if self.chapter else None)
            self.container = self.add(owner, "part", locator, leaf)
            self.container.label, self.container.heading = f"PART {part.group(1)}", part.group(2).strip()
            self.notes_to(self.container, False)
            self.hold(self.container, index)
            self.awaiting_heading = None if self.container.heading else self.container
            return
        numbered = SECTION.match(text) or SUBSECTION.match(text) or LETTER.match(text)
        if awaiting is not None and not awaiting.heading and not numbered:
            if heading_unit or unit.get("align") == "center":
                reference = self.drafting.reference
                if reference and awaiting.kind == "schedule" and reference.match(text):
                    self.awaiting_heading = awaiting  # its title is the next line
                else:
                    awaiting.heading = text  # "PART III" then "LICENSED INDEPENDENT POWER PRODUCERS"
                self.notes_to(awaiting, False)
                self.hold(awaiting, index)
                return
        if heading_unit:
            following = self.depth_of(self.next_text(index))
            if self.drafting.headings_above and self.region == "body" and following == SECTION_DEPTH:
                self.pending_notes.append(index)  # the section's heading, held by it as a margin note is
                return
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
        definition = self.drafting.definition.match(text) if not in_schedule else None
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


# --- ohada-uniform-act-1 -----------------------------------------------------------------------------

# A division of a Uniform Act as the Journal officiel prints it (AUSCGIE, 2014): its name and number, then
# its title on the same line or on the next heading: "TITRE 1 - DEFINITION DE LA SOCIETE", "LIVRE 1" then
# "CONSTITUTION DE LA SOCIETE COMMERCIALE", "LIVRE 10 - (NOUVEAU) - CAPITAL VARIABLE", "Chapitre 1 -FORME
# DES STATUTS" (the SOMMAIRE's case, which also prints "Chapitre1- ETATS FINANCIERS"); divisions inserted
# by a revision number as articles do ("CHAPITRE 2-1 ATTRIBUTION GRATUITE D'ACTIONS", "LIVRE 4-2"). Each
# name's rank, node kind and locator token.
OHADA_DIVISIONS = {
    "PARTIE": (0, "part", "part"),
    "LIVRE": (1, "division", "book"),
    "TITRE": (2, "title", "title"),
    "SOUS-TITRE": (3, "division", "subtitle"),
    "CHAPITRE": (4, "chapter", "chap"),
    "SECTION": (5, "division", "dvs"),
}
OHADA_DIVISION = re.compile(
    r"^(PARTIE|LIVRE|TITRE|SOUS-TITRE|CHAPITRE|Chapitre|SECTION|Section)(?:\s+|(?=\d))"
    r"(\d{1,3}(?:-\d{1,2})?|PR[ÉE]LIMINAIRE|[IVXL]{1,6})"
    r"(?!\w)\s*[-–—:.]?\s*(.*)$",
    re.S,
)
# "Article 4", "Article 2-1" (inserted after article 2), "Article 626-1-1-1" (after 626-1-1), "Article
# premier", "Article 1er". In a heading, the words after it ("Article 5 : Champ d'application") are the
# article's heading; in a paragraph they are its first alinéa ("Article 198 : A peine de nullité, les
# sociétés …", as articles the 2014 revision rewrote are printed).
OHADA_ARTICLE = re.compile(
    r"^(?:Article|ARTICLE|Art\.)\s+(premier|1er|\d{1,4}(?:-\d{1,3}){0,4})(?![\w-])\s*(?:[:.—–-]\s*(.*))?$",
    re.S,
)
OHADA_ITEM = re.compile(r"^(\d{1,2}\s?°\)?|[a-z]\))(?=\s)")  # "1°)", "2°", "a)": an item's printed number


def folded(text: str) -> str:
    """Letters and digits only, without accents or case: a title as the SOMMAIRE and the body both
    print it."""
    plain = unicodedata.normalize("NFD", text)
    return "".join(c for c in plain if c.isalnum() and not unicodedata.combining(c)).casefold()


class UniformActBuilder(Builder):
    """An OHADA Uniform Act in French. Before the body: the issue's cover and its SOMMAIRE, whose
    division lines are contents entries pointing to the divisions they list; the Act's title ("ACTE
    UNIFORME RELATIF …"); the Council of Ministers' preamble ("Le Conseil des Ministres …", "Vu …"), to
    the words "… dont la teneur suit". Divisions nest by rank (PARTIE, LIVRE, TITRE, SOUS-TITRE,
    CHAPITRE, Section) and are qualified by the divisions they are in, because each restarts its
    numbering (part_1__book_1__title_3__chap_2); articles are numbered through the Act (art_12, and
    art_2-1 inserted after it). Each unit of an article after its heading is an alinéa, numbered in
    order (art_10__al_2): a list's items too, as French drafting counts alinéas, with their printed
    number as label ("1°)"). A heading printed without its number that the SOMMAIRE lists, under
    the division it is in, is that division (the Journal officiel numbers a chapter's first section
    only: "Section 1 - Principe", then "Nature" for Section 2): its locator is the SOMMAIRE's, its
    label empty, and a warning says so. Any other heading groups the articles after it. "Fait à …"
    opens the signatures' notice."""

    CONTENTS = re.compile(r"^(?:sommaire|table\s+des\s+mati[eè]res)\b", re.I)
    TITLE = re.compile(r"^acte\s+uniforme\b", re.I)
    PREAMBLE = re.compile(r"^(?:le\s+conseil\s+des\s+ministres\b|vu\b)", re.I)
    ENACTING = re.compile(r"\bdont\s+la\s+teneur\s+suit\b", re.I)
    SIGNATURE = re.compile(r"^fait\s+[àa]\b", re.I)

    def __init__(self, language: str | None, *args):
        super().__init__(*args)
        self.region = "front"  # front, contents, preamble, body, endmatter
        self.region_node: Node | None = None  # the open contents, preamble or notice
        self.open: list[tuple[int, Node]] = []  # open divisions with their rank
        self.listed: list[tuple[int, str]] = []  # the contents' divisions with their rank and locator
        self.titled: dict[tuple[str, str], tuple[int, str, str, str]] = {}  # (parent, title) -> division
        self.article: Node | None = None
        self.awaiting: Node | None = None  # a division whose title may be on the next heading
        self.caption: Node | None = None
        self.named: set[int] = set()  # the leaves of division and article headings
        self.enacted = False
        self.last_article: tuple[tuple[int, ...], str] | None = None
        self.counts: dict[str, int] = {}
        if language not in (None, "fr"):
            # French only, so far: another language's edition is refused, never read by these patterns.
            message = (
                f"No structure rules for aa-ohada/uniform_act in {language}: this layout is not supported yet"
            )
            self.issues.append(Issue("error", NO_RULES, None, None, message))

    def next_number(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def put(self, node: Node, index: int) -> None:
        self.notes_to(node, False)
        self.hold(node, index)

    def owner(self) -> Node:
        return self.article or (self.open[-1][1] if self.open else self.region_node or self.root)

    def region_opens(self, region: str, kind: str, locator: str, leaf: Leaf) -> Node:
        self.region, self.article, self.open = region, None, []
        self.region_node = self.add(self.root, kind, locator, leaf)
        return self.region_node

    def division_of(self, text: str) -> re.Match | None:
        """A division heading, never words in an alinéa ("Section 2 du présent chapitre …")."""
        found = OHADA_DIVISION.match(text)
        return found if found and len(text) <= 300 and not text.endswith((".", ";", ",")) else None

    def locator(self, found: re.Match, within: list[tuple[int, str]]) -> tuple[int, str, str]:
        """A division's rank, kind and locator, qualified by the division of lower rank it is in."""
        rank, kind, token = OHADA_DIVISIONS[found.group(1).upper()]
        parent = next((locator for r, locator in reversed(within) if r < rank), None)
        number = found.group(2)
        own = f"{token}_{number if re.fullmatch(r'[\d-]+', number) else slug(number)}"  # chap_2-1, as art_2-1
        return rank, kind, f"{parent}__{own}" if parent else own

    def place(self, index: int, leaf: Leaf) -> None:
        unit, text = self.unit(leaf), clean(leaf.text)
        heading_unit = unit.get("kind") == "heading"
        awaiting, self.awaiting = self.awaiting, None
        if leaf.role in ("caption", "table"):
            self.table(index, leaf)
            return
        if self.region == "endmatter":
            self.put(self.region_node, index)
            return
        if self.region == "body" and self.SIGNATURE.match(text):
            node = self.region_opens("endmatter", "notice", f"notice_{self.next_number('notice')}", leaf)
            self.put(node, index)
            return
        if self.region in ("front", "contents") and self.TITLE.match(text):
            self.root.heading = self.root.heading or text  # the Act's title, which ends the contents
            self.region, self.region_node = "front", None
            self.named.add(index)  # on the cover and again before the preamble
            self.put(self.root, index)
            return
        if self.region == "front" and self.CONTENTS.match(text):
            self.region_opens("contents", "contents", "contents", leaf).heading = text
            self.put(self.region_node, index)
            return
        if self.region in ("front", "contents") and self.PREAMBLE.match(text):
            self.put(self.region_opens("preamble", "preamble", "preamble", leaf), index)
            return
        division = self.division_of(text)
        article = OHADA_ARTICLE.match(text)
        if self.region == "contents" and not article:
            self.contents_entry(index, leaf, division)
            return
        if self.region in ("front", "preamble") and not (
            division or article or (self.enacted and heading_unit)
        ):
            self.put(self.region_node or self.root, index)
            self.enacted = self.enacted or (self.region == "preamble" and bool(self.ENACTING.search(text)))
            return
        if self.region != "body":
            self.region, self.region_node = "body", None
        if division:
            self.division(index, leaf, division)
        elif article:
            self.open_article(index, leaf, article, heading_unit)
        elif awaiting is not None and (heading_unit or unit.get("align") == "center"):
            awaiting.heading = text  # "LIVRE 1" then "CONSTITUTION DE LA SOCIETE COMMERCIALE"
            self.named.add(index)
            self.put(awaiting, index)
        elif heading_unit and (listed := self.listed_as(text)):
            self.unnumbered(index, leaf, text, *listed)
        elif heading_unit:
            self.article = None  # a heading that groups the articles after it
            self.put(self.owner(), index)
        elif self.article is not None:
            self.alinea(index, leaf, text)
        else:
            self.issue("warning", "outside_article", leaf, f"“{text[:40]}…” is outside any article")
            self.put(self.owner(), index)

    def contents_entry(self, index: int, leaf: Leaf, found: re.Match | None) -> None:
        contents = self.region_node
        if found is None:
            self.put(contents, index)
            return
        rank, _, target = self.locator(found, self.listed)
        while self.listed and self.listed[-1][0] >= rank:
            self.listed.pop()
        self.listed.append((rank, target))
        entry = self.add(contents, "contents_entry", f"contents__entry_{len(contents.children) + 1}", leaf)
        entry.label, entry.target = clean(leaf.text)[: found.end(2)], target
        self.put(entry, index)
        title = folded(re.sub(r"\s+\d{1,3}$", "", found.group(3)))  # without its page number
        if title:
            parent = target.rsplit("__", 1)[0] if "__" in target else ""
            self.titled[(parent, title)] = (
                rank,
                OHADA_DIVISIONS[found.group(1).upper()][1],
                target,
                entry.label,
            )

    def listed_as(self, text: str) -> tuple[int, str, str, str] | None:
        """The division the SOMMAIRE lists with this title, within one of the open divisions."""
        title = folded(text)
        for parent in [node.locator for _, node in reversed(self.open)] + [""]:
            if (parent, title) in self.titled:
                return self.titled[(parent, title)]
        return None

    def unnumbered(
        self, index: int, leaf: Leaf, text: str, rank: int, kind: str, locator: str, listed: str
    ) -> None:
        while self.open and self.open[-1][0] >= rank:
            self.open.pop()
        parent = self.open[-1][1] if self.open else self.root
        node = self.add(parent, kind, locator, leaf, heading=text)
        self.open.append((rank, node))
        self.article = None
        self.named.add(index)
        self.put(node, index)
        message = f"“{text[:60]}” is printed without its number: the contents list it as {listed} ({locator})"
        self.issue("warning", "number_from_contents", leaf, message)

    def division(self, index: int, leaf: Leaf, found: re.Match) -> None:
        rank, kind, locator = self.locator(found, [(r, node.locator) for r, node in self.open])
        while self.open and self.open[-1][0] >= rank:
            self.open.pop()
        parent = self.open[-1][1] if self.open else self.root
        node = self.add(parent, kind, locator, leaf, label=clean(leaf.text)[: found.end(2)])
        node.heading = found.group(3).strip()
        self.open.append((rank, node))
        self.article = None
        self.awaiting = None if node.heading else node
        self.named.add(index)
        self.put(node, index)

    def open_article(self, index: int, leaf: Leaf, found: re.Match, heading_unit: bool) -> None:
        number = "1" if found.group(1).casefold() in ("premier", "1er") else found.group(1)
        label = leaf.text[: found.end(1)]
        parent = self.open[-1][1] if self.open else self.root
        self.article = self.add(parent, "article", f"art_{number}", leaf, label=label)
        self.named.add(index)
        self.notes_to(self.article, True)
        if heading_unit or not found.group(2):
            self.article.heading = (found.group(2) or "").strip()
            self.hold(self.article, index)
        else:  # "Article 198 : A peine de nullité …": the alinéa starts after the colon
            item = OHADA_ITEM.match(found.group(2))
            alinea = self.add(
                self.article,
                "paragraph",
                f"{self.article.locator}__al_1",
                leaf,
                label=item.group(1) if item else "",
            )
            alinea.trim[index] = len(leaf.text[: found.start(2)].encode())
            self.article.opens_at = leaf.start
            self.hold(alinea, index)
        order = tuple(int(n) for n in number.split("-"))
        if self.last_article and order < self.last_article[0]:
            self.issue("warning", "article_order", leaf, f"{label} follows {self.last_article[1]}")
        self.last_article = (order, label)

    def alinea(self, index: int, leaf: Leaf, text: str) -> None:
        article = self.article
        number = sum(1 for child in article.children if child.kind == "paragraph") + 1
        item = OHADA_ITEM.match(text)
        node = self.add(
            article, "paragraph", f"{article.locator}__al_{number}", leaf, label=item.group(1) if item else ""
        )
        self.put(node, index)
        # A paragraph beginning in lower case may go on from the one before it across a page break; a
        # list's item ("les relations entre associés ;") and a numbered one ("a) dont …") always may.
        if text[:1].islower() and not item and self.unit(leaf).get("kind") != "list_item":
            self.issue(
                "warning",
                "lower_case_alinea",
                leaf,
                f"“{text[:40]}…” begins in lower case: if it goes on from the alinéa before it, join them, "
                f"or the alinéas after it in {article.label} are numbered one too many",
            )

    def table(self, index: int, leaf: Leaf) -> None:
        caption, self.caption = self.caption, None
        if leaf.role == "table" and caption is not None:
            first = self.leaves[caption.leaves[0]]
            if (first.page, first.unit) == (leaf.page, leaf.unit):
                self.hold(caption, index)
                return
        owner = self.owner()
        self.notes_to(owner, False)
        number = self.next_number("table:" + owner.locator)
        node = self.add(owner, "table", f"{owner.locator}__table_{number}", leaf)
        if leaf.role == "caption":
            node.heading, self.caption = leaf.text, node
        self.hold(node, index)

    def repeatable(self) -> set[int]:
        """Division and article headings repeat on other pages ("Section 1 - Dispositions générales"), and
        so do the unnumbered headings between articles ("Principe", "Rémunération"): only a heading that
        opens its page may be a running head."""
        below = {i for i, leaf in enumerate(self.leaves) if leaf.unit != 0}
        return super().repeatable() | self.named | below

    def build(self) -> None:
        super().build()
        for node in self.root.walk():
            if node.kind == "contents_entry" and node.target not in self.locators:
                self.issue(
                    "warning",
                    "contents_unmatched",
                    self.leaves[node.leaves[0]],
                    f"The contents list {node.label}, but the text has no {node.target}",
                )


# --- au-treaty-1, ecowas-act-1, ci-loi-1, cm-law-1, ao-lei-1: articles and their paragraphs ----------

# Ordinal parts of the French editions: "PREMIERE PARTIE : DES DROITS ET DES DEVOIRS".
FRENCH_ORDINALS = {
    "premiere": "1", "deuxieme": "2", "troisieme": "3", "quatrieme": "4", "cinquieme": "5", "sixieme": "6",
}  # fmt: skip
ONE = ("premier", "1er", "primeiro", "unico", "único")  # "Article premier", "Artigo único": article 1
ROMAN_LEVEL = re.compile(r"^\(?((?:x{0,3})(?:ix|iv|v?i{1,3}|v))\)(?=\s)")
LETTER_LEVEL = re.compile(r"^\(?([a-z])\)(?=\s)")
# Angola's heading on the line after "ARTIGO 1.º", alone or before the article's first words.
BRACKETED_HEADING = re.compile(r"^\(([^()]{1,200})\)(?=\s|$)", re.S)


@dataclass(frozen=True)
class Edition:
    """One language edition of an article family: its divisions, by rank, and how a provision opens."""

    divisions: tuple[tuple[str, int, str, str], ...]  # (printed word, rank, kind, token)
    words: tuple[str, ...] = ("ARTICLE", "Article", "Art.")
    provision: str = "article"  # or "section": Cameroon's English text prints "SECTION 7:"


@dataclass(frozen=True)
class ArticleStyle:
    """What an article family prints (ArticleBuilder): the editions calibrated, each a language with its
    words; whether every unit of an article is an alinéa (French drafting) or its paragraphs are
    numbered; how the title, the closing words and any stamp on every page are printed."""

    rules_id: str
    family: str  # as an error names it: "aa-au/charter"
    editions: dict[str, Edition]
    title: re.Pattern
    signature: re.Pattern
    alineas: bool = False
    paragraph: re.Pattern = re.compile(r"^(\d{1,2})\.(?=\s)")  # "1." (or Cameroon's "(1)")
    title_units: tuple[str, ...] = ("heading",)
    running_head: re.Pattern | None = None


ARTICLE_STYLES = {
    AU_TREATY: ArticleStyle(
        AU_TREATY,
        "aa-au/charter or protocol",
        {
            "en": Edition((("PART", 0, "part", "part"), ("CHAPTER", 1, "chapter", "chap"))),
            "fr": Edition((("PARTIE", 0, "part", "part"), ("CHAPITRE", 1, "chapter", "chap"))),
            "pt": Edition(
                (("PARTE", 0, "part", "part"), ("CAPÍTULO", 1, "chapter", "chap")),
                ("ARTIGO", "Artigo", "Art."),
            ),
        },
        title=re.compile(
            r"^(?:african\s+charter\b|protocol\s+(?:to|on|relating)\b|charte\s+africaine\b|protocole\s+relatif\b"
            r"|carta\s+africana\b|protocolo\s+relativo\b)",
            re.I,
        ),
        signature=re.compile(
            r"^(?:adopted\s+by\b|done\s+at\b|in\s+(?:witness|faith)\s+whereof\b|adopt[ée]e?\s+par\b|fait\s+[àa]\b"
            r"|feito\s+em\b|adoptad[oa]\s+(?:pel|em)\b)",
            re.I,
        ),
    ),
    ECOWAS_ACT: ArticleStyle(
        ECOWAS_ACT,
        "aa-ecowas/supplementary_act",
        {"en": Edition((("PART", 0, "part", "part"), ("CHAPTER", 1, "chapter", "chap")))},
        title=re.compile(r"^supplementary\s+act\b", re.I),
        # The signatures open with the closing formula, or, where it is not printed, the first "H. E."
        signature=re.compile(r"^(?:in\s+(?:faith|witness)\s+whereof\b|done\s+at\b|H\.\s?E\.\s)", re.I),
        running_head=re.compile(r"^CEDEAO\s+ECOWAS\b\s*"),
    ),
    CI_LAW: ArticleStyle(
        CI_LAW,
        "ci/law",
        {
            "fr": Edition(
                (
                    ("TITRE", 0, "title", "title"),
                    ("CHAPITRE", 1, "chapter", "chap"),
                    ("SECTION", 2, "division", "dvs"),
                )
            )
        },
        title=re.compile(r"^loi\s+n\s*[°o]", re.I),
        signature=re.compile(r"^fait\s+[àa]\b", re.I),
        alineas=True,
        title_units=("heading", "paragraph"),
    ),
    CM_LAW: ArticleStyle(
        CM_LAW,
        "cm/law",
        {
            "en": Edition(
                (("PART", 0, "part", "part"), ("CHAPTER", 1, "chapter", "chap")), ("SECTION",), "section"
            ),
            "fr": Edition(
                (
                    ("TITRE", 0, "title", "title"),
                    ("CHAPITRE", 1, "chapter", "chap"),
                    ("SECTION", 2, "division", "dvs"),
                ),
                ("ARTICLE", "Article"),
            ),
        },
        title=re.compile(r"^(?:law\s+n|loi\s+n\s*[°o]|relating\s+to\b|relative\s+[àa]\b)", re.I),
        signature=re.compile(r"^yaound[ée]\s*,?\s+(?:le|the)\b", re.I),
        paragraph=re.compile(r"^\((\d{1,2})\)(?=\s)"),
        running_head=re.compile(
            r"^PR[ÉE]SIDENCE\s+DE\s+LA\s+R[ÉE]PUBLIQUE\s+PRESIDENCY\s+OF\s+THE\s+REPUBLIC\b.*$", re.S
        ),
    ),
    AO_LAW: ArticleStyle(
        AO_LAW,
        "ao/law",
        {
            "pt": Edition(
                (
                    ("TÍTULO", 0, "title", "title"),
                    ("CAPÍTULO", 1, "chapter", "chap"),
                    ("SECÇÃO", 2, "division", "dvs"),
                    ("SUBSECÇÃO", 3, "division", "subdvs"),
                ),
                ("ARTIGO", "Artigo", "Art."),
            )
        },
        title=re.compile(r"^lei\s+n\.?\s*[º°o]", re.I),
        signature=re.compile(r"^vista\s+e\s+aprovada\b", re.I),
    ),
}


class ArticleBuilder(Builder):
    """A text of articles, in a family's style (ARTICLE_STYLES): the issue's or cover's front matter;
    the title; everything after it to the first division or article, the preamble ("HAVE AGREED AS
    FOLLOWS", "dont la teneur suit", "a seguinte"); divisions nesting by rank, each qualified by the
    division it is in because numbering restarts in each (part_ii__chap_1), titled on the same line or
    the next heading; articles numbered through the text (art_12, art_2-1 inserted after it, "premier"
    and "único" art_1), their heading the words after the number in a heading ("ARTICLE 3 : COMPETENCE
    DE LA COUR") or the bracketed line after it ("(Objecto)"), their first words in a paragraph unit
    ("Art. 2. — La présente loi …") the article's own. In French drafting every later unit is an
    alinéa (art_10__al_2), as OHADA's; otherwise numbered paragraphs ("1.", Cameroon's "(1)"), lettered
    items ("(a)", "a)") and roman ones ("(i)") nest (art_7__para_1__subpara_a, cited art. 7(1)(a)), and
    an unnumbered unit stays in the innermost open one. A unit opening with the family's closing words
    ("Fait à …", "Vista e aprovada …") opens the closing notice, which holds the rest of the issue. A
    stamp printed on every page (Cameroon's, ECOWAS's logo text) is reported, for the reviewer to remove.
    Another language's edition is refused, never read by these patterns."""

    def __init__(self, style: ArticleStyle, language: str | None, *args):
        super().__init__(*args)
        self.style = style
        only = next(iter(style.editions.values())) if len(style.editions) == 1 else None
        # A document without its language is read by a family's one edition, as OHADA's French one; a
        # family of several cannot tell which, and asks for it.
        self.edition = style.editions.get(language) if language else only
        self.region = "front"  # front, preamble, body, endmatter
        self.region_node: Node | None = None
        self.open: list[tuple[int, Node]] = []
        self.provision: Node | None = None
        self.levels: list[tuple[int, Node]] = []  # the open numbered paragraphs, by level
        self.awaiting: Node | None = None  # a division whose title may be on the next heading
        self.heading_due: Node | None = None  # an article whose heading may be on the next line
        self.caption: Node | None = None
        self.named: set[int] = set()
        self.unlettered: dict[str, list[Leaf]] = {}
        self.last: tuple[tuple[int, ...], str] | None = None
        self.counts: dict[str, int] = {}
        if self.edition is None:
            message = (
                f"No structure rules for {style.family} in {language}: this layout is not supported yet"
                if language
                else f"{style.family} is read in {', '.join(style.editions)}: set the document's language"
            )
            self.issues.append(Issue("error", NO_RULES, None, None, message))
            self.edition = next(iter(style.editions.values()))  # the text is held, never published
        words = "|".join(re.escape(w) for w, *_ in self.edition.divisions)
        ordinals = "|".join(FRENCH_ORDINALS)
        self.division_pattern = re.compile(
            rf"^(?:({ordinals})\s+({words})|({words})(?:\s+|(?=\d))(\d{{1,3}}(?:-\d{{1,2}})?|[IVXLC]{{1,6}}|"
            rf"PR[ÉE]LIMINAIRE|UNIQUE|[ÚU]NIC[OA]))(?!\w)\s*[-–—:.]?\s*(.*)$",
            re.S | re.I,
        )
        self.ranks = {
            unicodedata.normalize("NFC", w).upper(): (r, k, t) for w, r, k, t in self.edition.divisions
        }
        provisions = "|".join(re.escape(w) for w in self.edition.words)
        self.provision_pattern = re.compile(
            rf"^(?:{provisions})\s*(\d{{1,4}}(?:-\d{{1,3}}){{0,3}}|premier|1er|primeiro|[úu]nico)(?:\s*\.?\s*[º°]|er)?"
            rf"(?![\w-])(\s*(?:[.:\-–—]+\s*)*)(.*)$",
            re.S | re.I if self.edition.provision == "article" else re.S,
        )

    def next_number(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def put(self, node: Node, index: int) -> None:
        self.notes_to(node, False)
        self.hold(node, index)

    def owner(self) -> Node:
        if self.levels:
            return self.levels[-1][1]
        return self.provision or (self.open[-1][1] if self.open else self.region_node or self.root)

    def place(self, index: int, leaf: Leaf) -> None:
        unit, text = self.unit(leaf), clean(leaf.text)
        heading_unit = unit.get("kind") == "heading"
        awaiting, self.awaiting = self.awaiting, None
        heading_due, self.heading_due = self.heading_due, None
        if leaf.role in ("caption", "table"):
            self.table(index, leaf)
            return
        if self.style.running_head and (stamp := self.style.running_head.match(text)):
            self.issue(
                "warning",
                "running_head",
                leaf,
                f"“{text[: stamp.end()][:60]}” is the stamp printed on every page: remove it, or it is part of "
                f"{self.owner().label or self.owner().locator}",
            )
            text = text[stamp.end() :].strip()
            if not text:
                self.put(self.owner(), index)
                return
        if self.region == "endmatter":
            self.put(self.region_node, index)
            return
        if self.region == "body" and self.style.signature.match(text):
            self.region, self.provision, self.levels, self.open = "endmatter", None, [], []
            self.region_node = self.add(self.root, "notice", f"notice_{self.next_number('notice')}", leaf)
            self.put(self.region_node, index)
            return
        if (
            self.region == "front"
            and not self.root.heading
            and unit.get("kind") in self.style.title_units
            and self.style.title.match(text)
        ):
            self.root.heading = text
            self.named.add(index)
            self.put(self.root, index)
            self.region = "preamble"
            return
        division = self.division_of(text)
        provision = self.provision_of(text, heading_unit)
        if self.region in ("front", "preamble") and not (division or provision):
            if self.region == "preamble" and self.region_node is None:
                self.region_node = self.add(self.root, "preamble", "preamble", leaf)
            self.put(self.region_node or self.root, index)
            return
        if self.region != "body":
            self.region, self.region_node = "body", None
        if division:
            self.division(index, leaf, division)
        elif provision:
            self.open_provision(index, leaf, provision, heading_unit)
        elif awaiting is not None and (heading_unit or unit.get("align") == "center"):
            awaiting.heading = text  # "CHAPTER I" then "HUMAN AND PEOPLES' RIGHTS"
            self.named.add(index)
            self.put(awaiting, index)
        elif heading_due is not None and (found := BRACKETED_HEADING.match(text)):
            heading_due.heading = found.group(1).strip()  # "ARTIGO 1.º" then "(Objecto)"
            self.named.add(index)
            rest = text[found.end() :].strip()  # "(Entrada em vigor) A presente lei …"
            level, number, label = self.numbered(rest) if rest and not self.style.alineas else (0, "", "")
            if level == 1:
                sub = self.open_level(1, number, label, leaf)
                sub.trim[index] = len(leaf.text[: leaf.text.index(rest)].encode())
                heading_due.opens_at = leaf.start
                self.put(sub, index)
            else:
                self.put(heading_due, index)
        elif self.provision is not None:
            self.inside(index, leaf, text, unit)
        else:
            self.issue("warning", "outside_article", leaf, f"“{text[:40]}…” is outside any article")
            self.put(self.owner(), index)

    def division_of(self, text: str) -> tuple[int, str, str, str, str] | None:
        """A division heading (rank, kind, token, number, title), never words in a paragraph."""
        found = self.division_pattern.match(text)
        if not found or len(text) > 300 or text.endswith((".", ";", ",")):
            return None
        if found.group(1):
            word, number = found.group(2), FRENCH_ORDINALS[folded(found.group(1))]
        else:
            word, number = found.group(3), found.group(4)
        rank, kind, token = self.ranks[unicodedata.normalize("NFC", word).upper()]
        return rank, kind, token, number, found.group(5).strip()

    def division(self, index: int, leaf: Leaf, found: tuple[int, str, str, str, str]) -> None:
        rank, kind, token, number, title = found
        while self.open and self.open[-1][0] >= rank:
            self.open.pop()
        parent = self.open[-1][1] if self.open else self.root
        own = f"{token}_{number if re.fullmatch(r'[\d-]+', number) else slug(number)}"
        within = parent.locator if parent is not self.root else ""
        node = self.add(parent, kind, f"{within}__{own}" if within else own, leaf)
        node.label = (
            clean(leaf.text)[: len(clean(leaf.text)) - len(title)].strip(" :.-–—")
            if title
            else clean(leaf.text)
        )
        node.heading = title
        self.open.append((rank, node))
        self.provision, self.levels = None, []
        self.awaiting = None if title else node
        self.named.add(index)
        self.put(node, index)

    def provision_of(self, text: str, heading_unit: bool) -> re.Match | None:
        """An article's opening: in a heading, its number alone or with its heading; in a paragraph,
        only with the separator printed after the number, so a sentence citing one is not one."""
        found = self.provision_pattern.match(text)
        if not found or (not heading_unit and found.group(3) and not found.group(2).strip()):
            return None
        return found

    def open_provision(self, index: int, leaf: Leaf, found: re.Match, heading_unit: bool) -> None:
        raw = found.group(1)
        number = "1" if folded(raw) in [folded(w) for w in ONE] else raw
        kind = self.edition.provision
        token = "sec" if kind == "section" else "art"
        # A section's label is its printed number, as the Commonwealth Acts' are ("8": cited s. 8); an
        # article's, its printed opening ("ARTICLE 7", "Art. 2"), as OHADA's.
        label = raw if kind == "section" else clean(leaf.text)[: found.end(1)].strip()
        parent = self.open[-1][1] if self.open else self.root
        node = self.add(parent, kind, f"{token}_{number}", leaf, label=label)
        self.provision, self.levels = node, []
        self.named.add(index)
        self.notes_to(node, True)
        rest = found.group(3)
        if heading_unit or not rest:
            node.heading = rest.strip()
            self.hold(node, index)
            self.heading_due = None if node.heading else node
        else:  # "Art. 2. — La présente loi …": the article's first words, after its number
            offset = len(leaf.text[: found.start(3)].encode())
            if self.style.alineas:
                alinea = self.add(node, "paragraph", f"{node.locator}__al_1", leaf, label=self.item(rest))
                alinea.trim[index], node.opens_at = offset, leaf.start
                self.hold(alinea, index)
            elif (numbered := self.style.paragraph.match(rest)) is not None:
                sub = self.open_level(1, numbered.group(1), rest[: numbered.end()], leaf)
                sub.trim[index], node.opens_at = offset, leaf.start
                self.hold(sub, index)
            else:
                self.hold(node, index)
        order = tuple(int(n) for n in number.split("-"))
        if self.last and order < self.last[0]:
            self.issue("warning", "article_order", leaf, f"{label} follows {self.last[1]}")
        self.last = (order, label)

    def item(self, text: str) -> str:
        found = OHADA_ITEM.match(text)
        return found.group(1) if found else ""

    def inside(self, index: int, leaf: Leaf, text: str, unit: dict) -> None:
        """A unit of the open article: an alinéa, or a numbered paragraph or item, or words of the
        innermost one open."""
        provision = self.provision
        if self.style.alineas:
            number = sum(1 for child in provision.children if child.kind == "paragraph") + 1
            node = self.add(
                provision, "paragraph", f"{provision.locator}__al_{number}", leaf, label=self.item(text)
            )
            self.put(node, index)
            return
        level, number, label = self.numbered(text)
        if level:
            self.put(self.open_level(level, number, label, leaf), index)
            return
        owner = self.owner()
        if unit.get("kind") == "list_item":  # an item whose letter the reading did not keep
            self.unlettered.setdefault(owner.locator, []).append(leaf)
        self.put(owner, index)

    def numbered(self, text: str) -> tuple[int, str, str]:
        """A unit's level and number: 1 a numbered paragraph, 2 a lettered item, 3 a roman one (0: none).
        "(i)", "(v)" and "(x)" after "(h)", "(u)" and "(w)" go on the letters."""
        if found := self.style.paragraph.match(text):
            return 1, found.group(1), text[: found.end()]
        letter, roman = LETTER_LEVEL.match(text), ROMAN_LEVEL.match(text)
        last = next((n.label for lvl, n in reversed(self.levels) if lvl == 2), "")
        continues = letter and last and ord(letter.group(1)) == ord(number_of(last)[-1:] or "`") + 1
        if roman and not continues and (len(roman.group(1)) > 1 or letter is None or self.levels_at(3)):
            return 3, roman.group(1), text[: roman.end()]
        if letter:
            return 2, letter.group(1), text[: letter.end()]
        if roman:
            return 3, roman.group(1), text[: roman.end()]
        return 0, "", ""

    def levels_at(self, level: int) -> bool:
        return any(lvl == level for lvl, _ in self.levels)

    def open_level(self, level: int, number: str, label: str, leaf: Leaf) -> Node:
        while self.levels and self.levels[-1][0] >= level:
            self.levels.pop()
        parent = self.levels[-1][1] if self.levels else self.provision
        if self.edition.provision == "section":
            kind = {1: "subsection", 2: "paragraph", 3: "subparagraph"}[level]
        else:
            kind = {1: "paragraph", 2: "subparagraph", 3: "subparagraph"}[level]
        node = self.add(
            parent, kind, f"{parent.locator}__{BODY_TOKENS[kind]}_{number}", leaf, label=label.strip()
        )
        self.levels.append((level, node))
        return node

    def table(self, index: int, leaf: Leaf) -> None:
        caption, self.caption = self.caption, None
        if leaf.role == "table" and caption is not None:
            first = self.leaves[caption.leaves[0]]
            if (first.page, first.unit) == (leaf.page, leaf.unit):
                self.hold(caption, index)
                return
        owner = self.owner()
        self.notes_to(owner, False)
        node = self.add(
            owner, "table", f"{owner.locator}__table_{self.next_number('table:' + owner.locator)}", leaf
        )
        if leaf.role == "caption":
            node.heading, self.caption = leaf.text, node
        self.hold(node, index)

    def repeatable(self) -> set[int]:
        below = {i for i, leaf in enumerate(self.leaves) if leaf.unit != 0}
        return super().repeatable() | self.named | below

    def build(self) -> None:
        super().build()
        for locator, leaves in self.unlettered.items():
            node = self.locators[locator]
            where = node.label or locator
            self.issue(
                "warning",
                "unlettered_items",
                leaves[0],
                f"{len(leaves)} item{'s' if len(leaves) > 1 else ''} of {where} {'have' if len(leaves) > 1 else 'has'} "
                "no printed letter in the reading: if the page prints (a), (b) …, set each one's number",
            )


# --- judgment-paragraphs-1 ---------------------------------------------------------------------------

# "JUDGMENT", "RULING", "LEAD JUDGMENT DELIVERED BY …", "DISSENTING OPINION OF …", "JUDGMENT (Delivered
# by …)": an opinion, its type and, when printed, who wrote it.
OPINION = re.compile(
    r"^(?:(lead(?:ing)?|majority|concurring|dissenting|minority)\s+)?(judge?ment|ruling|opinion)\.?"
    r"(?:\s+of\s+the\s+court|\s+(?:of|by|(?:delivered|read|written)\s+by)\s+(.+?))?"
    r"(?:\s*\(\s*(?:delivered|read|written)\s+by\s*:?\s*(.+?)\s*\))?\s*:?$",
    re.I | re.S,
)
OPINION_TYPES = {"leading": "lead", "majority": "lead", "minority": "dissenting"}  # else the word itself
ORDERS_NOTHING = ("concurring", "dissenting")  # the orders are the lead opinion's or the court's
DELIVERED = re.compile(r"^\(?\s*(?:delivered|read|written)\s+by\s*:?\s*(.+?)\s*\)?$", re.I | re.S)
ORDER = re.compile(
    r"^(?:(?:the\s+)?(?:final\s+)?orders?(?:\s+of\s+(?:the\s+)?court)?|disposition)\s*[.:]?$", re.I
)
REASONS = re.compile(r"^for\s+(?:all\s+)?(?:these|those|the(?:\s+foregoing|\s+above)?)\s+reasons\b", re.I)
BRACKETED = re.compile(r"^\[(\d{1,4}[A-Za-z]?)\](?=\s|$)")
DOTTED = re.compile(r"^(\d{1,3}[A-Za-z]?)\.(?=\s|$)")
BARE = re.compile(r"^(\d{1,4}[A-Za-z]?)(?=\s|$)")  # a number the reviewer set beside the words: "12"


class JudgmentBuilder(Builder):
    """A judgment or ruling. What comes before the first opinion is its header (court, case number,
    parties, coram, date): one notice, whose words are reviewed before any field read from them is
    trusted. Each opinion holds its paragraphs: "[12]", or "12." where no paragraph number is printed
    in brackets, opens one at level 0 or no level, and so does a number the reviewer set beside the
    words; unnumbered words, indented quotations and items numbered within a paragraph are its
    leaves, and headings are the opinion's cross-headings. "ORDER", "DISPOSITION" or "For these
    reasons …" opens the operative orders, which a concurring or dissenting opinion never holds: an
    ORDER heading there is the court's own."""

    def __init__(self, serialized: Serialized, units: dict):
        super().__init__(serialized, units)
        self.header: Node | None = None
        self.opinion: Node | None = None
        self.paragraph: Node | None = None
        self.order: Node | None = None
        self.caption: Node | None = None
        self.last: dict[str, tuple[int, str]] = {}  # each opinion's last paragraph number and label
        self.item = 0  # the last item numbered within the open paragraph
        self.counts: dict[str, int] = {}
        # Where a paragraph number is printed in brackets, "12." numbers an item, never a paragraph.
        self.bracketed = any(
            leaf.role == "text" and BRACKETED.match(leaf.text) and not self.unit(leaf).get("level")
            for leaf in self.leaves
        )

    def next_number(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def qualified(self, own: str) -> str:
        """The first opinion's paragraphs and orders are unqualified; a later opinion's carry its locator."""
        later = self.opinion is not None and self.opinion.locator != "op_1"
        return f"{self.opinion.locator}__{own}" if later else own

    def owner(self, leaf: Leaf) -> Node:
        if self.order or self.paragraph or self.opinion:
            return self.order or self.paragraph or self.opinion
        if self.header is None:
            self.header = self.add(self.root, "notice", "header", leaf)
        return self.header

    def put(self, node: Node, index: int) -> None:
        self.notes_to(node, False)
        self.hold(node, index)

    def build(self) -> None:
        super().build()
        if self.leaves and not self.counts.get("op"):
            self.issue(
                "warning",
                "no_opinion",
                self.leaves[0],
                "No JUDGMENT, RULING or opinion heading and no paragraph number in brackets: "
                "the text before any orders is all header",
            )

    def place(self, index: int, leaf: Leaf) -> None:
        if leaf.role in ("caption", "table"):
            self.table(index, leaf)
            return
        unit, text = self.unit(leaf), clean(leaf.text)
        level, heading_unit = unit.get("level"), unit.get("kind") == "heading"
        marked = heading_unit or unit.get("align") == "center"
        opinion = OPINION.match(text) if len(text) <= 200 else None
        if opinion and (marked or text[: opinion.end(2)].isupper()):
            self.open_opinion(index, leaf, opinion)
            return
        if ORDER.match(text) and (marked or text.isupper()):
            self.open_order(index, leaf, text)
            return
        delivered = DELIVERED.match(text) if len(text) <= 200 else None
        open_ = self.opinion
        if delivered and open_ and open_.heading and not (open_.author or open_.children or self.order):
            open_.author = delivered.group(1)  # "JUDGMENT" then "(Delivered by …)"
            self.put(open_, index)
            return
        number = self.number(leaf, unit, text, level)
        if number:
            self.open_paragraph(index, leaf, *number)
            return
        ordering = open_ is not None and open_.label not in ORDERS_NOTHING and self.order is None
        if REASONS.match(text) and not level and ordering:
            self.open_order(index, leaf, "")
            return
        if heading_unit and self.order is None:
            self.paragraph = None  # a cross-heading: the opinion goes on, its paragraph ends
        self.put(self.owner(leaf), index)

    def number(self, leaf: Leaf, unit: dict, text: str, level) -> tuple[str, str] | None:
        """The (number, label) of the paragraph a unit opens; None for words within the open node."""
        if level:  # indented: a quotation or a list within the paragraph
            return None
        found = BRACKETED.match(text)
        if not found and re.fullmatch(r"\d{1,4}[A-Za-z]?", clean(unit.get("marker") or "")):
            found = BARE.match(text)
        if found:
            return found.group(1), found.group(0)
        found = DOTTED.match(text)
        if not found or self.bracketed or self.opinion is None or self.order is not None:
            return None
        value, last = int(re.match(r"\d+", found.group(1)).group(0)), self.last.get(self.opinion.locator)
        if level == 0 or last is None or value > last[0]:
            return found.group(1), found.group(0)
        if value not in (1, self.item + 1):
            self.issue(
                "warning",
                "paragraph_order",
                leaf,
                f"“{found.group(0)}” follows paragraph {last[1]}: held in it as a numbered item; "
                "set its level to 0 if it is a paragraph",
            )
        self.item = value
        return None

    def new_opinion(self, leaf: Leaf, kind: str, author: str) -> Node:
        locator = f"op_{self.next_number('op')}"
        self.opinion = self.add(self.root, "opinion", locator, leaf, label=kind, author=author)
        self.paragraph = self.order = None
        return self.opinion

    def open_opinion(self, index: int, leaf: Leaf, found: re.Match) -> None:
        named = (found.group(1) or ("ruling" if found.group(2).casefold() == "ruling" else "")).casefold()
        # A plain "JUDGMENT" or "OPINION" first is the lead opinion; a later one's type is not printed.
        kind = OPINION_TYPES.get(named, named) or ("" if self.counts.get("op") else "lead")
        node = self.new_opinion(leaf, kind, (found.group(3) or found.group(4) or "").strip())
        node.heading = clean(leaf.text)
        self.put(node, index)

    def open_order(self, index: int, leaf: Leaf, heading: str) -> None:
        """The operative orders: within the open lead opinion or ruling, else the court's own."""
        if self.opinion is not None and self.opinion.label in ORDERS_NOTHING:
            self.opinion = None
        self.paragraph = None
        parent, prefix = self.opinion or self.root, self.qualified("")
        locator = self.qualified(f"order_{self.next_number('order:' + prefix)}")
        self.order = self.add(parent, "order", locator, leaf, heading=heading)
        self.put(self.order, index)

    def open_paragraph(self, index: int, leaf: Leaf, number: str, label: str) -> None:
        if self.opinion is None:
            self.new_opinion(leaf, "", "")  # numbered paragraphs with no opinion heading before them
        value, last = int(re.match(r"\d+", number).group(0)), self.last.get(self.opinion.locator)
        if last is not None and value < last[0]:
            self.issue("warning", "paragraph_order", leaf, f"Paragraph {label} follows paragraph {last[1]}")
        self.last[self.opinion.locator] = (value, label)
        self.order, self.item = None, 0
        locator = self.qualified(f"para_{slug(number)}")
        self.paragraph = self.add(self.opinion, "paragraph", locator, leaf, label=label)
        self.put(self.paragraph, index)

    def table(self, index: int, leaf: Leaf) -> None:
        caption, self.caption = self.caption, None
        if leaf.role == "table" and caption is not None:
            first = self.leaves[caption.leaves[0]]
            if (first.page, first.unit) == (leaf.page, leaf.unit):
                self.hold(caption, index)
                return
        owner = self.owner(leaf)
        self.notes_to(owner, False)
        number = self.next_number("table:" + owner.locator)
        node = self.add(owner, "table", f"{owner.locator}__table_{number}", leaf)
        if leaf.role == "caption":
            node.heading, self.caption = leaf.text, node
        self.hold(node, index)
