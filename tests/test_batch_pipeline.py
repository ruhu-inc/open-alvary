import json
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter

from ingestion import batch, compare, readers, reconcile


def blank_pdf(path: Path, pages: int) -> Path:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    buffer = BytesIO()
    writer.write(buffer)
    path.write_bytes(buffer.getvalue())
    return path


# --- comparison --------------------------------------------------------------------------------


def test_words_both_checks_reject_are_flagged_and_numbers_are_high():
    flags = compare.compare(
        "Section 21 (1) is amended within 72 hours",
        {
            "mineru": "Section 21 (1) is amended within 7 days",
            "azure": "Section 21 (1) is amended within 7 days",
        },
    )
    assert [(f.severity, f.kind, f.primary) for f in flags] == [("high", "differs", "72 hours")]
    assert flags[0].checks == {"mineru": "7 days", "azure": "7 days"}


def test_split_checks_lower_the_severity_and_name_the_dissenter():
    flags = compare.compare(
        "the word genuineness", {"mineru": "the word genuineness", "azure": "the word genuiness"}
    )
    assert [(f.severity, f.readers_disagreeing) for f in flags] == [("low", ["azure"])]


def test_typography_only_is_low_and_reconstruction_is_high():
    assert compare.compare("Nigeria — the", {"mineru": "Nigeria - the", "azure": "Nigeria --- the"}) == []
    low = compare.compare("of the State); or", {"mineru": "of the State): or", "azure": "of the State): or"})
    assert {f.severity for f in low} == {"low"}
    reconstructed = compare.compare(
        "SCHEDULE TO THE BILL I certify that",
        {"mineru": "SCHEDULE TO THE BILL", "azure": "SCHEDULE TO THE BILL"},
    )
    assert [(f.severity, f.kind, f.primary) for f in reconstructed] == [
        ("high", "only_in_primary", "I certify that")
    ]


def test_words_every_check_has_but_the_primary_lacks_are_reported():
    flags = compare.compare(
        "Section of the Act", {"mineru": "Section of 44 of the Act", "azure": "Section of 44 of the Act"}
    )
    assert [(f.kind, f.severity) for f in flags] == [("missing_from_primary", "high")]
    assert all("44" in text for text in flags[0].checks.values())


def test_a_negation_gained_lost_or_swapped_is_high():
    tariff = "The Minister shall approve the tariff."
    negated = "The Minister shall not approve the tariff."
    lost = compare.compare(tariff, {"azure": negated, "mineru": negated})
    assert [(f.severity, f.kind, f.checks) for f in lost] == [
        ("high", "missing_from_primary", {"azure": "not", "mineru": "not"})
    ]
    gained = compare.compare(negated, {"azure": tariff, "mineru": tariff})
    assert [(f.severity, f.kind, f.primary) for f in gained] == [("high", "only_in_primary", "not")]
    swapped = compare.compare(
        "except with the consent of the Commission",
        {
            "azure": "unless with the consent of the Commission",
            "mineru": "unless with the consent of the Commission",
        },
    )
    assert [(f.severity, f.kind, f.primary) for f in swapped] == [("high", "differs", "except")]
    for word in ("no", "nor", "never", "without", "notwithstanding", "neither", "cannot"):
        flags = compare.compare(f"and {word} person", {"azure": "and the person", "mineru": "and the person"})
        assert [f.severity for f in flags] == ["high"], word


def test_a_negation_one_check_disputes_is_medium_and_its_typography_alone_is_low():
    split = compare.compare(
        "The Minister shall not approve",
        {"azure": "The Minister shall approve", "mineru": "The Minister shall not approve"},
    )
    assert [(f.severity, f.kind, f.readers_disagreeing) for f in split] == [
        ("medium", "only_in_primary", ["azure"])
    ]
    punctuation = compare.compare(
        "shall not, approve", {"azure": "shall not; approve", "mineru": "shall not; approve"}
    )
    assert [f.severity for f in punctuation] == ["low"]


# --- converters --------------------------------------------------------------------------------


def test_mineru_page_keeps_tables_lists_and_handwriting_and_sets_furniture_aside():
    blocks = [
        {"type": "header", "content": [{"type": "text", "content": "A 123"}]},
        {"type": "doc_title", "content": [{"type": "text", "content": "SCHEDULE"}]},
        {
            "type": "list",
            "content": [{"type": "text", "content": [{"type": "text", "content": "(a) first"}]}],
        },
        {
            "type": "table",
            "content": [
                {"type": "table_caption", "content": [{"type": "text", "content": "Table 1"}]},
                {"type": "table_body", "content": "<table><tr><td>A</td><td>B</td></tr></table>"},
            ],
        },
        {
            "type": "image",
            "sub_type": "text_image",
            "content": [{"type": "image_body", "content": "27th Day of Feb."}],
        },
        {"type": "chart", "content": [{"type": "chart_body", "content": "ignored"}]},
    ]
    page = readers.mineru_page(blocks)
    assert page["text"].splitlines() == ["SCHEDULE", "(a) first", "Table 1", "A B", "27th Day of Feb."]
    assert page["tables"] == [[["A", "B"]]] and page["furniture"] == ["A 123"]


def test_azure_pages_group_paragraphs_and_tables_by_page():
    result = {
        "paragraphs": [
            {"content": "A 302", "role": "pageHeader", "boundingRegions": [{"pageNumber": 1}]},
            {"content": "1. Short title", "boundingRegions": [{"pageNumber": 1}]},
            {"content": "Schedule", "boundingRegions": [{"pageNumber": 2}]},
        ],
        "tables": [
            {
                "rowCount": 1,
                "columnCount": 2,
                "boundingRegions": [{"pageNumber": 2}],
                "cells": [
                    {"rowIndex": 0, "columnIndex": 0, "content": "X"},
                    {"rowIndex": 0, "columnIndex": 1, "content": "Y"},
                ],
            }
        ],
    }
    pages = readers.azure_pages(result, 2)
    assert {k: pages[0][k] for k in ("page", "text", "tables", "furniture")} == {
        "page": 1,
        "text": "1. Short title",
        "tables": [],
        "furniture": ["A 302"],
    }
    assert pages[1]["tables"] == [[["X", "Y"]]]


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeAzure:
    def __init__(self):
        self.calls = []
        self.responses = [
            FakeResponse(429, headers={"Retry-After": "1"}),
            FakeResponse(202, headers={"Operation-Location": "https://x/op"}),
            FakeResponse(200, {"status": "running"}, {"Retry-After": "1"}),
            FakeResponse(503),
            FakeResponse(
                200,
                {"status": "succeeded", "analyzeResult": {"modelId": "prebuilt-layout", "paragraphs": []}},
            ),
        ]

    def request(self, method, url, headers=None, **kwargs):
        self.calls.append((method, url))
        return self.responses.pop(0)


def test_azure_reader_retries_throttling_and_polls_until_done(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_DI_ENDPOINT", "https://example.cognitiveservices.azure.com/")
    monkeypatch.setenv("AZURE_DI_KEY", "test")
    slept = []
    reader = readers.AzureReader(client=FakeAzure(), sleep=slept.append)
    reading = reader.read(blank_pdf(tmp_path / "a.pdf", 2))
    assert [p["page"] for p in reading.pages] == [1, 2] and reading.billed_pages == 2
    assert reading.raw["analyze"]["modelId"] == "prebuilt-layout"
    assert reading.version == reader.version()  # the stored version is the cache key
    assert slept == [1.0, 1.0, 2.0]  # Retry-After, Retry-After, then backoff for the 503


# --- reconciliation -----------------------------------------------------------------------------


def node(text, kind="list_item", page=1):
    return {"kind": kind, "level": kind, "text": text, "page": page, "ordered": True}


def test_markers_are_restored_only_when_the_checks_agree():
    primary = {
        "page": 1,
        "nodes": [node("Section 17 is amended"), node("Section 21 is amended")],
    }
    checks = {
        "mineru": {"text": "2. Section 17 is amended\n3. Section 21 is amended"},
        "azure": {"text": "2. Section 17 is amended\n8. Section 21 is amended"},
    }
    page = reconcile.reconcile_page(primary, checks)
    assert page["nodes"][0]["marker"] == "2." and page["nodes"][0]["marker_source"] == ["azure", "mineru"]
    assert "marker" not in page["nodes"][1]
    assert [f["kind"] for f in page["flags"] if f["severity"] == "high"][0] == "marker_conflict"


# --- batch runner -------------------------------------------------------------------------------


class FakeReader:
    def __init__(self, name, billed, fail=False, log=None):
        self.name, self.billed, self.fail, self.log = name, billed, fail, log if log is not None else []

    def version(self):
        return {"fake": self.name}

    def read(self, pdf):
        self.log.append(self.name)
        if self.fail:
            raise RuntimeError("service down")
        pages = [{"page": 1, "text": "1. Short title", "tables": [], "furniture": []}]
        if self.name == "docai":
            pages[0]["nodes"] = [node("Short title")]
        return readers.Reading(self.name, self.version(), pages, 1 if self.billed else 0)


def make_batch(tmp_path, fail=()):
    log = []
    factories = {
        name: (lambda name=name, billed=billed: FakeReader(name, billed, name in fail, log))
        for name, billed in (("docai", True), ("mineru", False), ("azure", True))
    }
    blank_pdf(tmp_path / "ng-act.pdf", 1)
    return batch.Batch(tmp_path, ["docai", "mineru", "azure"], factories), log


def test_plan_counts_only_uncached_billed_pages_and_runs_resume_from_cache(tmp_path):
    runner, log = make_batch(tmp_path)
    act = {"id": "ng-act"}
    assert sum(row["billed"] for row in runner.plan([act])) == 2
    outcome = runner.run_act(act)
    assert outcome["complete"] and outcome["summary"]["restored_markers"] == 1
    reading = json.loads((tmp_path / "ng-act.reading.json").read_text())
    assert reading["pages"][0]["nodes"][0]["marker"] == "1."
    assert sum(row["billed"] for row in runner.plan([act])) == 0
    runner.run_act(act)
    assert sorted(log) == ["azure", "docai", "mineru"]  # second run read nothing again


def test_one_failing_reader_is_isolated_and_recorded(tmp_path):
    runner, _ = make_batch(tmp_path, fail={"azure"})
    outcome = runner.run_act({"id": "ng-act"})
    assert (
        outcome["readers"]["azure"]["status"] == "failed"
        and "service down" in outcome["readers"]["azure"]["error"]
    )
    assert outcome["readers"]["docai"]["status"] == "read" and not outcome["complete"]
    assert not (tmp_path / "readings" / "ng-act" / "azure.json").exists()
    assert json.loads((tmp_path / "ng-act.reading.json").read_text())["summary"]["missing_checks"] == [
        "azure"
    ]


def test_a_changed_pdf_or_reader_version_invalidates_the_cache(tmp_path):
    runner, log = make_batch(tmp_path)
    runner.run_act({"id": "ng-act"})
    blank_pdf(tmp_path / "ng-act.pdf", 2)
    assert all(not row["cached"] for row in runner.plan([{"id": "ng-act"}]))


def test_mineru_output_is_kept_and_a_page_without_blocks_is_empty(tmp_path, monkeypatch):
    kit = tmp_path / "mineru-kit"
    kit.write_text("#!/bin/sh\n")
    kit.chmod(0o755)
    reader = readers.MinerUReader(
        kit=kit, models=tmp_path, hashes=tmp_path / "readings" / ".model-hashes.json"
    )
    monkeypatch.setattr(reader, "version", lambda: {"tool": "test"})
    runs = []

    def fake_run(command, **kwargs):
        runs.append(command)
        out = Path(command[command.index("-o") + 1])
        out.write_text(
            json.dumps(
                {
                    "pages": [
                        {"page_idx": 0, "blocks": [{"type": "text", "content": "Hello"}]},
                        {"page_idx": 1},
                    ]
                }
            )
        )
        return type("Done", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(readers.subprocess, "run", fake_run)
    pdf = blank_pdf(tmp_path / "a.pdf", 2)
    first = reader.read(pdf)
    assert [p["text"] for p in first.pages] == ["Hello", ""]
    reader.read(pdf)
    assert len(runs) == 1  # the kept output is reused, not re-read


def test_margin_notes_placed_differently_are_moved_not_invented():
    primary = "Cessation of office 12. A member shall cease to hold office"
    checks = {
        "mineru": "12. A member shall cease to hold office Cessation of office",
        "azure": "12. A member shall cease to hold office Cessation of office",
    }
    flags = compare.compare(primary, checks)
    assert [(f.kind, f.severity) for f in flags] == [("moved", "low")]


def test_a_printed_dash_written_as_hyphens_does_not_hide_the_next_marker():
    primary = {
        "page": 1,
        "nodes": [node("Nothing in this Act shall invalidate —"), node("any law passed by a State")],
    }
    checks = {
        "mineru": {"text": "2. Nothing in this Act shall invalidate --- (a) any law passed by a State"},
        "azure": {"text": "2. Nothing in this Act shall invalidate - (a) any law passed by a State"},
    }
    page = reconcile.reconcile_page(primary, checks)
    assert [n.get("marker") for n in page["nodes"]] == ["2.", "(a)"]
    assert not [f for f in page["flags"] if f["severity"] != "low"]


# --- structure ----------------------------------------------------------------------------------

from ingestion import structure  # noqa: E402


def azure_page(*blocks):
    return {
        "azure": {
            "blocks": [
                {"text": t, "role": None, "box": {"x0": x, "x1": x + 0.2, "y0": 0.1}} for t, x in blocks
            ]
        }
    }


def test_a_margin_note_glued_to_a_section_moves_beside_it_word_for_word():
    nodes = [
        node(
            "Promotion of generation from renewable energy 80. (1) Without prejudice to section 65",
            "paragraph",
        )
    ]
    checks = azure_page(
        ("80. (1) Without prejudice to section 65", 0.05),
        ("Promotion of generation from renewable energy", 0.78),
    )
    out = structure.restructure(nodes, checks)
    assert out[0]["text"] == "80. (1) Without prejudice to section 65"
    assert out[0]["notes"] == ["Promotion of generation from renewable energy"]


def test_a_note_emitted_as_a_heading_attaches_to_the_next_unit():
    nodes = [node("Power to borrow", "heading"), node("The Agency may borrow")]
    out = structure.restructure(nodes, azure_page(("Power to borrow", 0.8)))
    assert [(n["kind"], n["text"], n.get("notes")) for n in out] == [
        ("list_item", "The Agency may borrow", ["Power to borrow"])
    ]


def test_body_text_in_the_main_column_is_never_taken_for_a_note():
    nodes = [node("Power to borrow money is limited", "paragraph")]
    out = structure.restructure(nodes, azure_page(("Power to borrow", 0.1)))
    assert out[0]["text"] == "Power to borrow money is limited" and "notes" not in out[0]


def test_merged_subsections_paragraphs_and_sections_are_split_but_references_are_not():
    text = (
        "(2) Any State shall be a member. (3) The office of the Chairman shall rotate as provided in "
        "section 44 (2)(a) of this Act. 81. (1) The Commission may specify — (a) any matter; (b) any other"
    )
    out = structure.split_units([node(text, "paragraph")])
    assert [n["text"] for n in out] == [
        "(2) Any State shall be a member.",
        "(3) The office of the Chairman shall rotate as provided in section 44 (2)(a) of this Act.",
        "81. (1) The Commission may specify —",
        "(a) any matter;",
        "(b) any other",
    ]
    assert [structure.depth(n) for n in out] == [1, 1, 0, 2, 2]


def test_restructured_text_keeps_every_word():
    from ingestion import layout_parser

    nodes = [node("Promotion of generation 80. (1) Without prejudice. (2) In granting licences", "paragraph")]
    out = structure.restructure(nodes, azure_page(("Promotion of generation", 0.8)))
    before = sorted(
        layout_parser.TOKEN.findall(
            "Promotion of generation 80. (1) Without prejudice. (2) In granting licences"
        )
    )
    assert sorted(layout_parser.TOKEN.findall(layout_parser.page_text(out, 1))) == before


def test_a_note_glued_between_two_merged_sections_goes_to_the_later_one():
    text = "80. (1) The Commission shall promote renewable energy. Directions to generation companies 81. (1) The Commission may specify"
    checks = azure_page(("Directions to generation companies", 0.8))
    out = structure.restructure([node(text, "paragraph")], checks)
    assert [(n["text"][:7], n.get("notes")) for n in out] == [
        ("80. (1)", None),
        ("81. (1)", ["Directions to generation companies"]),
    ]


def test_a_marker_after_the_previous_units_margin_note_is_restored():
    primary = {
        "page": 1,
        "nodes": [
            node("82. (1) There is established the Commission, which —", "paragraph"),
            node("shall be a body corporate"),
        ],
    }
    primary["nodes"][0]["notes"] = ["Establishment of the Commission"]
    checks = {
        "mineru": {
            "text": "82. (1) There is established the Commission, which — (a) shall be a body corporate Establishment of the Commission"
        },
        "azure": {
            "text": "Establishment of the Commission 82. (1) There is established the Commission, which — (a) shall be a body corporate"
        },
    }
    page = reconcile.reconcile_page(primary, checks)
    assert page["nodes"][1]["marker"] == "(a)"


def test_a_note_glued_into_the_next_section_returns_to_the_section_printed_beside_it():
    nodes = [
        node("6. Section 27 (2) of the Principal Act is amended by substituting the words", "paragraph"),
        node(
            "Amendment of section 27 Amendment of section 30 7. Section 30 of the Principal Act is amended —",
            "paragraph",
        ),
    ]
    blocks = [
        ("6. Section 27 (2) of the Principal Act is amended by substituting", 0.05, 0.10),
        ("Amendment of section 27", 0.78, 0.10),
        ("7. Section 30 of the Principal Act is amended —", 0.05, 0.20),
        ("Amendment of section 30", 0.78, 0.205),
    ]
    checks = {
        "azure": {
            "blocks": [
                {"text": t, "role": None, "box": {"x0": x, "x1": x + 0.2, "y0": y}} for t, x, y in blocks
            ]
        }
    }
    out = structure.restructure(nodes, checks)
    assert [(n["text"][:3], n.get("notes")) for n in out] == [
        ("6. ", ["Amendment of section 27"]),
        ("7. ", ["Amendment of section 30"]),
    ]


def test_merged_articles_of_civil_law_and_treaty_drafting_are_split_but_citations_of_them_are_not():
    # As the Layout Parser runs them together in Cameroon's, Côte d'Ivoire's and Angola's laws and the
    # AU's treaties; the separator printed after an article's number is what marks a new one.
    text = (
        "shall respect individual privacy. SECTION 7: Whoever processes personal data shall comply with "
        "Article 5 of the Charter and section 41 of the Act. ARTICLE 4.- Le traitement est licite ; "
        "Art. 5. — Sont exclus : ARTIGO 6.º (Objecto) A presente lei aplica-se. Article 7 : Saisine de la Cour"
    )
    out = structure.split_units([node(text, "paragraph")])
    assert [n["text"][:14] for n in out] == [
        "shall respect ",
        "SECTION 7: Who",
        "ARTICLE 4.- Le",
        "Art. 5. — Sont",
        "ARTIGO 6.º (Ob",
        "Article 7 : Sa",
    ]
    assert " ".join(n["text"] for n in out) == text


def test_a_laws_closing_words_and_the_next_act_in_its_gazette_are_split_from_its_last_article():
    out = structure.split_units(
        [
            node(
                "Art. 54. — La présente loi sera publiée au Journal officiel. Fait à Abidjan, le 19 juin 2013. "
                "Alassane OUATTARA. DECRET n° 2012-525 du 20 juin 2012 portant composition",
                "paragraph",
            ),
            node(
                "A presente lei entra em vigor à data da sua publicação. Vista e aprovada em Luanda.",
                "paragraph",
            ),
        ]
    )
    assert [n["text"][:12] for n in out] == [
        "Art. 54. — L",
        "Fait à Abidj",
        "DECRET n° 20",
        "A presente l",
        "Vista e apro",
    ]


def test_senegals_and_burkina_fasos_article_heads_divisions_and_closing_words_are_split():
    # Senegal's laws as the CDP publishes them: "Article 7." then a capital after a sentence has ended, or
    # alone at the end of a page's last block; "Art. 4. - " with a hyphen; a chapter heading by the dash
    # after its number; the enacting formula after the last article. Burkina Faso's "Ainsi fait …". A
    # sentence citing an article ("l'Article 3 de la loi", "Article 9.2 of …") is never cut.
    text = (
        "ne prend pas part au vote. Article 7. Le Président nomme, comme le prévoit l'Article 3 de la loi. "
        "Article 8. Le mandat est de quatre ans; Article 9.2 of the Convention. Art. 4. - Les personnes "
        "mentionnées en sont informées. Chapitre II. – Recommandé électronique. Art. 54. – Le message "
        "constitue un envoi recommandé. "
        "La présente loi sera exécutée comme loi de l'Etat. Ainsi fait et délibéré en séance publique. "
        "Article 54."
    )
    out = structure.split_units([node(text, "paragraph")])
    assert [n["text"][:14] for n in out] == [
        "ne prend pas p",
        "Article 7. Le ",
        "Article 8. Le ",
        "Art. 4. - Les ",
        "Chapitre II. –",
        "Art. 54. – Le ",
        "La présente lo",
        "Ainsi fait et ",
        "Article 54.",
    ]
    assert " ".join(n["text"] for n in out) == text


def test_cape_verdes_quoted_articles_closing_words_and_republication_are_split():
    # The Boletim Oficial's amending laws (I Série n.º 28, 2021), as the Layout Parser runs them together: the
    # quoted text after "… a seguinte redação:", "Aprovada em …" after the last article, and "REPUBLICAÇÃO"
    # after the signatures, though no punctuation ends them. A quoted article cited in the words ("artigos
    # 2.º, 10.º") and an approval in the words ("aprovada pela Lei nº …") are never cut.
    text = (
        "A presente lei altera os artigos 2.º, 10.º, 12.º, 13.º, 18.º, 27.º e 29.º, que passam a ter a "
        "seguinte redação: «Artigo 2.º [...] A CNPD é uma entidade administrativa independente, "
        "aprovada pela Lei nº 133/V/2001. A presente lei entra em vigor trinta dias após a sua "
        "publicação. Aprovada em 11 de fevereiro de 2021. O Presidente da Assembleia Nacional, Jorge "
        "Pedro Maurício dos Santos REPUBLICAÇÃO Lei nº 133/V/2001, de 22 de janeiro"
    )
    out = structure.split_units([node(text, "paragraph")])
    assert [n["text"][:14] for n in out] == [
        "A presente lei",
        "«Artigo 2.º [.",
        "Aprovada em 11",
        "REPUBLICAÇÃO L",
    ]
    assert " ".join(n["text"] for n in out) == text


def test_a_section_after_a_closing_quotation_mark_is_split():
    out = structure.split_units(
        [
            node(
                '"(1) A service provider shall keep data for two years." 10. Section 41 (1) is amended',
                "paragraph",
            )
        ]
    )
    assert [n["text"][:4] for n in out] == ['"(1)', "10. "]


def test_a_flattened_arrangement_list_splits_where_the_check_reader_starts_paragraphs():
    text = "212. Negligently damaging materials 213. Intentionally disrupting supply PART XXI — MISCELLANEOUS 224. Exemption of lines"
    blocks = [
        ("212. Negligently damaging materials", 0.1),
        ("213. Intentionally disrupting supply", 0.12),
        ("PART XXI — MISCELLANEOUS", 0.14),
        ("224. Exemption of", 0.16),
        ("lines", 0.17),
    ]
    checks = {
        "azure": {
            "blocks": [{"text": t, "role": None, "box": {"x0": 0.1, "x1": 0.6, "y0": y}} for t, y in blocks]
        }
    }
    out = structure.restructure([node(text, "paragraph")], checks)
    assert [(n["kind"], n["text"]) for n in out] == [
        ("paragraph", "212. Negligently damaging materials"),
        ("paragraph", "213. Intentionally disrupting supply"),
        ("heading", "PART XXI — MISCELLANEOUS"),
        ("paragraph", "224. Exemption of lines"),
    ]


def test_auth_failures_are_recognised_and_never_retried():
    from ingestion.layout_parser import is_auth_failure

    assert is_auth_failure(
        RuntimeError("503 Getting metadata from plugin failed with error: Reauthentication is needed.")
    )
    assert not is_auth_failure(RuntimeError("503 Service Unavailable"))


class Unauthorised(FakeReader):
    def read(self, pdf):
        raise RuntimeError("503 Getting metadata from plugin failed with error: Reauthentication is needed.")


def test_a_sign_in_failure_stops_the_run_instead_of_moving_on(tmp_path):
    blank_pdf(tmp_path / "ng-act.pdf", 1)
    factories = {"docai": lambda: Unauthorised("docai", True), "mineru": lambda: FakeReader("mineru", False)}
    outcome = batch.Batch(tmp_path, ["docai", "mineru"], factories).run_act({"id": "ng-act"})
    assert "sign in again" in outcome["stop"] and outcome["readers"]["mineru"]["status"] == "read"


def test_preflight_runs_every_reader_check_before_any_reading(tmp_path):
    class Refusing(FakeReader):
        def preflight(self):
            raise readers.PreflightError("refused")

    runner = batch.Batch(tmp_path, ["docai"], {"docai": lambda: Refusing("docai", True)})
    with pytest.raises(readers.PreflightError):
        runner.preflight(["docai"])


def test_dot_leaders_and_the_naira_sign_read_as_n_are_not_flagged():
    checks = {"mineru": "fine of N50,000.00 on", "azure": "fine of ₦50,000.00 on"}
    assert compare.compare("fine of ₦50,000.00 on", checks) == []
    leaders = compare.compare(
        "Day of .. .. .. .. 2024", {"mineru": "Day of 2024", "azure": "Day of ...... 2024"}
    )
    assert leaders == []
    real = compare.compare(
        "Day of June .. .. 2024", {"mineru": "Day of July 2024", "azure": "Day of July 2024"}
    )
    assert [(f.primary, f.severity) for f in real] == [("June", "high")]  # a date


def test_a_note_goes_to_the_provision_with_the_same_numbers_when_openings_repeat():
    nodes = [
        node(
            "(3) The National Integrated Electricity Policy and Plan adopted under subsections (1) and (2)",
            "paragraph",
        ),
        node(
            "Periodic review of the National Integrated Electricity Policy 4. (1) The National Integrated Electricity Policy and Plan approved",
            "paragraph",
        ),
    ]
    blocks = [
        ("(3) The National Integrated Electricity Policy and Strategic", 0.085, 0.7349),
        ("4. (1) The National Integrated Electricity Policy and Strate", 0.064, 0.7822),
        ("Periodic review of the National Integrated Electricity Policy", 0.782, 0.7901),
    ]
    checks = {
        "azure": {
            "blocks": [
                {"text": t, "role": None, "box": {"x0": x, "x1": x + 0.2, "y0": y}} for t, x, y in blocks
            ]
        }
    }
    out = structure.restructure(nodes, checks)
    owner = next(n for n in out if n.get("notes"))
    assert owner["text"].startswith("4. (1)") and owner["notes"] == [
        "Periodic review of the National Integrated Electricity Policy"
    ]
