"""Akoma Ntoso 3.0 export of a reviewed Act.

Akoma Ntoso (OASIS LegalDocML) is the open XML standard for legislation. The reviewed units are
mapped onto its hierarchy: text before the enacting words goes to the preface; PART headings
become parts; numbered units become sections, subsections, paragraphs and sub-paragraphs (a
section's margin note is its heading); other headings become cross-headings; unnumbered text and
tables sit in the content of the unit they follow; everything from the schedules heading on is
kept in a schedules container. The output is well-formed AKN vocabulary; it is not validated
against the OASIS schema here.
"""

import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

NS = "http://docs.oasis-open.org/legaldocml/ns/akn/3.0"
OA = "https://openalvary.org/ns/review/1"
ENACTING = re.compile(r"^(?:BE IT )?ENACTED\b", re.I)
PART = re.compile(r"^PART\s+([IVXLC]+)\b\s*[—–-]?\s*(.*)$")
SCHEDULES = re.compile(r"^(?:SCHEDULES?|First Schedule)\b", re.I)
SECTION = re.compile(r"^(\d{1,3}[A-Z]?)\.\s*(?:[—–-]\s*)?(?:\((\d{1,3}[A-Z]?)\)\s*)?(.*)$", re.S)
SUBSECTION = re.compile(r"^\((\d{1,3}[A-Z]?)\)\s*(.*)$", re.S)
PARAGRAPH = re.compile(r"^\(([a-z]{1,2})\)\s*(.*)$", re.S)
SUBPARAGRAPH = re.compile(r"^\(([ivxl]{2,6})\)\s*(.*)$", re.S)
LEVELS = ["section", "subsection", "paragraph", "subparagraph"]
PREFIX = {"section": "sec", "subsection": "subsec", "paragraph": "para", "subparagraph": "subpara"}

ET.register_namespace("", NS)
ET.register_namespace("oa", OA)


def q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


class Container:
    def __init__(self, tag: str, num: str = "", heading: str = "", eid: str = "", attrs: dict | None = None):
        self.tag, self.num, self.heading, self.eid = tag, num, heading, eid
        self.attrs = attrs or {}
        self.items: list = []  # ("block", Element), ("child", Container) or ("cross", text)

    def add_block(self, element):
        self.items.append(("block", element))

    def add_child(self, child: "Container"):
        self.items.append(("child", child))
        return child

    def element(self) -> ET.Element:
        node = ET.Element(q(self.tag), {**({"eId": self.eid} if self.eid else {}), **self.attrs})
        if self.num:
            ET.SubElement(node, q("num")).text = self.num
        if self.heading:
            ET.SubElement(node, q("heading")).text = self.heading
        if self.tag == "body":  # the body holds only provisions: loose text goes in a container
            for kind, value in self.items:
                if kind == "child":
                    node.append(value.element())
                elif kind == "cross":
                    ET.SubElement(node, q("crossHeading")).text = value
                else:
                    holder = ET.SubElement(node, q("hcontainer"), {"name": "text"})
                    ET.SubElement(holder, q("content")).append(value)
            return node
        children = [i for i, (kind, _) in enumerate(self.items) if kind in {"child", "cross"}]
        if not children:
            content = ET.SubElement(node, q("content"))
            blocks = [value for _, value in self.items] or [paragraph("")]
            content.extend(blocks)
            return node
        first, last = children[0], children[-1]
        before = [v for i, (k, v) in enumerate(self.items) if k == "block" and i < first]
        if before:
            ET.SubElement(node, q("intro")).extend(before)
        for index, (kind, value) in enumerate(self.items):
            if kind == "child":
                node.append(value.element())
            elif kind == "cross":
                ET.SubElement(node, q("crossHeading")).text = value
            elif first < index < last:  # text between provisions: a generic container keeps order
                holder = ET.SubElement(node, q("hcontainer"), {"name": "text"})
                ET.SubElement(holder, q("content")).append(value)
        after = [v for i, (k, v) in enumerate(self.items) if k == "block" and i > last]
        if after:
            ET.SubElement(node, q("wrapUp")).extend(after)
        return node


INLINE = {"bold": "b", "italic": "i", "underline": "u", "superscript": "sup", "subscript": "sub"}
CELL_STATE_TITLES = {"illegible": "Illegible in the original", "unsupported": "Not text: not transcribed"}


def paragraph(text: str, marks: list | None = None, offset: int = 0, tag: str = "p") -> ET.Element:
    """A <p> (or another element with inline content, such as a table's <caption>) whose formatted
    runs become AKN inline elements (<b>, <i>, <u>, <sup>, <sub>)."""
    element = ET.Element(q(tag))
    ranges = [m for m in marks or [] if m.get("type") in INLINE]
    if not ranges:
        element.text = text
        return element
    cuts = sorted(
        {0, len(text), *[max(0, min(len(text), v - offset)) for m in ranges for v in (m["start"], m["end"])]}
    )
    last = None
    for start, end in zip(cuts, cuts[1:], strict=False):
        piece = text[start:end]
        active = [
            INLINE[m["type"]] for m in ranges if m["start"] - offset <= start and m["end"] - offset >= end
        ]
        if not active:
            if last is None:
                element.text = (element.text or "") + piece
            else:
                last.tail = (last.tail or "") + piece
            continue
        outer = inner = ET.Element(q(active[0]))
        for tag in active[1:]:
            inner = ET.SubElement(inner, q(tag))
        inner.text = piece
        element.append(outer)
        last = outer
    return element


def table(unit: dict) -> ET.Element:
    """The table with its caption; each cell's words in a <p>, formatted as in the review. A merged
    cell is written once with rowspan and colspan, as printed; the positions it covers are left out.
    A cell the reviewer marked illegible or unsupported says so in its class and title (TXP-02)."""
    element = ET.Element(q("table"))
    if unit.get("caption"):
        element.append(paragraph(unit["caption"], unit.get("caption_marks"), tag="caption"))
    cell_marks = unit.get("cell_marks") or {}
    cell_states = unit.get("cell_states") or {}
    extent = {(r, c): (rows, columns) for r, c, rows, columns in unit.get("spans") or []}
    covered = {
        (r + dr, c + dc)
        for (r, c), (rows, columns) in extent.items()
        for dr in range(rows)
        for dc in range(columns)
        if dr or dc
    }
    for index, row in enumerate(unit["rows"]):
        tr = ET.SubElement(element, q("tr"))
        for column, cell in enumerate(row):
            if (index, column) in covered:
                continue
            rows, columns = extent.get((index, column), (1, 1))
            attributes = {"rowspan": str(rows)} if rows > 1 else {}
            if columns > 1:
                attributes["colspan"] = str(columns)
            state = cell_states.get(f"{index},{column}")
            if state:
                attributes |= {"class": state, "title": CELL_STATE_TITLES[state]}
            ET.SubElement(tr, q("th" if index < unit.get("header_rows", 0) else "td"), attributes).append(
                paragraph(cell, cell_marks.get(f"{index},{column}"))
            )
    return element


def unit_text(unit: dict) -> str:
    return f"{unit['marker']} {unit['text']}".strip() if unit.get("marker") else unit.get("text", "")


def unit_paragraph(unit: dict) -> ET.Element:
    """The unit as a <p>, keeping its formatting (marks are relative to the text after any marker)."""
    offset = -(len(unit["marker"]) + 1) if unit.get("marker") else 0
    return paragraph(unit_text(unit), unit.get("marks"), offset)


class Builder:
    def __init__(self):
        self.body = Container("body")
        self.part: Container | None = None
        self.stack: list[Container] = []  # open section, subsection, paragraph, sub-paragraph
        self.ids: dict[str, int] = {}

    def eid(self, base: str) -> str:
        count = self.ids.get(base, 0) + 1
        self.ids[base] = count
        return base if count == 1 else f"{base}_{count}"

    def parent_for(self, level: int) -> Container:
        del self.stack[level:]
        if self.stack:
            return self.stack[-1]
        return self.part or self.body

    def open(self, level: int, num: str, heading: str = "") -> Container:
        parent = self.parent_for(level)
        tag = LEVELS[level]
        own = f"{PREFIX[tag]}_{num.strip('.()')}"
        # Section numbers are unique within an Act; lower levels are qualified by their section.
        base = own if tag == "section" or not parent.eid or parent.tag == "part" else f"{parent.eid}__{own}"
        child = parent.add_child(Container(tag, num, heading, self.eid(base)))
        self.stack.append(child)
        return child

    def current(self) -> Container:
        return self.stack[-1] if self.stack else (self.part or self.body)

    def add(self, unit: dict):
        if unit["kind"] == "table":
            self.current().add_block(table(unit))
            return
        text, notes = unit_text(unit), unit.get("notes") or []
        part = PART.match(text)
        if unit["kind"] == "heading" and part:
            self.stack.clear()
            self.part = self.body.add_child(
                Container(
                    "part", f"PART {part.group(1)}", part.group(2).strip(), self.eid(f"part_{part.group(1)}")
                )
            )
            return
        if unit["kind"] == "heading":
            del self.stack[:]  # a cross-heading groups the provisions that follow it
            (self.part or self.body).items.append(("cross", text))
            return
        section = SECTION.match(text)
        if section:
            container = self.open(0, f"{section.group(1)}.", notes[0] if notes else "")
            if section.group(2):
                container = self.open(1, f"({section.group(2)})")
            container.add_block(paragraph(section.group(3).strip()))
            return
        for level, pattern in ((1, SUBSECTION), (3, SUBPARAGRAPH), (2, PARAGRAPH)):
            match = pattern.match(text)
            if match:
                self.open(level, f"({match.group(1)})").add_block(paragraph(match.group(2).strip()))
                return
        self.current().add_block(unit_paragraph(unit))


LEGISLATION = {"act", "bill"}


# ISO 639-1 (BCP 47 primary subtag) to the ISO 639-2 codes Akoma Ntoso uses in its URIs.
LANGUAGES = {
    "en": "eng",
    "fr": "fra",
    "pt": "por",
    "ar": "ara",
    "sw": "swa",
    "ha": "hau",
    "yo": "yor",
    "ig": "ibo",
}


def build(
    act: dict,
    pages: list[dict],
    content_sha256: str,
    country: str | None = None,
    doc_record=None,
    status: str = "suggested",
    steps: dict | None = None,
    details_sha256: str | None = None,
) -> bytes:
    """Acts and bills get the legislative hierarchy; other types an AKN document in reading order.
    With the document's record (document-record-1), the metadata carries its number, dates,
    publication, the steps by which it was made, and the whole record."""
    identity = doc_record.identity if doc_record else None
    country = country or (doc_record.place.code if doc_record else None) or act.get("jurisdiction") or "ng"
    kind = act.get("type", "act")
    units = [unit for page in pages for unit in page["units"]]
    start = next((i for i, u in enumerate(units) if u["kind"] != "table" and ENACTING.match(unit_text(u))), 0)
    end = next(
        (
            i
            for i, u in enumerate(units)
            if i > start and u["kind"] == "heading" and SCHEDULES.match(unit_text(u))
        ),
        len(units),
    )

    root = ET.Element(q("akomaNtoso"))
    tag = kind if kind in LEGISLATION else "doc"
    document = ET.SubElement(root, q(tag), {"name": kind})
    meta = ET.SubElement(document, q("meta"))
    year = str((identity.year if identity and identity.year else None) or act.get("year", ""))
    number = (
        re.sub(r"[^a-z0-9.-]+", "-", identity.numbers[0].value.lower()).strip("-")
        if identity and identity.numbers
        else (re.sub(r"[^a-z0-9-]", "", act["id"].removeprefix(f"{country}-")))
    )
    work = f"/akn/{country}/{kind if kind in LEGISLATION else 'doc'}/{year}/{number}"
    today = datetime.now(UTC).date().isoformat()
    this = next((e for e in doc_record.expressions if e.this_text), None) if doc_record else None
    language = LANGUAGES.get((this.language if this else "en").split("-")[0], "eng")
    events = list(doc_record.events) if doc_record else []
    made = next(
        (
            e.when.value.isoformat()
            for e in events
            if e.kind in ("assented", "promulgated", "signed") and e.when.value
        ),
        None,
    )
    name = identity.title if identity else act["title"]
    publication = doc_record.publications[0] if doc_record and doc_record.publications else None
    identification = ET.SubElement(meta, q("identification"), {"source": "#openalvary"})
    for level, this, uri in (
        ("FRBRWork", f"{work}/!main", work),
        ("FRBRExpression", f"{work}/{language}@/!main", f"{work}/{language}@"),
        ("FRBRManifestation", f"{work}/{language}@/!main.xml", f"{work}/{language}@.akn"),
    ):
        frbr = ET.SubElement(identification, q(level))
        ET.SubElement(frbr, q("FRBRthis"), {"value": this})
        ET.SubElement(frbr, q("FRBRuri"), {"value": uri})
        ET.SubElement(
            frbr,
            q("FRBRdate"),
            {
                "date": (made or f"{year}-01-01") if level == "FRBRWork" else today,
                "name": ("assent" if made else "year") if level == "FRBRWork" else "review",
            },
        )
        ET.SubElement(
            frbr, q("FRBRauthor"), {"href": "#legislature" if level == "FRBRWork" else "#openalvary"}
        )
        if level == "FRBRWork":
            ET.SubElement(frbr, q("FRBRcountry"), {"value": country})
            ET.SubElement(frbr, q("FRBRnumber"), {"value": number})
            ET.SubElement(frbr, q("FRBRname"), {"value": name})
        if level == "FRBRExpression":
            ET.SubElement(frbr, q("FRBRlanguage"), {"language": language})
    if publication and publication.when and publication.when.value:
        ET.SubElement(
            meta,
            q("publication"),
            {
                "date": publication.when.value.isoformat(),
                "name": "gazette",
                "showAs": publication.name,
                **({"number": publication.number} if publication.number else {}),
            },
        )
    labels = steps or {}
    stepped = [
        (re.sub(r"[^A-Za-z0-9_-]", "_", e.step), e.when.value.isoformat(), labels.get(e.step, e.step))
        for e in events
        if e.step and e.when.value
    ]
    if made:
        lifecycle = ET.SubElement(meta, q("lifecycle"), {"source": "#openalvary"})
        ET.SubElement(
            lifecycle,
            q("eventRef"),
            {"eId": "evt_1", "date": made, "source": "#original", "type": "generation"},
        )
    if stepped:
        workflow = ET.SubElement(meta, q("workflow"), {"source": "#openalvary"})
        for index, (eid, when, _) in enumerate(stepped, 1):
            ET.SubElement(workflow, q("step"), {"eId": f"step_{index}", "date": when, "refersTo": f"#{eid}"})
    references = ET.SubElement(meta, q("references"), {"source": "#openalvary"})
    ET.SubElement(
        references,
        q("TLCOrganization"),
        {"eId": "openalvary", "href": "/ontology/organization/openalvary", "showAs": "Open Alvary"},
    )
    ET.SubElement(
        references,
        q("TLCOrganization"),
        {
            "eId": "legislature",
            "href": f"/ontology/organization/{country}/legislature",
            "showAs": doc_record.issued_by[0].name if doc_record and doc_record.issued_by else "Legislature",
        },
    )
    if made:
        ET.SubElement(
            references,
            q("original"),
            {
                "eId": "original",
                "href": f"{work}/{language}@",
                "showAs": name,
            },
        )
    for eid, _, label in stepped:
        ET.SubElement(
            references,
            q("TLCProcess"),
            {"eId": eid, "href": f"/ontology/process/{country}/{eid}", "showAs": label},
        )
    proprietary = ET.SubElement(meta, q("proprietary"), {"source": "#openalvary"})
    ET.SubElement(
        proprietary,
        f"{{{OA}}}review",
        {"contentSha256": content_sha256, **({"detailsSha256": details_sha256} if details_sha256 else {})},
    )
    if doc_record:
        holder = ET.SubElement(
            proprietary, f"{{{OA}}}record", {"standard": doc_record.schema_version, "status": status}
        )
        holder.text = doc_record.model_dump_json(exclude_none=True)

    if kind not in LEGISLATION:
        # A general document: its text in reading order, headings marked, tables kept.
        main = ET.SubElement(document, q("mainBody"))
        for unit in units:
            if unit["kind"] == "table":
                main.append(table(unit))
            elif unit["kind"] == "heading":
                holder = ET.SubElement(main, q("hcontainer"), {"name": "heading"})
                ET.SubElement(holder, q("heading")).text = unit_text(unit)
            else:
                main.append(unit_paragraph(unit))
                for note in unit.get("notes") or []:
                    ET.SubElement(main, q("p"), {"class": "marginNote"}).text = note
        ET.indent(root)
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)
    preface = ET.SubElement(document, q("preface"))
    for unit in units[:start]:
        preface.append(table(unit) if unit["kind"] == "table" else unit_paragraph(unit))
    if start < len(units) and ENACTING.match(unit_text(units[start])):
        formula = ET.SubElement(
            ET.SubElement(document, q("preamble")), q("formula"), {"name": "enactingText"}
        )
        formula.append(paragraph(unit_text(units[start])))
        start += 1
    builder = Builder()
    for unit in units[start:end]:
        builder.add(unit)
    if end < len(units):
        schedules = builder.body.add_child(
            Container("hcontainer", eid="schedules", attrs={"name": "schedules"})
        )
        for unit in units[end:]:
            schedules.add_block(table(unit) if unit["kind"] == "table" else unit_paragraph(unit))
    document.append(builder.body.element())
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
