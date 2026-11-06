"""Structure rules for the treaties Rwanda keeps (TXP-03, rw-treaty-1): rw-law-1's divisions and articles in
each language, with what a treaty prints and a law does not: its title, preamble, contents, an article's
heading on the line after its number, the witnessing words with the signatures, and the annexes after them.

The excerpts are invented: each keeps the shape of a treaty as the Law Reform Commission's files print one
(its headings, numbering and closing words) with its words replaced, so no official text is reproduced here.
Add only invented text."""

from test_structure_families import resolve, spans_nest
from test_text_publication import build, by_locator, codes, head, para, span

from workbench import publication, structure

AGREEMENT = [
    [
        head("AGREEMENT ON THE CARE OF VASKETS BETWEEN THE REPUBLIC OF RWANDA AND THE REPUBLIC OF QUELBORN"),
        para("Translated into Kinyarwanda and French from English"),
        para("Adopted at Kigali on 4 March 2031"),
    ],
    [
        para("PREAMBLE:"),
        para("The Government of the Republic of Rwanda and the Government of the Republic of Quelborn,"),
        para("CONSIDERING the need to care for vaskets;"),
        para("HAVE AGREED AS FOLLOWS:"),
        head("ARTICLE 1"),
        para("DEFINITIONS"),
        para("In this Agreement, “vasket” means a vasket of any kind."),
        head("ARTICLE 2: SCOPE"),
        para("1. This Agreement applies to every vasket."),
        para("2. It does not apply to plinths."),
    ],
    [
        para("IN WITNESS WHEREOF, the undersigned, duly authorised, have signed this Agreement."),
        para("Done at Kigali on 4 March 2031, in two originals."),
        para("For the Republic of Rwanda For the Republic of Quelborn"),
        head("ANNEX I:"),
        head("LIST OF VASKETS"),
        head("ARTICLE 1"),
        para("The vaskets cared for are those listed below."),
    ],
]  # fmt: skip


def test_a_treatys_title_preamble_articles_signatures_and_annex():
    text, result = build(AGREEMENT, rules=structure.RW_TREATY, language="en")
    nodes = by_locator(result)
    assert result.root.heading.startswith("AGREEMENT ON THE CARE OF VASKETS")
    # The notes after the title are front matter; the preamble runs from its name to the first article.
    assert span(text, result.root).startswith("AGREEMENT ON THE CARE OF VASKETS")
    assert span(text, nodes["preamble"]).startswith("PREAMBLE:")
    assert span(text, nodes["preamble"]).endswith("HAVE AGREED AS FOLLOWS:")
    # An article's heading on its own line after the number, or after it on the same line.
    assert (nodes["art_1"].label, nodes["art_1"].heading) == ("ARTICLE 1", "DEFINITIONS")
    assert nodes["art_2"].heading == "SCOPE"
    # The witnessing words and the place and date open one notice.
    assert span(text, nodes["notice_1"]).startswith("IN WITNESS WHEREOF")
    assert "Done at Kigali" in span(text, nodes["notice_1"])
    # The annex after the signatures, titled on the next line, numbers its articles from 1 again.
    annex = nodes["annex_i"]
    assert (annex.label, annex.heading) == ("ANNEX I", "LIST OF VASKETS")
    assert nodes["annex_i__art_1"].parent is annex
    assert codes(result) == []
    assert resolve(result, "art. 2").locator == "art_2"
    assert resolve(result, "Annex I, art. 1").locator == "annex_i__art_1"
    spans_nest(text, result)


PROTOCOL = [
    [
        head("PROTOCOL ON THE TRADE IN VASKETS"),
        head("TABLE OF CONTENTS"),
        para("PREAMBLE"),
        head("ARTICLE 1 Interpretation"),
        head("ARTICLE 2 Trade in Vaskets"),
    ],
    [
        para("PREAMBLE"),
        para("The Partner States agree to trade in vaskets."),
        head("ARTICLE 1 Interpretation"),
        para("In this Protocol, “trade” means trade in vaskets."),
        head("ARTICLE 2 Trade in Vaskets"),
        para("The Partner States shall trade in vaskets freely."),
    ],
]  # fmt: skip


def test_a_treatys_contents_hold_the_headings_the_body_prints_again():
    text, result = build(PROTOCOL, rules=structure.RW_TREATY, language="en")
    nodes = by_locator(result)
    assert span(text, nodes["contents"]) == (
        "TABLE OF CONTENTS\n\nPREAMBLE\n\nARTICLE 1 Interpretation\n\nARTICLE 2 Trade in Vaskets"
    )
    assert span(text, nodes["preamble"]).startswith("PREAMBLE\n\nThe Partner States agree")
    assert [n.locator for n in result.root.children if n.kind == "article"] == ["art_1", "art_2"]
    assert nodes["art_2"].heading == "Trade in Vaskets"
    assert codes(result) == []  # read by rw-law-1, each article was printed twice: art_1_2, art_2_2
    _, law = build(PROTOCOL, rules=structure.RW_LAW, language="en")
    assert codes(law).count("repeated_locator") == 2


CONVENTION_FR = [
    [
        head("CONVENTION SUR LE SOIN DES VASKETS"),
        para("PRÉAMBULE"),
        para("Les Etats parties à la présente Convention,"),
        para("Sont convenus de ce qui suit :"),
        head("Article premier"),
        head("Objet"),
        para("La présente Convention protège les vaskets."),
        para("EN FOI DE QUOI, les soussignés ont signé la présente Convention."),
        para("Fait à Kigali, le 4 mars 2031."),
    ],
]  # fmt: skip

AGREEMENT_RW = [
    [
        head("AMASEZERANO AREBANA NO KWITA KURI VASKETI"),
        para("IRANGASHINGIRO"),
        para("Ibihugu byombi byemeranyije ibi bikurikira:"),
        head("Ingingo ya mbere"),
        head("Ibisobanuro"),
        para("Muri aya Masezerano, vasketi ni vasketi iyo ari yo yose."),
        head("Ingingo ya 2: Icyo agamije"),
        para("Aya Masezerano agamije kwita kuri vasketi."),
        para("Bikorewe i Kigali ku wa 4 Werurwe 2031."),
    ],
]  # fmt: skip


def test_a_treaty_in_french_and_in_kinyarwanda():
    text, result = build(CONVENTION_FR, rules=structure.RW_TREATY, language="fr")
    nodes = by_locator(result)
    assert span(text, nodes["preamble"]).endswith("Sont convenus de ce qui suit :")
    assert (nodes["art_1"].label, nodes["art_1"].heading) == ("Article premier", "Objet")
    assert span(text, nodes["notice_1"]).startswith("EN FOI DE QUOI") and codes(result) == []
    text, result = build(AGREEMENT_RW, rules=structure.RW_TREATY, language="rw")
    nodes = by_locator(result)
    assert span(text, nodes["preamble"]).startswith("IRANGASHINGIRO")
    assert (nodes["art_1"].heading, nodes["art_2"].heading) == ("Ibisobanuro", "Icyo agamije")
    assert span(text, nodes["notice_1"]).startswith("Bikorewe i Kigali") and codes(result) == []
    spans_nest(text, result)


def test_rwandas_treaties_are_read_by_the_treaty_rules_and_its_laws_as_before():
    for kind in ("agreement", "treaty", "convention", "protocol", "charter"):
        document = {"id": f"rw-{kind}-example-en", "jurisdiction": "rw", "type": kind, "language": "en"}
        assert structure.rules_for(document) == structure.RW_TREATY
    pages = [{"page": n, "units": units} for n, units in enumerate(AGREEMENT, start=1)]
    built = publication.build(
        {"id": "rw-agreement-example-en", "jurisdiction": "rw", "type": "agreement"}, pages
    )
    assert built.rules_id == "rw-treaty-1" and not built.errors
    assert structure.rules_for({"id": "rw-law-2024-1-en", "jurisdiction": "rw", "type": "law"}) == "rw-law-1"
