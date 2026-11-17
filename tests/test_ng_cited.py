"""What a Nigerian instrument does to the earlier ones it names, and how those are found in the collection
(ingestion.ng_cited).

The readings here are synthetic, in the shape the cloud readings are prepared in (ingestion.ng_documents),
their provisions worded as the staged collection's Acts and Statutory Instruments word theirs. No
Nigerian text is quoted: the instruments named are invented.
"""

from test_ng_documents import node, page, table

from ingestion import linked, ng_cited


def reading(*pages):
    return {"pages": list(pages)}


def act(doc_id, title, kind="act", **more):
    return {"id": doc_id, "type": kind, "title": title, "jurisdiction": "ng", "language": "en", **more}


def cites(doc_id, title, *nodes, kind="act", **more):
    """The citations read from a document of one page (page 7) holding these paragraphs."""
    return ng_cited.cited(
        reading(page(7, [node(7, text) for text in nodes])), act(doc_id, title, kind, **more)
    )


def test_a_provision_that_repeals_an_act_is_read_with_its_words_and_their_page():
    found = cites(
        "ng-act-9-2011",
        "Example Markets (Prohibition) Act, 2011",
        # As the Layout Parser reads a Gazette page: the section, then its marginal note.
        "23. The Authority may make rules. Repeal of Act No. 4, 2004 24.—(1) The Example Markets (Prohibition) "
        "Act, No. 4, 2004 is repealed. Savings.",
        "(2) The repeal of the enactment specified in subsection (1) shall not affect anything done under it.",
    )
    assert found == [
        {
            "kind": "repeals",
            "target": "Example Markets (Prohibition) Act, No. 4, 2004",
            "title": "Example Markets (Prohibition) Act",
            "year": 2004,
            "number": "4",
            "words": "24.—(1) The Example Markets (Prohibition) Act, No. 4, 2004 is repealed.",
            "page": 7,
        }
    ]


def test_a_part_a_limit_and_a_case_repeal_nothing():
    found = cites(
        "ng-act-9-2011",
        "Example Markets (Prohibition) Act, 2011",
        "22. Section 13 of the Sample Agency Act, 1989 is repealed. Repeal of section 13.",
        "5. The Waterworks (Sample Assessment) Order, 1952, is hereby revoked, in so far as it applies to Sample Town.",
        "(a) where the Sample Levy Act, 2001 is repealed or amended in a manner that affects the ruling ; or",
        "The First Schedule to the Sample Fund Act, 2010 is amended by substituting new sums.",
        "(4) Whenever the authorisation of a scheme under this Act is revoked, the Commission shall appoint a trustee.",
    )
    assert [(c["kind"], c["target"], c.get("note")) for c in found] == [
        ("amends", "Sample Agency Act, 1989", "Section 13"),
        ("amends", "Waterworks (Sample Assessment) Order, 1952", "In so far as it applies to Sample Town"),
        ("amends", "Sample Fund Act, 2010", "First Schedule"),
    ]
    assert found[0]["words"] == "22. Section 13 of the Sample Agency Act, 1989 is repealed."


def test_several_instruments_one_named_only_as_what_changed_another_and_the_principal_act():
    found = cites(
        "ng-act-9-2011",
        "Example Markets (Amendment) Act, 2011",
        "18. The Sample Tax Act, CAP E4, Laws of the Federation of Nigeria, 2004 and the Sample Tax Fund "
        "(Amendment) Act No. 17, 2003 are repealed. Repeal.",
        "2. Regulation 2 of the Groundnuts (Sample) Regulations 1951 (hereinafter referred to as the principal "
        "regulations) as amended by the Groundnuts (Sample) (Amendment) Regulations, 1956, is amended by the "
        "deletion of a word.",
        '1. The Example Markets Act, No. 6, 2005 (in this Act referred to as "the Principal Act") is amended as '
        "set out in this Act.",
        "2. Section 2 of the Principal Act is amended by inserting a new paragraph.",
        "3. Section 9 of the Principal Act is repealed.",
    )
    assert [(c["kind"], c["target"], c.get("year"), c.get("number"), c.get("note")) for c in found] == [
        # A chapter of a revised edition gives the edition's year, not the Act's: it has none.
        ("repeals", "Sample Tax Act, CAP E4, Laws of the Federation of Nigeria, 2004", None, None, None),
        ("repeals", "Sample Tax Fund (Amendment) Act No. 17, 2003", 2003, "17", None),
        ("amends", "Groundnuts (Sample) Regulations 1951", 1951, None, "Regulation 2"),  # not the 1956 ones
        ("amends", "Example Markets Act, No. 6, 2005", 2005, "6", "Sections 2 and 9"),
    ]


def test_the_long_title_the_memorandum_a_list_and_a_schedule_say_what_is_repealed():
    document = act("ng-act-1-2026", "Example Polls Act, 2026")
    read = reading(
        page(
            3,
            [
                node(
                    3,
                    "An Act to repeal the Example Polls Act, No. 13, 2022 and enacts the Example Polls Act, 2026 "
                    "to regulate the conduct of polls; and for related matters.",
                )
            ],
        ),
        page(
            9,
            [
                node(9, "165. The following enactments are repealed :"),
                node(9, "(a) The Example Polls Act 2002 ;", "list_item"),
                node(9, "(b) Sample Polls Commission (Establishment) Act No. 17 of 1998 ; and", "list_item"),
                node(
                    9,
                    "(c) the Sample Offences Decree No. 21 of 1995 and all subsequent amendments thereto.",
                    "list_item",
                ),
                node(9, "166. This Act may be cited as the Example Polls Act, 2026."),
            ],
        ),
        page(
            12,
            [
                table(
                    12,
                    [
                        ["Number", "Short title", "Extent of repeal"],
                        ["No. 13 of 1907", "The Sample Minerals (Amendment) Proclamation 1907", "All."],
                        ["No. 55 of 1958", "The Sample Excise Act, 1958", "Part V except sections 95 to 100"],
                    ],
                )
            ],
        ),
        page(
            13,
            [
                node(
                    13,
                    "This Act repeals certain tax Acts and enacts the Example Polls Act to provide for polls.",
                )
            ],
        ),
    )
    assert ng_cited.cited_as(read, document) == "Example Polls Act, 2026"
    found = {c["target"]: c for c in ng_cited.cited(read, document)}
    assert {target: (c["kind"], c.get("year"), c["page"]) for target, c in found.items()} == {
        "Example Polls Act 2002": ("repeals", 2002, 9),
        "Sample Polls Commission (Establishment) Act No. 17 of 1998": ("repeals", 1998, 9),
        "Sample Offences Decree No. 21 of 1995": ("repeals", 1995, 9),
        "Example Polls Act, No. 13, 2022": ("repeals", 2022, 3),
        "Sample Minerals (Amendment) Proclamation 1907": ("repeals", 1907, 12),
        "Sample Excise Act, 1958": ("amends", 1958, 12),  # a part only
    }  # "certain tax Acts and enacts the …" names no instrument
    assert found["Example Polls Act 2002"]["words"] == (
        "165. The following enactments are repealed : (a) The Example Polls Act 2002 ;"
    )
    assert found["Example Polls Act, No. 13, 2022"]["words"].startswith(
        "An Act to repeal the Example Polls Act"
    )
    assert found["Sample Minerals (Amendment) Proclamation 1907"]["words"] == (
        "Number | Short title | Extent of repeal — No. 13 of 1907 | "
        "The Sample Minerals (Amendment) Proclamation 1907 | All."
    )


def test_a_title_with_etc_and_a_name_whose_bracket_was_never_closed_are_still_read():
    found = cites(
        "ng-act-x",
        "Sample Crimes (Prohibition, Prevention, etc.) (Amendment) Act, 2024",
        "An Act to amend Sample Crimes (Prohibition, Prevention, etc.) Act, No. 17, 2015 to insert words; and "
        "for related matters.",
        # As printed: the bracket is opened and never closed.
        '1. The Sample Crimes (Prohibition, Prevention, etc.) Act, No. 17, 2015 (in this Act referred to as "the '
        'Principal Act" is amended as set out in this Act.',
        "2. Section 17 of the Principal Act is amended —",
    )
    assert [(c["kind"], c["target"], c["year"], c["number"], c["note"]) for c in found] == [
        (
            "amends",
            "Sample Crimes (Prohibition, Prevention, etc.) Act, No. 17, 2015",
            2015,
            "17",
            "Section 17",
        )
    ]
    assert found[0]["words"].startswith("1. The Sample Crimes")  # the provision before the long title


def test_subsidiary_legislation_names_the_act_it_is_made_under():
    (found,) = cites(
        "ng-si-23-2024",
        "Sample Approval Regulations, 2024",
        "In exercise of the powers conferred on it by sections 70 and 134 of the Example Communications Act, "
        "No. 19, 2003 and of all other powers enabling it in that behalf, the Commission makes the following "
        "Regulations—",
        kind="regulation",
    )
    assert found == {
        "kind": "made_under",
        "target": "Example Communications Act, No. 19, 2003",
        "title": "Example Communications Act",
        "year": 2003,
        "number": "19",
        "note": "Sections 70 and 134",
        "words": "In exercise of the powers conferred on it by sections 70 and 134 of the Example Communications "
        "Act, No. 19, 2003",
        "page": 7,
    }


def test_a_draft_or_a_bill_repeals_nothing_and_answers_to_no_name():
    repealing = "24. The Example Markets Act, 2004 is repealed."
    assert (
        cites("ng-regulation-draft", "Draft Example Markets Regulations 2025", repealing, kind="regulation")
        == []
    )
    assert cites("ng-act-bill", "EXAMPLE MARKETS (AMENDMENT) BILL, 2011", repealing) == []
    assert (
        ng_cited.identity(
            act("ng-act-bill", "EXAMPLE MARKETS BILL, 2002", listed_title="Example Markets Act, 2002")
        )
        is None
    )


def test_the_check_reading_must_read_the_citation_as_the_primary_does():
    def checked(check):
        held = page(7, [node(7, "19.—(1) The Sample Devices (Advertisement) Regulations, 1996 is revoked.")])
        return ng_cited.cited(
            reading({**held, "checks": {"docai-ocr": check}}),
            act("ng-si-62-2021", "Sample Products Advertisement Regulations, 2021", "regulation"),
        )

    # The check reads a line across the page, its marginal note with it: the words still stand in order.
    assert checked("19.-(1) The Sample Devices (Advertisement) Revocation.\nRegulations, 1996 is revoked.")
    assert (
        checked("19.-(1) The Sample Devices (Advertisement) Regulations, 1998 is revoked.") == []
    )  # another year
    assert checked("")  # no check reading of the page: its document is blocked from approval until it has one


def test_a_document_answers_to_the_names_its_text_and_title_bear_where_they_agree_on_its_year():
    read = reading(
        page(2, [node(2, "31. This Act may be cited as the Example Road Safety (Establishment) Act, 2007.")])
    )
    document = act("ng-act-x", "EXAMPLE ROAD SAFETY COMMISSION ACT, 2007")
    assert ng_cited.cited_as(read, document) == "Example Road Safety (Establishment) Act, 2007"
    found = ng_cited.identity({**document, "cited_as": ng_cited.cited_as(read, document)})
    assert found == {
        "keys": {"exampleroadsafetyestablishmentact", "exampleroadsafetycommissionact"},
        "year": 2007,
        "number": None,
        "issuer": None,
    }
    # Its Gazette's front page and its own citation give different years: found by neither.
    assert (
        ng_cited.identity(
            act(
                "ng-si-14-2019",
                "Sample Interception Regulations, 2019",
                cited_as="Sample Interception Regulations, 2018",
            )
        )
        is None
    )
    # A heading that is no name in full leaves its listing's; a number from its Gazette's front page.
    listed = act("ng-act-6-2005", "ARRANGEMENT OF SECTIONS", listed_title="Example Shipping Act, 2005")
    assert ng_cited.identity(listed) == {
        "keys": {"exampleshippingact"},
        "year": 2005,
        "number": "6",
        "issuer": None,
    }
    assert (
        ng_cited.identity(act("ng-wr-law-12-1957", "Local Sample Law, 1957", "law", number="12"))["issuer"]
        == "wr"
    )
    # An amending Act that quotes the principal Act's citation: the name its own title bears out.
    quoting = reading(
        page(
            3,
            [
                node(3, '"30. This Act may be cited as the Example Markets Act, 2004".'),
                node(3, "4. This Act may be cited as the Example Markets (Amendment) Act, 2011."),
            ],
        )
    )
    amending = act("ng-act-y", "EXAMPLE MARKETS (AMENDMENT) ACT, 2011")
    assert ng_cited.cited_as(quoting, amending) == "Example Markets (Amendment) Act, 2011"


def repeal(target, title, year, number=None, **more):
    return {"kind": "repeals", "target": target, "title": title, "year": year, "number": number, **more}


def test_a_law_is_repealed_where_a_later_text_says_so_and_never_in_force():
    provision = {"words": "24. (1) The Example Markets (Prohibition) Act 2004 is repealed.", "page": 16}
    documents = [
        act("ng-act-example-markets-prohibition-act-2003", "EXAMPLE MARKETS (PROHIBITION) ACT, 2003"),
        # One Act held in two copies, a Gazette's and an agency's.
        act("ng-act-4-2004", "Example Markets (Prohibition) Act, 2004"),
        act(
            "ng-act-the-example-markets",
            "EXAMPLE MARKETS",
            cited_as="Example Markets (Prohibition) Act, 2004",
        ),
        act(
            "ng-act-9-2011",
            "Example Markets (Prohibition) Act, 2011",
            cited_as="Example Markets (Prohibition) Act, 2011",
            cites=[
                repeal("Example Markets (Prohibition) Act 2004", "Example Markets (Prohibition) Act", 2004)
                | provision,
                repeal("Sample Agency Act, Cap. N30 LFN 2004", "Sample Agency Act", None),
            ],
        ),
        act(
            "ng-act-example-markets-amendment-act-2012",
            "EXAMPLE MARKETS (PROHIBITION) (AMENDMENT) ACT, 2012",
            cites=[
                {
                    "kind": "amends",
                    "target": "Example Markets (Prohibition) Act, No. 9 2011",
                    "title": "Example Markets (Prohibition) Act",
                    "year": 2011,
                    "number": "9",
                }
            ],
        ),  # fmt: skip
        act(
            "ng-si-3-2013",
            "Example Markets Regulations, 2013",
            "regulation",
            cites=[
                {
                    "kind": "made_under",
                    "target": "Example Markets (Prohibition) Act, 2004",
                    "title": "Example Markets (Prohibition) Act",
                    "year": 2004,
                    "note": "Section 20",
                }
            ],
        ),  # fmt: skip
        {"id": "ng-nicn-la-1-2020-2026-01-08", "type": "judgment", "title": "A v. B", "language": "en"},
    ]
    out = {d["id"]: d for d in ng_cited.link_works(documents)}
    later = {"work": "ng-act-9-2011", "title": "Example Markets (Prohibition) Act, 2011"}
    for copy in ("ng-act-4-2004", "ng-act-the-example-markets"):
        assert out[copy]["currency"] == {
            "status": "repealed",
            "basis": "text",
            "repealed_by": [later | provision],
        }
        assert out[copy]["relations"] == [
            {"kind": "repealed_by", "target": later["title"], "target_id": "ng-act-9-2011"}
        ]
        assert out[copy]["enables"] == ["ng-si-3-2013"]
    # The Act of the same title and another year is not the one repealed.
    assert out["ng-act-example-markets-prohibition-act-2003"]["currency"] == {"status": "unknown"}
    assert out["ng-act-9-2011"]["relations"] == [
        {"kind": "repeals", "target": "Example Markets (Prohibition) Act 2004", "target_id": "ng-act-4-2004"},
        {"kind": "repeals", "target": "Sample Agency Act, Cap. N30 LFN 2004"},  # as printed, without a target
        {
            "kind": "amended_by",
            "target": "Example Markets (Prohibition) (Amendment) Act, 2012",  # as laws are cited
            "target_id": "ng-act-example-markets-amendment-act-2012",
        },
    ]
    assert out["ng-act-9-2011"]["currency"] == {
        "status": "unknown",
        "amended_by": [
            {"work": "ng-act-example-markets-amendment-act-2012",
             "title": "Example Markets (Prohibition) (Amendment) Act, 2012"}
        ],
    }  # fmt: skip
    assert out["ng-si-3-2013"]["currency"]["parents_repealed"] == [
        {"work": "ng-act-4-2004", "title": "Example Markets (Prohibition) Act, 2004", "repealed_by": [later]}
    ]
    # Nigeria has no official listing of the laws in force: nothing is in force by this step.
    assert {d["currency"]["status"] for d in out.values() if "currency" in d} == {"repealed", "unknown"}
    assert not {d["currency"].get("basis") for d in out.values() if "currency" in d} - {"text", None}
    judgment = out["ng-nicn-la-1-2020-2026-01-08"]
    assert "currency" not in judgment and "relations" not in judgment  # a court's decision has none


def test_a_citation_in_doubt_stays_as_printed_without_a_target():
    def linked_to(source, cite, *held):
        out = ng_cited.link_works([*held, {**source, "cites": [cite]}])
        return out[-1]["relations"][0].get("target_id")

    later = act("ng-act-9-2011", "Example Markets Act, 2011")
    earlier = act("ng-act-4-2004", "Example Markets Act, 2004")
    cite = repeal("Example Markets Act, No. 4, 2004", "Example Markets Act", 2004, "4")
    assert linked_to(later, cite, earlier) == "ng-act-4-2004"
    # Both print a number and the numbers differ: perhaps another instrument of that name and year.
    assert linked_to(later, cite | {"number": "13"}, earlier) is None
    # Its title held under another year.
    assert linked_to(later, cite | {"year": 2002}, earlier) is None
    # An Act's name at the head of a regulation made under it.
    assert linked_to(later, cite, {**earlier, "type": "regulation"}) is None
    # A Region's law is not the Federation's.
    assert linked_to(later, cite, {**earlier, "id": "ng-wr-act-4-2004"}) is None
    # The text citing it must be the later one, and its own year known.
    assert linked_to(act("ng-act-1-1999", "Sample Act, 1999"), cite, earlier) is None
    assert linked_to(act("ng-act-untitled", "ARRANGEMENT OF SECTIONS"), cite, earlier) is None
    # A Bill, though its listing calls it an Act.
    assert (
        linked_to(
            later,
            cite,
            act("ng-act-b", "EXAMPLE MARKETS BILL, 2004", listed_title="Example Markets Act, 2004"),
        )
        is None
    )
    # Itself: no link at all.
    assert "relations" not in ng_cited.link_works([{**earlier, "cites": [cite]}])[0]


def test_rwanda_and_nigeria_share_what_follows_from_the_links():
    documents = [
        {"id": "a", "title": "A", "listed": "in_force"},
        {"id": "b", "title": "B", "cites": [{"kind": "repeals", "target": "A as printed", "to": "a"}] * 2},
    ]
    find = lambda whole: lambda cite, source: [cite["to"]]  # noqa: E731
    out = linked.link(documents, find, listing="The listing", once=True)
    assert out[0]["currency"] == {
        "status": "repealed",
        "basis": "text",
        "repealed_by": [{"work": "b", "title": "B"}],  # said twice by one text: one link, where asked
        "disagrees": "The listing files it among the laws in force",
    }
    # Without a listing what an entry carries as "listed" is not read: nothing is in force, nor disagrees.
    assert linked.link(documents, find)[0]["currency"]["repealed_by"] == [{"work": "b", "title": "B"}] * 2
    assert "disagrees" not in linked.link(documents, find)[0]["currency"]
    assert linked.link(documents[:1], find)[0]["currency"] == {"status": "unknown"}


# --- the review of 2026-11-17: a missed link is acceptable, a false one is not -------------------------


def read_as(*sentences, kind="act", title="Example Markets (Prohibition) Act, 2011"):
    """What each sentence, alone in a document, is read as: (kind, citation as printed, note)."""
    found = cites("ng-act-9-2011", title, *sentences, kind=kind)
    return [(c["kind"], c["target"], c.get("note")) for c in found]


def test_an_instrument_named_in_a_lead_in_is_not_the_thing_repealed():
    # What it was made under is not what is revoked.
    assert read_as(
        "The Sample Tariff Order, 1965 made under the Sample Tariff Act, 1965 is hereby revoked."
    ) == [("repeals", "Sample Tariff Order, 1965", None)]
    assert read_as(
        "The Sample Supply Regulations 1995 issued under the Sample Power Act CAP 106 LFN 1990 are hereby repealed."
    ) == [("repeals", "Sample Supply Regulations 1995", None)]
    # A thing held under an instrument is not the instrument.
    assert read_as("Any licence granted under the Sample Regulations, 2005 is revoked.") == []
    # An instrument a lead-in names is not acted on; the one after the lead-in is.
    repealed = [("repeals", "Example Markets Act, 2004", None)]
    assert (
        read_as(
            "Notwithstanding the provisions of the Sample Levy Act, 2001, the Example Markets Act, 2004 is repealed."
        )
        == repealed
    )
    assert (
        read_as(
            "Subject to section 6 of the Interpretation Act, 1964, the Example Markets Act, 2004 is repealed."
        )
        == repealed
    )
    assert read_as(
        "Pursuant to section 10 of the Sample Act, 2004, the Sample Regulations, 2005 are revoked."
    ) == [("repeals", "Sample Regulations, 2005", None)]
    # A lead-in that names no instrument leaves the whole list after it.
    assert read_as(
        "From the commencement of this Act, the Example Markets Act, 2004 and the Sample Levy Act, 2001 are repealed."
    ) == [*repealed, ("repeals", "Sample Levy Act, 2001", None)]
    assert read_as(
        "Except as otherwise provided in this Act, the Example Markets Act, No. 6, 2004 is repealed."
    ) == [("repeals", "Example Markets Act, No. 6, 2004", None)]


def test_a_partial_a_limited_a_quoted_or_a_negated_repeal_is_no_repeal():
    # A bracket that limits it, and words after it that do: an amendment, with the limit.
    assert read_as("The Example Markets Act, 2004 (other than sections 4 and 5) is repealed.") == [
        ("amends", "Example Markets Act, 2004", "Other than sections 4 and 5")
    ]
    assert read_as("The Example Markets Act, 2004 is repealed in relation to Lagos State.") == [
        ("amends", "Example Markets Act, 2004", "In relation to Lagos State")
    ]
    # A bracket that is neither its name nor a limit: not read.
    assert read_as("The Example Markets Act, 2004 (a copy of which is annexed) is repealed.") == []
    # What another provision says, and what is denied.
    assert (
        read_as(
            "Section 24 of the Principal Act, which provides that the Sample Levy Act, 1990 is repealed, is deleted."
        )
        == []
    )
    assert (
        read_as(
            "Nothing in this Act shall be construed as meaning that the Example Markets Act, 2004 is repealed."
        )
        == []
    )
    # Words an amending Act puts into another: that Act's, not this one's.
    assert read_as("“(2) The Sample Levy Act, 1990 is repealed.”") == []
    # Said of another law's doing, not done here.
    assert read_as("The Sample Levy Act, 1990 is repealed by the Example Markets Act, 2004.") == []
    assert read_as("The Sample Levy Act, 1990 is amended by the Example Markets Act, 2004.") == []
    assert read_as("The Sample Levy Act, 1990 is amended by inserting a new section 4.") == [
        ("amends", "Sample Levy Act, 1990", None)
    ]
    # Words after a repeal that are neither its end nor a known limit: not read.
    assert read_as("The Sample Levy Act, 1990 is repealed for the purposes of Part II.") == []


def test_a_condition_in_the_paragraph_that_opens_a_list_governs_its_items():
    def listed(opening):
        nodes = [
            node(7, opening),
            node(7, "(a) the Example Markets Act, 2004 is repealed ;", "list_item"),
            node(7, "(b) the Sample Levy Act, 2001 is amended by deleting section 3.", "list_item"),
        ]
        found = ng_cited.cited(reading(page(7, nodes)), act("ng-act-9-2011", "Example Markets Act, 2011"))
        return [(c["kind"], c["target"]) for c in found]

    assert listed("5. Where the President so directs—") == []
    assert listed("5. Nothing in this section shall mean that—") == []
    assert listed("5. On the commencement of this Act—") == [
        ("repeals", "Example Markets Act, 2004"),
        ("amends", "Sample Levy Act, 2001"),
    ]


def test_plain_provisions_as_the_staged_acts_print_them_are_still_read():
    """What the stricter rules must not lose: provisions worded as the staged collection's are."""
    amended = [("amends", "Sample Levy Act No. 2 of 2006", None)]
    assert (
        read_as(
            '1. The Sample Levy Act No. 2 of 2006, (in this Act referred to as the "Principal Act") is amended as set out below.'
        )
        == amended
    )
    assert (
        read_as(
            'The Sample Levy Act No. 2 of 2006 (hereinafter in this Act referred to as "the Principal Act") is amended as set out in this Act.'
        )
        == amended
    )
    assert (
        read_as(
            'The Sample Levy Act No. 2 of 2006 (in this Act referred as "the Principal Act") is amended as set out in this Act.'
        )
        == amended
    )
    assert read_as(
        "The Sample Market Rules, 1953, hereinafter referred to as the principal rules, are hereby amended as follows."
    ) == [("amends", "Sample Market Rules, 1953", None)]
    # An Act's first section, read on from its enacting words; a section's number printed without its stop.
    assert (
        read_as(
            "A Bill For An Act to make provision that is not otherwise made. ENACTED by the National Assembly of the "
            'Federal Republic of Nigeria - The Sample Levy Act No. 2 of 2006 (in this Act referred to as the "Principal Act") '
            "is amended as set out in this Act."
        )
        == amended
    )
    assert read_as("314 (1) The Sample Levy Act No 45, 1999 is repealed.") == [
        ("repeals", "Sample Levy Act No 45, 1999", None)
    ]
    # A repeal with what else is done by it, and with a saving, is a repeal.
    repealed = [("repeals", "Sample Areas Commission Decree 1998", None)]
    assert (
        read_as(
            "28.—(1) The Sample Areas Commission Decree 1998 is hereby repealed and accordingly the Commission established "
            'under that Decree (in this section referred to as "the dissolved Commission") is consequentially dissolved.'
        )
        == repealed
    )
    assert (
        read_as(
            "The Sample Areas Commission Decree 1998 is hereby revoked without prejudice to the recovery of any amounts due."
        )
        == repealed
    )
    # The whole of the part named, from its first word.
    assert read_as(
        "3. The proviso to paragraph (e) of section 28 of the Sample Board Law, 1954, is hereby amended by the "
        "substitution of a word."
    ) == [("amends", "Sample Board Law, 1954", "Proviso to paragraph (e) of section 28")]
    assert read_as("Bye-law 4 of the Sample Council (Market) Bye-laws, 1956 is hereby revoked.") == [
        ("amends", "Sample Council (Market) Bye-laws, 1956", "Bye-law 4")
    ]


def test_a_bracket_the_printer_never_closed_must_end_in_the_name_it_gives():
    assert (
        read_as(
            "The Sample Levy Act, 2001 (referred to in section 5 as amended from time to time, and no part of it "
            "is repealed."
        )
        == []
    )
    assert read_as(
        'The Sample Levy Act, 2001 (in this Act referred to as "the Principal Act" is amended as set out below.'
    ) == [("amends", "Sample Levy Act, 2001", None)]


def test_a_title_with_a_small_word_in_it_is_read_whole_not_from_its_last_words():
    assert read_as("The Carriage of Goods by Sea Act, 2005 is repealed.") == [
        ("repeals", "Carriage of Goods by Sea Act, 2005", None)
    ]


def test_a_compendium_of_many_instruments_repeals_nothing_as_itself():
    nodes = [
        node(7, "5. The Sample Levy Regulations, 1940 are hereby revoked."),
        node(7, "6. These Regulations may be cited as the Sample Defence (Levy) Regulations, 1942."),
        node(8, "4. These Regulations may be cited as the Sample Defence (Finance) Regulations, 1943."),
        node(9, "9. These Regulations may be cited as the Sample Defence (Trade) Regulations, 1944."),
    ]
    compendium = act("ng-regulation-sample-defence-regulations", "SAMPLE DEFENCE REGULATIONS", "regulation")
    assert ng_cited.cited(reading(page(7, nodes)), compendium) == []
    # One instrument that quotes another's citation is not one: an amending Act and its principal Act.
    amending = [
        node(7, "2. The Sample Levy Act, 1990 is repealed."),
        node(7, '"30. This Act may be cited as the Example Markets Act, 2004".'),
        node(7, "4. This Act may be cited as the Example Markets (Amendment) Act, 2011."),
    ]
    found = ng_cited.cited(
        reading(page(7, amending)), act("ng-act-y", "EXAMPLE MARKETS (AMENDMENT) ACT, 2011")
    )
    assert [c["target"] for c in found] == ["Sample Levy Act, 1990"]


def test_the_long_title_is_an_acts_own_and_stands_at_its_head():
    said = "The Commission notes that a Bill for an Act to repeal the Example Markets Act, 2004 is before the Assembly."
    assert read_as(said, kind="guideline", title="Sample Guidelines, 2011") == []
    assert read_as(said) == []


def linked_to(source, cite, *held):
    """The target a citation of ``source`` is given among the documents ``held``, or None."""
    out = ng_cited.link_works([*held, {**source, "cites": [cite]}])
    return out[-1]["relations"][0].get("target_id")


def test_subsidiary_legislation_does_not_repeal_or_amend_an_act():
    earlier = act("ng-act-4-2004", "Example Markets Act, 2004")
    cite = repeal("Example Markets Act, 2004", "Example Markets Act", 2004)
    regulations = act("ng-si-9-2011", "Sample Regulations, 2011", "regulation")
    assert linked_to(regulations, cite, earlier) is None
    assert linked_to(regulations, cite | {"kind": "amends"}, earlier) is None
    assert linked_to(regulations, cite | {"kind": "made_under"}, earlier) == "ng-act-4-2004"
    assert linked_to(act("ng-act-9-2011", "Example Markets Act, 2011"), cite, earlier) == "ng-act-4-2004"


def test_a_name_two_instruments_share_singles_out_neither_unless_its_number_does():
    five = act("ng-si-5-1965", "Sample Tariff Order, 1965", "order")
    eighty = act("ng-si-80-1965", "Sample Tariff Order, 1965", "order")
    later = act("ng-si-9-1970", "Sample Tariff Order, 1970", "order")
    cite = repeal("Sample Tariff Order, 1965", "Sample Tariff Order", 1965)
    out = {d["id"]: d for d in ng_cited.link_works([five, eighty, {**later, "cites": [cite]}])}
    assert out["ng-si-9-1970"]["relations"] == [{"kind": "repeals", "target": "Sample Tariff Order, 1965"}]
    assert out["ng-si-5-1965"]["currency"] == out["ng-si-80-1965"]["currency"] == {"status": "unknown"}
    # Its number singles one out, whichever comes first.
    numbered = cite | {"target": "Sample Tariff Order, No. 80, 1965", "number": "80"}
    for held in ([five, eighty], [eighty, five]):
        out = {d["id"]: d for d in ng_cited.link_works([*held, {**later, "cites": [numbered]}])}
        assert out["ng-si-80-1965"]["currency"]["status"] == "repealed"
        assert out["ng-si-5-1965"]["currency"] == {"status": "unknown"}
    # Two files under one name, neither numbered: subsidiary legislation is not known to be one instrument.
    copies = [
        act("ng-order-a", "Sample Tariff Order, 1965", "order"),
        act("ng-order-b", "SAMPLE TARIFF ORDER, 1965", "order"),
    ]
    assert linked_to(later, cite, *copies) is None
    assert linked_to(later, cite, copies[0]) == "ng-order-a"
    # An Act has one short title a year: two copies of it are one Act, unless their numbers differ.
    acts = [act("ng-act-a", "Example Markets Act, 2004"), act("ng-act-b", "EXAMPLE MARKETS ACT, 2004")]
    of_act = repeal("Example Markets Act, 2004", "Example Markets Act", 2004)
    repealing = act("ng-act-9-2011", "Example Markets Act, 2011")
    out = {d["id"]: d for d in ng_cited.link_works([*acts, {**repealing, "cites": [of_act]}])}
    assert {out[i]["currency"]["status"] for i in ("ng-act-a", "ng-act-b")} == {"repealed"}
    differing = [
        act("ng-act-4-2004", "Example Markets Act, 2004"),
        act("ng-act-7-2004", "Example Markets Act, 2004"),
    ]
    assert linked_to(repealing, of_act, *differing) is None


def test_nothing_is_in_force_without_a_listing_whatever_an_entry_carries():
    stray = {**act("ng-act-4-2004", "Example Markets Act, 2004"), "listed": "in_force"}
    assert ng_cited.link_works([stray])[0]["currency"] == {"status": "unknown"}


def test_a_text_citing_one_work_twice_links_back_twice_in_rwanda_as_before_and_once_in_nigeria():
    from ingestion import rwanda

    twice = [
        {
            "kind": "repeals",
            "target": "Law n° 1/2001",
            "instrument": "law",
            "number": "1/2001",
            "year": "2001",
        }
    ] * 2
    documents = [
        {"id": "rw-law-1-2001-en", "work": "rw-law-1-2001", "language": "en", "title": "A"},
        {"id": "rw-law-2-2010-en", "work": "rw-law-2-2010", "language": "en", "title": "B", "cites": twice},
    ]
    back = {"kind": "repealed_by", "target": "B", "target_id": "rw-law-2-2010"}
    assert rwanda.link_works(documents)[0]["relations"] == [back, back]  # as it was before 2026-11-17
    cite = repeal("Example Markets Act, 2004", "Example Markets Act", 2004)
    out = ng_cited.link_works(
        [
            act("ng-act-4-2004", "Example Markets Act, 2004"),
            act(
                "ng-act-9-2011",
                "Example Markets Act, 2011",
                cites=[cite, cite | {"target": "the Example Markets Act 2004"}],
            ),
        ]
    )
    assert out[0]["relations"] == [
        {"kind": "repealed_by", "target": "Example Markets Act, 2011", "target_id": "ng-act-9-2011"}
    ]
    assert out[0]["currency"]["repealed_by"] == [
        {"work": "ng-act-9-2011", "title": "Example Markets Act, 2011"}
    ]
