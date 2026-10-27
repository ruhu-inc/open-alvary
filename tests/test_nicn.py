"""The National Industrial Court's judgment pages as review documents (ingestion.nicn, CPR-07).

The fixtures are cut from real record pages of the court's portal, one per shape it publishes in
(Word's HTML; a PDF viewer's text layer copied into the page; the whole judgment in one table cell;
a placeholder with no judgment), their markup kept and every name and word of substance replaced.
"""

import hashlib
import json
import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from ingestion import nicn

FIXTURES = Path(__file__).parent / "fixtures" / "ng"


def page(name: str) -> bytes:
    return (FIXTURES / f"nicn-page-{name}.html").read_bytes()


def texts(units, header=None):
    return [u.text for u in units if header is None or u.header == header]


# --- the reader -----------------------------------------------------------------------------------


def test_a_word_page_gives_the_header_as_printed_and_leaves_the_portal_out():
    judgment = nicn.read(page("word"))
    assert judgment.shape == "word" and judgment.empty is None
    assert texts(judgment.header) == [
        "IN THE NATIONAL INDUSTRIAL COURT OF NIGERIA",
        "IN THE LAGOS JUDICIAL DIVISION",
        "HOLDEN AT LAGOS",
        "BEFORE HIS LORDSHIP, HON. JUSTICE C. D. EXAMPLE",
        "Date: 8th January 2026 SUIT NO. NICN/LA/123/2020",
        "BETWEEN",
        "ADA EXAMPLE … CLAIMANT",
        "AND",
        "EXAMPLE SERVICES LIMITED ... DEFENDANT",
        "JUDGMENT",
    ]
    kinds = [u.kind for u in judgment.header]
    assert kinds[:3] == ["heading"] * 3 and kinds[-1] == "heading" and set(kinds[3:-1]) == {"paragraph"}
    # The portal's buttons, its copyright line, a converter's advertising link and a drawn rule are
    # furniture, kept beside the reading and never among its units.
    assert judgment.furniture == [
        "PORTAL",
        "GET CTC OF THIS JUDGMENT",
        "© National Industrial Court of Nigeria- ALL RIGHTS RESERVED.",
        "__________________________",
        "Email Signature Generator",
    ]
    words = " ".join(u.text for u in judgment.units if u.rows is None)
    assert "PORTAL" not in words and "Email Signature" not in words and "Home" not in words


def test_a_word_page_keeps_printed_numbers_lists_quotations_and_tables_apart():
    body = nicn.read(page("word")).body
    shown = [(u.kind, u.marker, u.indent) for u in body]
    # Word numbers the judgment's own paragraphs with its lists: they stay paragraphs, numbered; a list
    # within a paragraph (lettered by the HTML list, or restarting at 1 further in) is its items.
    assert shown[:4] == [
        ("paragraph", "1.", None),
        ("list_item", "a.", 1),
        ("list_item", "b.", 1),
        ("list_item", "1.", 1),
    ]
    assert body[1].marker_source == "list numbering" and body[0].marker_source == "web page"
    assert body[0].text.startswith("The Claimant began this suit")  # the number is the marker, not the text
    assert (body[4].kind, body[4].text) == ("heading", "Summary of evidence")
    assert shown[5] == ("paragraph", "2.", None)
    quotation = body[6]
    assert quotation.indent == 1 and quotation.text.startswith("“Where the contract of service")
    assert {"type": "italic", "start": 0, "end": len(quotation.text)} in quotation.marks
    assert shown[7] == ("paragraph", "3.", None)
    table = body[8]
    assert table.kind == "table" and [[c["text"] for c in row] for row in table.rows] == [
        ["Item", "Amount"],
        ["Salary arrears", "₦1,200,000.00"],
        ["Leave allowance", "₦100,000.00"],
    ]
    # The judge's name and office after the last paragraph are never cross-headings.
    closing = [(u.kind, u.text) for u in body[10:12]]
    assert closing == [("paragraph", "HON. JUSTICE C. D. EXAMPLE"), ("paragraph", "JUDGE")]


def test_the_words_are_kept_exactly_and_the_naira_sign_restored_only_where_flagged():
    raw = page("word")
    judgment = nicn.read(raw)
    soup = BeautifulSoup(raw, "html.parser")
    container = soup.select_one("div.light-background div.col-12").get_text("")
    for furniture in ("__________________________", "Email Signature Generator"):
        container = container.replace(furniture, "")
    printed = re.sub(r"\s+", "", container)
    # A number an HTML list draws beside its item ("a.") is the reading's marker, never the page's text.
    read = "".join(
        " ".join(c["text"] for row in u.rows for c in row)
        if u.rows
        else ("" if u.marker_source == "list numbering" else u.marker or "") + u.text
        for u in judgment.units
    )
    assert re.sub(r"\s+", "", read) == printed.replace("?2,000", "₦2,000").replace("?1,200", "₦1,200")
    flags = [f for u in judgment.units for f in u.flags]
    assert [(f["kind"], f["primary"], f["others"]) for f in flags] == [
        ("naira_sign_restored", "₦2,000,000.00", {"As published": "?2,000,000.00"}),
        ("naira_sign_restored", "₦1,200,000.00", {"As published": "?1,200,000.00"}),
    ]
    # A question mark ending a question before a number is never a naira sign.
    question = nicn.Unit(
        kind="paragraph", text="Whether the suit is competent? 2. Whether the court has jurisdiction?"
    )
    nicn.check_text(question)
    assert question.flags == [] and "?" in question.text


def test_a_struck_through_n_before_an_amount_is_given_as_the_naira_sign_and_noted():
    filler = "The court considered the evidence of both parties and the submissions of counsel. " * 4
    raw = (
        '<html><body><div class="light-background"><div class="col-12">'
        f"<p>{filler}</p>"
        "<p>The claimant is awarded <s>N</s>4,620,000.00 and "
        "<span style='text-decoration: line-through'>N</span> 65,000 as costs. "
        "Counsel, Mr <s>N</s>. Okafor, and the clerk <s>struck these words</s> agreed.</p>"
        f"<p>{filler}</p></div></div></body></html>"
    ).encode()
    judgment = nicn.read(raw)
    unit = next(u for u in judgment.units if "awarded" in u.text)
    assert unit.text == (
        "The claimant is awarded ₦4,620,000.00 and ₦ 65,000 as costs. "
        "Counsel, Mr N. Okafor, and the clerk struck these words agreed."
    )
    assert [(f["severity"], f["kind"], f["primary"]) for f in unit.flags] == [
        ("low", "naira_sign_struck", "₦4,620,000.00; ₦ 65,000")
    ]
    assert unit.text[slice(*unit.flags[0]["span"])] == "₦4,620,000.00 and ₦ 65,000"
    assert not any(m["type"] == "struck" for m in unit.marks)  # never kept as formatting
    assert nicn.STRUCK_N not in json.dumps([u.text for u in judgment.units] + judgment.furniture)


def test_a_pdf_viewers_copy_has_its_summary_left_out_its_lines_joined_and_its_faults_flagged():
    judgment = nicn.read(page("pdf-lines"))
    assert judgment.shape == "pdf-lines"
    # The portal's summary card is furniture, kept as printed (it is the details' evidence too).
    assert judgment.summary.startswith(
        "IN THE NATIONAL INDUSTRIAL COURT OF NIGERIA Suit No: NICN/ABJ/99/2013"
    )
    assert judgment.summary.endswith("Judgment Delivered")
    assert "Page of 5" in judgment.furniture  # the viewer's toolbar
    header = texts(judgment.header)
    assert header[0] == "IN THE NATIONAL INDUSTRIAL COURT OF NIGERIA" and header[-1] == "JUDGMENT"
    assert "Between:" in header and "REPRESENTATION" in header  # the header's lines stay one unit each
    body = judgment.body
    assert body[0].text == (
        "By a complaint dated the 3rd of July 2013 and filed on the 4th of July 2013, the claimant prayed the court "
        "for the following reliefs."
    )
    assert (body[1].marker, body[1].text.endswith("between the claimant and the defendant;")) == ("1)", True)
    assert body[2].marker == "2)"
    assert [f["kind"] for f in judgment.flags] == ["lines_joined"]
    flags = {f["kind"]: f for u in body for f in u.flags}
    # Text encoded twice is flagged with what it probably meant, and left as published.
    assert flags["encoding_fault"]["primary"] == "â€™" and flags["encoding_fault"]["others"] == {
        "Probably meant": "’"
    }
    assert "claimantâ€™s" in body[3].text
    # The viewer counted five pages; the page holds two, and stops mid-sentence.
    assert (judgment.viewer_pages, judgment.copied_pages) == (5, 2)
    incomplete = flags["text_incomplete"]
    assert incomplete["severity"] == "high" and "counted 5 pages" in incomplete["primary"]
    assert "stops mid-sentence" in incomplete["primary"]


def test_one_block_is_cut_at_sentence_ends_and_masked_words_are_flagged_with_their_cause():
    judgment = nicn.read(page("one-block"))
    assert judgment.shape == "one-block"
    assert judgment.summary.startswith("THE NATIONAL INDUSTRIAL COURT OF NIGERIA")
    assert "Created with WordToHTML.net trial." in judgment.furniture
    assert judgment.header == []
    assert [f["kind"] for f in judgment.flags] == ["paragraphs_not_marked", "no_printed_header"]
    body = judgment.body
    assert len(body) > 1 and body[0].text.startswith("The claimant filed a General Form of Complaint")
    assert [u.marker for u in body if u.marker] == ["2.", "3."]  # a numbered sentence opens a unit
    masked = [f for u in body for f in u.flags if f["kind"] == "masked_words"]
    assert masked[0]["severity"] == "high" and masked[0]["primary"] == "4 words masked with asterisks"
    assert masked[0]["others"] == {"The page says": "Created with WordToHTML.net trial."}
    # The closing words (the judge's name and office) end it: not taken for text that stops short.
    assert not any(f["kind"] == "text_incomplete" for u in body for f in u.flags)


def test_a_page_with_no_judgment_gives_no_document():
    judgment = nicn.read(page("empty"))
    assert judgment.empty == "the page holds no judgment text ('Enter text here.')"
    record = {"url": "https://nicnadr.gov.ng/judgement/judgement.php?id=9004", "listed": {"record_id": 9004}}
    assert nicn.prepare_page("i", record, page("empty"), {}, "ng-nicn-r9004", []) == (None, judgment.empty)


def test_the_reading_is_read_once_page_by_page_and_every_unit_is_one_the_workbench_takes():
    from workbench import content

    judgment = nicn.read(page("word"))
    reading = nicn.reading(judgment, "a" * 64, 9001)
    assert reading["primary"]["reader"] == "web-page" and reading["checks"]["web_page"]["record_id"] == 9001
    for number, shown in enumerate(reading["pages"], 1):
        assert shown["page"] == number
        assert shown["flags"][0]["kind"] == "one_reading"  # nothing compared: reviewed whole
        units = content.units_from(shown)
        assert content.validate_units(units)
        words = sum(
            len(str(u.get("marker") or "").split()) + len(u.get("text", "").split())
            for u in units
            if u["kind"] != "table"
        )
        words += sum(len(c.split()) for u in units if u["kind"] == "table" for row in u["rows"] for c in row)
        assert all(0 <= f["tokens"][0] <= f["tokens"][1] <= max(words, 1) for f in shown["flags"])
    first = content.units_from(reading["pages"][0])
    quotation = next(u for u in first if u["text"].startswith("“Where"))
    assert quotation["level"] == 1 and quotation["marks"][0]["type"] == "italic"
    assert reading["pages"][0]["furniture"][:2] == ["PORTAL", "GET CTC OF THIS JUDGMENT"]


def test_long_judgments_are_cut_into_review_pages_between_units():
    units = [nicn.Unit(kind="heading", text="JUDGMENT", header=True)] + [
        nicn.Unit(kind="paragraph", text=" ".join(["word"] * 300), marker=f"{n}.") for n in range(1, 8)
    ]
    pages = nicn.paginate(units)
    assert [len(p) for p in pages] == [3, 2, 2, 1]  # about 700 words a page, the header with the first
    long = nicn.split_long([nicn.Unit(kind="paragraph", text="A sentence that runs on. " * 400, marker="5.")])
    assert len(long) > 1 and all(len(u.text) <= nicn.UNIT_CHARACTERS for u in long)
    assert long[0].marker == "5." and long[1].marker is None
    assert long[1].flags[0]["kind"] == "long_paragraph_split"


def test_the_structure_rules_read_the_judgments_paragraphs():
    from workbench import content, publication

    judgment = nicn.read(page("word"))
    reading = nicn.reading(judgment, "a" * 64)
    final = [{"page": p["page"], "units": content.units_from(p), "revision": 0} for p in reading["pages"]]
    built = publication.build({"id": "ng-nicn-x", "jurisdiction": "ng", "type": "judgment"}, final, reading)
    assert built.rules_id == "judgment-paragraphs-1" and not built.errors
    locators = [n.locator for n in built.structure.nodes]
    assert "header" in locators and "op_1" in locators
    assert {"para_1", "para_2", "para_3", "para_4"} <= set(locators)


# --- the details suggested ------------------------------------------------------------------------

ENTRY = {
    "listed": {
        "record_id": 9001,
        "title": "ADA EXAMPLE -VS- EXAMPLE SERVICES LIMITED",
        "suit_no": "NICN/LA/123/2020",
        "judgment_date": "2026-01-08",
    }
}


def suggest(name="word", entry=ENTRY, kind="judgment"):
    judgment = nicn.read(page(name))
    page_of = {id(u): n for n, units in enumerate(nicn.paginate(judgment.units), 1) for u in units}
    provenance = {"retrieved_at": "2026-10-23T00:28:42Z", "sha256": "f" * 64}
    url = "https://nicnadr.gov.ng/judgement/judgement.php?id=9001"
    return nicn.details(judgment, entry, kind, url, provenance, page_of)


def test_the_details_are_suggested_from_the_header_each_with_its_words_and_page():
    values, evidence = suggest()
    assert values["identity.family"] == "judgment" and values["identity.type"] == "judgment"
    assert values["profile.court.name"] == "National Industrial Court of Nigeria"
    assert values["profile.court.level"] == "specialised"  # as Nigeria's jurisdiction profile lists it
    assert (values["profile.court.division"], values["profile.court.seat"]) == ("Lagos", "Lagos")
    assert values["profile.case_numbers"] == ["NICN/LA/123/2020"]
    assert values["identity.number.printed"] == "SUIT NO. NICN/LA/123/2020"
    assert (values["event:decided.date"], values["identity.year"]) == ("2026-01-08", "2026")
    assert values["profile.bench"] == [{"name": "HON. JUSTICE C. D. EXAMPLE", "role": "sole"}]
    # The parties as printed, each with its role and the counsel the representation names for it.
    assert values["profile.parties"] == [
        {
            "name": "ADA EXAMPLE",
            "role": "claimant",
            "anonymised": "no",
            "represented_by": "A. B. Counsel Esq., with E. F. Junior Esq.",
        },
        {
            "name": "EXAMPLE SERVICES LIMITED",
            "role": "defendant",
            "anonymised": "no",
            "represented_by": "G. H. Advocate Esq.",
        },
    ]
    assert values["identity.full_title"] == "ADA EXAMPLE (Claimant) v. EXAMPLE SERVICES LIMITED (Defendant)"
    assert values["publication.kind"] == "official_website" and values["text.form"] == "official_website"
    assert values["publication.url"].endswith("id=9001")
    assert evidence["profile.court.division"] == {
        "value": "Lagos",
        "page": 1,
        "evidence": "IN THE LAGOS JUDICIAL DIVISION",
    }
    assert evidence["event:decided.date"]["evidence"] == "Date: 8th January 2026 SUIT NO. NICN/LA/123/2020"
    assert "2026-10-23T00:28:42Z" in evidence["publication.url"]["evidence"]
    # What the page does not print is suggested as accounted for, for the reviewer to confirm.
    assert evidence["identity.citation"] == {
        "absent": "not_in_source",
        "page": None,
        "evidence": "The page prints no neutral citation",
    }
    # The legal facts are never suggested: the outcome, standing as precedent, legal status, authority.
    for key in ("profile.outcome", "profile.precedent", "profile.treatment", "status.legal"):
        assert key not in values and key not in evidence
    assert all("authority" not in row for row in values["expressions"])


def test_the_portals_catalogue_stands_in_where_the_page_prints_no_header():
    entry = {
        "listed": {
            "record_id": 9003,
            "suit_no": "NICN/LA/77/2012",
            "judgment_date": "2013-07-25",
            "from_page": {
                "claimant": "ADA EXAMPLE",
                "defendant": "EXAMPLE BANK PLC",
                "structured_header": {"date_delivered": "2013-07-25", "judges": "HON. JUSTICE C. D. EXAMPLE"},
            },
        }
    }
    values, evidence = suggest("one-block", entry)
    assert (
        values["profile.case_numbers"] == ["NICN/LA/77/2012"] and values["event:decided.date"] == "2013-07-25"
    )
    assert evidence["event:decided.date"]["evidence"] == "The portal's summary: Date Delivered: 2013-07-25"
    assert [p["role"] for p in values["profile.parties"]] == ["claimant", "defendant"]
    assert values["profile.bench"] == [{"name": "HON. JUSTICE C. D. EXAMPLE", "role": "sole"}]
    assert evidence["profile.court.division"]["absent"] == "not_in_source"


# --- ids, collections and the cloud job -------------------------------------------------------------


def record(record_id, suit="NICN/LA/1/2020", date="2021-03-04", sha=None, **extra):
    return {
        "source": "nicn-judgments",
        "url": f"https://nicnadr.gov.ng/judgement/judgement.php?id={record_id}",
        "sha256": sha or hashlib.sha256(str(record_id).encode()).hexdigest(),
        "stored": "gs://b/x",
        "type": "judgment",
        "listed": {"record_id": record_id, "suit_no": suit, "judgment_date": date},
        **extra,
    }


def test_document_ids_are_stable_and_never_collide():
    items = {
        "a": record(500),
        "b": record(42),  # the same suit and date, an earlier record: it keeps the plain id
        "c": record(77, date=None),
        "d": record(78, suit=None, date=None),
        "e": record(9, suit="NICN/ABK/2M/2013", date="2018-09-26"),
    }
    ids = nicn.document_ids(items)
    assert ids == {
        "b": "ng-nicn-la-1-2020-2021-03-04",
        "a": "ng-nicn-la-1-2020-2021-03-04-r500",
        "c": "ng-nicn-la-1-2020-r77",
        "d": "ng-nicn-r78",
        "e": "ng-nicn-abk-2m-2013-2018-09-26",
    }
    later = {**items, "f": record(900)}  # a record that arrives later changes no earlier id
    assert {k: v for k, v in nicn.document_ids(later).items() if k != "f"} == ids


class MemoryStore:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.puts = []

    def exists(self, name):
        return name in self.objects

    def get(self, name):
        return self.objects[name]

    def put(self, name, data, content_type, replace=False):
        self.puts.append(name)
        self.objects[name] = data

    def list(self, prefix):
        return sorted(n for n in self.objects if n.startswith(prefix))


def stored(store, raw, record_id, **listed):
    sha = hashlib.sha256(raw).hexdigest()
    base = f"sources/ng/nicn-judgments/{sha}"
    store.objects[f"{base}.html"] = raw
    sidecar = {
        "url": f"https://nicnadr.gov.ng/judgement/judgement.php?id={record_id}",
        "retrieved_at": "2026-10-23T00:28:42Z",
        "type": "judgment",
        "listed": {"record_id": record_id, **listed},
    }
    store.objects[f"{base}.json"] = json.dumps(sidecar).encode()
    return {**record(record_id, sha=sha), "listed": sidecar["listed"]}


def test_the_bucket_collection_prepares_each_page_once_per_reader_version_and_assembles_the_manifest():
    store = MemoryStore()
    items = {
        "nicn-judgments:9001": stored(
            store, page("word"), 9001, suit_no="NICN/LA/123/2020", judgment_date="2026-01-08"
        ),
        "nicn-judgments:9004": stored(store, page("empty"), 9004),
    }
    collection = nicn.BucketCollection(store)
    counts = nicn.prepare_items(
        items, sorted(items), nicn.bucket_fetch(store), collection, log=lambda m: None
    )
    assert counts == {"prepared": 1, "skipped_done": 0, "empty": 1, "held": 0, "text_held": 0, "failed": 0}
    sha = items["nicn-judgments:9001"]["sha256"]
    folder = f"collections/ng/files/{sha}/"
    doc_id = "ng-nicn-la-123-2020-2026-01-08"
    for suffix in (".reading.json", ".provenance.json", ".document.json"):
        assert f"{folder}{doc_id}{suffix}" in store.objects
    index = json.loads(store.objects[f"collections/ng/issues/{sha}.json"])
    assert index["pipeline"] == nicn.READER and index["documents"][0]["objects"] == f"files/{sha}/"
    empty = json.loads(store.objects[f"collections/ng/issues/{items['nicn-judgments:9004']['sha256']}.json"])
    assert empty["documents"] == [] and empty["skipped"].startswith("the page holds no judgment text")
    provenance = json.loads(store.objects[f"{folder}{doc_id}.provenance.json"])
    assert provenance["source_object"] == f"sources/ng/nicn-judgments/{sha}.html"
    assert provenance["sha256"] == sha and provenance["retrieved_at"] == "2026-10-23T00:28:42Z"
    details = json.loads(store.objects[f"{folder}{doc_id}.document.json"])
    assert details["suggested"]["profile.court.name"] == "National Industrial Court of Nigeria"
    # Run again: nothing is prepared twice.
    store.puts.clear()
    again = nicn.prepare_items(items, sorted(items), nicn.bucket_fetch(store), collection, log=lambda m: None)
    assert again["skipped_done"] == 2 and store.puts == []
    collection.save_manifest()
    manifest = json.loads(store.objects["collections/ng/manifest.json"])
    assert (manifest["collection"], manifest["jurisdiction"], manifest["skipped_files"]) == ("ng", "ng", 1)
    (entry,) = manifest["documents"]
    assert entry["id"] == doc_id and entry["type"] == "judgment" and entry["media_type"] == "text/html"
    assert "suggested" not in entry and entry["page_count"] >= 1 and entry["flag_counts"]["medium"] >= 1
    assert "rights review" in manifest["rights_basis"]


def test_a_partial_judgment_is_held_back_and_a_masked_one_is_reviewed_for_its_details_only():
    store = MemoryStore()
    items = {
        "nicn-judgments:9002": stored(store, page("pdf-lines"), 9002, suit_no="NICN/ABJ/7/2014"),
        "nicn-judgments:9003": stored(store, page("one-block"), 9003, suit_no="NICN/LA/9/2015"),
    }
    collection = nicn.BucketCollection(store)
    counts = nicn.prepare_items(
        items, sorted(items), nicn.bucket_fetch(store), collection, log=lambda m: None
    )
    assert (counts["prepared"], counts["held"], counts["text_held"]) == (1, 1, 1)
    partial_sha = items["nicn-judgments:9002"]["sha256"]
    assert not any(name.startswith(f"collections/ng/files/{partial_sha}/") for name in store.objects)
    collection.save_manifest()
    manifest = json.loads(store.objects["collections/ng/manifest.json"])
    (held,) = manifest["held"]
    assert (
        held["record_id"] == 9002
        and held["url"].endswith("id=9002")
        and "stops mid-sentence" in held["reason"]
    )
    assert (
        held["id"] not in {d["id"] for d in manifest["documents"]}
        and "owner, 2026-10-25" in manifest["held_note"]
    )
    (masked,) = manifest["documents"]
    assert masked["record_id"] == 9003 and masked["text_held"]["reason"] == "masked_words"
    assert masked["text_held"]["note"].startswith("4 words masked")


def portal(*paragraphs: str) -> bytes:
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return f'<html><body><div class="light-background"><div class="col-12">{body}</div></div></body></html>'.encode()


def test_a_finished_judgment_ending_without_a_full_stop_is_not_held_but_an_opening_alone_is():
    reasoning = (
        "The claimant was dismissed without the notice his contract required and is owed his salary. " * 8
    )
    for ending in (
        ("Judgment is entered accordingly", "Hon. Justice A. B. Judge"),
        ("This is the Court's Judgement and it is hereby entered accordingly",),
        ("Appearances", "A. B. Counsel Esq. with C. D. Junior Esq. for the Claimant",
         "No representation for the 1st, 2nd and 3rd Defendants/Respondents"),
    ):  # fmt: skip
        judgment = nicn.read(portal("JUDGMENT", reasoning, reasoning, *ending))
        assert judgment.partial is None, ending
    cut = nicn.read(
        portal(
            "JUDGMENT", reasoning, "The claimant's counsel argued that the defendant failed to pay him the"
        )
    )
    assert cut.partial == "its text stops mid-sentence"
    # An argument naming counsel is not an ending: "for the plaintiff or applicant to prove if" runs on.
    argued = nicn.read(
        portal(
            "JUDGMENT",
            reasoning,
            "It means every fact necessary for the plaintiff or applicant to prove if traversed in",
        )
    )
    assert argued.partial == "its text stops mid-sentence"
    commenced = "This action was commenced by way of complaint filed on the 11th day of May 2015, wherein the claimant sought reliefs against the defendants jointly and severally."
    opening = nicn.read(portal("RULING", commenced, "1. The sum of N5,453,000.00.", "By a Motion on Notice"))
    assert opening.partial.startswith("it holds only an opening (") and opening.partial.endswith(
        "and no ending"
    )


def test_a_judgment_whose_markup_the_portal_printed_as_text_is_read_as_the_page_it_describes():
    import html as markup

    inner = "".join(
        f'<p style="text-align: justify;"><span style="font-size: 13.0pt;">{t}</span></p>'
        for t in ("In The Court of Appeal of Nigeria", "JUDGMENT", "The appeal succeeds in part. " * 30, "Hon. Justice A. B. Judge, JCA")
    )  # fmt: skip
    judgment = nicn.read(portal(markup.escape(inner)))
    assert [u.text for u in judgment.units][:2] == ["In The Court of Appeal of Nigeria", "JUDGMENT"]
    assert not any("<span" in u.text or "</p>" in u.text for u in judgment.units)
    assert [f["kind"] for f in judgment.flags if f["kind"] == "markup_as_text"] == ["markup_as_text"]
    assert judgment.court == "In The Court of Appeal of Nigeria" and judgment.partial is None


def test_a_law_reports_summaries_are_left_out_up_to_the_judgments_own_heading():
    filler = "The appellant was employed by the respondent and was dismissed without notice. " * 3
    summary = "<p>RATIO DECIDENDI</p><p>1. Employment - wrongful dismissal - remedy.</p><p>2. Notice.</p>"
    raw = (
        '<html><body><div class="light-background"><div class="col-12">'
        "<p>IN THE COURT OF APPEAL</p><p>HOLDEN AT LAGOS</p>"
        f"{summary}<p>LEAD JUDGMENT (Delivered by A. B. Judge, JCA)</p><p>{filler}</p><p>{filler}</p>"
        "</div></div></body></html>"
    ).encode()
    judgment = nicn.read(raw)
    assert judgment.editorial == [
        "RATIO DECIDENDI",
        "1. Employment - wrongful dismissal - remedy.",
        "2. Notice.",
    ]
    assert not any("RATIO" in u.text or "wrongful dismissal - remedy" in u.text for u in judgment.units)
    assert any(u.text.startswith("LEAD JUDGMENT") for u in judgment.units)
    assert [f["kind"] for f in judgment.flags if f["kind"].startswith("editorial")] == ["editorial_left_out"]
    assert judgment.court == "IN THE COURT OF APPEAL"  # filed under the court that decided it


def test_the_cloud_job_prepares_nigerias_court_pages_in_its_share(monkeypatch):
    from ingestion import corpus_store, prepare_job

    store = MemoryStore()
    items = {
        f"nicn-judgments:{n}": stored(
            store,
            page("word").replace(b"9001", str(n).encode()),
            n,
            suit_no=f"NICN/LA/{n}/2020",
            judgment_date="2026-01-08",
        )
        for n in (1, 2, 3)
    }
    store.objects["state/ng/sync-ledger.json"] = json.dumps({"items": items, "sources": {}}).encode()
    monkeypatch.setattr(corpus_store, "open_store", lambda *a, **k: store)
    monkeypatch.setenv("PREPARE_COUNTRY", "ng")
    monkeypatch.setenv("CLOUD_RUN_TASK_INDEX", "1")
    monkeypatch.setenv("CLOUD_RUN_TASK_COUNT", "2")
    assert prepare_job.main() == 0
    issues = [n for n in store.objects if n.startswith("collections/ng/issues/")]
    assert len(issues) == 1  # the second of two tasks takes every second stored page
    monkeypatch.setenv("PREPARE_SOURCES", "nicn-judgments,statehouse")
    with pytest.raises(SystemExit, match="no review-document reader yet for statehouse"):
        prepare_job.main()


# --- the workbench reviews the collection ---------------------------------------------------------


def test_the_workbench_lists_and_opens_the_ng_collection(tmp_path, monkeypatch):
    """ALVARY_REVIEW_COLLECTIONS naming the collection (here a folder; deployed, its place in the bucket)
    lists its judgments beside the first collection and opens them: the units as read, the page's flags,
    no PDF and no page images (the official page instead), and the details drafted from the suggestions."""
    from fastapi.testclient import TestClient

    from workbench import content
    from workbench.app import create_app
    from workbench.store import Store, connect

    folder = tmp_path / "ng"
    collection = nicn.FolderCollection(folder)
    sha = hashlib.sha256(page("word")).hexdigest()
    items = {"nicn-judgments:9001": record(9001, sha=sha, suit="NICN/LA/123/2020", date="2026-01-08")}
    items["nicn-judgments:9001"]["listed"]["title"] = "ADA EXAMPLE -VS- EXAMPLE SERVICES LIMITED"

    def fetch(found):
        return page("word"), {
            "url": found["url"],
            "retrieved_at": "2026-10-23T00:28:42Z",
            "listed": found["listed"],
        }

    assert nicn.prepare_items(items, list(items), fetch, collection)["prepared"] == 1
    collection.save_manifest()
    monkeypatch.setenv("ALVARY_REVIEW_COLLECTIONS", str(folder))
    first = tmp_path / "first"
    first.mkdir()
    store = Store(connect(f"sqlite:///{tmp_path / 'workbench.db'}"))
    store.add_operator("reviewer@example.org", "Rev", {"reviewer"})
    library = content.Library(first)
    browser = TestClient(create_app(store, library, None, dev_login=True), base_url="http://127.0.0.1")
    assert browser.post("/auth/dev", json={"email": "reviewer@example.org"}).status_code == 200
    browser.headers["X-CSRF-Token"] = browser.get("/api/me").json()["csrf"]

    doc_id = "ng-nicn-la-123-2020-2026-01-08"
    listed = {d["id"]: d for d in browser.get("/api/documents?limit=200").json()["documents"]}
    assert listed[doc_id]["type"] == "judgment"
    shown = browser.get(f"/api/documents/{doc_id}").json()
    assert shown["source"] == {
        "official_url": "https://nicnadr.gov.ng/judgement/judgement.php?id=9001",
        "original_pdf": False,
    }
    assert "rights review" in shown["act"]["rights_basis"]
    one = browser.get(f"/api/documents/{doc_id}/pages/1").json()
    units = one["machine_units"]
    assert units[0] == {
        "kind": "heading",
        "text": "IN THE NATIONAL INDUSTRIAL COURT OF NIGERIA",
        "align": "center",
        "marks": [{"type": "bold", "start": 0, "end": 43}],  # set in bold by its style
    }
    assert any(u.get("marker") == "1." and u["text"].startswith("The Claimant began") for u in units)
    assert any(u.get("level") == 1 and u["text"].startswith("“Where") for u in units)
    assert one["flags"][0]["kind"] == "one_reading"
    image = browser.get(f"/api/documents/{doc_id}/pages/1/image")
    assert image.status_code == 200 and image.headers["content-type"].startswith("image/svg+xml")
    assert b"published as a web page" in image.content
    assert browser.get(f"/api/documents/{doc_id}/original.pdf").status_code == 404
    # The details: the reader's suggestions make the first draft, each beside its words; the legal
    # facts are left to the reviewer.
    details = browser.get(f"/api/documents/{doc_id}/details").json()
    draft = details["record"]["details"]
    assert details["record"]["status"] == "suggested"
    assert draft["profile.court.name"] == "National Industrial Court of Nigeria"
    assert draft["profile.parties"][0]["represented_by"] == "A. B. Counsel Esq., with E. F. Junior Esq."
    assert draft["status.legal"] == "unknown" and "profile.outcome" not in draft
    assert "profile.outcome" in details["missing"]
    assert details["suggestions"]["profile.court.division"]["evidence"] == "IN THE LAGOS JUDICIAL DIVISION"
    assert details["suggestions"]["identity.citation"]["absent"] == "not_in_source"
    columns = next(f for f in details["standard"]["fields"] if f["key"] == "profile.parties")["columns"]
    assert [c["key"] for c in columns] == ["name", "role", "anonymised", "represented_by"]
    saved = browser.post(
        f"/api/documents/{doc_id}/details",
        json={"expected_revision": 0, "status": "draft", "details": draft, "absent": {}},
    )
    assert saved.status_code == 200, saved.text
    # A page saved as the reader gave it is accepted.
    page_one = browser.post(
        f"/api/documents/{doc_id}/pages/1",
        json={"expected_revision": 0, "units": units, "status": "edited", "resolutions": {}},
    )
    assert page_one.status_code == 200, page_one.text


def test_a_judgment_whose_text_is_held_waits_for_its_details_alone(tmp_path, monkeypatch):
    """Words masked in the source: no page waits for review, the queue asks for the details, the
    approval confirms the details alone, and publishing never takes the text."""
    from fastapi.testclient import TestClient

    from operations import publish
    from workbench import content
    from workbench.app import create_app
    from workbench.store import Store, connect

    folder = tmp_path / "ng"
    collection = nicn.FolderCollection(folder)
    sha = hashlib.sha256(page("one-block")).hexdigest()
    items = {"nicn-judgments:9003": record(9003, sha=sha, suit="NICN/LA/9/2015", date="2016-02-01")}

    def fetch(found):
        return page("one-block"), {
            "url": found["url"],
            "retrieved_at": "2026-10-23T00:28:42Z",
            "listed": found["listed"],
        }

    assert nicn.prepare_items(items, list(items), fetch, collection)["text_held"] == 1
    collection.save_manifest()
    monkeypatch.setenv("ALVARY_REVIEW_COLLECTIONS", str(folder))
    first = tmp_path / "first"
    first.mkdir()
    store = Store(connect(f"sqlite:///{tmp_path / 'workbench.db'}"))
    store.add_operator("reviewer@example.org", "Rev", {"reviewer"})
    library = content.Library(first)
    browser = TestClient(create_app(store, library, None, dev_login=True), base_url="http://127.0.0.1")
    assert browser.post("/auth/dev", json={"email": "reviewer@example.org"}).status_code == 200
    doc_id = "ng-nicn-la-9-2015-2016-02-01"
    (row,) = [d for d in browser.get("/api/documents?limit=200").json()["documents"] if d["id"] == doc_id]
    assert row["text_held"] is True and (row["pages"], row["open_high"]) == (0, 0)
    shown = browser.get(f"/api/documents/{doc_id}").json()
    ready = shown["readiness"]
    assert ready["text_held"]["reason"] == "masked_words" and ready["unchecked_pages"] == []
    assert ready["open_high_flags"] == [] and ready["ready"] is False  # its details are not confirmed yet
    assert set(shown["materials"]) == set(content.DETAILS_ONLY_MATERIALS)
    (task,) = [t for t in browser.get("/api/queue").json()["tasks"] if t["id"] == doc_id]
    assert task["kind"] == "details"
    # Publishing never takes a held text, even once its details are approved.
    act = library.act(doc_id)

    class Approved:
        def state(self, act, text=False):
            return {"head": {"decision": "approved"}, "text": None, "approved": True}

    texts = publish.texts_from_workbench(Approved(), library, store, "ng")
    assert texts.approved == [] and texts.skipped[act["id"]].startswith("its text is held: 4 words masked")


def test_a_judgment_from_the_bucket_never_fetches_the_web_page_as_its_original(tmp_path, monkeypatch):
    from workbench import content

    store = MemoryStore()
    items = {
        "nicn-judgments:9001": stored(
            store, page("word"), 9001, suit_no="NICN/LA/123/2020", judgment_date="2026-01-08"
        )
    }
    collection = nicn.BucketCollection(store)
    nicn.prepare_items(items, list(items), nicn.bucket_fetch(store), collection, log=lambda m: None)
    collection.save_manifest()
    monkeypatch.setattr(content, "_STORES", {"test-bucket": store})
    monkeypatch.setenv("ALVARY_REVIEW_COLLECTIONS", "gs://test-bucket/collections/ng/")
    monkeypatch.setenv("ALVARY_REVIEW_CACHE", str(tmp_path / "cache"))
    first = tmp_path / "first"
    first.mkdir()
    library = content.Library(first)
    doc_id = "ng-nicn-la-123-2020-2026-01-08"
    assert library.has(doc_id) and library.web_page(doc_id) and not library.has_original(doc_id)
    assert library.reading(doc_id)["primary"]["reader"] == "web-page"
    assert library.act(doc_id)["suggested"]["identity.type"] == "judgment"
    assert library.provenance(doc_id)["media_type"] == "text/html"
    with pytest.raises(FileNotFoundError):
        library.image(doc_id, 1)
    assert not any(path.suffix == ".html" for path in (tmp_path / "cache").rglob("*") if path.is_file())


def test_names_withheld_in_a_sensitive_case_are_never_published(tmp_path, monkeypatch):
    """Owner, 2026-10-27: names are hidden only in a sensitive case a reviewer marks. The published title,
    citations and summary give each withheld party as [Withheld]; a record that would still name one is
    refused; and the case's text is never published."""
    from datetime import UTC, datetime

    from operations import publish
    from schema.jurisdictions import registry
    from workbench import content
    from workbench.review import Review
    from workbench.store import Store, connect

    folder = tmp_path / "ng"
    collection = nicn.FolderCollection(folder)
    sha = hashlib.sha256(page("word")).hexdigest()
    items = {"nicn-judgments:9001": record(9001, sha=sha, suit="NICN/LA/123/2020", date="2026-01-08")}

    def fetch(found):
        return page("word"), {
            "url": found["url"],
            "retrieved_at": "2026-10-23T00:28:42Z",
            "listed": found["listed"],
        }

    nicn.prepare_items(items, list(items), fetch, collection)
    collection.save_manifest()
    monkeypatch.setenv("ALVARY_REVIEW_COLLECTIONS", str(folder))
    (tmp_path / "first").mkdir()
    library = content.Library(tmp_path / "first")
    review = Review(Store(connect(f"sqlite:///{tmp_path / 'w.db'}")), library, registry())
    act = library.act("ng-nicn-la-123-2020-2026-01-08")
    view = review.described(act, None, library.reading(act["id"]))
    values = {
        **view["values"],
        "identity.title": "AMINA BELLO -VS- EXAMPLE SERVICES LIMITED",
        "profile.parties": [
            {"name": "Amina Bello", "role": "claimant", "anonymised": "yes"},
            {"name": "Example Services Limited", "role": "defendant", "anonymised": "no"},
        ],
        "profile.anonymisation": "partial",
        "profile.anonymisation_reason": "child",
    }
    decision = {"basis": "b", "legal_note": "n", "expires_at": "2027-10-26T00:00:00+00:00", "evidence": []}

    def entry(changes):
        built = review.built(act, {**view, "values": {**values, **changes}})
        return publish.catalogue_entries(
            publish.Approved(act["id"], built, ["en"], "Owner", datetime.now(UTC)), decision
        )

    published = entry({})
    assert published["source"].title == "[Withheld] -VS- EXAMPLE SERVICES LIMITED"
    assert all("bello" not in c.value.lower() for c in published["citations"])
    with pytest.raises(publish.Refused, match="would still name a withheld party"):
        entry({"text.summary.text": "Bello was dismissed while on leave."})
    assert (
        publish.withheld_names(
            review.built(act, {**view, "values": {**values, "profile.anonymisation": "none"}})
        )
        == []
    )
