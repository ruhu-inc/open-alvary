"""The National Industrial Court's judgment pages as review documents (CPR-07).

    python -m ingestion.nicn read PAGE.html                    # one stored page as the workbench reads it
    python -m ingestion.nicn prepare --pages DIR --out FOLDER  # a few stored pages (<record id>.html) into a
                                                               # local collection: a check; the cloud job
                                                               # prepares them all (ingestion.prepare_job,
                                                               # PREPARE_COUNTRY=ng)

Each record page of the court's portal (nicnadr.gov.ng/judgement/judgement.php?id=N) is the judgment or
ruling itself, published as a web page: its text is the only reading there is, so every review page
says so (``one_reading``) and is reviewed whole. The reader keeps the words exactly as published and
leaves out the portal's furniture (its navigation, "PORTAL", "GET CTC OF THIS JUDGMENT", a copied PDF
viewer's toolbar, the copyright line, the online converters' advertising links and trial notices,
rules drawn with characters, and on older records the portal's own summary card: suit number,
parties, date, judges), which it keeps beside the reading as furniture and uses as evidence for the
details. It reads the four shapes the portal publishes:

- Word's HTML (most records since 2016): paragraphs, Word's list paragraphs, ordered lists, tables;
- a PDF viewer's text layer copied into the page (older records): one positioned line per printed
  line, joined here into paragraphs;
- plain text lines (older records): joined the same way;
- the whole text in one paragraph or one table cell, its paragraph breaks lost: cut here at sentence
  ends, flagged for the reviewer to set the paragraphs.

The header as printed (court, division, "HOLDEN AT", the judge, date, suit number, parties,
representation) becomes heading and paragraph units, up to the decision's heading ("JUDGMENT",
"RULING"); the body becomes paragraphs whose printed numbers ("1.", "[12]") are kept as markers,
cross-headings, list items and indented quotations (indent level 1), as judgment-paragraphs-1 reads
them (workbench.structure, CPR-04). A judgment has no printed pages: the reading's pages are sections
of the web page, of about WORDS_PER_PAGE words, cut between units.

Nothing is corrected silently. The one encoding fault the survey found, the naira sign published as
"?" before an amount, is restored as "₦" and flagged with the published form; other faults found
(text encoded twice, such as "â€™" for "’", or bytes that are not UTF-8) are flagged and left as
published; words the page itself masks with asterisks, text that stops mid-sentence (the portal
holds only part of some judgments) and images the page refers to but never published are flagged
high, for the reviewer to settle against the court's certified copy; so is another court's decision
posted on the portal (a Court of Appeal judgment), whose details are that court's.

The review documents go to collections/ng/ in the corpus bucket in the layout of Rwanda's collection
(BucketCollection), one document per record page, its id from the suit number and the date delivered
(document_ids).

The details are suggested from the page and its catalogue entry (ingestion.nigeria.nicn_entry), each
with the words it was read from, in the record's own keys (workbench.record_view): the court, its
division and seat, the suit numbers, the parties and their counsel, the judge, the date delivered,
whether it is a judgment or a ruling, and where and when the page was fetched. They are a first draft
for the reviewer, never recorded as reviewed; the legal facts (outcome, standing as precedent, legal
status) are left to the reviewer.
"""

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup, Comment, Doctype, NavigableString, ProcessingInstruction, Tag

COUNTRY = "ng"
SOURCE = "nicn-judgments"
COURT = "National Industrial Court of Nigeria"
PUBLICATION = "National Industrial Court of Nigeria: judgment portal"
# The reader's version: a page prepared by an earlier one is prepared again (BucketCollection.done).
READER = "nicn-html-2026-10-25.1"
WORDS_PER_PAGE = 700  # a review page: about one printed page of a judgment
UNITS_PER_PAGE = 120  # well under the workbench's 400 units a page
UNIT_CHARACTERS = 6000  # a unit longer than this is cut at a sentence end (the workbench takes 8192)

# --- the page's text, block by block --------------------------------------------------------------

SPACE = re.compile(r"[\s\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+")
INVISIBLE = re.compile(r"[\u00ad\u200b\u200c\u200d\u2060\ufeff]")  # soft hyphens, zero-width marks
# Elements with no words of the judgment, and the elements that set their words apart as a block.
SKIPPED = set(
    "script style noscript input button select textarea iframe object embed svg canvas head title meta link map".split()
)
BLOCK_TAGS = set(
    "p div li h1 h2 h3 h4 h5 h6 blockquote pre center address dd dt dl section article header footer main aside "
    "nav form fieldset figure figcaption hr table ol ul tr td th tbody thead tfoot".split()
)
MARK_TAGS = {
    "b": "bold",
    "strong": "bold",
    "i": "italic",
    "em": "italic",
    "cite": "italic",
    "u": "underline",
    "ins": "underline",
    "sup": "superscript",
    "sub": "subscript",
}
# The PDF viewer's toolbar, sidebar and dialogs copied with its text layer into older records.
VIEWER = re.compile(
    r"toolbar|sidebar|findbar|pageNumber|numPages|scaleSelect|loadingBar|errorWrapper|overlay|"
    r"printContainer|thumbnailView|outlineView|attachmentsView|doorHanger|presentationMode",
    re.I,
)
# What the online converters the court's staff used add to a page: an advertising link, a trial notice.
CONVERTER = re.compile(
    r"(?:Created\s+with\s+WordToHTML\.net(?:\s+trial)?\.?|Email\s+Signature\s+(?:Generator|Editor|Maker)|"
    r"(?:Converted|Created)\s+(?:by|with)\s+\S+\.(?:com|net|org)\b.{0,40})",
    re.I,
)
ADVERT_LINK = re.compile(r"utm_source=wth|signatureforemail\.com", re.I)
POSITIONED = re.compile(r"(?<![-\w])top:\s*(-?[\d.]+)px", re.I)
LEFT = re.compile(r"(?<![-\w])left:\s*(-?[\d.]+)px", re.I)


@dataclass
class Block:
    """A block of the page's text: its words with their formatting, and how the page sets it."""

    text: str
    marks: list = field(default_factory=list)  # [{"type", "start", "end"}] over text
    tag: str = "p"
    css: str = ""  # Word's paragraph class (MsoListParagraph …)
    left: float = 0.0  # margin-left, inches
    first: float = 0.0  # text-indent, inches
    align: str | None = None
    list_marker: str | None = None  # the number an ordered list draws (not in the text)
    list_depth: int = 0
    quote: bool = False  # inside <blockquote>
    line: bool = False  # one printed line (a PDF viewer's copy, plain text lines, a <br>)
    top: float | None = None  # a PDF viewer line's place on its page
    rows: list | None = None  # a table: rows of {"text", "row_span", "col_span"}
    ordinal: int = 0  # its place among the page's blocks


def _length(value: str) -> float:
    """A CSS length in inches (Word writes in, pt, cm; PDF viewers px)."""
    found = re.fullmatch(r"\s*(-?[\d.]+)\s*(in|pt|cm|mm|px|em|pc)?\s*", value or "")
    if not found:
        return 0.0
    try:
        number = float(found.group(1))
    except ValueError:
        return 0.0
    per_inch = {"in": 1, "pt": 72, "cm": 2.54, "mm": 25.4, "px": 96, "em": 6, "pc": 6}
    return number / per_inch.get(found.group(2) or "px", 96)


def _style(tag: Tag) -> dict[str, str]:
    out = {}
    for part in (tag.get("style") or "").split(";"):
        name, _, value = part.partition(":")
        if name.strip():
            out[name.strip().lower()] = value.strip().lower()
    return out


def _marks_of(tag: Tag, active: dict) -> dict:
    """The formatting a tag turns on or off for its words."""
    marks = dict(active)
    kind = MARK_TAGS.get(tag.name)
    if kind:
        marks[kind] = True
    style = _style(tag)
    weight = style.get("font-weight", "")
    if weight in ("bold", "bolder") or re.fullmatch(r"[6-9]00", weight):
        marks["bold"] = True
    elif weight in ("normal", "lighter") or re.fullmatch(r"[1-5]00", weight):
        marks["bold"] = False
    if "italic" in style.get("font-style", ""):
        marks["italic"] = True
    elif style.get("font-style") == "normal":
        marks["italic"] = False
    decoration = style.get("text-decoration", "") + style.get("text-decoration-line", "")
    if "underline" in decoration:
        marks["underline"] = True
    vertical = style.get("vertical-align", "")
    if vertical in ("super", "sub"):
        marks["superscript" if vertical == "super" else "subscript"] = True
    return marks


class Words:
    """A block's words as they are gathered: runs of white space made one space, each run's formatting
    kept as ranges over the text."""

    def __init__(self):
        self.text = ""
        self.ranges: list[tuple[int, int, str]] = []

    def add(self, raw: str, marks: dict) -> None:
        piece = SPACE.sub(" ", INVISIBLE.sub("", raw))
        if not piece:
            return
        if piece.startswith(" ") and (not self.text or self.text.endswith(" ")):
            piece = piece[1:]
        if not piece:
            return
        start = len(self.text)
        self.text += piece
        core_start = start + (len(piece) - len(piece.lstrip(" ")))
        core_end = start + len(piece.rstrip(" "))
        if core_end > core_start:
            for kind in ("bold", "italic", "underline", "superscript", "subscript"):
                if marks.get(kind):
                    self.ranges.append((core_start, core_end, kind))

    def empty(self) -> bool:
        return not self.text.strip()

    def take(self) -> tuple[str, list]:
        lead = len(self.text) - len(self.text.lstrip(" "))
        text = self.text.strip(" ")
        merged: dict[str, list[list[int]]] = {}
        for start, end, kind in sorted(self.ranges, key=lambda r: (r[2], r[0])):
            start, end = max(0, start - lead), min(len(text), end - lead)
            if start >= end:
                continue
            spans = merged.setdefault(kind, [])
            if spans and start <= spans[-1][1] + 1 and not text[spans[-1][1] : start].strip():
                spans[-1][1] = max(spans[-1][1], end)
            else:
                spans.append([start, end])
        marks = [{"type": kind, "start": s, "end": e} for kind, spans in merged.items() for s, e in spans]
        marks.sort(key=lambda m: (m["start"], m["type"]))
        return text, marks if len(marks) <= 200 else []


class Page:
    """A record page read into blocks, with the furniture left out and what the page lacks noted."""

    def __init__(self, raw: bytes):
        self.raw = raw
        try:
            self.html = raw.decode("utf-8")
            self.invalid_bytes = False
        except UnicodeDecodeError:
            self.html = raw.decode("utf-8", "replace")
            self.invalid_bytes = True
        self.soup = BeautifulSoup(self.html, "html.parser")
        self.blocks: list[Block] = []
        self.furniture: list[str] = []
        self.images: list[dict] = []  # images the judgment refers to: [{"src", "width", "height", "at"}]
        self.viewer_pages: int | None = None  # the page count a copied PDF viewer showed
        self.title = SPACE.sub(" ", self.soup.title.get_text(" ")).strip() if self.soup.title else ""
        self._chrome()
        self.container = self._container()
        if self.container is not None:
            self._walk(self.container, {}, dict(left=0.0, quote=False, depth=0), root=True)

    def _container(self) -> Tag | None:
        """The part of the page that holds the judgment: the portal's content column, else the body."""
        return self.soup.select_one("div.light-background div.col-12") or self.soup.body or self.soup

    def _chrome(self) -> None:
        """The portal's own words around the judgment (its buttons, its copyright line), kept as furniture
        and taken out of the page before it is read; its side panels and scripts left out."""
        for element in self.soup.select("nav, footer, script, style, noscript"):
            if element.get("id") == "copyright" or element.select_one("#copyright"):
                continue
            element.decompose()
        for element in self.soup.find_all(["button", "a", "li"]):
            if element.decomposed:
                continue
            text = SPACE.sub(" ", element.get_text(" ")).strip()
            if text.upper() in ("PORTAL", "GET CTC OF THIS JUDGMENT"):
                if text.upper() not in (f.upper() for f in self.furniture):
                    self.furniture.append(text)
                element.decompose()
        for element in self.soup.select("#copyright, footer"):
            if element.decomposed:
                continue
            words = SPACE.sub(" ", element.get_text(" ")).strip()
            if words and words not in self.furniture:
                self.furniture.append(words)
            element.decompose()

    def _attributes(self, tag: Tag, context: dict) -> dict:
        style = _style(tag)
        align = style.get("text-align") or (tag.get("align") or "").lower() or None
        if tag.name == "center":
            align = "center"
        return {
            "left": context["left"]
            + _length(style.get("margin-left", ""))
            + _length(style.get("padding-left", "")),
            "first": _length(style.get("text-indent", "")),
            "align": align if align in ("left", "center", "right", "justify") else None,
            "css": " ".join(tag.get("class") or []),
        }

    def _flush(self, words: Words, base: dict, **extra) -> None:
        if words.empty():
            return
        text, marks = words.take()
        if re.fullmatch(r"[-_=*~.·•\s]{3,}", text) and len(text.replace(" ", "")) >= 5:
            self.furniture.append(text)  # a rule drawn with characters: no words
            return
        if re.fullmatch(r"(?i)(?:page\s+)?\d{1,3}\s+of\s+\d{1,3}", text):
            self.furniture.append(text)  # a page counter copied from the PDF ("1 of 17")
            return
        if CONVERTER.fullmatch(text):
            self.furniture.append(text)  # a converter's own line ("Created with WordToHTML.net trial.")
            return
        self.blocks.append(Block(text=text, marks=marks, ordinal=len(self.blocks), **{**base, **extra}))

    def _walk(self, node: Tag, marks: dict, context: dict, root: bool = False) -> None:
        """The blocks of a block element: its own words between its block children, and theirs. `context`
        holds what its ancestors set (their margins added up, a quotation, a list's depth)."""
        own = self._attributes(node, context)
        base = {"tag": node.name, **own, "quote": context["quote"], "list_depth": context["depth"]}
        positioned = POSITIONED.search(node.get("style") or "")
        if positioned:
            left = LEFT.search(node.get("style") or "")
            base |= {"line": True, "top": float(positioned.group(1))}
            base["left"] = float(left.group(1)) / 96 if left else 0.0
        words = Words()
        broken = False  # a <br> in this block: its parts are printed lines

        def visit(child, marks: dict) -> None:
            nonlocal words, broken
            if isinstance(child, (Comment, Doctype, ProcessingInstruction)):
                return
            if isinstance(child, NavigableString):
                text = str(child)
                if root and "\n" in text.strip() and not marks:
                    # Plain text lines straight in the page (older records): each line a printed line.
                    lines = text.split("\n")
                    for k, line in enumerate(lines):
                        words.add(line, marks)
                        if k < len(lines) - 1:
                            self._flush(words, base, line=True)
                            words = Words()
                    return
                words.add(text, marks)
                return
            if not isinstance(child, Tag):
                return
            name = child.name.lower()
            if name in SKIPPED:
                if (
                    name == "input"
                    and child.get("id") == "pageNumber"
                    and str(child.get("max", "")).isdigit()
                ):
                    self.viewer_pages = int(child["max"])
                return
            if name == "img":
                self.images.append(
                    {
                        "src": child.get("src") or "",
                        "width": int(re.sub(r"\D", "", str(child.get("width") or "")) or 0),
                        "height": int(re.sub(r"\D", "", str(child.get("height") or "")) or 0),
                        "at": len(self.blocks),
                    }
                )
                return
            if ":" in name and name != "o:p":
                return  # Word's drawing markup (v:shape …): no words
            if name == "a" and ADVERT_LINK.search(child.get("href") or ""):
                text = SPACE.sub(" ", child.get_text(" ")).strip()
                if text:
                    self.furniture.append(text)  # a converter's advertising link
                return
            identity = " ".join([child.get("id") or "", *(child.get("class") or [])])
            if identity.strip() and VIEWER.search(identity):
                pages = child.find("input", id="pageNumber")
                if pages is not None and str(pages.get("max", "")).isdigit():
                    self.viewer_pages = int(pages["max"])
                text = SPACE.sub(" ", child.get_text(" ")).strip()
                if text:
                    self.furniture.append(text)
                return
            if name == "br":
                self._flush(words, base, line=True)
                words, broken = Words(), True
                return
            if name in BLOCK_TAGS:
                self._flush(words, base)
                words = Words()
                if name == "table":
                    self._table(child, marks, {**context, "left": own["left"]})
                elif name in ("ol", "ul"):
                    self._list(child, marks, {**context, "left": own["left"]})
                elif name == "hr":
                    pass
                else:
                    inner = {
                        **context,
                        "left": own["left"],
                        "quote": context["quote"] or name == "blockquote",
                    }
                    self._walk(child, _marks_of(child, marks), inner)
                return
            child_marks = _marks_of(child, marks)
            for grandchild in child.children:
                visit(grandchild, child_marks)

        for child in node.children:
            visit(child, marks)
        self._flush(words, base, **({"line": True} if broken else {}))

    def _list(self, node: Tag, marks: dict, context: dict) -> None:
        """An HTML list: each item a block with the number the list draws beside it."""
        style = _style(node)
        kind = (node.get("type") or "").strip() or style.get("list-style-type", "").strip()
        number = int(re.sub(r"\D", "", str(node.get("start") or "")) or 1)
        inner = {**context, "depth": context["depth"] + 1, "left": self._attributes(node, context)["left"]}
        for item in node.find_all("li", recursive=False):
            if str(item.get("value", "")).isdigit():
                number = int(item["value"])
            before = len(self.blocks)
            self._walk(item, _marks_of(item, marks), inner)
            if len(self.blocks) > before:
                self.blocks[before].list_marker = _list_marker(node.name, kind, number)
            number += 1

    def _table(self, node: Tag, marks: dict, context: dict) -> None:
        """A table of two or more cells is a table; a one-cell table only lays out what it holds."""
        rows = [
            row.find_all(["td", "th"], recursive=False)
            for row in node.find_all("tr")
            if row.find_parent("table") is node
        ]
        cells = [cell for row in rows for cell in row]
        if len(cells) <= 1:
            for cell in cells:
                self._walk(cell, marks, context)
            return
        out = []
        for row in rows:
            out.append(
                [
                    {
                        "text": SPACE.sub(" ", INVISIBLE.sub("", cell.get_text(" "))).strip(),
                        "row_span": max(1, int(re.sub(r"\D", "", str(cell.get("rowspan") or "")) or 1)),
                        "col_span": max(1, int(re.sub(r"\D", "", str(cell.get("colspan") or "")) or 1)),
                    }
                    for cell in row
                ]
            )
        out = [row for row in out if row]
        if any(cell["text"] for row in out for cell in row):
            self.blocks.append(
                Block(text="", tag="table", rows=out, left=context["left"], ordinal=len(self.blocks))
            )


def _roman(number: int) -> str:
    out = ""
    for value, letters in (
        (1000, "m"),
        (900, "cm"),
        (500, "d"),
        (400, "cd"),
        (100, "c"),
        (90, "xc"),
        (50, "l"),
        (40, "xl"),
        (10, "x"),
        (9, "ix"),
        (5, "v"),
        (4, "iv"),
        (1, "i"),
    ):
        while number >= value:
            out, number = out + letters, number - value
    return out


def _list_marker(tag: str, kind: str, number: int) -> str:
    """The number a browser draws beside a list item ("1.", "e.", "iv.", "•")."""
    if tag == "ul":
        return "•"
    if kind in ("a", "lower-alpha", "lower-latin"):  # type="a" and type="A" differ by case
        return f"{chr(ord('a') + (number - 1) % 26)}."
    if kind in ("A", "upper-alpha", "upper-latin"):
        return f"{chr(ord('A') + (number - 1) % 26)}."
    if kind in ("i", "lower-roman"):
        return f"{_roman(number)}."
    if kind in ("I", "upper-roman"):
        return f"{_roman(number).upper()}."
    return f"{number}."


# --- from blocks to the units of a judgment ---------------------------------------------------------

# The portal's summary card on older records ("Suit No: … Petitioner: … And Respondent: … Date
# Delivered: … Judge(s): … Judgment Delivered"), drawn from its catalogue above the judgment.
SUMMARY_START = re.compile(r"^Suit\s+No\s*:", re.I)
SUMMARY_END = re.compile(r"^(?:Judge?ments?|Rulings?)\s+Delivered\b", re.I)
SUMMARY_TEXT = re.compile(
    r"^\s*(?:IN\s+)?THE\s+NATIONAL\s+INDUSTRIAL\s+COURT\s+OF\s+NIGERIA\s*[-–—_=*\s]*Suit\s+No\s*:.*?"
    r"\b(?:Judge?ments?|Rulings?)\s+Delivered\b\s*",
    re.I | re.S,
)
# "IN THE NATIONAL INDUSTRIAL COURT OF NIGERIA", misspelt too ("INDUSTRAL").
COURT_LINE = re.compile(r"^(?:IN\s+)?THE\s+NATIONAL\s+(?:IND[A-Z]{4,9}\s+)?COURT(?:\s+OF\s*NIGERIA)?\b", re.I)
# Another court's judgment posted on the portal (a Court of Appeal decision on a labour matter).
OTHER_COURT = re.compile(
    r"^(?:IN\s+)?THE\s+(?P<court>COURT\s+OF\s+APPEAL|SUPREME\s+COURT|FEDERAL\s+HIGH\s+COURT|HIGH\s+COURT"
    r"|INDUSTRIAL\s+ARBITRATION\s+PANEL)(?:\s+OF\s+(?:THE\s+)?[A-Z][A-Za-z .]{2,40})?\b",
    re.I,
)
SPOKEN_DATE = re.compile(
    r"^(?:ON\s+)?(?:(?:MON|TUES|WEDNES|THURS|FRI|SATUR|SUN)DAY\s*,?\s*)?(?:THE\s+)?\d{1,2}\s*(?:ST|ND|RD|TH)?\s+"
    r"(?:DAY\s+OF\s+)?(?:JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)"
    r"\s*,?\s*(?:19|20)\d\d\.?$",
    re.I,
)
EDITORIAL = re.compile(r"(?i)ratio\s+decidendi|head\s*notes?|summary\s+of\s+(?:facts|the\s+case)")
CASE_NUMBER = re.compile(r"^[A-Z]{2,5}(?:/[A-Z0-9.]{1,10}){1,4}/(?:19|20)\d\d$")
DIVISION = re.compile(
    r"^(?:IN\s+)?(?:THE\s+)?(?P<name>[A-Z][A-Za-z .'-]{1,40}?)\s+JUDICIAL\s+DIVISION\b", re.I
)
HOLDEN = re.compile(
    r"^HOLDEN\s+(?:AT|IN)\s+(?P<seat>[A-Z][A-Za-z .'-]{1,40}?)\s*[.,]?(?=$|\s+(?:BEFORE|DATED?|ON)\b)", re.I
)
BEFORE = re.compile(
    r"^BEFORE\s*(?:(?:HIS|HER|THEIR)\s+(?:LORDSHIPS?|LADYSHIPS?)|THE\s+HON(?:OURABLE)?\.?\s*)?"
    r"\s*[:,;\-–—]*\s*(?P<rest>.*)$",
    re.I | re.S,
)
DATED = re.compile(r"^(?:DATED?|DELIVERED(?:\s+ON)?)\b\s*(?:THIS\s+)?[:\-–—]?\s*(?P<rest>.*)$", re.I | re.S)
SUIT_LABEL = re.compile(r"\b(?:SUIT|CASE|APPEAL|MOTION|PETITION)\s*(?:NOS?\.?|NUMBERS?)\s*[:.\-]?", re.I)
SUIT_NUMBER = re.compile(
    r"\b((?:NICN|NIC)\s*/\s*[A-Z]{1,10}\s*/\s*[A-Z]{0,3}\d{1,6}[A-Z]{0,3}(?:\s*/\s*[A-Z0-9]{1,4})?\s*/\s*(?:19|20)\d\d)\b",
    re.I,
)
BETWEEN = re.compile(r"^BETWEEN\s*[:.\-]?$", re.I)
AND = re.compile(r"^(?:AND|VS?\.?|VERSUS|-VS?-)$", re.I)
REPRESENTATION = re.compile(
    r"^(?:LEGAL\s+)?(?:REPRESENT(?:ATION|ION|ATIVES?)S?|APPEARANCES?)\b\s*[:\-–—]?\s*(?P<rest>.*)$",
    re.I | re.S,
)
ROLE = re.compile(
    r"(?P<role>(?:CLAIMANTS?|DEFENDANTS?|APPLICANTS?|RESPONDENTS?|PETITIONERS?|APPELLANTS?|COMPLAINANTS?|"
    r"PLAINTIFFS?|INTERVENERS?|OBJECTORS?|GARNISHEES?|INTERESTED\s+PART(?:Y|IES)|JUDGMENT\s+(?:CREDITOR|DEBTOR)S?)(?:\(S\))?"
    r"(?:\s*/\s*[A-Z][A-Za-z]{2,30}(?:\s+[A-Z][A-Za-z]{2,30})?(?:\(S\))?)*)\s*[.:,]?\s*$",
    re.I,
)
ROLE_OF = {
    "claimant": "claimant",
    "defendant": "defendant",
    "applicant": "applicant",
    "respondent": "respondent",
    "petitioner": "petitioner",
    "appellant": "appellant",
    "complainant": "complainant",
    "plaintiff": "plaintiff",
    "intervener": "intervener",
    "interested party": "interested_party",
    "interested parties": "interested_party",
}
DECISION = re.compile(
    r"^(?:(?:FINAL|BENCH|CONSENT|DEFAULT|SUMMARY|INTERLOCUTORY|ENROLLED)\s+)?(?:JUDGE?MENT|RULING)"
    r"(?:\s*/\s*(?:JUDGE?MENT|RULING))?(?:\s+(?:ON|IN)\s+[A-Z][\w .,'’/-]{2,80})?"
    r"(?:\s*\((?:DELIVERED|READ|WRITTEN)\b[^)]{2,80}\))?"
    # set on one line with the first cross-heading ("RULING INTRODUCTION"), never a party's role
    r"(?:\s+(?-i:(?!CREDITORS?\b|DEBTORS?\b)[A-Z][A-Z ]{2,40}))?\s*[.:\-–—]?$",
    re.I,
)
MARKER = re.compile(
    r"^(?P<marker>\[\d{1,4}[A-Za-z]?\]|\(\d{1,3}[A-Za-z]?\)|\d{1,3}(?:\.\d{1,3})*[A-Za-z]?[.)](?=\s)|"
    r"\((?:[a-z]{1,2}|[ivxlc]{1,6})\)|(?:[a-z]|[ivxlc]{1,6})\)(?=\s)|[ivxlc]{1,6}\.(?=\s)|[•·▪●○◦§]"
    r"|[-–—](?=\s))\s*(?P<rest>\S.*)$",
    re.S,
)
LIST_MARKER = re.compile(r"^(?P<marker>[A-Za-z]\.|[IVXLC]{1,6}\.)\s+(?P<rest>\S.*)$", re.S)  # in a list only
NUMBERED = re.compile(r"^(?:\[\d{1,4}[A-Za-z]?\]|\d{1,3}[A-Za-z]?\.)$")  # a paragraph's own number
TERMINAL = re.compile(r"[.!?:;][\"'”’)\]]*$")
SIGNATURE = re.compile(
    r"^(?:HON(?:OURABLE)?\.?\s+(?:(?:MR|MRS|MS|DR)\.?\s+)?JUSTICE\b|(?:PRESIDING\s+)?JUDGE\.?$|PRESIDENT\.?$|"
    r"[_.\-]{3,}|\(?SGD\.?\)?)",
    re.I,
)
ABBREVIATION = re.compile(
    r"(?:\b(?:Hon|Mr|Mrs|Ms|Dr|Prof|No|Nos|Vol|Cap|Ltd|Plc|Esq|Co|Inc|Corp|Ors|Anor|vs?|cf|e\.g|i\.e|etc|al|Sec|"
    r"Art|Para|Paras|pp?|Ch|Rtd|Engr|Arc|Chief|Barr|St|Gen|Col|Maj|Capt|Lt|Sgt|Insp|Supt|Comr|Jan|Feb|Mar|Apr|"
    r"Jun|Jul|Aug|Sept?|Oct|Nov|Dec|SAN|JSC|JCA|JP|LFN|Ord|Ss?|Supp|Rev|Edn|Sch|Pt|ibid|op|cit|NWLR|SC|Pg)"
    r"|(?<![A-Za-z])[A-Z])\.$"
)
SENTENCE_END = re.compile(r"[.!?][\"'”’)\]]*\s+(?=[\"'“‘(\[]?[A-Z0-9])")
# A judgment's closing words: the judge's name and office, a signature line.
CLOSING = re.compile(
    r"(?:\bJUDGE|\bPRESIDENT|\bJUSTICE\b[^.!?]{0,60}|\bJ\.?\s*P\.?|\(?SGD\.?\)?|[.…_]{3,}|\bPhD|\bOFR|\bCON)\W*$",
    re.I,
)


def _upper(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(c.isupper() for c in letters) / len(letters) > 0.85


def _whole(block: Block, kind: str) -> bool:
    """Whether a formatting covers all of a block's words."""
    covered = sum(m["end"] - m["start"] for m in block.marks if m["type"] == kind)
    return bool(block.text) and covered >= len(block.text.replace(" ", "")) * 0.9


def _spaced(text: str) -> str:
    """'J U D G M E N T' read as 'JUDGMENT'."""
    return text.replace(" ", "") if re.fullmatch(r"(?:\w ){3,}\w", text) else text


def is_decision(block: Block) -> bool:
    text = _spaced(block.text.strip())
    if len(text) > 120 or not DECISION.match(text):
        return False
    return (
        _upper(text.split("(")[0])
        or block.tag in ("h1", "h2", "h3", "h4", "h5", "h6")
        or block.align == "center"
        or _whole(block, "bold")
        or _whole(block, "underline")
    )


def headerish(text: str) -> bool:
    """A line of a judgment's header as printed: never joined to the next line."""
    text = text.strip()
    return bool(
        COURT_LINE.match(text)
        or OTHER_COURT.match(text)
        or SPOKEN_DATE.match(text)
        or CASE_NUMBER.match(text)
        or re.search(r"\bJustices?\s+of\s+the\s+(?:Court|Supreme)\b", text, re.I)
        or DIVISION.match(text)
        or HOLDEN.match(text)
        or re.match(r"^BEFORE\b", text, re.I)
        or re.match(r"^(?:DATED?|DELIVERED)\b", text, re.I)
        or SUIT_LABEL.search(text[:40])
        or BETWEEN.match(text)
        or AND.match(text)
        or REPRESENTATION.match(text)
        or DECISION.match(_spaced(text))
        or (len(text) <= 90 and ROLE.search(text) and _upper(text))
    )


def _slice(block: Block, start: int, end: int | None = None, **changes) -> Block:
    """Part of a block, its formatting moved with its words, white space at its ends left out."""
    end = len(block.text) if end is None else end
    piece = block.text[start:end]
    start += len(piece) - len(piece.lstrip())
    end -= len(piece) - len(piece.rstrip())
    marks = [
        {**m, "start": max(m["start"], start) - start, "end": min(m["end"], end) - start}
        for m in block.marks
        if m["end"] > start and m["start"] < end
    ]
    return Block(**{**block.__dict__, "text": block.text[start:end], "marks": marks, **changes})


def join_lines(blocks: list[Block]) -> tuple[list[Block], bool]:
    """Printed lines (a PDF viewer's copy, plain text lines, lines broken by <br>) joined into
    paragraphs: a line opens a paragraph after one that ends a sentence well short of the full
    measure, after a wider gap than the lines' own spacing, when it opens with a number after a line
    that ends a sentence or a lead-in, and whenever either line is a line of the header. Returns the
    blocks and whether any lines were joined."""
    out: list[Block] = []
    joined = False
    k = 0
    while k < len(blocks):
        if not blocks[k].line or blocks[k].rows is not None:
            out.append(blocks[k])
            k += 1
            continue
        run = []
        while k < len(blocks) and blocks[k].line and blocks[k].rows is None:
            run.append(blocks[k])
            k += 1
        lengths = sorted(len(b.text) for b in run)
        full = max(40, lengths[int(len(lengths) * 0.9)])
        gaps = sorted(
            b.top - a.top
            for a, b in zip(run, run[1:], strict=False)
            if a.top is not None and b.top is not None and b.top > a.top
        )
        spacing = gaps[len(gaps) // 2] if gaps else None
        # The header's lines, up to the decision's heading, stay as printed: one unit each.
        heading = next((n for n, b in enumerate(run[:80]) if DECISION.match(_spaced(b.text))), -1)
        current, last = run[0], run[0].text
        for n, line in enumerate(run[1:], 1):
            if n <= heading:
                out.append(current)
                current, last = line, line.text
                continue
            gap = line.top - current.top if line.top is not None and current.top is not None else None
            opens = bool(MARKER.match(line.text) or LIST_MARKER.match(line.text))
            ends = bool(TERMINAL.search(last))
            listed = current.text == last and bool(MARKER.match(last) or LIST_MARKER.match(last))
            if (
                headerish(last)
                or headerish(line.text)
                or (gap is not None and gap > 0 and spacing and gap > 1.6 * spacing)
                or (opens and (ends or last.endswith(":") or listed or len(last) < 0.6 * full))
                or (ends and len(last) < 0.8 * full)
                or (len(last) < 0.6 * full and line.text[:1].isupper() and not last.endswith(","))
            ):
                out.append(current)
                current, last = line, line.text
                continue
            joined = True
            glue = "" if re.search(r"[A-Za-z]-$", current.text) and line.text[:1].islower() else " "
            offset = len(current.text) + len(glue)
            current = Block(
                **{
                    **current.__dict__,
                    "text": current.text + glue + line.text,
                    "marks": current.marks
                    + [{**m, "start": m["start"] + offset, "end": m["end"] + offset} for m in line.marks],
                    "top": line.top,
                }
            )
            last = line.text
        out.append(current)
    return out, joined


def drop_summary(blocks: list[Block]) -> tuple[list[Block], str | None]:
    """The portal's summary card left out of the judgment (returned, as furniture)."""
    for i in range(min(30, len(blocks))):
        if blocks[i].rows is None and SUMMARY_START.match(blocks[i].text):
            for j in range(i, min(i + 16, len(blocks))):
                end = SUMMARY_END.match(blocks[j].text) if blocks[j].rows is None else None
                if end:
                    start = (
                        i - 1
                        if i and COURT_LINE.match(blocks[i - 1].text) and len(blocks[i - 1].text) < 80
                        else i
                    )
                    words = " ".join([*(b.text for b in blocks[start:j]), blocks[j].text[: end.end()]])
                    rest = blocks[j].text[end.end() :].strip()
                    kept = [_slice(blocks[j], end.end())] if rest else []
                    return blocks[:start] + kept + blocks[j + 1 :], words
            break
    for i in range(min(5, len(blocks))):
        found = SUMMARY_TEXT.match(blocks[i].text) if blocks[i].rows is None else None
        if found:
            rest = blocks[i].text[found.end() :].strip()
            kept = [_slice(blocks[i], found.end())] if rest else []
            return blocks[:i] + kept + blocks[i + 1 :], blocks[i].text[: found.end()].strip()
    return blocks, None


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """Where each sentence of a text begins and ends (an abbreviation's full stop ends none, nor does an
    item's number: "reliefs: 1. The sum …")."""
    spans, start = [], 0
    for found in SENTENCE_END.finditer(text):
        stop = found.start() + 1
        head = text[start:stop].rstrip("\"'”’)]")
        last = head.split(" ")[-1]
        if ABBREVIATION.search(last) or re.fullmatch(r"\(?(?:\d{1,3}|[a-z]|[ivx]{1,5})[.)]", last):
            continue
        end = found.end()
        spans.append((start, len(text[:end].rstrip())))
        start = end
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def chunks(block: Block, size: int) -> list[Block]:
    """A long run of text cut at sentence ends into pieces of about `size` characters, a numbered
    sentence ("2. The claimant …") opening a piece of its own."""
    out, start, end = [], None, None
    for s, e in sentence_spans(block.text):
        if start is None:
            start, end = s, e
            continue
        if end - start >= size or (MARKER.match(block.text[s:e]) and e - s > 20):
            out.append(_slice(block, start, end))
            start = s
        end = e
    if start is not None:
        out.append(_slice(block, start, end))
    return [b for b in out if b.text]


HEADER_CUE = re.compile(
    r"\s+(?=(?i:IN\s+THE\s+(?:NATIONAL\s+INDUSTRIAL\s+COURT|[A-Z][A-Z .'-]{1,30}\s+JUDICIAL\s+DIVISION)|HOLDEN\s+AT\b|"
    r"BEFORE\s+(?:HIS|HER|THEIR)\s+LORDSHIP|DATED?\s*:|DATED?\s+(?:THE\s+)?\d|SUIT\s+NO|BETWEEN\b|"
    r"REPRESENTATIONS?\b)|AND\s+[A-Z])|(?<=\bBETWEEN:)\s*|(?<=\bBetween:)\s*"
)
BLOB_DECISION = re.compile(r"(?<![A-Za-z])(?:JUDGE?MENT|RULING)(?=\s+[A-Z][a-z])")


def split_blob(block: Block) -> list[Block]:
    """The whole judgment in one block, its line and paragraph breaks lost: its header cut at the words
    that open each of its lines, its body at sentence ends."""
    head = BLOB_DECISION.search(block.text[:4000])
    cut = head.start() if head and HEADER_CUE.search(" " + block.text[: head.start()]) else 0
    out: list[Block] = []
    if cut:
        starts = [0] + [m.end() for m in HEADER_CUE.finditer(block.text[:cut])]
        for a, b in zip(starts, [*starts[1:], cut], strict=True):
            if block.text[a:b].strip():
                out.append(_slice(block, a, b, line=False))
        out.append(_slice(block, cut, head.end(), line=False))
        body = _slice(block, head.end())
    else:
        body = block
    return out + chunks(body, 1200)


def split_header(blocks: list[Block]) -> tuple[list[Block], list[Block]]:
    """The header as printed (up to and with the decision's heading) and the body."""
    for i in range(min(len(blocks), 80)):
        if blocks[i].rows is None and is_decision(blocks[i]):
            return blocks[: i + 1], blocks[i + 1 :]
    i, parties, counsel = 0, False, False
    while i < len(blocks) and blocks[i].rows is None:
        text = blocks[i].text
        if REPRESENTATION.match(text):
            counsel = True
        elif BETWEEN.match(text):
            parties = True
        elif not (
            headerish(text)
            or (parties and not counsel and len(text) <= 160 and (_upper(text) or ROLE.search(text)))
            or (
                counsel
                and len(text) <= 400
                and re.search(r"\bfor\s+the\b|\bappear|\babsent\b|\bpresent\b|Esq|\bSAN\b", text, re.I)
            )
        ):
            break
        i += 1
    return blocks[:i], blocks[i:]


@dataclass
class Unit:
    """A unit of the reading: what the workbench edits."""

    kind: str  # heading, paragraph, list_item, table
    text: str = ""
    marks: list = field(default_factory=list)
    marker: str | None = None
    marker_source: str | None = None
    indent: int | None = None  # 1-3: a quotation or an item within a paragraph
    align: str | None = None
    rows: list | None = None
    header: bool = False
    source: int = 0  # the ordinal of its first block
    flags: list = field(default_factory=list)  # [{"severity", "kind", "primary", "others", "span"}]


def _unit_of_table(block: Block, header: bool = False) -> Unit:
    return Unit(kind="table", rows=block.rows, header=header, source=block.ordinal)


def header_units(blocks: list[Block]) -> list[Unit]:
    out = []
    for block in blocks:
        if block.rows is not None:
            out.append(_unit_of_table(block, True))
            continue
        heading = bool(
            COURT_LINE.match(block.text)
            or OTHER_COURT.match(block.text)
            or DIVISION.match(block.text)
            or HOLDEN.match(block.text)
            or is_decision(block)
        )
        unit = Unit(
            kind="heading" if heading else "paragraph",
            text=block.text,
            marks=block.marks,
            align=block.align if block.align in ("center", "right") else None,
            header=True,
            source=block.ordinal,
        )
        if block.list_marker:
            unit.marker, unit.marker_source = block.list_marker, "list numbering"
        out.append(unit)
    return out


def body_units(blocks: list[Block]) -> list[Unit]:
    """The body's units: cross-headings, paragraphs with their printed numbers as markers, list items
    and indented quotations (indent 1), tables."""
    out: list[Unit] = []
    lefts: dict[int, float] = {}  # each text unit's left margin, for the quotations
    last_number, number_left = 0, None  # the judgment's last numbered paragraph, and where it is set
    for block in blocks:
        if block.rows is not None:
            out.append(_unit_of_table(block))
            continue
        listy = bool(block.list_marker) or "MsoListParagraph" in block.css or block.tag == "li"
        text, marks, marker, source = block.text, block.marks, None, None
        if block.list_marker:
            marker, source = block.list_marker, "list numbering"
        else:
            found = MARKER.match(text) or (LIST_MARKER.match(text) if listy else None)
            if found:
                rest = _slice(block, found.start("rest"))
                marker, source, text, marks = found.group("marker"), "web page", rest.text, rest.marks
        listed = listy and marker is not None  # Word's list style without a number is a paragraph
        numbered = re.fullmatch(r"(\d{1,3})\.", marker or "")
        if numbered:
            value = int(numbered.group(1))
            # The judgment's own paragraphs numbered by Word's lists: the next number, set where the
            # numbered paragraphs are; a list within a paragraph restarts, or is set further in.
            if value == last_number + 1 and (number_left is None or abs(block.left - number_left) < 0.15):
                listed = False
            if not listed:
                last_number = value
                number_left = block.left if number_left is None else number_left
        heading = block.tag in ("h1", "h2", "h3", "h4", "h5", "h6") or (
            len(text) <= 150
            and not re.search(r"[.,;]$", text)
            and not SIGNATURE.match(text)
            and not (marker and not _upper(text))
            and (
                _whole(block, "bold")
                or _whole(block, "underline")
                or (_upper(text) and len(text) <= 80 and len(text.split()) >= 2)
            )
        )
        kind = "heading" if heading else "list_item" if listed else "paragraph"
        unit = Unit(
            kind=kind,
            text=text,
            marks=marks,
            marker=marker,
            marker_source=source,
            indent=min(3, max(1, block.list_depth)) if kind == "list_item" else 1 if block.quote else None,
            align=block.align if block.align in ("center", "right") else None,
            source=block.ordinal,
        )
        lefts[id(unit)] = block.left
        out.append(unit)
    # An indented quotation: a paragraph set clearly further in than the judgment's own paragraphs (the
    # common margin of the long ones), or a little further in and opening with a quotation mark.
    widths = sorted(lefts[id(u)] for u in out if u.kind == "paragraph" and len(u.text) >= 200)
    baseline = widths[len(widths) // 4] if widths else 0.0
    for unit in out:
        further = lefts.get(id(unit), baseline) - baseline
        if (
            unit.kind == "paragraph"
            and unit.indent is None
            and (further >= 0.4 or (further >= 0.2 and unit.text[:1] in "“\"‘'"))
            and not (unit.marker and NUMBERED.match(unit.marker))
            and not SIGNATURE.match(unit.text)
        ):
            unit.indent = 1
    # The closing lines after the last paragraph (the judge's name, "PRESIDING JUDGE", the date) are
    # never cross-headings, however they are set.
    last = max((k for k, u in enumerate(out) if u.rows is None and len(u.text) >= 200), default=len(out))
    for unit in out[last + 1 :]:
        if unit.kind == "heading":
            unit.kind = "paragraph"
    return out


def split_long(units: list[Unit]) -> list[Unit]:
    """A unit longer than the workbench takes, cut at sentence ends; each later part flagged."""
    out = []
    for unit in units:
        if unit.rows is not None or len(unit.text) <= UNIT_CHARACTERS:
            out.append(unit)
            continue
        block = Block(text=unit.text, marks=unit.marks)
        parts = chunks(block, UNIT_CHARACTERS - 1000)
        for k, part in enumerate(parts):
            piece = Unit(**{**unit.__dict__, "text": part.text, "marks": part.marks, "flags": []})
            if k:
                piece.marker = piece.marker_source = None
                piece.flags.append(
                    {
                        "severity": "low",
                        "kind": "long_paragraph_split",
                        "primary": " ".join(part.text.split()[:8]),
                        "others": {},
                        "span": (0, min(len(part.text), 1)),
                    }
                )
            out.append(piece)
    return out


# --- what the page itself gets wrong: flagged for the reviewer ---------------------------------------

NAIRA = re.compile(r"(?:(?<=\s)|(?<=\()|^)\?(?=\d)")
# Text encoded twice: UTF-8's bytes read as Windows-1252 ("â€™" for "’").
MOJIBAKE = re.compile(
    "[\u00c2\u00c3\u00e2][\u0080-\u00bf\u0152\u0153\u0160\u0161\u0178\u017d\u017e\u0192\u02c6\u02dc"
    "\u2013\u2014\u2018-\u201e\u2020-\u2022\u2026\u2030\u2039\u203a\u20ac\u2122]{1,2}"
)
MASKED = re.compile(r"(?<![\w*])\*{2,}(?![\w*])")


def _cp1252(text: str) -> bytes | None:
    out = b""
    for ch in text:
        try:
            out += ch.encode("cp1252")
        except UnicodeEncodeError:
            if ord(ch) > 255:
                return None
            out += bytes([ord(ch)])
    return out


def _repaired(found: str) -> str | None:
    raw = _cp1252(found)
    try:
        fixed = raw.decode("utf-8") if raw else None
    except UnicodeDecodeError:
        return None
    return fixed if fixed and len(fixed) == 1 else None


def check_text(unit: Unit, why_masked: str | None = None) -> None:
    """The naira sign published as "?" restored and flagged; text encoded twice, bytes that are not
    UTF-8 and words the page masks flagged and left as published."""
    texts = [unit.text] if unit.rows is None else [c["text"] for row in unit.rows for c in row]
    restored = []
    for k, text in enumerate(texts):
        for found in NAIRA.finditer(text):
            word_end = re.search(r"\s|$", text[found.start() :]).start() + found.start()
            restored.append((found.start(), word_end, text[found.start() : word_end]))
        texts[k] = NAIRA.sub("₦", text)
    if restored:
        if unit.rows is None:
            unit.text = texts[0]
        else:
            flat = iter(texts)
            for row in unit.rows:
                for cell in row:
                    cell["text"] = next(flat)
        unit.flags.append(
            {
                "severity": "medium",
                "kind": "naira_sign_restored",
                "primary": "; ".join("₦" + word[1:] for _, _, word in restored)[:400],
                "others": {"As published": "; ".join(word for _, _, word in restored)[:400]},
                "span": (restored[0][0], restored[-1][1]) if unit.rows is None else None,
            }
        )
    joined = " ".join(texts)
    faults = [(m.start(), m.end(), m.group(), _repaired(m.group())) for m in MOJIBAKE.finditer(joined)]
    faults = [f for f in faults if f[3]]
    if faults:
        unit.flags.append(
            {
                "severity": "medium",
                "kind": "encoding_fault",
                "primary": "; ".join(dict.fromkeys(f[2] for f in faults))[:400],
                "others": {"Probably meant": "; ".join(dict.fromkeys(f[3] for f in faults))[:400]},
                "span": (faults[0][0], faults[-1][1]) if unit.rows is None else None,
            }
        )
    if "�" in joined:
        at = joined.index("�")
        unit.flags.append(
            {
                "severity": "medium",
                "kind": "invalid_bytes",
                "primary": joined[max(0, at - 30) : at + 30],
                "others": {},
                "span": (at, at + 1) if unit.rows is None else None,
            }
        )
    masked = list(MASKED.finditer(joined))
    if masked:
        unit.flags.append(
            {
                "severity": "high",
                "kind": "masked_words",
                "primary": f"{len(masked)} word{'s' if len(masked) > 1 else ''} masked with asterisks",
                "others": {"The page says": why_masked} if why_masked else {},
                "span": (masked[0].start(), masked[-1].end()) if unit.rows is None else None,
            }
        )


@dataclass
class Judgment:
    """A record page read: its units (header, then body), what was left out as furniture, the shape it
    was published in, and what it lacks."""

    units: list[Unit]
    furniture: list[str]
    shape: str
    title: str
    summary: str | None = None  # the portal's summary card, as printed
    viewer_pages: int | None = None
    copied_pages: int | None = None
    flags: list = field(default_factory=list)  # page-wide: [{"severity", "kind", "primary", "others"}]
    empty: str | None = None  # why there is no judgment to review
    court: str | None = None  # another court's name, as printed, when the portal posted its decision

    @property
    def header(self) -> list[Unit]:
        return [u for u in self.units if u.header]

    @property
    def body(self) -> list[Unit]:
        return [u for u in self.units if not u.header]


def read(raw: bytes) -> Judgment:
    """A stored record page as the units of a judgment, with the faults the page shows flagged."""
    page = Page(raw)
    blocks = page.blocks
    furniture = list(page.furniture)
    blocks, summary = drop_summary(blocks)
    texts = [b for b in blocks if b.rows is None]
    total = sum(len(b.text) for b in texts)
    shape = "word"
    if any(b.top is not None for b in blocks):
        shape = "pdf-lines"
    elif sum(1 for b in blocks if b.line) > len(blocks) / 2:
        shape = "text-lines"
    largest = max(texts, key=lambda b: len(b.text), default=None)
    blob = (
        largest is not None
        and len(largest.text) >= 1500
        and len(largest.text) >= 0.85 * total
        and len(texts) <= 4
    )
    flags = []
    if blob:
        shape = "one-block"
        at = blocks.index(largest)
        blocks = blocks[:at] + split_blob(largest) + blocks[at + 1 :]
        blocks, summary_left = drop_summary(blocks)
        summary = summary or summary_left
        flags.append(
            {
                "severity": "medium",
                "kind": "paragraphs_not_marked",
                "primary": "The page gives the judgment as one run of text: its paragraphs were cut at sentence ends",
                "others": {},
            }
        )
    blocks, joined = join_lines(blocks)
    if joined:
        flags.append(
            {
                "severity": "low",
                "kind": "lines_joined",
                "primary": "The page gives the judgment line by line: these paragraphs were joined from its lines",
                "others": {},
            }
        )
    blocks = [b for b in blocks if b.rows is not None or b.text.strip()]
    judgment = Judgment(
        units=[],
        furniture=furniture,
        shape=shape,
        title=page.title,
        summary=summary,
        flags=flags,
        viewer_pages=page.viewer_pages,
    )
    if sum(len(b.text) for b in blocks if b.rows is None) < 200 and not any(b.rows for b in blocks):
        judgment.empty = "the page holds no judgment text" + (
            f" ({' '.join(b.text for b in blocks)[:80]!r})" if blocks else ""
        )
        return judgment
    header, body = split_header(blocks)
    judgment.units = split_long(header_units(header) + body_units(body))
    # WordToHTML.net's trial masks words with asterisks; the page says when it made the page.
    trial = next((f for f in furniture if "wordtohtml" in f.lower()), None)
    for unit in judgment.units:
        check_text(unit, trial)
    opening = [u.text for u in judgment.units[:12] if u.rows is None]
    other = next((t for t in opening if OTHER_COURT.match(t)), None)
    if other and not any(COURT_LINE.match(t) for t in opening):
        judgment.court = OTHER_COURT.match(other).group(0)
        judgment.flags.append(
            {
                "severity": "high",
                "kind": "other_court",
                "primary": "The portal posted another court's decision: its details are that court's, not this court's",
                "others": {"Printed": other[:200]},
            }
        )
    editorial = next((u.text for u in judgment.units if EDITORIAL.fullmatch(u.text.strip())), None)
    if editorial:
        judgment.flags.append(
            {
                "severity": "medium",
                "kind": "editorial_matter",
                "primary": "A law report's editorial summaries are printed with the judgment: not the court's words",
                "others": {"Printed": editorial},
            }
        )
    if not any(
        COURT_LINE.match(u.text) or OTHER_COURT.match(u.text) or BETWEEN.match(u.text)
        for u in judgment.header
    ):
        judgment.flags.append(
            {
                "severity": "medium",
                "kind": "no_printed_header",
                "primary": "The page prints no header of its own (court, parties, date): see the details suggested "
                "from the portal's catalogue",
                "others": {},
            }
        )
    tops = [b.top for b in page.blocks if b.top is not None]
    if tops:
        judgment.copied_pages = 1 + sum(1 for a, b in zip(tops, tops[1:], strict=False) if b < a)
    _incomplete(judgment)
    _images(judgment, page)
    return judgment


def _incomplete(judgment: Judgment) -> None:
    """The text stops mid-sentence (high), or the copied PDF viewer counted more pages than the page
    holds text of (high with it, medium alone: a page of the PDF may hold no text, a signature)."""
    texts = [u for u in judgment.body if u.rows is None and u.text]
    if not texts:
        return
    last = texts[-1]
    reasons = []
    if judgment.viewer_pages and judgment.copied_pages and judgment.copied_pages < judgment.viewer_pages:
        reasons.append(
            f"the PDF viewer copied into the page counted {judgment.viewer_pages} pages; "
            f"the page holds the text of {judgment.copied_pages}"
        )
    stops = (
        len(last.text) >= 60
        and not TERMINAL.search(last.text.rstrip())
        and not CLOSING.search(last.text[-120:])
    )
    if stops:
        reasons.append("its text stops mid-sentence")
    if reasons:
        last.flags.append(
            {
                "severity": "high" if stops else "medium",
                "kind": "text_incomplete",
                "primary": "The portal may hold only part of this judgment: " + "; ".join(reasons),
                "others": {"Last words": " ".join(last.text.split()[-12:])},
                "span": (max(0, len(last.text) - 1), len(last.text)),
            }
        )


def _images(judgment: Judgment, page: Page) -> None:
    """An image the judgment refers to but the portal never published (a link to a file on the
    typist's computer): its content is missing."""
    for image in page.images:
        src = image["src"]
        if re.match(r"https?://nicnadr\.gov\.ng/", src) or not src:
            continue
        name = re.split(r"[\\/]", src)[-1][:80]
        target = next(
            (u for u in judgment.units if u.source >= image["at"]),
            judgment.units[-1] if judgment.units else None,
        )
        if target is None:
            continue
        size = (
            f"{image['width']} × {image['height']}"
            if image["width"] and image["height"]
            else "size not given"
        )
        large = image["width"] >= 200 and image["height"] >= 200
        target.flags.append(
            {
                "severity": "high" if large else "medium",
                "kind": "image_not_published",
                "primary": f"An image ({name}, {size}) the page refers to was never published",
                "others": {"Its address": src[:300]},
                "span": (0, min(len(target.text), 1)) if target.rows is None else None,
            }
        )


# --- the reading the workbench reviews ------------------------------------------------------------


def _tokens(text: str | None) -> int:
    return len((text or "").split())


def _unit_tokens(unit: Unit) -> int:
    if unit.rows is not None:
        return sum(_tokens(c["text"]) for row in unit.rows for c in row)
    return _tokens(unit.text)


def _word_at(text: str, at: int) -> int:
    count = len(text[:at].split())
    return max(0, count - 1) if at and not text[at - 1].isspace() and at < len(text) else count


def paginate(units: list[Unit]) -> list[list[Unit]]:
    """Sections of about WORDS_PER_PAGE words, cut between units: the header with the first, a
    cross-heading with what follows it."""
    pages: list[list[Unit]] = []
    current: list[Unit] = []
    words = 0
    for unit in units:
        size = _unit_tokens(unit) + _tokens(unit.marker)
        if (
            current
            and not unit.header
            and current[-1].kind != "heading"
            and (words + size > WORDS_PER_PAGE or len(current) >= UNITS_PER_PAGE)
        ):
            pages.append(current)
            current, words = [], 0
        current.append(unit)
        words += size
    if current:
        pages.append(current)
    return pages


def reading(judgment: Judgment, sha256: str, record_id: int | None = None) -> dict:
    """The reading in the shape the workbench reviews (as rw_gazette.reading gives it): each page's
    nodes, its flags placed by word position (each unit's marker, then its text), and on every page
    the flag that it was read once."""
    pages = []
    for number, units in enumerate(paginate(judgment.units), 1):
        nodes, flags, position = [], [], 0
        flags.append(
            {
                "severity": "medium",
                "kind": "one_reading",
                "primary": "",
                "checks": {},
                "compare": True,
                "tokens": [0, 0],
            }
        )
        for flag in judgment.flags:
            flags.append(
                {
                    "severity": flag["severity"],
                    "kind": flag["kind"],
                    "primary": flag["primary"],
                    "checks": flag["others"],
                    "compare": True,
                    "tokens": [0, 0],
                }
            )
        if number == 1:
            nodes += [{"kind": "furniture", "text": text, "page": 1} for text in judgment.furniture]
            if judgment.summary:
                nodes.append({"kind": "furniture", "text": judgment.summary, "page": 1})
        for unit in units:
            marker = _tokens(unit.marker)
            for flag in unit.flags:
                span = flag.get("span")
                if span is None or unit.rows is not None:
                    tokens = [position, position + max(marker, 1)]
                else:
                    first = _word_at(unit.text, span[0])
                    last = max(first + 1, len(unit.text[: span[1]].split()))
                    tokens = [position + marker + first, position + marker + last]
                flags.append(
                    {
                        "severity": flag["severity"],
                        "kind": flag["kind"],
                        "primary": flag["primary"],
                        "checks": flag["others"],
                        "compare": True,
                        "tokens": tokens,
                    }
                )
            position += marker + _unit_tokens(unit)
            if unit.rows is not None:
                nodes.append(
                    {
                        "kind": "table",
                        "page": number,
                        "caption": "",
                        "header_rows": [],
                        "body_rows": unit.rows,
                    }
                )
                continue
            node = {"kind": unit.kind, "text": unit.text, "page": number}
            if unit.marker:
                node["marker"], node["marker_source"] = unit.marker, unit.marker_source
            if unit.indent:
                node["indent"] = unit.indent
            if unit.align:
                node["align"] = unit.align
            if unit.marks:
                node["marks"] = unit.marks
            nodes.append(node)
        furniture = [n["text"] for n in nodes if n["kind"] == "furniture"]
        pages.append(
            {
                "page": number,
                "nodes": nodes,
                "text": "\n".join(
                    " ".join(c["text"] for row in n["body_rows"] for c in row)
                    if n["kind"] == "table"
                    else n["text"]
                    for n in nodes
                    if n["kind"] != "furniture"
                ),
                "tables": [],
                "furniture": furniture,
                "flags": flags,
                "checks": {},
            }
        )
    counts = {
        level: sum(f["severity"] == level for p in pages for f in p["flags"])
        for level in ("high", "medium", "low")
    }
    return {
        "input_sha256": sha256,
        "primary": {"reader": "web-page", "version": {"tool": "ingestion.nicn", "version": READER}},
        "checks": {
            "web_page": {
                "shape": judgment.shape,
                "record_id": record_id,
                "pages_are": "sections of the web page, cut between units; the judgment prints no page numbers",
                **({"viewer_pages": judgment.viewer_pages} if judgment.viewer_pages else {}),
                **({"copied_pages": judgment.copied_pages} if judgment.copied_pages else {}),
            }
        },
        "summary": {"pages": len(pages), "flags": counts, "restored_markers": 0, "missing_checks": []},
        "pages": pages,
    }


# --- the details suggested from the page and its catalogue entry -------------------------------------

LEVELS = {  # Nigeria's courts as schema/jurisdictions.json lists them
    "national industrial court of nigeria": "specialised",
    "court of appeal": "appellate",
    "supreme court of nigeria": "apex",
    "federal high court": "superior",
}
COUNSEL = re.compile(
    r"(?P<counsel>.*?)\s*\bfor\s+the\s+(?P<which>(?:\d{1,2}\s*(?:st|nd|rd|th)\s*(?:(?:,|and|&)\s*)?)*)"
    r"(?P<role>claimants?|defendants?|applicants?|respondents?|petitioners?|appellants?|complainants?|plaintiffs?|"
    r"objectors?|garnishees?|interveners?|judgment\s+(?:creditor|debtor)s?)\b",
    re.I,
)
ORDINAL = re.compile(r"(\d{1,2})\s*(?:st|nd|rd|th)", re.I)


def _date(text: str) -> str | None:
    from ingestion.nigeria import to_date

    # As typed: "17 TH APRIL", "19thJune", "23RDJUNE", "16TH DAY OFAPRIL", "29TH OF SEPTEMBER".
    text = re.sub(r"(\d)\s+(st|nd|rd|th)\b", r"\1\2", text, flags=re.I)
    text = re.sub(r"(\d)(st|nd|rd|th)(?=[A-Za-z])", r"\1\2 ", text, flags=re.I)
    text = re.sub(
        r"\bof(?=(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b)", "of ", text, flags=re.I
    )
    text = re.sub(r"\bday\s+of\s+|\bof\s+(?=[A-Za-z]{3,}\b)", "", text, flags=re.I)
    found = re.search(
        r"\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,}\.?\s*,?\s*(?:19|20)\d\d"
        r"|[A-Za-z]{3,}\.?\s+\d{1,2}(?:st|nd|rd|th)?\s*,?\s*(?:19|20)\d\d"
        r"|\b\d{1,2}/\d{1,2}/(?:19|20)\d\d\b|\b(?:19|20)\d\d-\d\d-\d\d\b",
        text,
        re.I,
    )
    return to_date(found.group(0)) if found else None


def _role(words: str) -> str:
    first = re.split(r"\s*/\s*", words.strip().lower())[0]
    first = re.sub(r"\(s\)$", "", first)
    for name, role in ROLE_OF.items():
        if first == name or first == name + "s":
            return role
    return "other"


def _name(text: str) -> str:
    text = re.sub(r"^\s*\(?\d{1,3}[.)]\s*", "", text)
    return re.sub(r"[\s.…·_=\-–—,]+$", "", text).strip()


def party_start(units: list[Unit]) -> int | None:
    """Where the header's list of parties begins: after "BETWEEN", else after the last line naming the
    court, the judge, the date or the suit number before the first "AND"."""
    start = next((k for k, u in enumerate(units) if BETWEEN.match(u.text)), None)
    if start is not None:
        return start
    first_and = next((k for k, u in enumerate(units) if AND.match(u.text.strip())), None)
    if first_and is None:
        return None
    before = [
        k
        for k, u in enumerate(units[:first_and])
        if COURT_LINE.match(u.text)
        or DIVISION.match(u.text)
        or HOLDEN.match(u.text)
        or re.match(r"(?i)before\b", u.text.strip())
        or DATED.match(u.text.strip())
        or SUIT_LABEL.search(u.text)
    ]
    return max(before) if before else None


def parties(units: list[Unit]) -> list[dict]:
    """The parties as the header lists them between "BETWEEN" and the representation, each with the
    role printed beside it (a role printed once for several names covers each of them)."""
    start = party_start(units)
    if start is None:
        return []
    out, pending, last_role = [], [], None
    for unit in units[start + 1 :]:
        text = unit.text.strip()
        if REPRESENTATION.match(text) or (unit.kind == "heading" and DECISION.match(_spaced(text))):
            break
        if AND.match(text):
            out += [{**p, "role": p["role"] or last_role or "other"} for p in pending]
            pending, last_role = [], None
            continue
        role = ROLE.search(text)
        name = _name(text[: role.start()] if role else text)
        numbered = bool(unit.marker) or bool(re.match(r"^\(?\d{1,3}[.)]\s", text))
        if name:
            held = pending or out
            open_ = held and (
                held[-1]["name"].count("(") > held[-1]["name"].count(")")
                or held[-1]["name"].count("[") > held[-1]["name"].count("]")
            )
            if held and (name[:1] in "([" or open_ or (pending and not numbered and pending[-1]["numbered"])):
                held[-1]["name"] += " " + name  # a name, or its capacity, running on to the next line
            else:
                pending.append({"name": name, "role": None, "numbered": numbered})
        if role:
            last_role = _role(role.group("role"))
            out += [{**p, "role": p["role"] or last_role} for p in pending]
            pending = []
    out += [{**p, "role": p["role"] or last_role or "other"} for p in pending]
    return [{"name": p["name"][:300], "role": p["role"], "anonymised": "no"} for p in out if p["name"]]


def counsel(lines: list[str]) -> list[tuple[str, str, list[int]]]:
    """Who appeared for whom: (counsel as printed, the role they appeared for, which of that role's
    parties by number, when printed: "for the 1st Respondent")."""
    out = []
    for line in lines:
        for clause in re.split(r";(?!\s*for\s+the\b)|(?<=[a-z)])\.\s+(?=[A-Z])", line):
            if re.match(r"\s*(?:no\s+representation|absent|parties\s+absent)", clause, re.I):
                continue
            for found in COUNSEL.finditer(clause):
                who = re.sub(
                    r"^\s*(?:and|while|,)\s+|\s*\b(?:appear(?:s|ed|ing)?|holding|holds)\b.*$",
                    "",
                    found.group("counsel"),
                    flags=re.I,
                )
                who = who.strip(" ,;:-–—")
                if who and len(who) <= 300:
                    which = [int(n) for n in ORDINAL.findall(found.group("which") or "")]
                    out.append((who, _role(found.group("role")), which))
    return out


def judges(units: list[Unit]) -> tuple[list[str], str, Unit | None]:
    """The judge or judges named after "BEFORE HIS LORDSHIP" (on its line or the lines after), the
    words they were read from, and the unit those words begin in."""
    for k, unit in enumerate(units):
        before = BEFORE.match(unit.text.strip())
        if not before:
            continue
        lines = [before.group("rest")] if before.group("rest").strip(" :-–—") else []
        printed = [unit.text]
        for later in units[k + 1 : k + 6]:
            text = later.text.strip()
            justice = re.search(r"\bJUSTICE\b|\bHON(?:OURABLE)?\b", text, re.I)
            if not justice and (lines or headerish(text)):
                break
            lines.append(text)
            printed.append(text)
        names = []
        for line in lines:
            for part in re.split(r"\s+AND\s+|;", line):
                part = re.sub(r"\s+Justices?\s+of\s+the\s+.*$", "", part.strip(" :-–—"), flags=re.I)
                part = re.sub(r"\s*[-–—,]*\s*(?:\(?PRESIDING\s+)?JUDGE\)?\.?\s*$", "", part, flags=re.I)
                part = re.sub(r"\s+(?:DATED?|ON)\b.*$", "", part, flags=re.I).strip(" ,.:-–—")
                if part and re.search(r"[A-Za-z]{2}", part):
                    names.append(part)
        return names[:9], " / ".join(printed), unit
    return [], "", None


def details(
    judgment: Judgment, entry: dict, kind: str, url: str, provenance: dict, page_of: dict
) -> tuple[dict, dict]:
    """The details suggested for a judgment, in the record's keys (workbench.record_view): the values
    as a first draft for the reviewer, and beside each the page and words it was read from. The legal
    facts (outcome, standing as precedent, legal status, a version's authority) are never suggested."""
    header = judgment.header
    listed = entry.get("listed") or {}
    from_page = listed.get("from_page") or {}
    card = from_page.get("structured_header") or {}
    values: dict = {
        "identity.family": "judgment",
        "identity.type": kind if kind in ("judgment", "ruling") else "judgment",
    }
    evidence: dict = {}

    def given(key: str, value, words: str, unit: Unit | None = None) -> None:
        if value in (None, "", []):
            return
        values[key] = value
        evidence[key] = {
            "value": value,
            "page": page_of.get(id(unit)) if unit is not None else None,
            "evidence": words[:400],
        }

    def find(pattern: re.Pattern, units=header) -> tuple[re.Match | None, Unit | None]:
        for unit in units:
            if unit.rows is None:
                found = pattern.search(unit.text)
                if found:
                    return found, unit
        return None, None

    decision = next((u for u in header if u.kind == "heading" and DECISION.match(_spaced(u.text))), None)
    if decision is not None:
        evidence["identity.type"] = {"value": values["identity.type"], "page": 1, "evidence": decision.text}
    elif listed.get("from_page", {}).get("heading") or card.get("category"):
        evidence["identity.type"] = {
            "value": values["identity.type"],
            "page": None,
            "evidence": f"The portal's catalogue: {card.get('category') or from_page.get('heading')}",
        }

    title = listed.get("title") or judgment.title.replace("- National Industrial Court of Nigeria", "").strip(
        " -"
    )
    given("identity.title", title, f"The page's title: {judgment.title}")

    # The court: this court, unless the page prints another's decision.
    court_line, court_unit = find(COURT_LINE)
    if judgment.court:
        name = " ".join(
            w.capitalize() if w.lower() not in ("of", "the") else w.lower() for w in judgment.court.split()
        )
        name = re.sub(r"^(?:In\s+)?the\s+", "", name, flags=re.I)
        name = "Supreme Court of Nigeria" if name.lower().startswith("supreme court") else name
        given("profile.court.name", name, judgment.court)
        level = next((v for k, v in LEVELS.items() if name.lower().startswith(k)), None)
        if level:
            given("profile.court.level", level, "Nigeria's jurisdiction profile (schema/jurisdictions.json)")
        given("issued_by", name, judgment.court)
    else:
        words = court_unit.text if court_unit else f"The court's own judgment portal ({PUBLICATION})"
        given("profile.court.name", COURT, words, court_unit)
        given(
            "profile.court.level",
            "specialised",
            f"Nigeria's jurisdiction profile lists the {COURT} as specialised",
        )
        given("issued_by", COURT, words, court_unit)
    division, division_unit = find(DIVISION)
    if division:
        given(
            "profile.court.division",
            division.group("name").strip().title(),
            division_unit.text,
            division_unit,
        )
    seat, seat_unit = find(HOLDEN)
    if seat:
        given("profile.court.seat", seat.group("seat").strip().title(), seat_unit.text, seat_unit)

    # The suit numbers: as the header prints them, else as the catalogue read them.
    suits, suit_unit = [], None
    for unit in header:
        for found in SUIT_NUMBER.findall(unit.text if unit.rows is None else ""):
            number = re.sub(r"\s+", "", found).upper()
            if number not in suits:
                suits.append(number)
                suit_unit = suit_unit or unit
        if unit.rows is None and CASE_NUMBER.match(unit.text.strip()) and unit.text.strip() not in suits:
            suits.append(unit.text.strip())
            suit_unit = suit_unit or unit
    if suits:
        given("profile.case_numbers", suits, suit_unit.text, suit_unit)
        given("identity.number.value", suits[0], suit_unit.text, suit_unit)
        printed = SUIT_LABEL.search(suit_unit.text)
        given(
            "identity.number.printed",
            (suit_unit.text[printed.start() :] if printed else suits[0]).strip()[:120],
            suit_unit.text,
            suit_unit,
        )
    elif from_page.get("suit_numbers") or listed.get("suit_no"):
        suits = from_page.get("suit_numbers") or [listed["suit_no"]]
        given("profile.case_numbers", suits, f"The portal's catalogue: {', '.join(suits)}")
        given("identity.number.value", suits[0], f"The portal's catalogue: {suits[0]}")

    # The date delivered: the header's date, else the portal's.
    delivered, words, date_unit = None, None, None
    for unit in header:
        if unit.rows is not None or (unit.kind == "heading" and not DATED.match(unit.text)):
            continue
        text = unit.text.strip()
        labelled = DATED.match(text)
        if (
            labelled
            or SPOKEN_DATE.match(text)
            or re.search(r"\b(?:19|20)\d\d\b", text)
            and not ROLE.search(text)
        ):
            found = _date(labelled.group("rest") if labelled else text)
            if found and not SUIT_NUMBER.fullmatch(text):
                delivered, words, date_unit = found, text, unit
                break
    if delivered is None and (card.get("date_delivered") or listed.get("judgment_date")):
        delivered = card.get("date_delivered") or listed.get("judgment_date")
        words = (
            f"The portal's summary: Date Delivered: {card['date_delivered']}"
            if card.get("date_delivered")
            else f"The portal's catalogue: {listed.get('judgment_date')}"
        )
    if delivered:
        given("event:decided.date", delivered, words, date_unit)
        portal = card.get("date_delivered") or listed.get("judgment_date")
        if date_unit is not None and portal and portal != delivered:  # the portal's catalogue says otherwise
            evidence["event:decided.date"]["secondary"] = {
                "value": portal,
                "evidence": "The portal's catalogue",
            }
        if len(delivered) >= 4:
            given("identity.year", delivered[:4], words, date_unit)

    # The parties and their counsel, the judges.
    rows, side = parties(header), None
    if rows:
        side = header[party_start(header)]
        listed_lines = []
        for unit in header[header.index(side) + 1 :]:
            if REPRESENTATION.match(unit.text) or (
                unit.kind == "heading" and DECISION.match(_spaced(unit.text))
            ):
                break
            listed_lines.append(unit.text)
        words = " / ".join(listed_lines)[:400]
    elif listed.get("from_page", {}).get("claimant"):
        rows = [
            {"name": from_page["claimant"], "role": "claimant", "anonymised": "no"},
            {"name": from_page["defendant"], "role": "defendant", "anonymised": "no"},
        ]
        words = f"The page's title: {judgment.title}"
    lines, counsel_unit = [], None
    rep = next((k for k, u in enumerate(header) if REPRESENTATION.match(u.text)), None)
    if rep is not None:
        rest = REPRESENTATION.match(header[rep].text).group("rest")
        lines = ([rest] if rest.strip() else []) + [
            u.text for u in header[rep + 1 :] if not (u.kind == "heading" and DECISION.match(_spaced(u.text)))
        ]
        counsel_unit = header[rep]
    else:  # newer judgments print the representation after the judge's signature
        body = [u for u in judgment.body if u.rows is None]
        signed = max(
            (
                k
                for k, u in enumerate(body)
                if SIGNATURE.match(u.text)
                or re.fullmatch(r"(?i)\(?(?:presiding\s+)?judge\)?\.?", u.text.strip())
            ),
            default=None,
        )
        labelled = max((k for k, u in enumerate(body[-20:]) if REPRESENTATION.match(u.text)), default=None)
        if labelled is not None:
            after = body[-20:][labelled + 1 :]
        else:
            after = body[signed + 1 :] if signed is not None else []
        tail = [u for u in after if len(u.text) <= 400 and COUNSEL.search(u.text)]
        lines = [u.text for u in tail]
        counsel_unit = tail[0] if tail else None
    appeared = counsel(lines)
    for who, role, which in appeared:
        same = [r for r in rows if r["role"] == role]
        chosen = [same[n - 1] for n in which if 0 < n <= len(same)] or same
        for row in chosen:
            row["represented_by"] = "; ".join(filter(None, [row.get("represented_by"), who]))
    if rows:
        given("profile.parties", rows, words, side)
        sides: list[list[dict]] = []
        for row in rows:
            if not sides or sides[-1][-1]["role"] != row["role"]:
                sides.append([])
            sides[-1].append(row)

        def side_of(group: list[dict]) -> str:
            role = group[0]["role"].replace("_", " ").capitalize()
            plural = "s" if len(group) > 1 and group[0]["role"] != "other" else ""
            return " and ".join(r["name"] for r in group) + f" ({role}{plural})"

        full = " v. ".join(side_of(group) for group in sides)
        given("identity.full_title", full[:3900], words, side)
    if lines:
        evidence["profile.representation"] = {
            "value": " / ".join(lines)[:400],
            "page": page_of.get(id(counsel_unit)),
            "evidence": " / ".join(lines)[:400],
        }
    names, printed, bench_unit = judges(header)
    if names:
        role = "sole" if len(names) == 1 else "member"
        given("profile.bench", [{"name": n[:300], "role": role} for n in names], printed, bench_unit)
    elif card.get("judges") or listed.get("judge"):
        name = card.get("judges") or listed["judge"]
        given(
            "profile.bench", [{"name": name.strip(" -"), "role": "sole"}], f"The portal's catalogue: {name}"
        )

    # Where and when it was published and fetched; the form of this text.
    when = provenance.get("retrieved_at") or "on an unrecorded date"
    fetched = f"Fetched {when} from {url}; SHA-256 {provenance.get('sha256')}"
    given("publication.kind", "official_website", f"The court's judgment portal: {url}")
    given("publication.name", PUBLICATION, f"The court's judgment portal: {url}")
    given(
        "publication.publisher",
        COURT,
        "The portal's own line: © National Industrial Court of Nigeria- ALL RIGHTS RESERVED.",
    )
    given("publication.url", url, fetched)
    given(
        "text.form",
        "official_website",
        "The text as the court's portal publishes it, not its certified true copy",
    )
    given("text.version.label", "As published on the court's judgment portal", fetched)
    values["expressions"] = [{"language": "en", "script": "Latn", "direction": "ltr", "this_text": "yes"}]
    evidence["expressions"] = {
        "value": values["expressions"],
        "page": None,
        "evidence": "The judgment is written in English",
    }
    # Not printed on the page: suggested as accounted for, for the reviewer to confirm.
    evidence["identity.citation"] = {
        "absent": "not_in_source",
        "page": None,
        "evidence": "The page prints no neutral citation",
    }
    evidence["text.summary.text"] = {
        "absent": "not_in_source",
        "page": None,
        "evidence": "The page has no headnote or summary",
    }
    evidence["language_rule.rule"] = {
        "absent": "not_applicable",
        "page": None,
        "evidence": "Published in English only",
    }
    if "profile.court.division" not in values:
        evidence["profile.court.division"] = {
            "absent": "not_in_source",
            "page": None,
            "evidence": "The header names no judicial division",
        }
    return values, evidence


# --- document ids, collections and preparing the stored pages -----------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def document_ids(items: dict) -> dict[str, str]:
    """A document id for each stored record: ng-nicn-<suit number>-<date delivered> as the catalogue reads
    them ("ng-nicn-la-497-2020-2026-01-08"); where several records share both, the lowest record id keeps
    that id and each other one adds its own ("…-r10436"), so an id never changes when a later record
    arrives; without a date, the record id is added always; without a suit number, the record id alone
    ("ng-nicn-r236")."""
    keys: dict[str, list[tuple[int, str]]] = {}
    for item_id, record in items.items():
        if record.get("source") != SOURCE or not record.get("sha256"):
            continue
        listed = record.get("listed") or {}
        record_id = int(listed.get("record_id") or 0)
        suit = re.sub(r"\s+", "", listed.get("suit_no") or "").upper()
        date = listed.get("judgment_date") or ""
        if suit and re.fullmatch(r"\d{4}-\d\d-\d\d", date):
            base = f"ng-nicn-{_slug(re.sub(r'^NICN/', '', suit))}-{date}"
        elif suit:
            base = f"ng-nicn-{_slug(re.sub(r'^NICN/', '', suit))}-r{record_id}"
        else:
            base = f"ng-nicn-r{record_id}"
        keys.setdefault(base, []).append((record_id, item_id))
    out = {}
    for base, held in keys.items():
        for k, (record_id, item_id) in enumerate(sorted(held)):
            out[item_id] = base if k == 0 or base.endswith(f"-r{record_id}") else f"{base}-r{record_id}"
    return out


RIGHTS_BASIS = (
    "Private review only. Nigerian texts are not published until the rights review the survey asks for "
    "(owner, 2026-10-22). The National Industrial Court's portal publishes its judgments without terms of use "
    'beyond its line "© National Industrial Court of Nigeria- ALL RIGHTS RESERVED." and has no robots.txt.'
)


def new_manifest() -> dict:
    return {
        "collection": "ng",
        "jurisdiction": "ng",
        "description": "Nigeria: official texts for review, from the official sources the owner approved on "
        "2026-10-22 (the National Industrial Court's judgments and rulings first)",
        "rights_basis": RIGHTS_BASIS,
        "documents": [],
    }


# What the review list needs of a document stays in the manifest; the details suggested and the evidence
# for them go in <id>.document.json, read only when the document is opened.
DETAILS = ("suggested", "suggested_evidence")


def split_entry(document: dict) -> tuple[dict, dict]:
    entry = {k: v for k, v in document.items() if k not in DETAILS and k != "summary"}
    if document.get("summary"):
        entry["page_count"] = document["summary"]["pages"]
        entry["flag_counts"] = document["summary"]["flags"]
    return entry, {k: document[k] for k in DETAILS if document.get(k) is not None}


def prepare_page(item_id: str, record: dict, raw: bytes, sidecar: dict, doc_id: str, duplicates: list[str]):
    """One stored record page as a review document: (manifest document, reading, provenance), or
    (None, reason) when the page holds no judgment."""
    sha = hashlib.sha256(raw).hexdigest()
    judgment = read(raw)
    if judgment.empty:
        return None, judgment.empty
    listed = sidecar.get("listed") or record.get("listed") or {}
    record_id = listed.get("record_id")
    kind = sidecar.get("type") or record.get("type") or "judgment"
    url = sidecar.get("url") or record.get("url")
    source_object = f"sources/{COUNTRY}/{SOURCE}/{sha}.html"
    provenance = {
        "url": url,
        "final_url": sidecar.get("final_url") or url,
        "retrieved_at": sidecar.get("retrieved_at") or record.get("last_fetched"),
        "sha256": sha,
        "bytes": len(raw),
        "media_type": "text/html",
        "source": SOURCE,
        "publisher": COURT,
        "record_id": record_id,
        "item": item_id,
        "source_object": source_object,
        "listing": listed,
        "header_as_printed": (sidecar.get("listed_raw") or record.get("listed_raw") or {}).get("header_text"),
        "portal_summary": judgment.summary,
        "furniture_left_out": judgment.furniture,
    }
    read_out = reading(judgment, sha, record_id)
    page_of = {id(unit): number for number, units in enumerate(paginate(judgment.units), 1) for unit in units}
    values, evidence = details(judgment, {"listed": listed}, kind, url, provenance, page_of)
    document = {
        "id": doc_id,
        "summary": read_out["summary"],
        "type": values["identity.type"],
        "title": values.get("identity.title") or doc_id,
        "year": int(values["identity.year"]) if str(values.get("identity.year", "")).isdigit() else None,
        "jurisdiction": COUNTRY,
        "language": "en",
        "url": url,
        "source_object": source_object,
        "media_type": "text/html",
        "court": values.get("profile.court.name"),
        "suit_numbers": values.get("profile.case_numbers") or [],
        "date": values.get("event:decided.date"),
        "record_id": record_id,
        "shape": judgment.shape,
        **({"likely_duplicates": duplicates} if duplicates else {}),
        "suggested": values,
        "suggested_evidence": evidence,
    }
    return (document, read_out, provenance), None


class FolderCollection:
    """Review documents in a folder (a check here): each document's reading, provenance and details,
    and the manifest, as the workbench reads a collection in private staging."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.documents: dict[str, dict] = {}

    def done(self, sha: str) -> bool:
        return False

    def write(self, document: dict, reading_: dict, provenance: dict, sha: str) -> dict:
        entry, details_ = split_entry(document)
        for suffix, data in (
            (".reading.json", reading_),
            (".provenance.json", provenance),
            (".document.json", details_),
        ):
            (self.folder / f"{document['id']}{suffix}").write_text(
                json.dumps(data, ensure_ascii=False, indent=1) + "\n"
            )
        self.documents[document["id"]] = entry
        return entry

    def finish(self, sha: str, entries: list[dict], skipped: str | None = None) -> None:
        pass

    def save_manifest(self) -> None:
        manifest = {**new_manifest(), "documents": sorted(self.documents.values(), key=lambda d: d["id"])}
        (self.folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n")


class BucketCollection:
    """Review documents in the corpus bucket, under collections/ng/, in the layout of Rwanda's
    (ingestion.rwanda.BucketCollection): each stored file's documents under files/<sha256>/, an index of
    them per file under issues/<sha256>.json (named as Rwanda's, a stored file being this collection's
    issue), and the manifest assembled from the indexes. No file is copied: a document names its source
    object. A file whose index was written by this `pipeline` (the reader's version) with the readings
    there are now is not prepared again; a PDF prepared before its OCR reading is (ingestion.ng_documents)."""

    def __init__(self, store, prefix: str = f"collections/{COUNTRY}/", pipeline: str = READER):
        self.store, self.prefix, self.pipeline = store, prefix, pipeline

    def done(self, sha: str, readings: dict | None = None) -> bool:
        name = f"{self.prefix}issues/{sha}.json"
        if not self.store.exists(name):
            return False
        index = json.loads(self.store.get(name))
        return (
            isinstance(index, dict)
            and index.get("pipeline") == self.pipeline
            and (index.get("readings") or {}) == (readings or {})
        )

    def _put(self, name: str, data) -> None:
        self.store.put(name, json.dumps(data, ensure_ascii=False).encode(), "application/json", replace=True)

    def write(self, document: dict, reading_: dict, provenance: dict, sha: str) -> dict:
        folder = f"{self.prefix}files/{sha}/"
        entry, details_ = split_entry(document)
        entry["objects"] = f"files/{sha}/"
        self._put(f"{folder}{document['id']}.reading.json", reading_)
        self._put(f"{folder}{document['id']}.provenance.json", provenance)
        self._put(f"{folder}{document['id']}.document.json", details_)
        return entry

    def finish(
        self,
        sha: str,
        entries: list[dict],
        skipped: str | None = None,
        source: str = SOURCE,
        readings: dict | None = None,
    ) -> None:
        index = {"pipeline": self.pipeline, "source": source, "documents": entries}
        if readings:
            index["readings"] = readings
        if skipped:
            index["skipped"] = skipped
        self._put(f"{self.prefix}issues/{sha}.json", index)

    def save_manifest(self) -> None:
        """The manifest from every index (any source of the collection), one document per id: of one
        judgment's records, the earliest; of one law found in two files (a Gazette issue and an agency's
        copy), the first, the others named on it (other_sources)."""
        documents: dict[str, dict] = {}
        skipped = 0
        for name in self.store.list(f"{self.prefix}issues/"):
            index = json.loads(self.store.get(name))
            skipped += bool(index.get("skipped"))
            for document in index.get("documents", []):
                held = documents.get(document["id"])
                if held is None:
                    documents[document["id"]] = document
                elif document.get("record_id") and held.get("record_id"):
                    if document["record_id"] < held["record_id"]:
                        documents[document["id"]] = document
                else:
                    other = {k: document.get(k) for k in ("url", "source_object", "objects")}
                    held.setdefault("other_sources", []).append(other)
        manifest = {
            **new_manifest(),
            "documents": sorted(documents.values(), key=lambda d: d["id"]),
            "skipped_files": skipped,
        }
        self._put(f"{self.prefix}manifest.json", manifest)


def prepare_items(items: dict, chosen: list[str], fetch, collection, log=print) -> dict:
    """Prepare stored record pages for review: `items` is the ledger's items (for the ids and the
    likely duplicates), `chosen` the items to prepare, `fetch(record)` returns a page's bytes and its
    sidecar. A page already prepared by this reader version is skipped; one that fails is noted and the
    rest go on."""
    ids = document_ids(items)
    counts = {"prepared": 0, "skipped_done": 0, "empty": 0, "failed": 0}
    for item_id in chosen:
        record = items[item_id]
        sha = record.get("sha256")
        if record.get("source") != SOURCE or not sha or item_id not in ids:
            continue
        if collection.done(sha):
            counts["skipped_done"] += 1
            continue
        try:
            raw, sidecar = fetch(record)
            duplicates = sorted(
                {ids[d["item"]] for d in record.get("likely_duplicates") or [] if d.get("item") in ids}
                - {ids[item_id]}
            )
            result, reason = prepare_page(item_id, record, raw, sidecar, ids[item_id], duplicates)
            if result is None:
                collection.finish(sha, [], reason)
                counts["empty"] += 1
                log(f"no judgment in {item_id}: {reason}")
                continue
            document, reading_, provenance = result
            entry = collection.write(document, reading_, provenance, sha)
            collection.finish(sha, [entry])
            counts["prepared"] += 1
        except Exception as error:  # noqa: BLE001 - noted and left for a later run
            counts["failed"] += 1
            log(f"failed {item_id}: {type(error).__name__}: {str(error)[:300]}")
    return counts


def bucket_fetch(store):
    """A stored page and its sidecar from the corpus bucket."""

    def fetch(record: dict) -> tuple[bytes, dict]:
        name = f"sources/{COUNTRY}/{SOURCE}/{record['sha256']}.html"
        return store.get(name), json.loads(store.get(name[: -len(".html")] + ".json"))

    return fetch


def folder_fetch(pages: Path):
    """Stored pages saved in a folder as <record id>.html (a check here), their sidecar from the ledger."""

    def fetch(record: dict) -> tuple[bytes, dict]:
        raw = (pages / f"{record['listed']['record_id']}.html").read_bytes()
        if hashlib.sha256(raw).hexdigest() != record["sha256"]:
            raise ValueError("this copy is not the stored page (its fingerprint differs)")
        sidecar = {
            "url": record["url"],
            "retrieved_at": record.get("last_fetched"),
            "type": record.get("type"),
            "listed": record.get("listed"),
            "listed_raw": record.get("listed_raw"),
        }
        return raw, sidecar

    return fetch


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ingestion.nicn", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("read", help="one stored page as the workbench reads it")
    one.add_argument("page", type=Path)
    many = sub.add_parser(
        "prepare", help="stored pages saved here (<record id>.html) into a folder collection"
    )
    many.add_argument("--pages", type=Path, required=True)
    many.add_argument("--out", type=Path, required=True)
    many.add_argument("--ledger", type=Path, default=Path("private-staging/ng/sync-ledger.json"))
    args = parser.parse_args(argv)
    if args.command == "read":
        judgment = read(args.page.read_bytes())
        print(
            json.dumps(
                reading(judgment, hashlib.sha256(args.page.read_bytes()).hexdigest()),
                ensure_ascii=False,
                indent=1,
            )
        )
        return 0
    items = json.loads(args.ledger.read_text())["items"]
    have = {p.stem for p in args.pages.glob("*.html")}
    chosen = [
        item_id
        for item_id, record in items.items()
        if record.get("source") == SOURCE and str((record.get("listed") or {}).get("record_id")) in have
    ]
    collection = FolderCollection(args.out)
    counts = prepare_items(items, sorted(chosen), folder_fetch(args.pages), collection)
    collection.save_manifest()
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
