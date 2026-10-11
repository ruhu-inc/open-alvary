"""Document details: the comprehensive record a reader sees beside the text (standard DETAILS-1).

Every document carries the same set of details: identity; the steps by which it became law and
their dates; the gazette that published it; the form and version of the text; its legal status
and its links to other laws; its language; its official source. Each detail is either given or,
when a reviewer confirms the record, accounted for: not stated in this copy, not applicable, or
not yet known. Nothing is left silently blank.

Suggestions are read from the document itself (the gazette front page, the enacting words, the
schedule of the Bill, the Clerk's certificate and the President's assent), each with the page it
came from, for a reviewer to confirm or correct. Suggestions are never published as such.
"""

import re
from dataclasses import dataclass
from datetime import date

from ingestion.first_collection import DOCUMENT_TYPES

STANDARD = "details-1"
LEGISLATION = {"act", "bill", "regulation", "order"}
PASSED = {"act", "bill"}


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    group: str
    kind: str  # text | long | date | year | choice | url | language | jurisdiction | list | relations
    choices: tuple[str, ...] = ()
    required: bool = True  # must be given or accounted for before the record is confirmed
    types: frozenset[str] | None = None  # document types it applies to; None: all
    help: str = ""


GROUPS = (
    ("identity", "Identity"),
    ("passage", "Passage and assent"),
    ("publication", "Publication"),
    ("text", "The text"),
    ("status", "Status and related laws"),
    ("language", "Language"),
    ("source", "Official source"),
)
TEXT_FORMS = ("gazette_supplement", "authenticated_bill", "official_reprint", "other")
CONSOLIDATION = ("not_consolidated", "consolidated")
LEGAL_STATUS = ("unknown", "current", "historical", "repealed", "superseded")
LANGUAGE_STATUS = ("original", "official_translation", "unofficial_translation")
ISSUES = ("ordinary", "extraordinary")
RELATIONS = (
    "repeals",
    "amends",
    "amended_by",
    "repealed_by",
    "supersedes",
    "superseded_by",
    "cross_reference",
)
CHOICE_LABELS = {
    "gazette_supplement": "Gazette supplement (the Act as published)",
    "authenticated_bill": "Certified Bill (as passed and authenticated, before gazetting)",
    "official_reprint": "Official reprint",
    "not_consolidated": "Not consolidated (as enacted)",
    "consolidated": "Consolidated (amendments incorporated)",
    "unknown": "Not yet checked",
    "current": "In force",
    "historical": "Historical",
    "repealed": "Repealed",
    "superseded": "Superseded",
    "original": "Original language",
    "official_translation": "Official translation",
    "unofficial_translation": "Unofficial translation",
    "ordinary": "Ordinary",
    "extraordinary": "Extraordinary",
    "repeals": "Repeals",
    "amends": "Amends",
    "amended_by": "Amended by",
    "repealed_by": "Repealed by",
    "supersedes": "Supersedes",
    "superseded_by": "Superseded by",
    "cross_reference": "Refers to",
}
ABSENT = {
    "not_in_source": "Not stated in this copy",
    "not_applicable": "Not applicable",
    "unknown": "Not yet known",
}

FIELDS = (
    Field("short_title", "Short title", "identity", "text"),
    Field("long_title", "Long title", "identity", "long", types=frozenset(LEGISLATION)),
    Field("document_type", "Type", "identity", "choice", tuple(sorted(DOCUMENT_TYPES))),
    Field("jurisdiction", "Jurisdiction", "identity", "jurisdiction", help="ISO 3166 code, e.g. ng"),
    Field("number", "Number", "identity", "text", help="The Act number given on gazetting, e.g. 8"),
    Field("year", "Year", "identity", "year"),
    Field("citation", "Citation", "identity", "text", help="e.g. Copyright Act, No. 8, 2022"),
    Field("enacting_body", "Enacted by", "identity", "text", types=frozenset(LEGISLATION)),
    Field("bill_title", "Bill", "passage", "text", types=frozenset(PASSED), help="The Bill's short title"),
    Field("passed_senate", "Passed by the Senate", "passage", "date", types=frozenset(PASSED)),
    Field(
        "passed_house", "Passed by the House of Representatives", "passage", "date", types=frozenset(PASSED)
    ),
    Field("authenticated_on", "Certified by the Clerk", "passage", "date", types=frozenset(PASSED)),
    Field("authenticated_by", "Clerk", "passage", "text", types=frozenset(PASSED)),
    Field("assent_date", "Assented to", "passage", "date", types=frozenset(PASSED)),
    Field("assented_by", "Assented to by", "passage", "text", types=frozenset(PASSED)),
    Field("commencement_date", "Commencement", "passage", "date", types=frozenset(LEGISLATION)),
    Field("gazette_name", "Gazette", "publication", "text"),
    Field("gazette_number", "Gazette number", "publication", "text"),
    Field("gazette_volume", "Volume", "publication", "text"),
    Field("gazette_date", "Gazette date", "publication", "date"),
    Field("gazette_place", "Place of publication", "publication", "text"),
    Field("gazette_issue", "Issue", "publication", "choice", ISSUES),
    Field("gazette_notice", "Government notice", "publication", "text", required=False),
    Field("gazette_pages", "Gazette pages", "publication", "text"),
    Field("official_publisher", "Publisher", "publication", "text"),
    Field("text_form", "Form of this text", "text", "choice", TEXT_FORMS),
    Field("version_label", "Version", "text", "text", help="e.g. As enacted"),
    Field("consolidation", "Consolidation", "text", "choice", CONSOLIDATION),
    Field("summary", "Explanatory memorandum", "text", "long", types=frozenset(LEGISLATION)),
    Field("subjects", "Subjects", "text", "list", required=False),
    Field("legal_status", "Legal status", "status", "choice", LEGAL_STATUS),
    Field("status_checked_on", "Status checked on", "status", "date"),
    Field("status_note", "Status note", "status", "long", required=False),
    Field("relations", "Related laws", "status", "relations", required=False),
    Field("language", "Language", "language", "language", help="BCP 47, e.g. en"),
    Field("language_status", "Language of this text", "language", "choice", LANGUAGE_STATUS),
    Field("official_source_url", "Official source online", "source", "url"),
)
BY_KEY = {field.key: field for field in FIELDS}

LIMITS = {"text": 300, "long": 4000}
DATE_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LANGUAGE_TAG = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
JURISDICTION = re.compile(r"^[a-z]{2}(?:-[a-z0-9]{1,3})?$")
TARGET_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,159}$")


def applies(field: Field, document_type: str) -> bool:
    return field.types is None or document_type in field.types


def empty(value) -> bool:
    return value is None or value == "" or value == []


def validate(details: dict, absent: dict) -> tuple[dict, dict]:
    """Clean a submitted record; ValueError names the first detail that is not acceptable."""
    if not isinstance(details, dict) or not isinstance(absent, dict):
        raise ValueError("details and absent must be objects")
    unknown = (set(details) | set(absent)) - set(BY_KEY)
    if unknown:
        raise ValueError(f"unknown details: {', '.join(sorted(unknown))}")
    clean = {}
    for key, value in details.items():
        if empty(value):
            continue
        clean[key] = _value(BY_KEY[key], value)
    for key, reason in absent.items():
        if reason not in ABSENT:
            raise ValueError(f"{key}: absence must be one of {', '.join(ABSENT)}")
        if key in clean:
            raise ValueError(f"{key}: given and marked absent")
    return clean, dict(absent)


def _value(field: Field, value):
    label = field.label
    if field.kind in ("text", "long"):
        if not isinstance(value, str) or len(value.strip()) > LIMITS[field.kind]:
            raise ValueError(f"{label}: text of at most {LIMITS[field.kind]} characters")
        return value.strip()
    if field.kind == "date":
        if not isinstance(value, str) or not DATE_ISO.match(value):
            raise ValueError(f"{label}: a date as YYYY-MM-DD")
        date.fromisoformat(value)  # raises ValueError for impossible dates
        return value
    if field.kind == "year":
        if not isinstance(value, (str, int)) or not re.fullmatch(r"1[89]\d\d|20\d\d|2100", str(value)):
            raise ValueError(f"{label}: a four-digit year")
        return str(value)
    if field.kind == "choice":
        if value not in field.choices:
            raise ValueError(f"{label}: one of {', '.join(field.choices)}")
        return value
    if field.kind == "url":
        if not isinstance(value, str) or not re.match(r"^https?://\S+$", value) or len(value) > 500:
            raise ValueError(f"{label}: a web address")
        return value
    if field.kind == "language":
        if not isinstance(value, str) or not LANGUAGE_TAG.match(value):
            raise ValueError(f"{label}: a BCP 47 language tag such as en")
        return value
    if field.kind == "jurisdiction":
        if not isinstance(value, str) or not JURISDICTION.match(value):
            raise ValueError(f"{label}: an ISO 3166 code such as ng")
        return value
    if field.kind == "list":
        if not isinstance(value, list) or len(value) > 20:
            raise ValueError(f"{label}: a list of at most 20 entries")
        items = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        if len(items) != len(value) or any(len(item) > 80 for item in items):
            raise ValueError(f"{label}: entries of 1 to 80 characters")
        return items
    if field.kind == "relations":
        if not isinstance(value, list) or len(value) > 50:
            raise ValueError(f"{label}: a list of at most 50 links")
        return [_relation(item) for item in value]
    raise ValueError(f"{label}: unsupported")


def _relation(item) -> dict:
    if not isinstance(item, dict) or set(item) - {"kind", "target", "target_id", "note"}:
        raise ValueError("Related laws: each link has a kind, a target, and optionally target_id and note")
    if item.get("kind") not in RELATIONS:
        raise ValueError(f"Related laws: kind is one of {', '.join(RELATIONS)}")
    target = item.get("target")
    if not isinstance(target, str) or not target.strip() or len(target) > 300:
        raise ValueError("Related laws: the target law's title or citation (at most 300 characters)")
    out = {"kind": item["kind"], "target": target.strip()}
    if item.get("target_id"):
        if not isinstance(item["target_id"], str) or not TARGET_ID.match(item["target_id"]):
            raise ValueError("Related laws: target_id is a catalogue id")
        out["target_id"] = item["target_id"]
    if item.get("note"):
        if not isinstance(item["note"], str) or len(item["note"]) > 300:
            raise ValueError("Related laws: a note of at most 300 characters")
        out["note"] = item["note"].strip()
    return out


def missing(details: dict, absent: dict, document_type: str) -> list[str]:
    """Required details neither given nor accounted for; empty when the record is complete."""
    return [
        field.key
        for field in FIELDS
        if field.required
        and applies(field, document_type)
        and empty(details.get(field.key))
        and field.key not in absent
    ]


# --- suggestions from the document --------------------------------------------------------------

MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
# "3rd day of March, 2023", "08 Day of June, 2023", "18TH.... Day of FEBRUARY, 2026", "24 May, 2023"
DATE = r"(\d{1,2})(?i:st|nd|rd|th)?\.*\s+(?i:day\s+of\s+)?([A-Za-z]+),?\s+(\d{4})"
POST_NOMINALS = {"gcfr", "gcon", "fcna", "fnia", "fcia", "cfr", "con", "san", "mfr", "ofr", "esq"}
STOP_WORDS = {"i", "ls", "assent", "assent."}


def parse_date(day: str, month: str, year: str) -> str | None:
    number = MONTHS.get(month[:3].lower())
    if not number:
        return None
    try:
        return date(int(year), number, int(day)).isoformat()
    except ValueError:
        return None


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _strip_leaders(text: str) -> str:
    return re.sub(r"(?:\s*\.){2,}\s*$", "", re.sub(r"\s*(?:\.\s*){3,}", " ", text)).strip()


def person(before: str) -> str | None:
    """The name (and post-nominals) that ends `before`, as signed: "Bola Ahmed Tinubu, GCFR"."""
    tokens = before.split()
    names, letters = [], []
    while tokens and len(names) + len(letters) < 9:
        token = tokens.pop()
        bare = token.strip(",")
        if bare.lower().rstrip(".") in POST_NOMINALS and not names:
            letters.insert(0, bare)
            continue
        if bare.lower() in STOP_WORDS or not re.fullmatch(r"[A-Z][A-Za-z'’.-]*", bare):
            break
        if bare.endswith(".") and len(bare.rstrip(".")) > 1:  # a sentence ends here, not an initial
            break
        names.insert(0, bare)
    if not names:
        return None
    half = len(names) // 2
    if len(names) % 2 == 0 and [w.lower() for w in names[:half]] == [w.lower() for w in names[half:]]:
        names = names[:half]  # signed, then printed: "BOLA AHMED TINUBU BOLA AHMED TINUBU"
    name = " ".join(word if len(word.rstrip(".")) <= 1 else word.capitalize() for word in names)
    return ", ".join([name, *letters])


def _suggest(value, page: int | None, evidence: str) -> dict:
    return {"value": value, "page": page, "evidence": evidence[:240]}


def _absent(reason: str, page: int | None, evidence: str) -> dict:
    return {"absent": reason, "page": page, "evidence": evidence[:240]}


def suggest(reading: dict, document: dict, manifest: dict) -> dict:
    """Each detail as read from the document, with the page and words it came from."""
    pages = reading["pages"]
    flats = [(page["page"], _flat(page.get("text", ""))) for page in pages]
    out: dict[str, dict] = {}
    document_type = document.get("type") or manifest.get("document_type") or "other"
    out["document_type"] = _suggest(document_type, None, "Collection manifest")
    if manifest.get("jurisdiction"):
        out["jurisdiction"] = _suggest(manifest["jurisdiction"], None, "Collection manifest")

    front = _gazette(pages[0], out) if pages else False
    tail = [(number, text) for number, text in flats[-3:]]
    certificate = next(((n, t) for n, t in tail if "Acts Authentication Act" in t), None)
    if front:
        out["text_form"] = _suggest("gazette_supplement", pages[0]["page"], "Gazette front page")
    elif certificate:
        out["text_form"] = _suggest(
            "authenticated_bill", certificate[0], "Clerk's certificate under the Acts Authentication Act"
        )
        for key in ("gazette_name", "gazette_number", "gazette_volume", "gazette_date", "gazette_place"):
            out[key] = _absent("not_in_source", None, "This copy has no gazette front page")
        for key in ("gazette_issue", "gazette_pages", "official_publisher"):
            out[key] = _absent("not_in_source", None, "This copy has no gazette front page")
        out["number"] = _absent(
            "not_in_source", None, "This copy is the certified Bill; the Act number is given on gazetting"
        )

    _short_title(flats, document, out)
    _enacting(flats, out)
    _schedule(pages, flats, out)
    _certificate(tail, out)
    _summary(pages, out)
    _relations(out)

    title = out.get("short_title", {}).get("value") or document.get("title", "")
    year = re.search(r"(\d{4})\s*$", title)
    if year or document.get("year"):
        out["year"] = _suggest(year.group(1) if year else str(document["year"]), None, title)
    number = out.get("number", {}).get("value")
    if number and title:
        base = re.sub(r",?\s*\d{4}$", "", title)
        out["citation"] = _suggest(
            f"{base}, No. {number}, {out['year']['value']}", None, "Title, number and year"
        )
    elif title:
        out["citation"] = _suggest(title, None, "Short title (no Act number in this copy)")

    out["language"] = _suggest("en", None, "Nigerian federal legislation is enacted in English")
    out["language_status"] = _suggest("original", None, "The text as enacted")
    out["version_label"] = _suggest("As enacted", None, "The text as passed and assented to")
    if "not consolidated" in manifest.get("description", "").lower():
        out["consolidation"] = _suggest("not_consolidated", None, manifest["description"])
    out["legal_status"] = _suggest("unknown", None, "Currentness has not been checked")
    out["status_checked_on"] = _absent("unknown", None, "Currentness has not been checked")
    out["official_source_url"] = _absent("unknown", None, f"Retrieved from {document.get('url', 'a mirror')}")
    return {key: value for key, value in out.items() if key in BY_KEY}


def _gazette(page: dict, out: dict) -> bool:
    furniture = [f if isinstance(f, str) else f.get("text", "") for f in page.get("furniture", [])]
    text = _flat(" ".join([page.get("text", ""), *furniture]))
    if "Official Gazette" not in text:
        return False
    number = page["page"]
    out["gazette_name"] = _suggest(
        "Federal Republic of Nigeria Official Gazette"
        if "Federal Republic of Nigeria" in text
        else "Official Gazette",
        number,
        "Gazette front page",
    )
    issue = re.search(r"No\.\s*(\d+)\s+([A-Z][a-z]+)\s*[-–—]?\s*" + DATE + r"\s+Vol\.\s*([\dIVXLC]+)", text)
    if issue:
        evidence = issue.group(0)
        out["gazette_number"] = _suggest(issue.group(1), number, evidence)
        out["gazette_place"] = _suggest(issue.group(2), number, evidence)
        when = parse_date(*issue.group(3, 4, 5))
        if when:
            out["gazette_date"] = _suggest(when, number, evidence)
        out["gazette_volume"] = _suggest(issue.group(6), number, evidence)
    out["gazette_issue"] = _suggest(
        "extraordinary" if "Extraordinary" in text else "ordinary", number, "Gazette front page"
    )
    notice = re.search(r"Government Notice No\.\s*(\d+)", text)
    if notice:
        out["gazette_notice"] = _suggest(f"Government Notice No. {notice.group(1)}", number, notice.group(0))
    for table in page.get("tables", []):
        rows = table if isinstance(table, list) else table.get("rows", [])
        if rows and rows[0] and str(rows[0][0]).strip().lower().startswith("act no"):
            for row in rows[1:2]:
                if len(row) >= 3:
                    evidence = " ".join(str(cell) for cell in row)
                    out["number"] = _suggest(str(row[0]).strip(), number, evidence)
                    out["gazette_pages"] = _suggest(str(row[2]).strip(), number, evidence)
    publisher = re.search(
        r"Printed and Published by (?:The )?(Federal Government Printer, [A-Z][a-z]+)", text
    )
    if publisher:
        out["official_publisher"] = _suggest(publisher.group(1), number, publisher.group(0))
    return True


def _short_title(flats, document, out):
    for number, text in flats:
        cited = re.search(r"(?:This|The) (?:Act|Law) may be cited as (?:the )?(.{3,200}?\d{4})\s*\.", text)
        if cited:
            out["short_title"] = _suggest(cited.group(1).strip(), number, cited.group(0))
            return
    if document.get("title"):
        out["short_title"] = _suggest(document["title"], None, "Collection manifest")


def _enacting(flats, out):
    for number, text in flats:
        long_title = re.search(
            r"\b(An Act to .+?)\s*(?:\[[^\]]*\]\s*Commencement|\[\s*ENACTED|ENACTED by)", text, re.I
        )
        if long_title and "long_title" not in out:
            out["long_title"] = _suggest(long_title.group(1).strip(), number, long_title.group(0))
        commenced = re.search(r"\[\s*" + DATE + r"\s*\]\s*Commencement", text, re.I)
        blank = re.search(r"\[\s*\]\s*Commencement", text)
        if commenced and "commencement_date" not in out:
            when = parse_date(*commenced.groups())
            if when:
                out["commencement_date"] = _suggest(when, number, commenced.group(0))
        elif blank and "commencement_date" not in out:
            out["commencement_date"] = _absent(
                "not_in_source", number, "The commencement date is left blank in this copy: [ ] Commencement"
            )
        body = re.search(r"ENACTED by the (National Assembly of the Federal Republic of Nigeria)", text, re.I)
        if body and "enacting_body" not in out:
            out["enacting_body"] = _suggest(
                "National Assembly of the Federal Republic of Nigeria", number, body.group(0)
            )
        if "long_title" in out and "enacting_body" in out:
            break
    if "commencement_date" not in out:
        out["commencement_date"] = _absent("not_in_source", None, "No commencement date found in this copy")


def _schedule(pages, flats, out):
    """The schedule printed with a certified Bill: its titles and the dates each House passed it."""
    for page in reversed(pages[-3:]):
        for table in page.get("tables", []):
            rows = table if isinstance(table, list) else table.get("rows", [])
            if len(rows) < 2:
                continue
            header = [re.sub(r"^\(\d\)\s*", "", str(cell)).strip().lower() for cell in rows[0]]
            if not any("passed by the senate" in cell for cell in header):
                continue
            row = rows[1]
            evidence = "Schedule to the Bill"
            for index, cell in enumerate(header):
                if index >= len(row):
                    continue
                value = str(row[index]).strip().rstrip(".")
                if "short title" in cell:
                    out["bill_title"] = _suggest(value, page["page"], evidence)
                elif "long title" in cell:
                    out["long_title"] = _suggest(str(row[index]).strip(), page["page"], evidence)
                elif "passed by the senate" in cell or "passed by the house" in cell:
                    found = re.search(DATE, value, re.I)
                    when = parse_date(*found.groups()) if found else None
                    if when:
                        key = "passed_senate" if "senate" in cell else "passed_house"
                        out[key] = _suggest(when, page["page"], f"{evidence}: {value}")
            return
    for number, text in reversed(flats[-3:]):  # no table: the two dates before the certificate
        start = text.lower().find("passed by the senate")
        end = text.find("I certify", start)
        if start < 0 or end < 0:
            continue
        dates = [parse_date(*m.groups()) for m in re.finditer(DATE, text[start:end], re.I)]
        dates = [d for d in dates if d]
        if len(dates) >= 2:
            out["passed_senate"] = _suggest(dates[-2], number, "Schedule to the Bill")
            out["passed_house"] = _suggest(dates[-1], number, "Schedule to the Bill")
        return


def _certificate(tail, out):
    for number, text in reversed(tail):
        clerk = re.search(r"(?i:clerk to the national assembly),?\s*" + DATE, text)
        if clerk and "authenticated_on" not in out:
            when = parse_date(*clerk.groups())
            if when:
                out["authenticated_on"] = _suggest(when, number, clerk.group(0))
            name = person(text[: clerk.start()])
            if name:
                out["authenticated_by"] = _suggest(
                    f"{name}, Clerk to the National Assembly", number, clerk.group(0)
                )
        president = re.search(r"President of the Federal Republic of Nigeria,?\s*" + DATE, text, re.I)
        if president and "assent_date" not in out and re.search(r"I\s+ASSENT", text, re.I):
            when = parse_date(*president.groups())
            if when:
                out["assent_date"] = _suggest(when, number, president.group(0))
            name = person(text[: president.start()])
            if name:
                out["assented_by"] = _suggest(
                    f"{name}, President of the Federal Republic of Nigeria", number, president.group(0)
                )
    if "authenticated_on" in out and "assent_date" not in out:
        out["assent_date"] = _absent("not_in_source", None, "This copy carries no assent")
        out["assented_by"] = _absent("not_in_source", None, "This copy carries no assent")


def _summary(pages, out):
    nodes = [
        (page["page"], node) for page in pages for node in page["nodes"] if node.get("kind") != "furniture"
    ]
    for index, (number, node) in enumerate(nodes):
        text = _flat(node.get("text", ""))
        if not text.upper().startswith("EXPLANATORY MEMORANDUM"):
            continue
        rest = text[len("EXPLANATORY MEMORANDUM") :].strip()
        if not rest and index + 1 < len(nodes):
            number, rest = nodes[index + 1][0], _flat(nodes[index + 1][1].get("text", ""))
        if rest:
            out["summary"] = _suggest(rest, number, "Explanatory memorandum")
        return


def _relations(out):
    long_title = out.get("long_title", {}).get("value", "")
    links = []
    repeal = re.search(
        r"to repeal (?:the )?(.+?)(?=\s+and (?:to )?(?:re-?)?enacts?\b|\s+and to\b|;|$)", long_title, re.I
    )
    if repeal:
        links.append({"kind": "repeals", "target": repeal.group(1).strip(" ,")})
    amend = re.search(r"to amend (?:the )?(.+?)(?=\s+to\s|;|\s+and\s|$)", long_title, re.I)
    if amend:
        links.append({"kind": "amends", "target": amend.group(1).strip(" ,")})
    alter = re.search(
        r"to alter (?:the )?(Constitution of the Federal Republic of Nigeria,? 1999)", long_title, re.I
    )
    if alter:
        links.append({"kind": "amends", "target": alter.group(1), "note": "Alteration of the Constitution"})
    if links:
        out["relations"] = _suggest(links, out["long_title"].get("page"), "Long title")


def initial(suggestions: dict) -> tuple[dict, dict]:
    """A first draft from the suggestions, for the reviewer to work through."""
    details = {key: s["value"] for key, s in suggestions.items() if "value" in s}
    absent = {key: s["absent"] for key, s in suggestions.items() if "absent" in s}
    return details, absent


def schema() -> dict:
    """The standard as the workbench form needs it."""
    return {
        "standard": STANDARD,
        "groups": [{"key": key, "label": label} for key, label in GROUPS],
        "fields": [
            {
                "key": f.key,
                "label": f.label,
                "group": f.group,
                "kind": f.kind,
                "choices": [{"value": c, "label": CHOICE_LABELS.get(c, c.capitalize())} for c in f.choices],
                "required": f.required,
                "types": sorted(f.types) if f.types else None,
                "help": f.help,
            }
            for f in FIELDS
        ],
        "absent": ABSENT,
        "relations": [{"value": r, "label": CHOICE_LABELS[r]} for r in RELATIONS],
    }
