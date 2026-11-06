"""Bound volumes of Nigerian laws cut into their instruments (ingestion.ng_documents.VOLUMES).

A volume (a year's Acts bound with the Gazette supplements printed beside them, a Region's year of Laws
and the subsidiary legislation made under them, a Governor's proclamations or rules bound together)
holds many instruments; filed as one document it stood under its first heading ("Interpretation Act,
1964"). Each instrument is printed from where it opens: its number ("1964, No. 11", "(L.S.) No. 8 1957",
"NO. 2. 1910", "Rule No. 3 of 1912", "W.R.L.N. 4 of 1957"), its title and its enacting words ("AN ACT
TO", "A LAW TO", "A PROCLAMATION", "BE IT ENACTED", "In exercise of the powers"). It runs to where the
next one opens, its later pages carrying its number or title in their running heads.

An instrument is cut, over whole pages, only when nothing says the cut is wrong:
- it opens at the top of its page, and the next one at the top of its own: a page two instruments share
  is held, pages being cut whole;
- its number is printed, and no running head over its pages names another number or title: a page that
  does ends it, and the pages from there are held as an instrument whose opening was not found;
- its printed page numbers run on without a gap, and its last page prints an ending (a signature, the
  clerk's certificate, the printer's line) or the next page of its series opens another: else a page of
  it may be missing from the scan;
- its title is found: the short title it is cited by; else, for a notice, the title the volume's index
  gives its number at its first page; else its printed heading;
- every page of it was read by the Layout Parser, and the Layout Parser's page is the OCR's page (a page
  read from another file was found on 2026-11-05);
- the volume's own tables, where it prints them, list no other title at its first printed page, no
  second instrument there, and no other instrument at a page inside it.

Pages before the first instrument (title pages, contents, tables, indexes), an index printed between
instruments, Bills and blank pages are left out: they are not instruments. An Act's title and arrangement
of sections printed before the page with its number are its own. A number printed twice is one
instrument when the two printings are one text (a page scanned twice: the copy that prints its ending is
kept), else both are held. What cannot be cut with confidence is held, with its reason.

Nothing here reads a PDF: it takes the reconciled reading (ingestion.ng_documents.cloud_reading).
"""

import re
from collections import Counter

YEAR = r"(?:18|19|20)\d\d"
ROMAN = r"(?=[IVXL])L?X{0,3}(?:IX|IV|V?I{0,3})"
# A notice's number: "L.N. 49 of 1964", "W.R.L.N. 4 of 1957", "E.R.L.N. No. 16 of 1954"; a rule's or
# regulation's: "Rule No. 3 of 1912".
NOTICE_NUMBER = re.compile(
    rf"(?:[WEN]\.?\s?R\.?\s?)?L\.\s?N\.?\s*(?:No\.?\s*)?(?P<number>\d{{1,4}})\s+of\s+(?P<year>{YEAR})\b", re.I
)
RULE_NUMBER = re.compile(
    rf"(?P<kind>Rule|Regulation)s?\s+No\.?\s*(?P<number>\d{{1,4}})\s+of\s+(?P<year>{YEAR})\b", re.I
)
# An Act's or Law's number as its running heads print it: "W.R. No. 12 of 1957", "1964, No. 11" (a Lagos
# Act's "1964, No. III").
HEAD_NUMBERS = (
    re.compile(rf"\b[WEN]\.\s?R\.\s?No\.?\s*(?P<number>\d{{1,4}})\s+of\s+(?P<year>{YEAR})\b", re.I),
    re.compile(rf"(?<!\d)(?P<year>{YEAR}),?\s+No\.?\s*(?P<number>\d{{1,4}}|{ROMAN})\b(?![.,]?\d)"),
)
# The same printed over its opening: "1964, No. 3" alone, a Region's assent "(L.S.) No. 8 1957", a
# proclamation's "NO. 2. 1910" (its head "NO. 4. 1910. 10").
ACT_NUMBER = re.compile(rf"(?P<year>{YEAR}),?\s+No\.?\s*(?P<number>\d{{1,4}}|{ROMAN})\.?")
ASSENT_NUMBER = re.compile(
    rf"\(L\.\s?S\.\)\s*\W?\s*No\.?\s*(?P<number>\d{{1,3}})[.,]?\s+(?P<year>{YEAR})\b", re.I
)
SERIAL_NUMBER = re.compile(rf"^NO\.?\s*(?P<number>\d{{1,3}})\b[.,]?\s+.{{0,30}}?(?P<year>{YEAR})\b", re.I)
# A Bill: its heading, or its running head printing the year and "No." with the number left blank
# ("1964, No. Navy").
BILL = re.compile(r"^(?:A\s+BILL\b|THE\s+.{3,160}\bBILL(?:,?\s*\d{4})?\.?$)", re.I)
BILL_HEAD = re.compile(rf"\b{YEAR},?\s+No\.?\s+(?![\dIVXL]+\b)[A-Z]|\bBILL\b")
# The words that open an instrument's body, or are printed over it.
ENACTING = re.compile(
    r"\b(?:A\s+LAW\s+TO|AN\s+ACT\s+TO|AN\s+ORDINANCE\s+TO|A\s+PROCLAMATION\b|BE\s+IT\s+(?:THEREFORE\s+)?ENACTED"
    r"|In\s+exercise\s+of\s+the\s+powers?|Enacted\s+by\s+the\s+Governor|(?:are|is)\s+hereby\s+made\s+and\s+published"
    r"|Assented\s+to\s+in\s+Her\s+Majesty|ARRANGEMENT\s+OF\s+SECTIONS)",
    re.I,
)
PROCLAMATION = re.compile(r"A\s+PROCLAMATION\.?", re.I)
# A line printed over an instrument's opening that is not its text: the Gazette supplement it was printed
# in, a page number, the motto, the assent, the Region's or Colony's name, a date of commencement.
HEADER = re.compile(
    r"^(?:Supplement\s+to\s+(?:the\s+)?(?:\w+\s+){0,3}Gazette\b.*|\(?\s*[A-D]?\s?\d{1,4}\.?\s*\)?|UNITY\s+AND\s+FAITH"
    r"|Assented\s+to\b.*|(?:Western|Eastern|Northern)\s+Region\s+of\s+Nigeria|Protectorate\s+of\s+Nigeria|Northern\s+Nigeria"
    r"|COLONY\s+OF\s+SOUTHERN\s+NIGERIA\.?|IN\s+THE\s+\w+\s+YEAR\s+OF\s+THE\s+REIGN\s+OF|HER\s+MAJESTY\s+QUEEN\s+ELIZABETH\s+II"
    r"|PUBLIC\s+NOTICE|DATE\s+OF\s+COMMENCEMENT\b.*|Commencement\s*:.*"
    rf"|{YEAR}\.?|No\.?\s*\d{{1,3}})$",
    re.I,
)
# An instrument's last page: its signature, the clerk's certificate, the printer's line.
ENDING = re.compile(
    r"(?i:\bprinted\s+impression\s+has\s+been\s+carefully\s+compared\b|\bpublished\s+by\s+authority\s+of\b"
    r"|\bprinted\s+(?:and\s+published\s+)?by\s+the\s+(?:acting\s+)?government\s+printer\b"
    r"|\bclerk\s+of\s+(?:the\s+)?executive\s+council\b)"
    r"|\b(?:MADE|Made|GIVEN|Given|DATED|Dated|Approved|ORDERED|Ordered|Passed)\b[^.]{0,160}?\b(?:this|the)\s+\d{1,2}"
    r"(?:st|nd|rd|th)?\s+day\s+of\b|\bEXPLANATORY\s+NOTE\b"
)
# A page that is not an instrument's: a volume's index or table.
INDEX = re.compile(
    r"^(?:CUMULATIVE\s+)?INDEX(?:\s+TO\b.*)?$|^TABLE\s+OF\s+(?:CONTENTS|LAWS|STATUTES|THE\s+[\w\s]+LAWS)\b|^(?:TABLE\s+OF\s+)?CONTENTS\.?$"
)
# The kinds of instrument a title may name (kind_of: the one it names last).
KINDS = (
    (re.compile(r"\bBYE-?\s?LAWS?\b", re.I), "bye_laws"),
    (re.compile(r"\bORDERS?\s+IN\s+COUNCIL\b", re.I), "order"),
    (re.compile(r"\bACT\b", re.I), "act"),
    (re.compile(r"\bLAW\b", re.I), "law"),
    (re.compile(r"\bORDINANCE\b", re.I), "ordinance"),
    (re.compile(r"\bPROCLAMATION\b", re.I), "proclamation"),
    (re.compile(r"\bREGULATIONS?\b", re.I), "regulation"),
    (re.compile(r"\bRULES?\b", re.I), "rules"),
    (re.compile(r"\bORDER\b", re.I), "order"),
    (re.compile(r"\bDECLARATION\b", re.I), "declaration"),
    (re.compile(r"\bNOTICE\b", re.I), "notice"),
    (re.compile(r"^INSTRUMENTS?\s+(?:ESTABLISHING|AMENDING|REVOKING)\b", re.I), "instrument"),
)
# "This Law may be cited as the Illiterates Protection Law, 1956": the title an instrument is cited by.
CITED = re.compile(
    r"\b(?:This|These)\s+(?:Act|Law|Ordinance|Proclamation|Regulations?|Rules?|Orders?(?:\s+in\s+Council)?"
    r"|Bye-?\s?laws?|Notice|Instrument)\s+may\s+be\s+cited\s+as\s+(?:the\s+)?[\"“]?(?:The\s+)?"
    r"(?P<title>[^\"”]{3,200}?\b(?:Act|Law|Ordinance|Proclamation|Regulations?|Rules?|Orders?(?:\s+in\s+Council)?"
    r"|Bye-?\s?[Ll]aws?|Notice|Declaration)\b,?\s*" + YEAR + r")\b",
    re.I,
)
# The law a notice is made under, printed over its title: "(No. 24 of 1950)", "(Cap. 127)", "(Chapter 140)".
ENABLING = re.compile(r"\((?:No\.?\s*\d+\s+of\s+\d{4}|Cap(?:\.|ter)\s*\d+)[^)]*\)\s*\.?$", re.I)
TOP = 6  # the nodes at the head of a page an Act's or Law's opening is looked for in
AGREE = 0.5  # the share of the Layout Parser's words the OCR must have on a page for the two to be one page
PRINTED = re.compile(r"(?<![\w.])(?P<series>[A-D])[ .\-]{0,2}(?P<number>\d{1,4})(?![\w])")
BARE = re.compile(r"^\(?\s*(?P<a>\d{1,3})\s*\)?(?=[\s.]|$)|(?<=\s)(?P<b>\d{1,3})\s*\)?\.?$")


def _texts(page: dict) -> list[tuple[str, str]]:
    """A page's nodes as (kind, text), its furniture apart and its tables as their cells' words."""
    out = []
    for node in page["nodes"]:
        if node["kind"] == "furniture":
            continue
        if node["kind"] == "table":
            cells = [c["text"] for row in node["header_rows"] + node["body_rows"] for c in row]
            out.append(("table", " ".join(cells)))
        else:
            out.append((node["kind"], (node.get("text") or "").strip()))
    return out


def _furniture(page: dict) -> list[str]:
    return [(f if isinstance(f, str) else f.get("text", "")).strip() for f in page.get("furniture") or []]


def blank(page: dict) -> bool:
    return sum(len(text.split()) for _, text in _texts(page)) < 5


def printed_number(page: dict) -> tuple[str, int] | None:
    """A page's printed number from its running head or foot: ("A", 17), or ("", 40) in a volume that
    prints no series; None where it prints none."""
    furniture = _furniture(page)
    for line in furniture:
        found = list(PRINTED.finditer(line))
        if found:
            ends = [m for m in found if m.start() == 0 or m.end() == len(line)]
            match = (ends or found)[0]
            return match.group("series"), int(match.group("number"))
    for line in furniture:
        match = BARE.search(re.sub(rf"\b{YEAR}\b\.?|\bNO\.?\s*\d{{1,3}}\b\.?", " ", line, flags=re.I).strip())
        if match:
            return "", int(match.group("a") or match.group("b"))
    return None


def number_in(text: str) -> tuple[str, str, str] | None:
    """The instrument number a running head (or a notice's line) prints: (series, number, year), the series
    telling a notice ("ln") or a rule ("rule") from an Act, Law or proclamation ("no")."""
    for series, pattern in (("ln", NOTICE_NUMBER), ("rule", RULE_NUMBER)):
        match = pattern.search(text)
        if match:
            return series, str(int(match.group("number"))), match.group("year")
    for pattern in HEAD_NUMBERS:
        match = pattern.search(text)
        if match:
            return "no", _number(match.group("number")), match.group("year")
    return None


def _number(number: str) -> str:
    return str(int(number)) if number.isdigit() else number.upper()


def same_number(one: tuple | None, other: tuple | None) -> bool:
    """Two numbers one, a roman one read with ones for its I's ("111" for "III") included."""
    if one is None or other is None:
        return one == other
    if one == other:
        return True

    def roman(number):
        return number.replace("1", "I").replace("L", "I") if set(number) <= set("1IL") else number

    return one[0] == other[0] and one[2] == other[2] and roman(one[1]) == roman(other[1])


def opening_number(block: list[str], furniture: list[str]) -> tuple[str, str, str] | None:
    """An Act's, Law's or proclamation's number as printed over its opening: "1964, No. 3" on a line of its
    own, the assent's "(L.S.) No. 8 1957", "NO. 2. 1910" (or that in the page's head)."""
    joined = " ".join(block)
    match = ASSENT_NUMBER.search(joined)
    if match is None:
        match = next((m for t in block for m in [ACT_NUMBER.fullmatch(t.strip(" ."))] if m), None)
    if match is None:
        match = next((m for t in [*block, joined, *furniture] for m in [SERIAL_NUMBER.match(t)] if m), None)
    if match is None:
        match = next((m for f in furniture for m in [HEAD_NUMBERS[1].search(f)] if m), None)
    return ("no", _number(match.group("number")), match.group("year")) if match else None


def kind_of(title: str) -> str:
    """The kind a title names last ("… (Amendment) Law, 1956": law; "… Adoptive Bye-laws Order, 1956": order),
    before what it is made under; "other" where it names none."""
    tail = re.split(r"\s+(?:made\s+)?under\s+", title, maxsplit=1, flags=re.I)[0]
    found = [(m.end(), -k, kind) for k, (pattern, kind) in enumerate(KINDS) for m in pattern.finditer(tail)]
    # The last named ("Local Government Law, 1957 : Appointed Day Notice": a notice).
    return max(found)[2] if found else "other"


def _title_line(kind: str, text: str) -> bool:
    """A printed title: "INTERPRETATION ACT, 1964", "The Lagos General Rate Order, 1964", in capitals or
    ending with its year, and naming its kind."""
    if kind == "table" or not text or len(text) > 200 or ENACTING.match(text) or ENABLING.search(text):
        return False
    return kind_of(text) != "other" and (text.upper() == text or re.search(rf"{YEAR}\.?$", text) is not None)


def agreement(page: dict) -> float:
    """The share of the Layout Parser's words on a page that the OCR also read there (1.0 with too few
    words to tell, or no OCR)."""
    ocr = (page.get("checks") or {}).get("docai-ocr")
    mine = Counter(w.lower() for _, t in _texts(page) for w in re.findall(r"[A-Za-z]{3,}", t))
    if ocr is None or sum(mine.values()) < 20:
        return 1.0
    theirs = Counter(w.lower() for w in re.findall(r"[A-Za-z]{3,}", ocr))
    return sum((mine & theirs).values()) / sum(mine.values())


def _number_line(text: str, kind: str = "paragraph") -> bool:
    """Whether a line is a notice's or rule's number printed over it ("W.R.L.N. 2 of 1957", "E.R.L.N. No. 2
    of 1954 The Markets Ordinance (Cap. 127)"), not a sentence citing one ("Rule No. 10 of 1911 is hereby
    revoked")."""
    match = NOTICE_NUMBER.match(text) or RULE_NUMBER.match(text)
    if match is None or kind in ("list_item", "table"):
        return False
    rest = text[match.end() :].strip(" .,:;")
    return not rest or (
        rest[0].isupper() and not re.search(r"\b(?:is|are|shall|was|were|has|have|hereby)\b", rest)
    )


def _title_read_as_head(page: dict) -> str | None:
    """A title the Layout Parser read as the page's head ("NAVY ACT, 1964 A 189"), its page number apart."""
    for line in _furniture(page):
        line = PRINTED.sub("", line).strip(" .")
        if _title_line("heading", line) and line.upper() == line:
            return line
    return None


def openings(page: dict) -> list[dict]:
    """The instruments that open on a page, each with its node, its number where printed with it, its
    title block, whether text of another instrument is printed above it (`shared`), and whether it is a
    Bill. A notice or rule opens wherever its number begins a line (or, read as the page's head, over its
    enacting words); an Act, Law or proclamation at the head of a page, its title or number with its
    enacting words."""
    texts = _texts(page)
    furniture = _furniture(page)
    out = []
    for k, (kind, text) in enumerate(texts):
        block = [t for _, t in texts[k : k + TOP]]
        window = " ".join(t[:400] for t in block)
        number = None
        if _number_line(text, kind):
            number = number_in(text[:120])
        elif not out and k < TOP:
            joined = _joined_heading(texts, k) or (_title_read_as_head(page) if k == 0 else None)
            titled = bool(joined) or bool(BILL.match(text) or PROCLAMATION.fullmatch(text))
            numbered = opening_number(block[:3], [])
            # A notice whose number the Layout Parser read as the page's head, the law it is made under
            # or its own title its first line.
            head = next((n for f in furniture for n in [number_in(f)] if n and n[0] == "ln"), None)
            if (
                head
                and k == 0
                and (
                    titled
                    or re.search(r"In\s+exercise\s+of|PUBLIC\s+NOTICE|GIVEN\s+at", window, re.I)
                    or (kind == "heading" and text.upper() == text and len(text) < 200)
                )
            ):
                number = head
            elif (numbered or titled) and ENACTING.search(window):
                number = numbered or opening_number(block, furniture) or ("no", None, None)
            else:
                continue
        else:
            continue
        above = [
            t
            for kind_above, t in texts[(out[-1]["node"] + 1 if out else 0) : k]
            if t
            and not HEADER.match(t)
            and not _title_line(kind_above, t)
            and not number_in(t[:120])
            and not opening_number([t], [])
            and not (kind_above == "heading" and _joined_heading(texts, k - 1))
        ]
        if opening_number([" ".join(above)], []) or sum(len(t.split()) for t in above) < 3:
            above = []  # its number, read in pieces ("NO." and "8. 1910.")
        out.append(
            {
                "node": k,
                "number": number if number[1] else None,
                "shared": bool(out) or bool(above),
                "bill": any(BILL.match(t) for _, t in texts[max(0, k - 2) : k + TOP])
                or (number[0] == "no" and any(BILL_HEAD.search(f) for f in furniture)),
                "block": block,
                "heading": next(
                    (h for j in range(k, min(k + TOP, len(texts))) for h in [_joined_heading(texts, j)] if h),
                    _title_read_as_head(page) if k == 0 else None,
                ),
            }
        )
    if not out and any(BILL_HEAD.search(f) for f in furniture) and not blank(page):
        out.append({"node": 0, "number": None, "shared": False, "bill": True, "block": [], "heading": None})
    return out


def _joined_heading(texts: list[tuple[str, str]], k: int) -> str | None:
    """A printed title at node k, or one printed over two lines ("DEFENCE INDUSTRIES CORPORATION OF
    NIGERIA" / "ACT, 1964")."""
    if k < 0 or k >= len(texts):
        return None
    kind, text = texts[k]
    if _title_line(kind, text):
        return text
    if kind == "heading" and k + 1 < len(texts) and texts[k + 1][0] == "heading":
        joined = f"{text} {texts[k + 1][1]}"
        if re.match(
            rf"^(?:\(.*\)\s+)?(?:ACT|LAW|ORDINANCE|PROCLAMATION|REGULATIONS?|RULES|ORDER)\b,?\s*{YEAR}\.?$",
            texts[k + 1][1],
        ):
            return joined if _title_line("heading", joined) else None
    return None


def _own_text(pages: list[dict], opened: dict) -> str:
    """An instrument's words from where it opens, its first pages before the rest (an Act prints its short
    title last)."""
    first = _texts(pages[0])[opened["node"] :]
    return " ".join([t for _, t in first] + [t for p in pages[1:] for _, t in _texts(p)])


def _plain(title: str, heading: str | None = None) -> str:
    """A title as cited, its line-break hyphens joined ("Contribu- tions"; "Com-pensation" where its printed
    heading has the word whole)."""
    title = re.sub(r"(\w)-\s+(?=[a-z])", r"\1", re.sub(r"\s+", " ", title)).strip(' .,;:"“”')
    if heading:
        whole = {w.lower() for w in re.findall(r"[A-Za-z]+", heading)}
        title = re.sub(
            r"\b([A-Za-z]+)-([a-z]+)\b",
            lambda m: m.group(1) + m.group(2) if (m.group(1) + m.group(2)).lower() in whole else m.group(0),
            title,
        )
    return title[0].upper() + title[1:] if title else title


def title_of(pages: list[dict], opened: dict, number: tuple | None, index: dict) -> tuple[str | None, str]:
    """An instrument's title and where it was found: the title it is cited by, from its own words; else, for
    a notice, the title the volume's index gives its number at its own first page; else its printed
    heading (an Act's or Law's); (None, "") when none is found."""
    cited = CITED.search(_own_text(pages, opened))
    if cited:
        return _plain(cited.group("title"), opened.get("heading")), "cited"
    if number and number[0] in ("ln", "rule"):
        listed = index.get(number)
        if listed and listed[1] == printed_number(pages[0]) and kind_of(listed[0]) != "other":
            return _plain(listed[0]), "index"
        if number[0] == "rule":
            return _rule_title(opened, number), "number"
        heading = _notice_heading(opened["block"])
        return (_plain(heading), "heading") if heading else (None, "")
    heading = opened.get("heading")
    if heading and not PROCLAMATION.fullmatch(heading):
        return _plain(heading), "heading"
    return None, ""


def _notice_heading(block: list[str]) -> str | None:
    """A notice's printed title: the first line over its enacting words naming a subsidiary instrument and
    its year ("The Lagos General Rate Order, 1964"), or an instrument establishing or amending a council;
    never the law it is made under, printed first."""
    for text in block[1:]:
        if ENACTING.search(text[:60]) or re.match(r"(?:SCHEDULE|EXPLANATORY|\d+\.)", text):
            break
        if ENABLING.search(text) or HEADER.match(text) or text.startswith("("):
            continue  # the law it is made under, or the end of a title printed over two lines
        if re.match(r"^INSTRUMENTS?\s+(?:ESTABLISHING|AMENDING|REVOKING)\b", text, re.I) and len(text) < 200:
            return text
        if (
            _title_line("heading", text)
            and kind_of(text) not in ("act", "law", "ordinance", "proclamation")
            and re.search(rf"{YEAR}\.?$", text)
        ):
            return text
    return None


def _rule_title(opened: dict, number: tuple[str, str, str]) -> str:
    """A rule's title as printed with its number: "Rule No. 1 of 1912 made under the Lagos Harbour Dues
    Ordinance, 1910"."""
    head = RULE_NUMBER.search(" ".join(opened["block"][:1]))
    label = f"{head.group('kind').capitalize() if head else 'Rule'} No. {number[1]} of {number[2]}"
    block = opened["block"]
    for k, text in enumerate(block):
        under = None
        if re.fullmatch(r"MADE\s+UNDER\.?", text, re.I) and k + 1 < len(block):
            under = block[k + 1]
        made = re.match(
            r"^(?:(?:RULES?|REGULATIONS?)\s+(?:FOR\s+.*?\s+)?)?MADE\s+UNDER\s+(.+?)(?:\s+At\s+a\s+meeting\b.*)?$",
            text,
        )
        if made:
            under = made.group(1)
        if under:
            under = re.sub(r"^THE\s+", "", under.strip(' ."“”')).strip(' ."“”')
            return f"{label} made under the {under.title()}"
    return label


def contents(pages: list[dict]) -> tuple[dict, dict]:
    """What a volume's own tables list: the titles at each printed page ("Chiefs Law, 1957 | A 201") and
    notices' titles by number ("Aid to Pioneer Industries (No. 1) Order, 1957 | 129 | B 164")."""
    by_page, by_number = {}, {}
    for page in pages:
        heads = " ".join(t for _, t in _texts(page)[:3]) + " " + " ".join(_furniture(page))
        series = "ln" if re.search(r"L\.\s?N\.", heads) else None
        for node in page["nodes"]:
            if node["kind"] != "table":
                continue
            header = " ".join(c["text"] for row in node["header_rows"] for c in row)
            notices = series or ("ln" if re.search(r"L\.\s?N\.", header) else None)
            for row in node["header_rows"] + node["body_rows"]:
                cells = [c["text"].strip() for c in row]
                if len(cells) < 2:
                    continue
                at = re.fullmatch(r"([A-D])\s?(\d{1,4})", cells[-1].replace(".", " ").strip())
                title = re.sub(r"(?:\s*\.){2,}.*$|(?:\s*\.\.\.)+\s*$|(?:\s*…)+\s*$", "", cells[0]).strip()
                if not at or not re.search(r"[A-Za-z]{3}", title) or title.lower() == "title":
                    continue
                by_page.setdefault((at.group(1), int(at.group(2))), [])
                if title not in by_page[(at.group(1), int(at.group(2)))]:
                    by_page[(at.group(1), int(at.group(2)))].append(title)
                if notices and len(cells) >= 3 and re.fullmatch(r"\d{1,4}", cells[-2]):
                    by_number.setdefault(str(int(cells[-2])), (title, (at.group(1), int(at.group(2))), None))
    return by_page, by_number


def same_title(one: str, other: str) -> bool:
    def words(text):
        return {
            w
            for w in re.findall(r"[a-z]{3,}", text.lower())
            if w not in {"the", "and", "law", "act", "order", "of"}
        }

    a, b = words(one), words(other)
    return bool(a and b) and len(a & b) >= 0.6 * min(len(a), len(b))


def blanks_counted(pages: list[dict]) -> bool:
    """Whether a volume's page numbers count its blank pages (a Gazette supplement's do; a Governor's
    proclamations, printed one by one, leave the back of each last page unnumbered)."""
    votes: Counter = Counter()
    numbers = {p["page"]: printed_number(p) for p in pages}
    by_page = {p["page"]: p for p in pages}
    for page in pages:
        if not blank(page) or page["page"] + 1 not in by_page or blank(by_page[page["page"] + 1]):
            continue
        before, after = numbers.get(page["page"] - 1), numbers.get(page["page"] + 1)
        if before and after and before[0] == after[0]:
            votes[after[1] - before[1]] += 1
    return votes[2] >= votes[1]


def gaps(pages: list[dict], counted: bool = True) -> list[str]:
    """What the printed page numbers of an instrument's pages say is wrong: pages missing ("printed pages
    A 48–A 49 missing from the scan"), a page scanned twice, numbers going back. A number read wrongly on
    one page (its neighbours agreeing with each other) is passed over."""
    numbered = [(p["page"], n) for p in pages for n in [printed_number(p)] if n]

    def agree(one, other):
        (page, number), (other_page, other_number) = one, other
        return other_number[0] == number[0] and 0 < (other_number[1] - number[1]) * (
            1 if other_page > page else -1
        ) <= abs(other_page - page)

    def missing(one, other) -> bool:
        """Whether two pages' numbers could be read right with pages missing between them: a few, ahead."""
        (page, number), (other_page, other_number) = sorted([one, other])
        return number[0] == other_number[0] and 0 < other_number[1] - number[1] - (other_page - page) <= 10

    trusted = []
    for k, here in enumerate(numbered):
        before = numbered[k - 1] if k else None
        after = numbered[k + 1] if k + 1 < len(numbered) else None
        if before and after:
            if agree(before, after) and not agree(here, before) and not agree(here, after):
                continue  # read wrongly, its neighbours agreeing with each other
        elif before or after:
            other = before or after
            if not agree(here, other) and not missing(here, other):
                continue  # at its first or last page, read wrongly: no few pages missing would explain it
        trusted.append(here)
    by_page = {p["page"]: p for p in pages}
    out = []
    for (page, number), (later, after) in zip(trusted, trusted[1:], strict=False):
        series = f"{number[0]} " if number[0] else ""
        if after[0] != number[0]:
            out.append(f"its printed pages change series between pages {page} and {later}")
            continue
        printed = after[1] - number[1]
        between = range(page + 1, later)
        expected = len(between) + 1 if counted else len([p for p in between if not blank(by_page[p])]) + 1
        if printed == 0:
            out.append(f"printed page {series}{number[1]} is scanned twice (pages {page} and {later})")
        elif printed < 0:
            out.append(f"its printed page numbers go back between pages {page} and {later}")
        elif printed > expected:
            first, last = number[1] + expected, after[1] - 1
            label = f"{series}{first}" if first == last else f"{series}{first}–{series}{last}"
            out.append(f"printed page{'s' if first != last else ''} {label} missing from the scan")
    # A page between two that read the same number twice over (A 17, blank, A 17) is the same page twice.
    for (page, number), (later, after) in zip(numbered, numbered[1:], strict=False):
        if (
            number == after
            and (page, number) not in trusted
            and f"scanned twice (pages {page}" not in " ".join(out)
        ):
            out.append(
                f"printed page {number[0]} {number[1]} is scanned twice (pages {page} and {later})".strip()
            )
    return out


def _index_page(page: dict) -> bool:
    """A page of the volume's indexes or tables, not of an instrument (whose own running head names it)."""
    if any(n and n[0] == "no" for f in _furniture(page) for n in [number_in(f)]):
        return False
    return any(INDEX.match(t) for _, t in _texts(page)[:2]) or any(
        re.match(r"(?:CUMULATIVE\s+)?INDEX\b", f, re.I) for f in _furniture(page)
    )


def head_title(page: dict) -> str | None:
    """The title words a page's running head prints ("62 The Statute Laws Revision Proclamation 1910"), its
    page and instrument numbers apart; None for a head naming no instrument (the Gazette's or printer's
    line, a schedule's heading) or of fewer than two words."""
    for line in _furniture(page):
        if (
            HEADER.match(line)
            or ENDING.search(line)
            or re.search(r"\bSupplement\s+to\b|\bGazette\b", line, re.I)
        ):
            continue
        if not any(pattern.search(line) for pattern, _ in KINDS):
            continue  # a part's or schedule's heading, the printer's line: not an instrument's title
        words = PRINTED.sub(" ", line)
        for pattern in (NOTICE_NUMBER, RULE_NUMBER, *HEAD_NUMBERS):
            words = pattern.sub(" ", words)
        words = re.sub(rf"\b(?:{YEAR}|No\.?|of|the)\b|[^A-Za-z' ]", " ", words, flags=re.I)
        if len([w for w in words.split() if len(w) > 2]) >= 2:
            return " ".join(words.split())
    return None


def _heads(page: dict) -> list[tuple[str, str, str]]:
    return [n for f in _furniture(page) for n in [number_in(f)] if n]


def cut(reading: dict, unread: int = 0) -> dict:
    """A volume's instruments: {"instruments": [...], "held": [...], "left": [...]}. Each instrument has its
    pages, series, number, year, title and kind; each held one the same and its reason; each run of pages
    left out the reason it is not an instrument. `unread` counts the pages after the reading the Layout
    Parser has not read (the volume's OCR read more)."""
    pages = reading["pages"]
    by_page = {p["page"]: p for p in pages}
    counted = blanks_counted(pages)
    opens = [{**found, "page": page["page"]} for page in pages for found in openings(page)]
    years = Counter(o["number"][2] for o in opens if o["number"] and o["number"][0] in ("ln", "rule"))
    usual = years.most_common(1)[0][0] if years else None
    out: dict = {"instruments": [], "held": [], "left": []}
    first = opens[0]["page"] if opens else pages[-1]["page"] + 1
    by_printed, by_number = contents([p for p in pages if p["page"] < first])
    # The notices' index may be printed among them, as a cumulative index.
    for page in pages:
        if page["page"] >= first and _index_page(page):
            for key, value in contents([page])[1].items():
                by_number.setdefault(key, value)
    index = (
        {("ln", number, usual): (title, at) for number, (title, at, _) in by_number.items()} if usual else {}
    )
    if first > pages[0]["page"]:
        out["left"].append(
            {"pages": [pages[0]["page"], first - 1], "reason": "title pages, tables and indexes"}
        )
    # Each opening runs to the next; an index or table printed after it is none of its own, nor are pages
    # whose running heads name another number (an instrument whose opening is not in the scan).
    segments = []
    for k, opened in enumerate(opens):
        after = opens[k + 1] if k + 1 < len(opens) else None
        end = (after["page"] + 1 if after["shared"] else after["page"]) if after else pages[-1]["page"] + 1
        own = [p for p in range(opened["page"], end) if p in by_page]
        for n, page_number in enumerate(own[1:], 1):
            if _index_page(by_page[page_number]):
                out["left"].append(
                    {"pages": [page_number, own[-1]], "reason": "an index or table of the volume"}
                )
                own = own[:n]
                break
        if opened["bill"]:
            out["left"].append({"pages": [own[0], own[-1]], "reason": "a Bill, not a law"})
            continue
        number = opened["number"]
        if number is None:
            # An Act's number printed in the running heads of its later pages only.
            heads = Counter(n for p in own[1:3] for n in _heads(by_page[p]) if n[0] == "no")
            number = heads.most_common(1)[0][0] if heads else None
        rest: list[int] = []
        named = " ".join(filter(None, [opened.get("heading"), *(head_title(by_page[p]) for p in own[1:3])]))
        for n, page_number in enumerate(own[1:], 1):
            page = by_page[page_number]
            other = [h for h in _heads(page) if number and h[0] == number[0] and not same_number(h, number)]
            titled = head_title(page) if not _heads(page) else None
            if other or (titled and n >= 3 and not same_title(titled, named)):
                own, rest = own[:n], own[n:]
                break
        while len(own) > 1 and blank(by_page[own[-1]]):
            own = own[:-1]  # the back of its last page
        segments.append(
            {
                "opened": opened,
                "pages": own,
                "number": number,
                "shared_after": bool(after and after["shared"] and after["page"] == own[-1] and not rest),
                "next": after,
                "cut_short": bool(rest),
            }
        )
        while len(rest) > 1 and blank(by_page[rest[-1]]):
            rest = rest[:-1]
        if rest:
            other = next(
                (
                    h
                    for h in _heads(by_page[rest[0]])
                    if number and h[0] == number[0] and not same_number(h, number)
                ),
                None,
            )
            segments.append(
                {
                    "opened": {
                        "node": 0,
                        "number": other,
                        "shared": False,
                        "bill": False,
                        "block": [],
                        "heading": None,
                    },
                    "pages": rest,
                    "number": other,
                    "shared_after": bool(after and after["shared"]),
                    "next": after,
                    "fragment": True,
                }
            )
    # One instrument opened twice in a row: its arrangement of sections, then its body; or its first page
    # scanned twice, the earlier copy left out.
    merged: list[dict] = []
    for segment in segments:
        before = merged[-1] if merged else None
        untitled = (
            before
            and before["number"] is None
            and segment["number"]
            and before["opened"].get("heading")
            and same_title(
                before["opened"]["heading"],
                " ".join(
                    [segment["opened"].get("heading") or ""]
                    + [f for p in segment["pages"][:3] for f in _furniture(by_page[p])]
                ),
            )
            and all(
                blank(by_page[p]) for p in range(before["pages"][-1] + 1, segment["pages"][0]) if p in by_page
            )
        )
        if untitled:
            before["number"] = segment["number"]
        if (
            before
            and segment["number"]
            and before["number"] == segment["number"]
            and not segment["opened"]["shared"]
            and not before["shared_after"]
            and not segment.get("fragment")
            and not before.get("fragment")
        ):
            if printed_number(by_page[before["pages"][0]]) == printed_number(by_page[segment["pages"][0]]):
                # The copy that ends is kept: the earlier where it prints its ending, else the later.
                last = by_page[before["pages"][-1]]
                if ENDING.search(" ".join([t for _, t in _texts(last)] + _furniture(last))):
                    copy = [segment["pages"][0], segment["pages"][-1]]
                    before["next"], before["shared_after"] = segment["next"], segment["shared_after"]
                else:
                    copy, merged[-1] = [before["pages"][0], segment["pages"][0] - 1], segment
                out["left"].append({"pages": copy, "reason": f"No. {segment['number'][1]} printed again"})
            else:
                pages_between = range(before["pages"][-1] + 1, segment["pages"][0])
                merged[-1] = {
                    **before,
                    "pages": before["pages"] + [p for p in pages_between if p in by_page] + segment["pages"],
                    "shared_after": segment["shared_after"],
                    "next": segment["next"],
                }
            continue
        merged.append(segment)
    found = []
    for k, segment in enumerate(merged):
        opened, own, number = segment["opened"], segment["pages"], segment["number"]
        documents = [by_page[p] for p in own]
        title, source = (
            title_of(documents, opened, number, index) if not segment.get("fragment") else (None, "")
        )
        kind = kind_of(title or "")
        if number and number[0] == "rule":
            head = RULE_NUMBER.search(" ".join(opened["block"][:1]))
            kind = {"rule": "rules", "regulation": "regulation"}.get(
                head.group("kind").lower() if head else "", kind
            )
        elif kind == "other" and any(re.match(r"PUBLIC\s+NOTICE", t, re.I) for t in opened["block"][:3]):
            kind = "notice"
        entry = {
            "pages": own,
            "series": number[0] if number else None,
            "number": number[1] if number else None,
            "year": number[2] if number else None,
            "title": title or "",
            "title_from": source,
            "kind": kind,
            "printed": printed_number(documents[0]),
        }
        reasons = []
        if segment.get("fragment"):
            named = (
                f"No. {number[1]} of {number[2]}"
                if number
                else head_title(documents[0]) or "another instrument"
            )
            reasons.append(f"its running heads name {named}, whose opening was not found")
        if number is None:
            reasons.append("no number is printed with it")
        elif number[0] in ("ln", "rule") and usual and number[2] != usual:
            reasons.append(f"its number is printed with the year {number[2]} among the volume's of {usual}")
        if title is None and not segment.get("fragment"):
            reasons.append("its title was not found")
        if opened["shared"]:
            reasons.append(f"it shares page {own[0]} with the instrument before it")
        if segment["shared_after"]:
            reasons.append(f"it shares page {own[-1]} with the instrument after it")
        reasons += gaps(documents, counted)
        foreign = [p["page"] for p in documents if agreement(p) < AGREE]
        if foreign:
            reasons.append(
                f"the Layout Parser's reading of page {', '.join(map(str, foreign))} is not the OCR's page"
            )
        # Its end: an ending printed on its last page, or the next page of its series opening another.
        last = documents[-1]
        ending = ENDING.search(" ".join([t for _, t in _texts(last)] + _furniture(last))) is not None
        mine = printed_number(last)
        later = next(
            (
                s
                for s in merged[k + 1 :]
                if mine
                and printed_number(by_page[s["pages"][0]])
                and printed_number(by_page[s["pages"][0]])[0] == mine[0]
            ),
            None,
        )
        follows = False
        if later and not later["opened"]["shared"] and not later.get("fragment"):
            theirs = printed_number(by_page[later["pages"][0]])
            between = range(own[-1] + 1, later["pages"][0])
            adjacent = all(p not in by_page or blank(by_page[p]) for p in between)
            expected = 1 + (len(between) if counted and adjacent else 0)
            follows = theirs[1] - mine[1] == expected
        if not ending and not follows and not segment["shared_after"]:
            reasons.append(
                "its last page prints no ending and the next page of its series does not open another"
            )
        if k + 1 == len(merged) and unread:
            reasons.append(
                f"the Layout Parser has not read the volume's last {unread} pages, where it may go on"
            )
        reasons += _against_tables(entry, documents, by_printed, opened["shared"])
        if reasons:
            entry["reason"] = "; ".join(reasons)
        found.append(entry)
    _printed_twice(found, by_page)
    for entry in found:
        if entry.get("copy"):
            out["left"].append(
                {
                    "pages": [entry["pages"][0], entry["pages"][-1]],
                    "reason": f"No. {entry['number']} printed again",
                }
            )
        elif entry.get("reason"):
            out["held"].append(entry)
        else:
            out["instruments"].append(entry)
    if unread:
        out["held"].append(
            {
                "pages": [],
                "title": "",
                "kind": "other",
                "reason": f"the Layout Parser has not read the volume's last {unread} pages",
                "unread": unread,
            }
        )
    # Runs of pages left out for one reason, one after another, as one.
    left: list[dict] = []
    for entry in sorted(out["left"], key=lambda e: e["pages"][0]):
        if left and left[-1]["reason"] == entry["reason"] and entry["pages"][0] <= left[-1]["pages"][1] + 1:
            left[-1]["pages"][1] = max(left[-1]["pages"][1], entry["pages"][1])
        else:
            left.append({**entry, "pages": list(entry["pages"])})
    out["left"] = left
    return out


def _against_tables(entry: dict, documents: list[dict], tables: dict, shared: bool) -> list[str]:
    """What the volume's own tables say against a cut: another title at its first page, two instruments
    listed there (a page it shares with one not found), another instrument listed at a page inside it."""
    out = []
    title = entry["title"]
    listed = tables.get(entry["printed"]) if entry["printed"] else None
    if listed and not shared:
        entry["listed_in_volume"] = listed[0]
        if title and not any(same_title(one, title) for one in listed):
            out.append(f"the volume's table lists another title at its first page ({listed[0]})")
        elif len(listed) > 1:
            out.append(f"the volume's table lists {len(listed)} instruments at its first page")
    inside = sorted(
        {
            (number, one)
            for page in documents[1:]
            for number in [printed_number(page)]
            if number and number != entry["printed"]
            for one in tables.get(number, [])
            if not (title and same_title(one, title))
        }
    )
    if inside:
        (series, at), one = inside[0]
        out.append(
            f"the volume's table lists another instrument inside it ({one}, at {series} {at})".replace(
                "  ", " "
            )
        )
    return out


def _printed_twice(found: list[dict], by_page: dict) -> None:
    """One number printed twice: one instrument where the printings are one text (the later copy left out),
    else both held."""
    groups: dict[tuple, list[dict]] = {}
    for entry in found:
        if entry["number"]:
            groups.setdefault((entry["series"], entry["number"], entry["year"]), []).append(entry)
    for group in groups.values():
        if len(group) < 2:
            continue
        words = [
            Counter(
                w.lower()
                for p in e["pages"]
                for w in re.findall(r"[A-Za-z]{3,}", by_page[p].get("text") or "")
            )
            for e in group
        ]
        same = all(
            sum((words[0] & other).values()) >= 0.9 * max(sum(words[0].values()), sum(other.values()))
            for other in words[1:]
        )
        kept = next((e for e in group if "reason" not in e), group[0]) if same else None
        for entry in group:
            if entry is kept:
                continue
            if same:
                entry["copy"] = True
            else:
                entry["reason"] = "; ".join(
                    filter(None, [entry.get("reason"), "another text is printed with the same number"])
                )
