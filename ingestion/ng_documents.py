"""Nigeria's stored PDFs as review documents, once the cloud has read them (CPR-07): a design, built
where the first collection's readings can test it, waiting for the corpus's own readings.

A PDF of the Nigerian collection (an Act, a Gazette issue, a regulation or order, a Supreme Court
judgment) has no text of its own we trust: the scan-reader job reads it (ingestion.cloud_read) with
the Layout Parser, kept at readings/ng/docai/<sha256>.json, and with Google's OCR, kept at
readings/ng/ocr/<sha256>.json, each page under its own number. Its review documents are made from
those, in collections/ng/ beside the National Industrial Court's judgments (ingestion.nicn), in the
same layout:

- the reading: the Layout Parser's, the primary, reconciled with the OCR's as the check
  (ingestion.reconcile: the check never changes a word; it restores list markers both see and flags
  every word it does not support). A file the Layout Parser has not read waits; a file it read
  without the OCR is prepared with the OCR named as a missing check, which blocks approval
  (content.readiness) until the OCR has read it and the file is prepared again;
- the instruments: a Gazette issue holds several (the Acts, Statutory Instruments and notices its
  front page lists, with their printed pages: "Act No. | Short Title | Page", "8 | Copyright Act,
  2022 | A179-243"); each becomes its own document over the physical pages its printed pages map to,
  as Rwanda's issues are split per instrument (ingestion.rwanda). A file with no such front page (a
  certified Act, an agency's regulation, a judgment) is one instrument;
- its type and structure rules: an Act or a constitutional alteration is ng/act (ng-federal-act-1);
  a Supreme Court judgment or ruling ng/judgment or ng/ruling (judgment-paragraphs-1); a
  regulation, order, rules, guideline, circular or practice direction has no rules yet, so its
  structure is one node with the error no_structure_rules, which blocks its approval until rules
  are written from real texts (TXP-03, DAT-05);
- its id: an Act ng-act-<number>-<year> ("ng-act-8-2022"), a Statutory Instrument
  ng-si-<number>-<year>, a notice ng-gn-<number>-<year>; else the type and its title's slug; a
  Supreme Court judgment ng-sc-<suit number>-<date delivered>.

Nothing here reads a PDF or calls a paid service: it takes the stored readings, and a file not yet
read waits (prepare_pdfs, run by the corpus-preparer job with PREPARE_COUNTRY=ng and the source in
PREPARE_SOURCES).
"""

import copy
import json
import re

from ingestion import layout_parser, reconcile

COUNTRY = "ng"
# The kinds of instrument the stored files hold (ledger types) and the document type each is reviewed as.
TYPES = {
    "act": "act",
    "constitution": "act",
    "constitutional_alteration": "act",
    "regulation": "regulation",
    "order": "order",
    "rules": "rules",
    "guideline": "guideline",
    "circular": "circular",
    "practice_direction": "practice_direction",
    "judgment": "judgment",
    "ruling": "ruling",
    "gazette": "gazette",
}
# A Gazette's front page: the issue line, and the supplements it publishes with their printed pages.
ISSUE = re.compile(r"No\.\s*(?P<number>\d+)", re.I)
VOLUME = re.compile(r"Vol(?:ume)?\.?\s*(?P<volume>\d+)", re.I)
NOTICE = re.compile(r"Government\s+Notice\s+No\.?\s*(?P<notice>\d+)", re.I)
CONTENTS_KIND = (
    (re.compile(r"^(?:Act\s+No|No\.\s+of\s+Act)", re.I), "act"),
    (re.compile(r"^S\.?\s*I\.?\s*No", re.I), "si"),
    (re.compile(r"^(?:Notice|G\.?\s*N\.?)\s+No", re.I), "gn"),
)
PRINTED_PAGES = re.compile(
    r"^(?P<series>[A-D])\s?(?P<start>\d{1,4})\s*[-–—]\s*(?:[A-D]\s?)?(?P<end>\d{1,4})$"
)
PRINTED_PAGE = re.compile(r"(?:^(?P<a>[A-D])\s?(?P<na>\d{1,4})\b)|(?:\b(?P<b>[A-D])\s?(?P<nb>\d{1,4})$)")
LEADERS = re.compile(r"(?:\s*\.){2,}\s*$|\s*(?:\.\s){2,}.*$")
TITLE_KIND = (
    (re.compile(r"\bACT\b,?\s*(?:19|20)\d\d\.?$", re.I), "act"),
    (re.compile(r"\bREGULATIONS?\b,?\s*(?:19|20)\d\d\.?$", re.I), "regulation"),
    (re.compile(r"\bORDER\b,?\s*(?:19|20)\d\d\.?$", re.I), "order"),
    (re.compile(r"\bRULES\b,?\s*(?:19|20)\d\d\.?$", re.I), "rules"),
)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def cloud_reading(docai: dict | None, ocr: dict | None) -> dict | None:
    """A stored file's reading for review: the Layout Parser's reconciled with the OCR's as its check
    (ingestion.reconcile); None while the Layout Parser has not read it. Without the OCR, the check is
    named missing, so the document is not approved on one reading."""
    if not docai:
        return None
    primary = {
        "input_sha256": docai["sha256"],
        "reader": "docai",
        "version": docai["version"],
        # Its inline maths read as words ("\\(2^{nd}\\)" is "2nd", the "nd" raised): layout_parser.
        "pages": [layout_parser.plain_maths_page(page) for page in docai["pages"]],
    }
    checks = {"docai-ocr": {"version": ocr["version"], "pages": ocr["pages"]}} if ocr else {}
    reading = reconcile.reconcile(primary, checks)
    reading["summary"]["missing_checks"] = [] if ocr else ["docai-ocr"]
    return reading


def _cells(node: dict) -> list[list[str]]:
    return [[cell["text"].strip() for cell in row] for row in node["header_rows"] + node["body_rows"]]


def issue(reading: dict) -> dict:
    """A Gazette issue's own details from its front page: number, place, date, volume, whether it is an
    extraordinary issue and the notice that publishes its supplements; {} when the file has none."""
    from ingestion.nigeria import to_date

    first = reading["pages"][0]
    texts = [n.get("text", "") for n in first["nodes"] if n["kind"] != "table"]
    if not any(re.search(r"Official\s+Gazette", t, re.I) for t in texts):
        return {}
    out: dict = {"name": "Federal Republic of Nigeria Official Gazette", "page": first["page"]}
    for node in first["nodes"]:
        if node["kind"] != "table":
            continue
        for row in _cells(node):
            line = " | ".join(row)
            if ISSUE.search(line) and VOLUME.search(line):
                out["number"] = ISSUE.search(line).group("number")
                out["volume"] = VOLUME.search(line).group("volume")
                for cell in row:
                    place = re.match(r"^(?P<place>[A-Z][a-z]+)\s*[-–—,]?\s*(?P<date>.+)$", cell)
                    if place and to_date(re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", place.group("date"))):
                        out["place"] = place.group("place")
                        out["date"] = to_date(re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", place.group("date")))
    notice = next((NOTICE.search(t) for t in texts if NOTICE.search(t)), None)
    if notice:
        out["notice"] = f"Government Notice No. {notice.group('notice')}"
    out["issue"] = "extraordinary" if any(re.search(r"\bExtraordinary\b", t) for t in texts) else "ordinary"
    return out


def contents(reading: dict) -> list[dict]:
    """The supplements a Gazette's front page lists: each one's kind (an Act, a Statutory Instrument, a
    notice), number, short title as printed and printed pages ("A179-243")."""
    out = []
    for node in reading["pages"][0]["nodes"]:
        if node["kind"] != "table":
            continue
        rows = _cells(node)
        head = next(
            (k for k, row in enumerate(rows) if any(re.search(r"short\s+title", c, re.I) for c in row)), None
        )
        if head is None:
            continue
        kind = next(
            (k for pattern, k in CONTENTS_KIND if rows[head] and pattern.match(rows[head][0])), "other"
        )
        for row in rows[head + 1 :]:
            if len(row) < 3:
                continue
            pages = PRINTED_PAGES.match(row[-1].replace(" ", ""))
            title = LEADERS.sub("", row[1]).strip()
            if not title:
                continue
            out.append(
                {
                    "kind": kind,
                    "number": row[0].strip(" .") or None,
                    "title": title,
                    "printed": row[-1].strip(),
                    "series": pages.group("series") if pages else None,
                    "from": int(pages.group("start")) if pages else None,
                    "to": int(pages.group("end")) if pages else None,
                }
            )
    return out


def printed_pages(reading: dict) -> dict[int, tuple[str, int]]:
    """Each physical page's printed number as its running head gives it ("A 179"), filled in between
    two pages that print theirs, and after the last one (a back cover so numbered falls outside every
    supplement's printed pages)."""
    found: dict[int, tuple[str, int]] = {}
    for page in reading["pages"]:
        for text in page.get("furniture") or []:
            match = PRINTED_PAGE.search(str(text).strip())
            if match:
                series = match.group("a") or match.group("b")
                found[page["page"]] = (series, int(match.group("na") or match.group("nb")))
                break
    numbers = sorted(found)
    for before, after in zip(numbers, numbers[1:], strict=False):
        (series, a), (series_after, b) = found[before], found[after]
        if series == series_after and b - a == after - before:
            for page in range(before + 1, after):
                found[page] = (series, a + page - before)
    if numbers:
        series, last = found[numbers[-1]]
        for page in (p["page"] for p in reading["pages"] if p["page"] > numbers[-1]):
            found[page] = (series, last + page - numbers[-1])
    return found


def instruments(reading: dict, listed_type: str, listed_title: str | None = None) -> list[dict]:
    """The instruments a file holds, each with its physical pages: those a Gazette's front page lists,
    over the pages their printed numbers map to (the front page with them when it publishes only one);
    else the whole file, one instrument, titled by its first heading."""
    pages = [page["page"] for page in reading["pages"]]
    listed = contents(reading)
    printed = printed_pages(reading)
    out = []
    for entry in listed:
        doubtful = entry["from"] is None or entry["to"] is None or entry["from"] > entry["to"]
        held = [
            p
            for p, (series, number) in sorted(printed.items())
            if not doubtful and series == entry["series"] and entry["from"] <= number <= entry["to"]
        ]
        out.append(
            {
                **entry,
                "type": {"act": "act", "si": "regulation", "gn": "gazette_notice"}.get(
                    entry["kind"], "other"
                ),
                "pages": held,
                "doubtful_pages": doubtful,
            }
        )
    if out and len(out) == 1:
        # A file of one supplement: every page is its instrument's, the cover with it, and its schedules
        # and explanatory memorandum printed past the range the front page lists (before 2026-10-29
        # those were in no document: 18 Acts, 60 pages). The pages past the range are named.
        mapped = set(out[0]["pages"])
        out[0]["beyond_listed"] = (
            [] if not mapped else [p for p in pages if p not in mapped and p != pages[0]]
        )
        out[0]["pages"] = pages
        return out
    if out and not any(entry["pages"] for entry in out):
        out = []  # no listed range could be mapped: the whole file is one instrument, below, its pages doubtful
    if out:
        # Several supplements: a page no range claims goes with the instrument printed before it (its
        # schedules); the pages before the first, the issue's front page, are the issue's own record.
        claimed = {p for entry in out for p in entry["pages"]}
        first = min(claimed)
        ordered = sorted((entry for entry in out if entry["pages"]), key=lambda e: e["pages"][0])
        for p in pages:
            if p in claimed or p < first:
                continue
            owner = [entry for entry in ordered if entry["pages"][0] < p][-1]
            owner.setdefault("beyond_listed", []).append(p)
            owner["pages"] = sorted({*owner["pages"], p})
        front = [p for p in pages if p < first]
        if front:
            out.insert(
                0,
                {
                    "kind": "gazette",
                    "type": "gazette",
                    "number": None,
                    "title": "",
                    "pages": front,
                    "front": True,
                },
            )
        return out
    title = next(
        (
            n["text"]
            for p in reading["pages"][:3]
            for n in p["nodes"]
            if n["kind"] == "heading" and n.get("text")
        ),
        listed_title or "",
    )
    kind = next((k for pattern, k in TITLE_KIND if pattern.search(title)), TYPES.get(listed_type, "other"))
    return [
        {
            "kind": kind,
            "type": kind,
            "number": None,
            "title": title,
            "pages": pages,
            # Listed by its front page, but no range could be mapped to the file's pages.
            "doubtful_pages": bool(listed),
        }
    ]


def document_id(instrument: dict) -> str:
    """ng-act-8-2022, ng-si-15-2023, ng-gn-24-2023; else the type and the title's slug."""
    year = re.findall(r"(?:19|20)\d\d", instrument.get("title") or "")
    number = re.sub(r"\D", "", instrument.get("number") or "")
    prefix = {"act": "act", "regulation": "si", "gazette_notice": "gn"}.get(
        instrument["type"], instrument["type"]
    )
    if number and year:
        return f"ng-{prefix}-{int(number)}-{year[-1]}"
    return f"ng-{_slug(instrument['type'])}-{_slug(instrument.get('title') or 'untitled')[:90]}"


def part(reading: dict, pages: list[int]) -> dict:
    """The reading of an instrument's pages, under the issue's own page numbers, its counts redone."""
    out = copy.deepcopy({k: v for k, v in reading.items() if k != "pages"})
    out["pages"] = [copy.deepcopy(page) for page in reading["pages"] if page["page"] in set(pages)]
    counts = {"high": 0, "medium": 0, "low": 0}
    for page in out["pages"]:
        for flag in page["flags"]:
            counts[flag["severity"]] += 1
    out["summary"] = {**out["summary"], "pages": len(out["pages"]), "flags": counts}
    return out


def documents(reading: dict, record: dict) -> list[tuple[dict, dict]]:
    """A stored file's review documents: (manifest document, reading) for each instrument it holds."""
    listed = record.get("listed") or {}
    if record.get("source") == "supreme-court" and record.get("type") in ("judgment", "ruling"):
        values, evidence = sc_details(listed, record["type"], record.get("url") or "")
        document = {
            "id": sc_document_id(listed),
            "type": record["type"],
            "title": values.get("identity.title") or listed.get("case_title") or "",
            "jurisdiction": COUNTRY,
            "language": "en",
            "url": record.get("url"),
            **(
                {"work": sc_document_id({**listed, "opinion": "lead"})}
                if listed.get("opinion") == "contribution"
                else {}
            ),
            "suggested": values,
            "suggested_evidence": evidence,
        }
        return [(document, reading)]
    found = instruments(reading, record.get("type") or "", listed.get("title"))
    if (
        len(found) == 1
        and not found[0].get("number")
        and listed.get("number")
        and found[0]["type"] == TYPES.get(record.get("type") or "")
    ):
        found[0]["number"] = str(listed["number"])  # the listing's Act number, where the copy prints none
    gazette = issue(reading)
    out = []
    for instrument in found:
        if not instrument["pages"]:
            continue
        if instrument.get("front"):
            # The issue's front page (its number, volume, date, notice and contents), shared by the
            # supplements it publishes: the issue's own record, kept and reviewed as such.
            number = "-".join(str(gazette.get(k) or "") for k in ("volume", "number")).strip("-")
            year = (gazette.get("date") or "")[:4]
            doc_id = (
                f"ng-gazette-{_slug(number) or 'issue'}-{year}"
                if year
                else f"ng-gazette-{_slug(number) or 'issue'}"
            )
            title = "Federal Republic of Nigeria Official Gazette" + (
                f" No. {gazette['number']}, Vol. {gazette['volume']}" if gazette.get("number") else ""
            )
            document = {
                "id": doc_id,
                "type": "gazette",
                "title": title + (f" ({gazette['date']})" if gazette.get("date") else ""),
                "jurisdiction": COUNTRY,
                "language": "en",
                "url": record.get("url"),
                "pages": [instrument["pages"][0], instrument["pages"][-1]],
                **({"issue": gazette} if gazette else {}),
            }
            out.append((document, part(reading, instrument["pages"])))
            continue
        doc_id = document_id(instrument)
        document = {
            "id": doc_id,
            "type": instrument["type"],
            "title": instrument["title"],
            "jurisdiction": COUNTRY,
            "language": "en",
            "url": record.get("url"),
            "pages": [instrument["pages"][0], instrument["pages"][-1]],
            **({"issue": gazette} if gazette else {}),
            **({"printed_pages": instrument["printed"]} if instrument.get("printed") else {}),
            **({"pages_doubtful": True} if instrument.get("doubtful_pages") else {}),
        }
        portion = part(reading, instrument["pages"])
        beyond = instrument.get("beyond_listed") or []
        if beyond:
            # Pages past the range the front page lists (a schedule, an explanatory memorandum): kept with
            # the instrument, and named for the reviewer to confirm they are its own.
            first = next(page for page in portion["pages"] if page["page"] == beyond[0])
            first["flags"] = [
                *first["flags"],
                {
                    "severity": "medium",
                    "kind": "pages_beyond_listed_range",
                    "primary": f"pages {beyond[0]}–{beyond[-1]}",
                    "checks": {"front page": instrument.get("printed") or "not listed"},
                    "tokens": [0, 0],
                    "readers_disagreeing": [],
                },
            ]
            portion["summary"]["flags"]["medium"] += 1
            document["pages_beyond_listed"] = beyond
        out.append((document, portion))
    return out


# --- the Supreme Court: its catalogue gives the details --------------------------------------------


def sc_document_id(listed: dict) -> str:
    """ng-sc-<suit number>-<date delivered> for the lead judgment ("ng-sc-sc-123-2010-2019-05-03"), with
    the justice's name after it for a concurring or dissenting opinion, filed as its own record."""
    suit = re.sub(r"\s+", "", listed.get("suit_no") or "")
    out = f"ng-sc-{_slug(suit)}" if suit else f"ng-sc-case-{listed.get('case_id')}"
    if listed.get("judgment_date"):
        out += f"-{listed['judgment_date']}"
    if listed.get("opinion") == "contribution" and listed.get("justice"):
        out += f"-{_slug(listed['justice'])[:40]}"
    return out


def sc_details(listed: dict, kind: str, url: str) -> tuple[dict, dict]:
    """A Supreme Court judgment's details as its catalogue gives them (ingestion.nigeria.sc_record), in
    the record's keys, each with the catalogue as its evidence; never its outcome or standing."""
    court = "Supreme Court of Nigeria"
    catalogue = f"The court's catalogue ({url})"
    values: dict = {
        "identity.family": "judgment",
        "identity.type": kind,
        "issued_by": court,
        "profile.court.name": court,
        "profile.court.level": "apex",
        "publication.kind": "official_website",
        "publication.name": "Supreme Court of Nigeria: judgments delivered",
        "publication.publisher": court,
        "publication.url": url,
        "text.form": "official_website",
        "text.version.label": "As published on the court's website",
        "expressions": [{"language": "en", "script": "Latn", "direction": "ltr", "this_text": "yes"}],
    }
    if listed.get("case_title"):
        values["identity.title"] = listed["case_title"]
    if listed.get("suit_no"):
        values["profile.case_numbers"] = [listed["suit_no"]]
        values["identity.number.value"] = listed["suit_no"]
    if listed.get("judgment_date"):
        values["event:decided.date"] = listed["judgment_date"]
        values["identity.year"] = listed["judgment_date"][:4]
    sides = listed.get("parties") or {}
    counsel = listed.get("counsel") or {}
    parties = [
        {
            "name": sides[side],
            "role": side,
            "anonymised": "no",
            **({"represented_by": counsel[side]} if counsel.get(side) else {}),
        }
        for side in ("petitioner", "respondent")
        if sides.get(side)
    ]
    if parties:
        values["profile.parties"] = parties
        # Its name as cases are cited, and in full with each side's role, where the catalogue gives no
        # title (none of the 247 on 2026-10-30), as the National Industrial Court's are written.
        values.setdefault("identity.title", " v. ".join(p["name"] for p in parties))
        values["identity.full_title"] = " v. ".join(
            f"{p['name']} ({p['role'].capitalize()})" for p in parties
        )
    lead, dissenting = (listed.get("lead_judge") or "").lower(), (listed.get("dissenting") or "").lower()
    bench = [
        {
            "name": justice,
            "role": "author"
            if lead and lead in justice.lower()
            else "dissenting"
            if dissenting and dissenting in justice.lower()
            else "member",
        }
        for justice in listed.get("justices") or []
    ]
    if bench:
        values["profile.bench"] = bench
    evidence = {key: {"value": value, "page": None, "evidence": catalogue} for key, value in values.items()}
    return values, evidence


# --- preparing the files whose readings have arrived -------------------------------------------------

# The version of this preparation: a file prepared by an earlier one is prepared again. 2026-10-29.2: the
# OCR check read as stored (words, no text), inline maths read as words; every page in a document
# (schedules past the listed range, the issue's front page as its own record, unmapped ranges whole).
PIPELINE = "ng-pdf-2026-10-30.1"
# The sources whose stored files are PDFs read by the scan-reader job (their other files, the Supreme
# Court's data records and SEC's circular pages, have no reader here yet).
PDF_SOURCES = {
    "nass-acts",
    "nln-repository",
    "moj-constitution",
    "supreme-court",
    "ncaa",
    "ncc",
    "sec",
    "ndpc",
    "nerc",
    "nafdac",
    "nesrea",
    "cac",
    "naicom",
    "fhc-rules",
}


def prepare_pdfs(store, items: dict, chosen: list[str], collection, log=print) -> dict:
    """Each chosen stored PDF whose Layout Parser reading is in the bucket, into the collection
    (nicn.BucketCollection with this PIPELINE): its documents, an index naming the readings used, so a
    file is prepared again once its OCR reading arrives. A file not yet read waits; one that fails is
    noted and the rest go on."""
    counts = {"prepared": 0, "documents": 0, "waiting": 0, "skipped_done": 0, "failed": 0}
    for item_id in chosen:
        record = items[item_id]
        sha = record.get("sha256")
        if (
            not sha
            or record.get("format", "pdf") != "pdf"
            or not str(record.get("stored", "")).endswith(".pdf")
        ):
            continue
        layout, check = f"readings/{COUNTRY}/docai/{sha}.json", f"readings/{COUNTRY}/ocr/{sha}.json"
        if not store.exists(layout):
            counts["waiting"] += 1
            continue
        used = {"docai": True, "ocr": store.exists(check)}
        if collection.done(sha, used):
            counts["skipped_done"] += 1
            continue
        try:
            reading = cloud_reading(
                json.loads(store.get(layout)), json.loads(store.get(check)) if used["ocr"] else None
            )
            source_object = f"sources/{COUNTRY}/{record['source']}/{sha}.pdf"
            sidecar_name = source_object[: -len(".pdf")] + ".json"
            sidecar = json.loads(store.get(sidecar_name)) if store.exists(sidecar_name) else {}
            entries = []
            for document, part_reading in documents(reading, record):
                provenance = {
                    "url": record.get("url"),
                    "final_url": sidecar.get("final_url") or record.get("url"),
                    "retrieved_at": sidecar.get("retrieved_at") or record.get("last_fetched"),
                    "sha256": sha,
                    "bytes": sidecar.get("bytes") or record.get("bytes"),
                    "source": record["source"],
                    "item": item_id,
                    "source_object": source_object,
                    "listing": record.get("listed"),
                    "pages": document.get("pages"),
                }
                document = {**document, "summary": part_reading["summary"], "source_object": source_object}
                entries.append(collection.write(document, part_reading, provenance, sha))
            collection.finish(sha, entries, source=record["source"], readings=used)
            counts["prepared"] += 1
            counts["documents"] += len(entries)
        except Exception as error:  # noqa: BLE001 - noted and left for a later run
            counts["failed"] += 1
            log(f"failed {item_id}: {type(error).__name__}: {str(error)[:300]}")
    return counts
