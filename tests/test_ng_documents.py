"""Nigeria's stored PDFs as review documents once the cloud has read them (ingestion.ng_documents).

The readings here are synthetic, in the Layout Parser's page shape (ingestion.readers.layout_pages) and
modelled on the first collection's Gazette issues: a front page with the issue line and the
supplements it publishes, running heads with the printed page numbers. No Nigerian text is quoted.
"""

import json

from ingestion import ng_documents, nicn


def cell(text):
    return {"text": text, "row_span": 1, "col_span": 1}


def table(page, rows, header=1):
    cells = [[cell(text) for text in row] for row in rows]
    return {
        "kind": "table",
        "page": page,
        "caption": "",
        "header_rows": cells[:header],
        "body_rows": cells[header:],
    }


def node(page, text, kind="paragraph", level=None, **extra):
    return {"kind": kind, "text": text, "page": page, "level": level or kind, **extra}


def page(number, nodes, furniture=()):
    return {
        "page": number,
        "nodes": [*(node(number, f, "furniture") for f in furniture), *nodes],
        "text": "\n".join(n.get("text", "") for n in nodes if n["kind"] != "table"),
        "tables": [],
        "furniture": list(furniture),
    }


def gazette(supplements):
    """A Gazette issue publishing the given supplements: (number, title, printed pages, [page texts])."""
    contents = [["Act No.", "Short Title", "Page"]] + [
        [number, f"{title} .. .. .. ..", printed] for number, title, printed, _ in supplements
    ]
    front = page(
        1,
        [
            node(1, "Extraordinary"),
            node(1, "Federal Republic of Nigeria", "heading", "heading-1"),
            node(1, "Official Gazette", "heading", "heading-1"),
            table(1, [["No. 12", "Lagos 3rd March, 2024", "Vol. 111"]], header=0),
            node(1, "Government Notice No. 7 The following is published as supplement to this Gazette :"),
            table(1, contents),
        ],
        ["Printed and Published by the Federal Government Printer, Lagos"],
    )
    pages, number = [front], 2
    for _, title, printed, texts in supplements:
        first = int(printed.split("-")[0][1:])
        for k, text in enumerate(texts):
            head = f"A {first + k}" if k % 2 else f"A {first + k} 2024 No. 3 {title}"
            pages.append(page(number, [node(number, text)], [head] if k != 1 else []))
            number += 1
    return {"sha256": "a" * 64, "reader": "docai", "version": {"processor": "x"}, "pages": pages}


TWO = [
    (
        "3",
        "Example Act, 2024",
        "A10-11",
        ["EXAMPLE ACT, 2024", "1. This Act may be cited as the Example Act."],
    ),
    (
        "4",
        "Example (Amendment) Act, 2024",
        "A12-13",
        ["EXAMPLE (AMENDMENT) ACT, 2024", "1. Section 2 is amended."],
    ),
]


def test_a_gazette_issue_gives_its_own_details_and_the_supplements_it_publishes():
    reading = ng_documents.cloud_reading(gazette(TWO), None)
    assert ng_documents.issue(reading) == {
        "name": "Federal Republic of Nigeria Official Gazette",
        "page": 1,
        "number": "12",
        "volume": "111",
        "place": "Lagos",
        "date": "2024-03-03",
        "notice": "Government Notice No. 7",
        "issue": "extraordinary",
    }
    listed = ng_documents.contents(reading)
    assert [(e["kind"], e["number"], e["title"], e["series"], e["from"], e["to"]) for e in listed] == [
        ("act", "3", "Example Act, 2024", "A", 10, 11),
        ("act", "4", "Example (Amendment) Act, 2024", "A", 12, 13),
    ]
    # Each page's printed number, from its running head, filled in where a page prints none.
    assert ng_documents.printed_pages(reading) == {2: ("A", 10), 3: ("A", 11), 4: ("A", 12), 5: ("A", 13)}


def test_an_issue_is_split_into_its_instruments_each_under_the_issues_page_numbers():
    reading = ng_documents.cloud_reading(gazette(TWO), None)
    record = {"source": "nln-repository", "type": "gazette", "url": "https://x/gazette.pdf", "listed": {}}
    found = ng_documents.documents(reading, record)
    # The issue's front page is its own record (before 2026-10-29 it was in no document).
    assert [(d["id"], d["type"], d["pages"]) for d, _ in found] == [
        ("ng-gazette-111-12-2024", "gazette", [1, 1]),
        ("ng-act-3-2024", "act", [2, 3]),
        ("ng-act-4-2024", "act", [4, 5]),
    ]
    assert (
        found[0][0]["title"] == "Federal Republic of Nigeria Official Gazette No. 12, Vol. 111 (2024-03-03)"
    )
    document, part = found[2]
    assert [p["page"] for p in part["pages"]] == [4, 5] and part["summary"]["pages"] == 2
    assert document["issue"]["number"] == "12" and document["printed_pages"] == "A12-13"
    # With one supplement only, its cover goes with it.
    single = ng_documents.documents(ng_documents.cloud_reading(gazette(TWO[:1]), None), record)
    assert [(d["id"], d["pages"]) for d, _ in single] == [("ng-act-3-2024", [1, 3])]


def test_a_file_with_no_gazette_front_page_is_one_instrument_titled_by_its_first_heading():
    reading = {
        "sha256": "b" * 64,
        "reader": "docai",
        "version": {},
        "pages": [
            page(1, [node(1, "EXAMPLE REGULATIONS, 2024", "heading", "heading-1"), node(1, "1. Citation.")]),
            page(2, [node(2, "2. Commencement.")]),
        ],
    }
    reconciled = ng_documents.cloud_reading(reading, None)
    found = ng_documents.documents(
        reconciled, {"source": "ncaa", "type": "regulation", "url": "u", "listed": {}}
    )
    assert [(d["id"], d["type"], d["pages"]) for d, _ in found] == [
        ("ng-regulation-example-regulations-2024", "regulation", [1, 2])
    ]
    act = ng_documents.documents(
        reconciled, {"source": "nass-acts", "type": "act", "url": "u", "listed": {"number": 5}}
    )
    assert act[0][0]["id"] == "ng-regulation-example-regulations-2024"  # the title decides the kind


def test_the_layout_parser_is_checked_by_the_ocr_and_a_missing_check_blocks_approval():
    docai = gazette(TWO[:1])
    ocr = {
        "version": {"processor": "ocr"},
        "pages": [
            {"page": p["page"], "text": p["text"].replace("Example Act.", "Exampel Act.")}
            for p in docai["pages"]
        ],
    }
    checked = ng_documents.cloud_reading(docai, ocr)
    assert (
        checked["checks"] == {"docai-ocr": {"processor": "ocr"}}
        and checked["summary"]["missing_checks"] == []
    )
    flagged = [f for p in checked["pages"] for f in p["flags"]]
    assert flagged and {"docai-ocr"} <= set(flagged[0]["checks"])
    alone = ng_documents.cloud_reading(docai, None)
    assert alone["summary"]["missing_checks"] == ["docai-ocr"]
    assert ng_documents.cloud_reading(None, ocr) is None  # waits for the Layout Parser


def test_the_ocr_check_as_the_scan_reader_stores_it_words_and_boxes_only():
    """Google's OCR is stored as words with their boxes and no text (layout_parser.ocr_page): the
    check reads them as lines, so a file with its OCR check prepares (before 2026-10-29 it failed)."""
    docai = gazette(TWO[:1])

    def stored(text: str) -> list[dict]:
        words = []
        for row, line in enumerate(text.replace("Example Act.", "Exampel Act.").splitlines()):
            for column, word in enumerate(line.split()):
                y = 0.05 + row * 0.02
                words.append(
                    {
                        "text": word,
                        "x0": 0.1 + column * 0.05,
                        "y0": y,
                        "x1": 0.14 + column * 0.05,
                        "y1": y + 0.015,
                    }
                )
        return words

    ocr = {
        "version": {"processor": "ocr"},
        "pages": [{"page": p["page"], "words": stored(p["text"])} for p in docai["pages"]],
    }
    checked = ng_documents.cloud_reading(docai, ocr)
    assert checked["summary"]["missing_checks"] == []
    flagged = [f for p in checked["pages"] for f in p["flags"]]
    assert flagged and {"docai-ocr"} <= set(flagged[0]["checks"])
    # Read as lines, as printed, not one run of words.
    lines = {p["page"]: len([x for x in p["text"].splitlines() if x.split()]) for p in docai["pages"]}
    assert {p["page"]: len(p["checks"]["docai-ocr"].splitlines()) for p in checked["pages"]} == lines
    assert max(lines.values()) > 1


def test_the_structure_rules_follow_the_type_and_regulations_wait_for_theirs():
    from workbench import content, publication

    reading = ng_documents.cloud_reading(gazette(TWO), None)
    record = {"source": "nln-repository", "type": "gazette", "url": "u", "listed": {}}
    document, part = ng_documents.documents(reading, record)[1]  # [0] is the issue's front page
    final = [{"page": p["page"], "units": content.units_from(p), "revision": 0} for p in part["pages"]]
    assert publication.build(document, final, part).rules_id == "ng-federal-act-1"
    regulation = {**document, "type": "regulation"}
    built = publication.build(regulation, final, part)
    assert built.rules_id is None and [e.code for e in built.errors] == ["no_structure_rules"]


def test_a_supreme_court_judgment_takes_its_details_from_the_courts_catalogue():
    listed = {
        "case_id": 77,
        "suit_no": "SC.123/2010",
        "parties": {"petitioner": "A. EXAMPLE", "respondent": "B. EXAMPLE LTD"},
        "case_title": "A. EXAMPLE V. B. EXAMPLE LTD",
        "judgment_date": "2019-05-03",
        "justices": ["C. JUSTICE, JSC", "D. JUSTICE, JSC", "E. JUSTICE, JSC"],
        "lead_judge": "C. JUSTICE",
        "dissenting": "E. JUSTICE",
        "counsel": {"petitioner": "F. Counsel SAN", "respondent": ""},
        "opinion": "lead",
    }
    reading = ng_documents.cloud_reading(
        {"sha256": "c" * 64, "reader": "docai", "version": {}, "pages": [page(1, [node(1, "JUDGMENT")])]},
        None,
    )
    record = {"source": "supreme-court", "type": "judgment", "url": "https://sc/x.pdf", "listed": listed}
    ((document, _),) = ng_documents.documents(reading, record)
    assert document["id"] == "ng-sc-sc-123-2010-2019-05-03"
    values = document["suggested"]
    assert values["profile.court.level"] == "apex" and values["event:decided.date"] == "2019-05-03"
    assert [b["role"] for b in values["profile.bench"]] == ["author", "member", "dissenting"]
    assert values["profile.parties"][0] == {
        "name": "A. EXAMPLE",
        "role": "petitioner",
        "anonymised": "no",
        "represented_by": "F. Counsel SAN",
    }
    assert "profile.outcome" not in values
    concurring = ng_documents.documents(
        reading, {**record, "listed": {**listed, "opinion": "contribution", "justice": "D. JUSTICE, JSC"}}
    )[0][0]
    assert concurring["id"] == "ng-sc-sc-123-2010-2019-05-03-d-justice-jsc"
    assert concurring["work"] == "ng-sc-sc-123-2010-2019-05-03"
    assert values["identity.title"] == "A. EXAMPLE V. B. EXAMPLE LTD"  # the catalogue's, where it gives one
    assert values["identity.full_title"] == "A. EXAMPLE (Petitioner) v. B. EXAMPLE LTD (Respondent)"
    # 2026-10-30: none of the 247 in the catalogue gave a title; it is the parties', as cases are cited.
    untitled = {k: v for k, v in listed.items() if k != "case_title"}
    values = ng_documents.documents(reading, {**record, "listed": untitled})[0][0]["suggested"]
    assert values["identity.title"] == "A. EXAMPLE v. B. EXAMPLE LTD"


class MemoryStore:
    def __init__(self):
        self.objects = {}

    def exists(self, name):
        return name in self.objects

    def get(self, name):
        return self.objects[name]

    def put(self, name, data, content_type, replace=False):
        self.objects[name] = data

    def list(self, prefix):
        return sorted(n for n in self.objects if n.startswith(prefix))


def test_files_are_prepared_once_read_and_again_when_the_ocr_arrives():
    store = MemoryStore()
    sha = "a" * 64
    items = {
        "nln:1": {
            "source": "nln-repository",
            "type": "gazette",
            "sha256": sha,
            "url": "https://x/gazette.pdf",
            "stored": f"gs://b/sources/ng/nln-repository/{sha}.pdf",
            "listed": {},
        }
    }
    collection = nicn.BucketCollection(store, pipeline=ng_documents.PIPELINE)
    assert ng_documents.prepare_pdfs(store, items, list(items), collection)["waiting"] == 1
    store.objects[f"readings/ng/docai/{sha}.json"] = json.dumps(gazette(TWO)).encode()
    first = ng_documents.prepare_pdfs(store, items, list(items), collection)
    assert (first["prepared"], first["documents"]) == (1, 3)  # the issue's front page and its two Acts
    index = json.loads(store.objects[f"collections/ng/issues/{sha}.json"])
    assert index["readings"] == {"docai": True, "ocr": False} and index["source"] == "nln-repository"
    assert ng_documents.prepare_pdfs(store, items, list(items), collection)["skipped_done"] == 1
    store.objects[f"readings/ng/ocr/{sha}.json"] = json.dumps(
        {"version": {}, "pages": [{"page": p["page"], "text": p["text"]} for p in gazette(TWO)["pages"]]}
    ).encode()
    assert ng_documents.prepare_pdfs(store, items, list(items), collection)["prepared"] == 1  # read again
    collection.save_manifest()
    manifest = json.loads(store.objects["collections/ng/manifest.json"])
    assert [d["id"] for d in manifest["documents"]] == [
        "ng-act-3-2024",
        "ng-act-4-2024",
        "ng-gazette-111-12-2024",
    ]
    entry = manifest["documents"][0]
    assert (
        entry["source_object"] == f"sources/ng/nln-repository/{sha}.pdf"
        and entry["objects"] == f"files/{sha}/"
    )


def test_every_page_of_a_file_reaches_a_document():
    """A schedule printed past the range the front page lists stays with its Act, named for the reviewer;
    a file whose listed ranges cannot be mapped is one instrument, its pages doubtful (before 2026-10-29
    such pages, or whole files, reached no document)."""
    reading = ng_documents.cloud_reading(gazette(TWO[:1]), None)
    extra = {
        **reading["pages"][-1],
        "page": reading["pages"][-1]["page"] + 1,
        "nodes": [{"kind": "heading", "text": "SCHEDULE", "page": 9}],
        "flags": [],
    }
    reading = {**reading, "pages": [*reading["pages"], extra]}
    record = {"source": "nass-acts", "type": "act", "url": "u", "listed": {}}
    [(document, part)] = ng_documents.documents(reading, record)
    assert document["pages"] == [1, extra["page"]] and document["pages_beyond_listed"] == [extra["page"]]
    assert any(f["kind"] == "pages_beyond_listed_range" for p in part["pages"] for f in p["flags"])
    unmapped = ng_documents.instruments(
        {"pages": [{"page": 1, "nodes": []}, {"page": 2, "nodes": []}]}, "act"
    )
    assert unmapped[0]["pages"] == [1, 2]


def test_only_a_task_that_finds_every_task_finished_builds_the_manifest(monkeypatch):
    """2026-10-31: each task built the manifest at its end, and one that began before the last indexes
    were written wrote after the others, its manifest missing 154 documents."""
    from ingestion import prepare_job

    store = MemoryStore()
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "corpus-preparer-x")
    assert [prepare_job.last_to_finish(store, "ng", task, 3) for task in (1, 0)] == [False, False]
    assert prepare_job.last_to_finish(store, "ng", 2, 3)  # the last to finish, after every index
    monkeypatch.delenv("CLOUD_RUN_EXECUTION")
    assert prepare_job.last_to_finish(MemoryStore(), "ng", 0, 3)  # a run on the workstation
