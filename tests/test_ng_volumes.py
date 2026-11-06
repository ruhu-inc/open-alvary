"""Bound volumes cut into their instruments (ingestion.ng_volumes, ingestion.ng_documents.VOLUMES).

The readings are synthetic, in the Layout Parser's page shape and modelled on the six bound volumes of the
National Library's repository read on 2026-11-05: a Region's year of Laws with its subsidiary legislation,
a year's Acts with the Bills printed beside them, a Governor's proclamations. No Nigerian text is quoted.
"""

import json

from ingestion import ng_documents, ng_volumes, nicn
from tests.test_ng_documents import MemoryStore, node, page

CERTIFIED = (
    "This printed impression has been carefully compared by me with the Bill that has passed the House."
)


def law(number, title, pages, first_printed, heads="W.R. No. {number} of 1950 {short}"):
    """A Region's Law as a volume prints it: its assent and number, its long title and enacting words, its
    short title cited in section 1, running heads over its later pages, the clerk's certificate last."""
    short = title.removesuffix(" Law, 1950")
    out = []
    for k in range(pages):
        printed = f"A {first_printed + k}"
        if k == 0:
            nodes = [
                node(
                    0,
                    f"Assented to in Her Majesty's name this 1st day of May, 1950. A. GOVERNOR (L.S.) No. {number} 1950",
                ),
                node(0, "Example Region of Nigeria", "heading"),
                node(
                    0,
                    f"A LAW TO MAKE PROVISION FOR {short.upper()}. BE IT ENACTED by the Legislature as follows:",
                ),
                node(0, f"1. This Law may be cited as the {title}."),
            ]
            furniture = [printed]
        else:
            nodes = [
                node(0, f"{k + 1}. The {short.lower()} shall be kept as the Minister directs, and so on.")
            ]
            furniture = [f"{printed} " + heads.format(number=number, short=short)]
        if k == pages - 1:
            nodes.append(node(0, CERTIFIED))
        out.append((nodes, furniture))
    return out


def notice(number, title, printed, ending=True):
    """A notice: its number, the law it is made under, its title, its enacting words and its citation."""
    nodes = [
        node(0, f"W.R.L.N. {number} of 1950", "heading"),
        node(0, "The Example Ferries Law, 1950 (No. 1 of 1950)", "heading"),
        node(0, title.upper(), "heading"),
        node(
            0,
            f"In exercise of the powers conferred by section 2 of the Law, the Minister makes this {title.split()[-2]}:",
        ),
        node(0, f"1. This Order may be cited as the {title}."),
    ]
    if ending:
        nodes.append(node(0, "MADE at Example this 2nd day of June, 1950. A. MINISTER"))
    return nodes, [f"B {printed}"]


def volume(spec):
    """A volume's Layout Parser reading from (nodes, furniture) per page, numbered from 1."""
    pages = []
    for number, (nodes, furniture) in enumerate(spec, 1):
        nodes = [{**n, "page": number} for n in nodes]
        pages.append(page(number, nodes, furniture))
    return {"sha256": "v" * 64, "reader": "docai", "version": {}, "pages": pages}


TITLE_PAGES = [
    ([node(0, "LAWS OF THE EXAMPLE REGION 1950", "heading")], []),
    ([], []),
]
BILL = [
    (
        [
            node(0, "THE EXAMPLE BOATS BILL", "heading"),
            node(0, "A BILL FOR AN ACT TO PROVIDE FOR BOATS. BE IT ENACTED by the Legislature as follows:"),
        ],
        ["C 1"],
    )
]


def cut(spec, ocr=None):
    return ng_volumes.cut(ng_documents.cloud_reading(volume(spec), ocr))


def test_a_volume_is_cut_into_its_instruments_and_its_other_pages_are_left_out():
    spec = [
        *TITLE_PAGES,
        *law(1, "Example Ferries Law, 1950", 2, 1),
        *law(2, "Example Markets Law, 1950", 3, 3),
        *BILL,
        notice(1, "Example Ferries (Fares) Order, 1950", 1),
    ]
    found = cut(spec)
    assert [
        (e["pages"], e["series"], e["number"], e["year"], e["kind"], e["title"]) for e in found["instruments"]
    ] == [
        ([3, 4], "no", "1", "1950", "law", "Example Ferries Law, 1950"),
        ([5, 6, 7], "no", "2", "1950", "law", "Example Markets Law, 1950"),
        ([9], "ln", "1", "1950", "order", "Example Ferries (Fares) Order, 1950"),
    ]
    assert found["held"] == []
    assert found["left"] == [
        {"pages": [1, 2], "reason": "title pages, tables and indexes"},
        {"pages": [8, 8], "reason": "a Bill, not a law"},
    ]


def test_what_cannot_be_cut_with_confidence_is_held_with_its_reason():
    markets = law(2, "Example Markets Law, 1950", 3, 3)
    markets[2] = (markets[2][0], ["A 6 W.R. No. 2 of 1950 Example Markets"])  # A 5 is not in the scan
    first, second = (
        notice(2, "Example Ferries (Night) Order, 1950", 2),
        notice(3, "Example Ferries (Day) Order, 1950", 2),
    )
    spec = [
        *TITLE_PAGES,
        *law(1, "Example Ferries Law, 1950", 2, 1),
        *markets,
        (first[0] + second[0], first[1]),  # two notices on one page
        notice(4, "Example Ferries (Tolls) Order, 1950", 3),
    ]
    words = " ".join(f"word{n}" for n in range(60))
    ocr = {
        "version": {},
        "pages": [
            {"page": n + 1, "text": words if n + 1 == 9 else "\n".join(x.get("text", "") for x in nodes)}
            for n, (nodes, _) in enumerate(spec)
        ],
    }
    found = cut(spec, ocr)
    assert [e["number"] for e in found["instruments"]] == ["1"]
    reasons = {(e["series"], e["number"]): e["reason"] for e in found["held"]}
    assert reasons[("no", "2")] == "printed page A 5 missing from the scan"
    assert reasons[("ln", "2")] == "it shares page 8 with the instrument after it"
    assert reasons[("ln", "3")] == "it shares page 8 with the instrument before it"
    assert reasons[("ln", "4")] == "the Layout Parser's reading of page 9 is not the OCR's page"


def test_an_end_that_may_be_missing_and_a_page_naming_another_instrument_are_held():
    ferries = law(1, "Example Ferries Law, 1950", 2, 1)
    ferries[1] = (ferries[1][0][:-1], ferries[1][1])  # no certificate, and the next Law opens three pages on
    markets = law(2, "Example Markets Law, 1950", 2, 5)
    # A page of Law No. 3, whose opening page is not in the scan, after Law No. 2.
    stray = ([node(0, "4. The boats shall be kept, and so on.")], ["A 8 W.R. No. 3 of 1950 Example Boats"])
    found = cut([*TITLE_PAGES, *ferries, *markets, stray])
    held = {e["number"]: e["reason"] for e in found["held"]}
    assert held["1"] == "its last page prints no ending and the next page of its series does not open another"
    assert held["3"].startswith("its running heads name No. 3 of 1950, whose opening was not found")
    assert [(e["number"], e["pages"]) for e in found["instruments"]] == [("2", [5, 6])]


def test_a_number_printed_twice_is_one_instrument_and_an_arrangement_goes_with_its_act():
    # A Law's first page scanned twice (the earlier copy left out), and an Act's title and arrangement of
    # sections printed before the page that prints its number and enacting words.
    ferries = law(1, "Example Ferries Law, 1950", 2, 1)
    act = [
        (
            [
                node(0, "EXAMPLE HARBOURS ACT, 1950", "heading"),
                node(0, "UNITY AND FAITH"),
                node(0, "ARRANGEMENT OF SECTIONS", "heading"),
                node(0, "1. Harbours."),
            ],
            ["A 3"],
        ),
        (
            [
                node(0, "1950, No. 2", "heading"),
                node(
                    0, "AN ACT TO MAKE PROVISION FOR HARBOURS. BE IT ENACTED by the Legislature as follows:"
                ),
                node(0, "1. This Act may be cited as the Example Harbours Act, 1950."),
            ],
            ["A 4"],
        ),
        ([node(0, "2. Harbours shall be kept."), node(0, CERTIFIED)], ["A 5 1950, No. 2 Example Harbours"]),
    ]
    found = cut([*TITLE_PAGES, ferries[0], *ferries, *act])
    assert [(e["number"], e["pages"], e["kind"], e["title"]) for e in found["instruments"]] == [
        ("1", [4, 5], "law", "Example Ferries Law, 1950"),
        ("2", [6, 7, 8], "act", "Example Harbours Act, 1950"),
    ]
    assert {"pages": [3, 3], "reason": "No. 1 printed again"} in found["left"]


def test_the_volumes_own_tables_check_its_cuts():
    contents = (
        [
            {
                "kind": "table",
                "page": 1,
                "caption": "",
                "header_rows": [[{"text": "Title"}, {"text": "Page"}]],
                "body_rows": [
                    [{"text": "Example Ferries Law, 1950 ... ..."}, {"text": "A 1"}],
                    [{"text": "Example Wharves Law, 1950 ... ..."}, {"text": "A 3"}],
                ],
            }
        ],
        [],
    )
    found = cut(
        [
            contents,
            ([], []),
            *law(1, "Example Ferries Law, 1950", 2, 1),
            *law(2, "Example Markets Law, 1950", 2, 3),
        ]
    )
    assert [e["number"] for e in found["instruments"]] == ["1"]
    assert found["instruments"][0]["listed_in_volume"] == "Example Ferries Law, 1950"
    assert found["held"][0]["reason"] == (
        "the volume's table lists another title at its first page (Example Wharves Law, 1950)"
    )


def test_a_volume_is_prepared_as_its_instruments_its_held_ones_and_left_pages_named(monkeypatch):
    sha = "v" * 64
    store = MemoryStore()
    markets = law(2, "Example Markets Law, 1950", 3, 3)
    markets[2] = (markets[2][0], ["A 6 W.R. No. 2 of 1950 Example Markets"])
    spec = [
        *TITLE_PAGES,
        *law(1, "Example Ferries Law, 1950", 2, 1),
        *markets,
        notice(1, "Example Ferries (Fares) Order, 1950", 1),
    ]
    store.objects[f"readings/ng/docai/{sha}.json"] = json.dumps(volume(spec)).encode()
    monkeypatch.setitem(ng_documents.VOLUMES, sha, {"what": "a bound volume of many laws", "issuer": "xr"})
    items = {
        "nln-repository:v": {
            "source": "nln-repository",
            "type": "act",
            "sha256": sha,
            "url": "https://x/v.pdf",
            "stored": f"gs://b/sources/ng/nln-repository/{sha}.pdf",
            "listed": {"title": "Laws of the Example Region 1950"},
        }
    }
    collection = nicn.BucketCollection(store, pipeline=ng_documents.PIPELINE)
    counts = ng_documents.prepare_pdfs(store, items, list(items), collection)
    collection.save_manifest()
    manifest = json.loads(store.objects["collections/ng/manifest.json"])
    assert (counts["prepared"], counts["documents"], counts["held"]) == (1, 2, 1)
    documents = {d["id"]: d for d in manifest["documents"]}
    assert sorted(documents) == ["ng-xr-law-1-1950", "ng-xr-ln-1-1950"]
    ferries = documents["ng-xr-law-1-1950"]
    assert (ferries["type"], ferries["title"], ferries["pages"], ferries["page_count"]) == (
        "law",
        "Example Ferries Law, 1950",
        [3, 4],
        2,
    )
    assert (
        ferries["printed_pages"] == "A 1–A 2"
        and ferries["cut_from_volume"]["title"] == "Laws of the Example Region 1950"
    )
    reading = json.loads(store.objects[f"collections/ng/files/{sha}/ng-xr-law-1-1950.reading.json"])
    assert [p["page"] for p in reading["pages"]] == [3, 4]
    (held,) = manifest["held"]
    assert (held["id"], held["type"], held["pages"], held["reason"]) == (
        "ng-xr-law-2-1950",
        "law",
        [5, 7],
        "printed page A 5 missing from the scan",
    )
    index = json.loads(store.objects[f"collections/ng/issues/{sha}.json"])
    assert index["left"] == [{"pages": [1, 2], "reason": "title pages, tables and indexes"}]


def test_a_volume_held_whole_says_why(monkeypatch):
    sha = "w" * 64
    store = MemoryStore()
    store.objects[f"readings/ng/docai/{sha}.json"] = json.dumps(
        volume(law(1, "Example Ferries Law, 1950", 2, 1))
    ).encode()
    monkeypatch.setitem(
        ng_documents.VOLUMES, sha, {"what": "a compendium", "held": "a compendium, held on the owner's word"}
    )
    items = {
        "nln-repository:w": {
            "source": "nln-repository",
            "type": "act",
            "sha256": sha,
            "url": "https://x/w.pdf",
            "stored": f"gs://b/sources/ng/nln-repository/{sha}.pdf",
            "listed": {"title": "Example declarations"},
        }
    }
    collection = nicn.BucketCollection(store, pipeline=ng_documents.PIPELINE)
    assert ng_documents.prepare_pdfs(store, items, list(items), collection)["held"] == 1
    collection.save_manifest()
    manifest = json.loads(store.objects["collections/ng/manifest.json"])
    assert (
        manifest["documents"] == []
        and manifest["held"][0]["reason"] == "a compendium, held on the owner's word"
    )
